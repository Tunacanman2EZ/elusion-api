"""friends/routes.py - the edge: the four routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth   the token check (puts the account on g.user)
    get_db         the request's database connection
    find_user      a name -> its users row, or None (the table's NOCASE
                   collation: "tunacan" finds Tunacan)
    bad_request    a message -> app.py's 400
    ignores        (db, a, b) -> whether account a ignores account b
    presence       (db, ids) -> {id: last seen}, in one query
    person         (row, presence, now) -> one account as a friends panel
                   reads it: name, rank, colour, online
    limits         () -> (most friends, most requests outstanding) - asked
                   each time, so the numbers are app.py's tunables

THE ENDPOINT NAMES ARE app.py's - friends_list, friends_request,
friends_respond, friends_remove - and routes.lock holds them.

Four routes cover the whole life of a friendship: ask, answer, list, end.
There is no separate "withdraw" - removing somebody deletes the row whatever
state it is in, which is the same action from the requester's side of a
pending row.
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, find_user, bad_request, ignores, presence, person, limits):

    def target_of(payload):
        """The account a friends route is aimed at: (row, None), or (None,
        the refusal)."""
        name = str(payload.get("username", "")).strip()
        if name == "":
            return None, bad_request("username is required")
        row = find_user(name)
        if row is None:
            return None, ({"error": "Not Found", "message": "No such account."}, 404)
        if int(row["id"]) == int(g.user["id"]):
            return None, bad_request("you cannot add yourself")
        return row, None

    def friends_list():
        """
        Your friends, who is online, and any requests waiting
        ---
        tags:
          - Friends
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Friends, incoming requests, outgoing requests, and the server clock
          401:
            description: Missing, invalid or expired token
        """
        db = get_db()
        me = int(g.user["id"])
        now = int(time.time())
        buckets, other_of = services.sort_rows(data.rows_of(db, me), me)
        ids = list(other_of.keys())
        seen = presence(db, ids)
        out = services.listed(buckets, other_of, data.people(db, ids), lambda row: person(row, seen, now))
        accepted, pending_sent = data.counts(db, me)
        most_friends, most_pending = limits()
        out["now"] = now
        out["limits"] = services.limits_answer(most_friends, most_pending, accepted, pending_sent)
        return out, 200

    def friends_request():
        """
        Ask somebody to be friends
        ---
        tags:
          - Friends
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: target
            schema:
              type: object
              required: [username]
              properties:
                username:
                  type: string
        responses:
          200:
            description: Request sent, or accepted if they had already asked you
          400:
            description: Missing username, or aimed at yourself
          404:
            description: No such account
          409:
            description: Already friends, or already asked
        """
        payload = request.get_json(silent=True) or {}
        target, refusal = target_of(payload)
        if refusal is not None:
            return refusal
        db = get_db()
        me = int(g.user["id"])
        them = int(target["id"])
        now = int(time.time())

        if ignores(db, them, me):
            return {"error": "Forbidden",
                    "message": "%s is not taking friend requests from you." % target["username"]}, 403

        link = data.link(db, me, them)
        if link is not None:
            if link["state"] == "accepted":
                return {"error": "Conflict", "message": "You are already friends."}, 409
            if int(link["requester_id"]) == me:
                return {"error": "Conflict", "message": "You have already asked them."}, 409
            # THEY ASKED FIRST AND THIS IS THE ANSWER. Two people who add
            # each other at the same time should end up friends, not
            # deadlocked with a pending request pointing each way that
            # neither one can see a button for.
            data.accept(db, them, me, now)
            db.commit()
            return {"username": target["username"], "state": "accepted"}, 200

        most_friends, most_pending = limits()
        accepted, pending_sent = data.counts(db, me)
        if accepted >= most_friends:
            return bad_request("your friends list is full (%d)" % most_friends)
        if pending_sent >= most_pending:
            return bad_request("you have %d requests waiting for an answer; that is the limit" % most_pending)
        their_accepted, _ = data.counts(db, them)
        if their_accepted >= most_friends:
            # THEIR limit, said plainly. Refusing without saying whose list
            # is full reads as the request silently failing.
            return bad_request("their friends list is full")

        data.ask(db, me, them, now)
        db.commit()
        return {"username": target["username"], "state": "pending"}, 200

    def friends_respond():
        """
        Accept or decline a request somebody sent you
        ---
        tags:
          - Friends
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: answer
            schema:
              type: object
              required: [username, accept]
              properties:
                username:
                  type: string
                accept:
                  type: boolean
        responses:
          200:
            description: Accepted, or declined and the request removed
          400:
            description: Missing username, or aimed at yourself
          404:
            description: No such account, or no request from them
        """
        payload = request.get_json(silent=True) or {}
        target, refusal = target_of(payload)
        if refusal is not None:
            return refusal
        db = get_db()
        me = int(g.user["id"])
        them = int(target["id"])
        now = int(time.time())

        # ONLY A REQUEST AIMED AT ME. Answering my own outgoing request would
        # be accepting on the other person's behalf, so the direction is part
        # of the lookup rather than something checked afterwards.
        if data.pending_from(db, them, me) is None:
            return {"error": "Not Found", "message": "No request from that account."}, 404

        if not bool(payload.get("accept", False)):
            data.drop_request(db, them, me)
            db.commit()
            # The requester is not told. A decline that sends a notification
            # is a decline most people will not send.
            return {"username": target["username"], "state": "declined"}, 200

        most_friends, _most_pending = limits()
        accepted, _ = data.counts(db, me)
        if accepted >= most_friends:
            return bad_request("your friends list is full (%d)" % most_friends)
        data.accept(db, them, me, now)
        db.commit()
        return {"username": target["username"], "state": "accepted"}, 200

    def friends_remove():
        """
        Remove a friend, or withdraw a request you sent
        ---
        tags:
          - Friends
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: target
            schema:
              type: object
              required: [username]
              properties:
                username:
                  type: string
        responses:
          200:
            description: The link between the two accounts is gone
          400:
            description: Missing username, or aimed at yourself
          404:
            description: No such account, or no link to remove
        """
        payload = request.get_json(silent=True) or {}
        target, refusal = target_of(payload)
        if refusal is not None:
            return refusal
        db = get_db()
        me = int(g.user["id"])
        them = int(target["id"])
        if data.link(db, me, them) is None:
            return {"error": "Not Found", "message": "You are not connected to that account."}, 404
        data.drop_link(db, me, them)
        db.commit()
        return {"username": target["username"], "state": "none"}, 200

    app.add_url_rule("/api/friends", endpoint="friends_list",
                     view_func=require_auth(friends_list), methods=["GET"])
    app.add_url_rule("/api/friends/request", endpoint="friends_request",
                     view_func=require_auth(friends_request), methods=["POST"])
    app.add_url_rule("/api/friends/respond", endpoint="friends_respond",
                     view_func=require_auth(friends_respond), methods=["POST"])
    app.add_url_rule("/api/friends/remove", endpoint="friends_remove",
                     view_func=require_auth(friends_remove), methods=["POST"])
