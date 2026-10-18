"""parties/routes.py - the edge: the seven routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth      the token check (puts the account on g.user)
    get_db            the request's database connection
    bad_request       a message -> app.py's 400
    find_user         a name -> its users row, or None
    ignores           (db, a, b) -> whether account a ignores account b
    playing_slot_sql  app.py's PLAYING_SLOT_SQL: which character an account
                      is playing
    online_window     seconds since last seen that still count as on
    voice_settings    () -> ("direct" | "relay" | "", key id, key token),
                      read from the server's settings each time
    turn_fetch        (key id, token, ttl) -> the relay's iceServers, or None:
                      the one call out to Cloudflare, a seam the suites replace
    voice_issued      {user_id: (expires_at, ice_servers)} handed out
    voice_asks        {user_id: [times the relay was asked for them]}
    voice_lock        the lock over both

THE ENDPOINT NAMES ARE app.py's - party_read, party_invite, party_respond,
party_leave, party_kick, party_lead, party_voice - and routes.lock holds them.
"""
import time

from flask import g, request

from . import data, services
from .services import MAX_PARTY_INVITES, MAX_PARTY_MEMBERS, PARTY_INVITE_SECONDS


def register(app, *, require_auth, get_db, bad_request, find_user, ignores, playing_slot_sql,
             online_window, voice_settings, turn_fetch, voice_issued, voice_asks, voice_lock):

    def party_dict(db, party_id, now):
        rows = data.member_rows(db, party_id)
        ids = [int(r["user_id"]) for r in rows]
        return services.party_answer(party_id, rows, data.playing_saves(db, ids, playing_slot_sql, now, online_window),
                                     data.last_seen(db, ids), now, online_window)

    def party_body(db, user_id, now=None):
        """GET /api/party's answer, which every party route returns too: your
        party (or None) and the invitations waiting on you."""
        now = int(time.time()) if now is None else now
        seat = data.membership(db, user_id)
        party = None
        if seat is not None:
            # A LEADER WHO DELETED THEIR ACCOUNT took their row with them: the
            # read puts things right before it answers.
            if data.fix(db, int(seat["party_id"])):
                db.commit()
            elif data.membership(db, user_id) is not None:
                party = party_dict(db, int(seat["party_id"]), now)
        invites = [{"from": r["username"], "at": int(r["created_at"] or 0)}
                   for r in data.invites_to(db, user_id, now)]
        return {"party": party, "invites": invites,
                "capacity": MAX_PARTY_MEMBERS, "invite_seconds": PARTY_INVITE_SECONDS}

    def party_read():
        """
        Your party and the invitations waiting on you
        ---
        tags:
          - Parties
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Your party (null for none), each member's character, and your invitations
          401:
            description: Missing, invalid or expired token
        """
        db = get_db()
        body = party_body(db, int(g.user["id"]))
        db.commit()
        return body, 200

    def party_invite():
        """
        Ask somebody to join your party - or to start one with you
        ---
        tags:
          - Parties
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: who
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string}
        responses:
          200:
            description: The invitation is waiting for them
          400:
            description: Yourself, already in a party, your party is full, too many waiting, or you are not its leader
          401:
            description: Missing, invalid or expired token
          403:
            description: They are not taking invitations from you
          404:
            description: No such account
        """
        payload = request.get_json(silent=True) or {}
        who = str(payload.get("username", "")).strip()
        if who == "":
            return bad_request("username is required")
        db = get_db()
        me = int(g.user["id"])
        target = find_user(who)
        if target is None:
            return {"error": "Not Found", "message": "No such account."}, 404
        if int(target["id"]) == me:
            return bad_request("you cannot invite yourself")
        seat = data.membership(db, me)
        if seat is not None:
            if not int(seat["is_leader"] or 0):
                return bad_request("only the party's leader can invite")
            if len(data.member_rows(db, int(seat["party_id"]))) >= MAX_PARTY_MEMBERS:
                return bad_request("a party holds %d" % MAX_PARTY_MEMBERS)
        if data.membership(db, int(target["id"])) is not None:
            return bad_request("%s is already in a party" % target["username"])
        if ignores(db, int(target["id"]), me):
            return {"error": "Forbidden",
                    "message": "%s is not taking party invites from you." % target["username"]}, 403
        now = int(time.time())
        data.prune_invites(db, now)
        if data.invites_sent(db, me, int(target["id"])) >= MAX_PARTY_INVITES:
            return bad_request("you already have %d invitations waiting" % MAX_PARTY_INVITES)
        data.invite(db, int(target["id"]), me, now)
        db.commit()
        return {"username": target["username"], "state": "invited",
                "expires_in": PARTY_INVITE_SECONDS}, 200

    def party_respond():
        """
        Accept or turn down a party invitation
        ---
        tags:
          - Parties
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
              required: [from, accept]
              properties:
                from:   {type: string}
                accept: {type: boolean}
        responses:
          200:
            description: Joined (or started) their party, or the invitation is gone
          400:
            description: You are already in a party, or theirs is full
          401:
            description: Missing, invalid or expired token
          404:
            description: No invitation from them, or it ran out
        """
        payload = request.get_json(silent=True) or {}
        wanted = str(payload.get("from", "")).strip()
        accept = bool(payload.get("accept", False))
        if wanted == "":
            return bad_request("from is required")
        db = get_db()
        me = int(g.user["id"])
        now = int(time.time())
        asker = find_user(wanted)
        invite = None
        if asker is not None:
            invite = data.invite_from(db, me, int(asker["id"]), now)
        if invite is None:
            return {"error": "Not Found", "message": "No invitation from them - it may have run out."}, 404
        if not accept:
            # TURNED DOWN SILENTLY, as a guild's or a friend's is.
            data.drop_invite(db, me, int(asker["id"]))
            db.commit()
            return {"from": asker["username"], "state": "declined"}, 200
        if data.membership(db, me) is not None:
            return bad_request("leave your party first")
        theirs = data.membership(db, int(asker["id"]))
        if theirs is None:
            # THE PARTY IS MADE NOW, with the one who asked leading it.
            party_id = data.new_party(db, int(asker["id"]), now)
        else:
            party_id = int(theirs["party_id"])
            if len(data.member_rows(db, party_id)) >= MAX_PARTY_MEMBERS:
                return bad_request("that party is full")
        data.join(db, me, party_id, now)
        # EVERY OTHER INVITATION GOES TOO: they are in a party now.
        data.drop_invites_to(db, me)
        db.commit()
        return dict(party_body(db, me, now), state="joined"), 200

    def party_leave():
        """
        Leave your party
        ---
        tags:
          - Parties
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: You are out - and the party is gone if one was left
          401:
            description: Missing, invalid or expired token
          404:
            description: You are not in a party
        """
        db = get_db()
        me = int(g.user["id"])
        seat = data.membership(db, me)
        if seat is None:
            return {"error": "Not Found", "message": "You are not in a party."}, 404
        data.remove_member(db, me)
        data.drop_invites_from(db, me)
        # A LEADER LEAVING HANDS IT ON to the longest there; the last but one
        # leaving takes it down.
        gone = data.fix(db, int(seat["party_id"]))
        db.commit()
        return dict(party_body(db, me), state="left", disbanded=gone), 200

    def party_kick():
        """
        Remove somebody from your party (its leader only)
        ---
        tags:
          - Parties
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: who
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string}
        responses:
          200:
            description: They are out
          400:
            description: Yourself - leave instead
          401:
            description: Missing, invalid or expired token
          404:
            description: You lead no party, or they are not in it
        """
        payload = request.get_json(silent=True) or {}
        who = str(payload.get("username", "")).strip()
        db = get_db()
        me = int(g.user["id"])
        seat = data.membership(db, me)
        if seat is None or not int(seat["is_leader"] or 0):
            return {"error": "Not Found", "message": "You are not leading a party."}, 404
        target = find_user(who) if who else None
        theirs = data.membership(db, int(target["id"])) if target is not None else None
        if theirs is None or int(theirs["party_id"]) != int(seat["party_id"]):
            return {"error": "Not Found", "message": "They are not in your party."}, 404
        if int(target["id"]) == me:
            return bad_request("leave the party instead")
        data.remove_member(db, int(target["id"]))
        gone = data.fix(db, int(seat["party_id"]))
        db.commit()
        return dict(party_body(db, me), state="removed", disbanded=gone), 200

    def party_lead():
        """
        Hand your party on to somebody in it (its leader only)
        ---
        tags:
          - Parties
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: who
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string}
        responses:
          200:
            description: They lead it now
          401:
            description: Missing, invalid or expired token
          404:
            description: You lead no party, or they are not in it
        """
        payload = request.get_json(silent=True) or {}
        who = str(payload.get("username", "")).strip()
        db = get_db()
        me = int(g.user["id"])
        seat = data.membership(db, me)
        if seat is None or not int(seat["is_leader"] or 0):
            return {"error": "Not Found", "message": "You are not leading a party."}, 404
        target = find_user(who) if who else None
        theirs = data.membership(db, int(target["id"])) if target is not None else None
        if theirs is None or int(theirs["party_id"]) != int(seat["party_id"]):
            return {"error": "Not Found", "message": "They are not in your party."}, 404
        data.hand_on(db, int(seat["party_id"]), int(target["id"]))
        db.commit()
        return dict(party_body(db, me), state="handed"), 200

    def party_voice():
        """
        The relay a party's voice call goes through - for somebody in a party
        ---
        tags:
          - Parties
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: '{"ice_servers": [...], "policy": "relay", "expires_at": t}'
          401:
            description: Missing, invalid or expired token
          409:
            description: You are in no party
          429:
            description: Asked for too many relays this hour
          502:
            description: The relay did not answer
          503:
            description: Voice is not set up on this server
        """
        db = get_db()
        me = int(g.user["id"])
        if data.membership(db, me) is None:
            return {"error": "Conflict", "message": "Voice is for parties: invite somebody first."}, 409
        mode, key_id, token = voice_settings()
        if mode == "direct":
            return {"ice_servers": [], "policy": "all", "expires_at": 0}, 200
        if mode != "relay":
            return {"error": "Service Unavailable",
                    "message": "Voice is not set up on this server yet."}, 503
        now = int(time.time())
        with voice_lock:
            issued = voice_issued.get(me)
            if issued is not None and issued[0] - now >= services.VOICE_TURN_REUSE_SECONDS:
                return {"ice_servers": issued[1], "policy": "relay", "expires_at": issued[0]}, 200
            asks = [t for t in voice_asks.get(me, []) if t > now - 3600]
            if len(asks) >= services.VOICE_FETCHES_PER_HOUR:
                voice_asks[me] = asks
                return {"error": "Too Many Requests",
                        "message": "Voice was switched on a lot this hour. Try again later."}, 429
            asks.append(now)
            voice_asks[me] = asks
        servers = services.clean_ice_servers(turn_fetch(key_id, token, services.VOICE_TURN_TTL))
        if not any("username" in s for s in servers):
            return {"error": "Bad Gateway", "message": "The voice relay did not answer. Try again in a minute."}, 502
        expires = now + services.VOICE_TURN_TTL
        with voice_lock:
            voice_issued[me] = (expires, servers)
        return {"ice_servers": servers, "policy": "relay", "expires_at": expires}, 200

    app.add_url_rule("/api/party", endpoint="party_read",
                     view_func=require_auth(party_read), methods=["GET"])
    app.add_url_rule("/api/party/invite", endpoint="party_invite",
                     view_func=require_auth(party_invite), methods=["POST"])
    app.add_url_rule("/api/party/respond", endpoint="party_respond",
                     view_func=require_auth(party_respond), methods=["POST"])
    app.add_url_rule("/api/party/leave", endpoint="party_leave",
                     view_func=require_auth(party_leave), methods=["POST"])
    app.add_url_rule("/api/party/kick", endpoint="party_kick",
                     view_func=require_auth(party_kick), methods=["POST"])
    app.add_url_rule("/api/party/lead", endpoint="party_lead",
                     view_func=require_auth(party_lead), methods=["POST"])
    app.add_url_rule("/api/party/voice", endpoint="party_voice",
                     view_func=require_auth(party_voice), methods=["GET"])
