"""switches/routes.py - the edge: the status read, the owner's six switches
and the owner's announcement, as they were in app.py (the sixth, the kill
receipts, since 0.57.0).

register() is handed app.py's own pieces - it never imports app.py:

    require_auth            the token check (puts the account on g.user)
    require_owner           the owner or a co-owner, the bare 404 to anybody
                            else
    require_the_owner       the owner alone, a co-owner included out
    require_fresh_password  asks for the password again, for the switches
                            that change the world
    get_db                  the request's database connection
    bad_request             a message -> app.py's 400
    catalogue               gamedata: its schema version, its items and its
                            monsters, which /api/status counts
    get_setting             a key -> its stored value, or None (app.py's
                            get_server_setting())
    set_setting             (key, value, by) - one switch written, with who
                            and when; no commit (set_server_setting())
    maintenance_key, min_build_key, pvp_key, trade_key, co_owners_key,
    receipts_key            the rows the six switches live in
    maintenance_state       () -> the maintenance switch, read and normalised
    seconds_left            (state) -> seconds until sessions start ending
    default_message         what a closed server says when the owner said
                            nothing (MAINTENANCE_DEFAULT_MESSAGE)
    pvp_enabled             () -> whether PvP is on
    trades_enabled          () -> whether new trades are allowed
    receipts_on             () -> whether a kill needs the books' receipt
    min_client_build        () -> the oldest build allowed in, 0 for any
    current_build           CURRENT_CLIENT_BUILD: the newest build there is
    co_owners_on            () -> whether the co-owners are in right now
    co_owners_changed       () - forget this request's answer to that, after
                            the switch moves
    co_owner_names          () -> who ELUSION_CO_OWNERS names - asked on every
                            call, since a suite empties it
    post_broadcast          (body, kind, by) -> the line's id: the server's
                            voice; no commit
    broadcast_limit         MAX_BROADCAST_LENGTH: the longest line it keeps
    log_staff_action        the moderation log's writer
    online_window           ONLINE_WINDOW_SECONDS: how recently a client must
                            have been heard to count as in the world
    end_player_sessions     (db) -> how many were signed out: everybody but
                            the owner, for a close with no grace at all
    open_trades             (db) -> how many trades are still open (trade/'s)

THE ENDPOINT NAMES ARE app.py's - server_status, set_maintenance,
set_min_build, set_pvp, set_trade, read_co_owners, set_co_owners and
send_broadcast - and set_receipts, new with its switch; routes.lock holds
them, with each one's rank and its asking for the password again.
"""
import json
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, require_owner, require_the_owner, require_fresh_password, get_db, bad_request,
             catalogue, get_setting, set_setting, maintenance_key, min_build_key, pvp_key, trade_key, co_owners_key,
             receipts_key, maintenance_state, seconds_left, default_message, pvp_enabled, trades_enabled,
             receipts_on, min_client_build,
             current_build, co_owners_on, co_owners_changed, co_owner_names, post_broadcast, broadcast_limit,
             log_staff_action, online_window, end_player_sessions, open_trades):

    def server_status():
        """
        Server health and maintenance window
        ---
        tags:
          - Status
        responses:
          200:
            description: The server is up; message and back_at describe any planned downtime
        """
        # NO @require_auth, deliberately. The login screen needs to be able to ask
        # "are you there?" before anyone has logged in, and a 401 is not an answer
        # to that question.
        #
        # WHY THIS EXISTS: the client currently infers the difference between "your
        # internet is down" and "the host's server is down" from an HTTP failure
        # code, and that inference is unreliable - a dead port on localhost reports
        # a timeout rather than a refused connection, and a timeout looks identical
        # either way. It cannot be inferred. It has to be told.
        #
        # A reachable /api/status also means a scheduled restart can ANNOUNCE
        # itself rather than surfacing as an error. Downtime the player was warned
        # about is maintenance; the same downtime unannounced is a broken game.
        # NO LONGER SET BY HAND. The owner throws the switch from the HUD and it
        # lives in server_settings, so a closed server announces itself to every
        # client that asks - including one sitting on the login screen that has
        # never logged in and has no token to ask with.
        state = maintenance_state()
        return {
            "online": True,
            "gamedata_schema": catalogue.EXPECTED_SCHEMA,
            "items": len(catalogue.ITEMS),
            "enemies": len(catalogue.ENEMIES),
            "maintenance": state["on"],
            "message": state["message"] if state["on"] else "",
            "back_at": state["back_at"],
            "seconds_left": seconds_left(state),
            # ON THE UNAUTHENTICATED ROUTE, on purpose. "Is PvP on" is a fact about
            # the world, not about an account, and the login screen is exactly where
            # somebody wants to know before they walk in.
            "pvp": pvp_enabled(),
            # Whether players may open a trade. The trade window asks before it
            # offers anything, and a player on the login screen can know too.
            "trade": trades_enabled(),
            # Whether a kill needs the server's own count of the fight (0.57.0).
            # The GM panel shows it; a game asks again on a 202 either way.
            "kill_receipts": receipts_on(),
            # THE BUILD GATE, ON THE ONE ROUTE IT DOES NOT GATE. An outdated client
            # is refused everywhere else, so this is where it finds out what it
            # needs - and a client that is merely BEHIND, with the gate still
            # disarmed, can say so without being refused at all. That is what makes
            # shipping it at 0 useful rather than inert: the notice works from day
            # one, the refusal waits until it is wanted.
            "min_client_build": min_client_build(),
            "current_client_build": current_build,
        }, 200

    def set_maintenance():
        """
        Close or reopen the server (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [on]
              properties:
                "on":          {type: boolean, description: "true closes the server"}
                message:       {type: string,  description: "what players are told"}
                back_at:       {type: string,  description: "RFC3339, or null"}
                grace_seconds: {type: integer, description: "seconds players get to save before being signed out"}
        responses:
          200:
            description: The switch as it now stands
          400:
            description: Malformed body
          404:
            description: Not the owner (the same 404 every owner route gives)
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        if "on" not in payload:
            return bad_request('send {"on": true} to close the server, or {"on": false} to reopen it')

        db = get_db()
        now = int(time.time())

        # ------------------------------------------------------------------ open
        if not bool(payload.get("on")):
            # Written as an explicit {"on": false} rather than deleting the row, so
            # server_settings keeps a record of who reopened it and when.
            set_setting(maintenance_key, json.dumps({"on": False}), g.user["username"])
            post_broadcast("The server is open again.", "system", g.user["username"])
            log_staff_action(g.user, "maintenance", "server", None, "reopened")
            db.commit()
            return {
                "maintenance": False, "message": "", "back_at": None,
                "seconds_left": 0, "sessions_ended": 0,
            }, 200

        # ----------------------------------------------------------------- close
        message = str(payload.get("message", "")).strip()[:200] or default_message

        back_at = payload.get("back_at")
        if back_at is not None:
            back_at = str(back_at).strip()[:64] or None

        # Clamped to MAINTENANCE_MAX_GRACE_SECONDS - see services.grace().
        grace = services.grace(payload.get("grace_seconds", services.MAINTENANCE_DEFAULT_GRACE_SECONDS))
        if grace is None:
            return bad_request("grace_seconds must be a whole number of seconds")

        set_setting(maintenance_key, json.dumps({
            "on": True,
            "message": message,
            "back_at": back_at,
            # THE ONE NUMBER THAT MATTERS. Stored as an absolute time, not a
            # duration, because every later request has to answer "is the window
            # spent" without knowing when the switch was thrown.
            "kick_at": now + grace,
            "by": g.user["username"],
            "at": now,
        }), g.user["username"])

        # WHO IS ACTUALLY IN THE WORLD, not who holds a session - a token lasts
        # thirty days, so counting sessions would report a crowd that went home
        # weeks ago. This is the number the owner is really asking for: how many
        # people is the grace window for.
        online = data.online_count(db, now - online_window)

        # NO GRACE MEANS NO SAVE, and it is spelled out here rather than hidden
        # behind a default. Asking for zero is asking to drop whatever the clients
        # had not sent yet; every other value lets the heartbeat carry the notice
        # and the clients flush first.
        # SAID OUT LOUD, IN THE WORLD. The login screen only reaches people who
        # are not playing yet; this is the half that reaches the ones who are.
        # Posted before the sessions are ended so it is already in the table when
        # the last poll before disconnection asks for it.
        #
        # THE COUNTDOWN IS NEVER THE PART CUT - see services.closing_notice().
        post_broadcast(services.closing_notice(message, grace, broadcast_limit), "system", g.user["username"])

        ended = end_player_sessions(db) if grace == 0 else 0

        log_staff_action(
            g.user, "maintenance", "server", None,
            "closed, %ds grace, %d online%s" % (
                grace, online, ", %d signed out now" % ended if ended else ""),
        )
        db.commit()

        return {
            "maintenance": True,
            "message": message,
            "back_at": back_at,
            "grace_seconds": grace,
            "disconnect_at": now + grace,
            "seconds_left": grace,
            "online_now": online,
            "sessions_ended": ended,
        }, 200

    def set_min_build():
        """
        Set the oldest client build allowed to connect (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [build]
              properties:
                build: {type: integer, description: "oldest build allowed; 0 disarms the gate"}
        responses:
          200:
            description: The gate as it now stands
          400:
            description: Malformed body
          404:
            description: Not the owner (the same 404 every owner route gives)
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        if "build" not in payload:
            return bad_request('send {"build": 3} to require build 3 or newer, '
                               'or {"build": 0} to let every client in')

        try:
            wanted = int(payload.get("build"))
        except (TypeError, ValueError):
            return bad_request("build must be a whole number")
        if wanted < 0:
            return bad_request("build cannot be negative")

        # A MINIMUM ABOVE THE BUILD THAT EXISTS LOCKS EVERYONE OUT INCLUDING YOU,
        # and unlike the maintenance switch there is no owner exemption to climb
        # back in through - the gate runs before authentication, because refusing a
        # protocol mismatch cannot depend on parsing a token that build may not
        # know how to send. So the one typo that would need a shell on the server to
        # undo is refused here instead.
        if wanted > current_build:
            return bad_request(
                "build %d is newer than any client that exists (current is %d). "
                "That would refuse every player including you, and this gate runs "
                "before authentication - there would be no way back in from the game."
                % (wanted, current_build))

        db = get_db()
        # Written even when unchanged, like the two switches beside it, so
        # server_settings keeps who set it and when.
        set_setting(min_build_key, json.dumps({"build": wanted}), g.user["username"])

        # NOT ANNOUNCED IN CHAT, and that is the difference from the PvP switch.
        # The people this affects are the ones who cannot connect, so a broadcast
        # reaches everybody except its audience. The client reads it off
        # /api/status - the one route the gate never refuses - which is where a
        # player who cannot get in is already looking.
        log_staff_action(g.user, "minbuild", "server", None,
                         "min client build set to %d" % wanted)
        db.commit()

        return {
            "min_client_build": wanted,
            "current_client_build": current_build,
            "armed": wanted > 0,
        }, 200

    def set_pvp():
        """
        Turn player-versus-player on or off (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: switch
            schema:
              type: object
              required: [on]
              properties:
                "on": {type: boolean}
        responses:
          200:
            description: The switch, as it now stands
          400:
            description: No 'on' field
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        # OWNER, LIKE THE MAINTENANCE SWITCH ABOVE IT, and for the same reason: this
        # is a statement about the whole world rather than an action on one account.
        # require_owner answers with a 404 rather than a 403 - see the refusal rule
        # in SECURITY.md; a 403 would confirm the route exists to somebody who has
        # no business knowing it does.
        payload = request.get_json(silent=True) or {}
        if "on" not in payload:
            return bad_request('send {"on": true} or {"on": false}')

        wanted = bool(payload.get("on"))
        db = get_db()

        # WRITTEN EVEN WHEN IT DOES NOT CHANGE, like the maintenance row, so
        # server_settings keeps a record of who set it and when. An explicit
        # {"on": false} rather than a deleted row, for the same reason.
        set_setting(pvp_key, json.dumps({"on": wanted}), g.user["username"])

        # ANNOUNCED, because a rule about whether other players may hurt you is not
        # something to discover by being hurt. post_broadcast reaches everyone who
        # is online; /api/status carries it for everyone who is not yet.
        #
        # NAMED, AND IN THE WORLD'S OWN VOICE. "PvP is now ON" is a settings screen
        # talking; a world boss is a thing that HAPPENS to a world, and the line
        # people should see is the one that reads like an event. The name is
        # g.user["username"] - the server's own spelling of it, the same authority
        # the login screen greets somebody by, so it matches the crown over the
        # owner's head rather than whatever case was typed at a keyboard.
        post_broadcast(
            ("%s has gone hostile." % g.user["username"]) if wanted
            else ("%s has cooled off - no longer hostile." % g.user["username"]),
            "system", g.user["username"])
        log_staff_action(g.user, "pvp", "server", None, "on" if wanted else "off")
        db.commit()

        return {
            "pvp": wanted,
            # SAID OUT LOUD ON EVERY ANSWER, so a panel written against this route
            # cannot imply more than the switch does. Nothing can damage another
            # player yet - there are no positions on the server and no remote bodies
            # in the client. The flag is the authority the damage path will ask when
            # it exists; today it is a flag and an announcement, and the button that
            # sets it says so too.
            "damage_implemented": False,
        }, 200

    def set_trade():
        """
        Let players open trades, or stop them (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: switch
            schema:
              type: object
              required: [on]
              properties:
                "on": {type: boolean, description: "false stops new trades; open ones may finish"}
        responses:
          200:
            description: The switch, as it now stands, and how many trades are still open
          400:
            description: No 'on' field
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        # OWNER, like maintenance and PvP: a rule about the whole world. See
        # TRADE_KEY in app.py for what off does and does not do.
        payload = request.get_json(silent=True) or {}
        if "on" not in payload or not isinstance(payload.get("on"), bool):
            return bad_request('send {"on": true} or {"on": false}')

        wanted = payload["on"]
        db = get_db()
        set_setting(trade_key, json.dumps({"on": wanted}), g.user["username"])

        # ANNOUNCED, so a player whose offer is refused already knows why. The
        # trade window says so too; this reaches the people who are not in it.
        post_broadcast(
            "Trading is open again." if wanted
            else "Trading is paused for now. A trade already open can still finish.",
            "system", g.user["username"])
        log_staff_action(g.user, "trade", "server", None, "on" if wanted else "off")
        db.commit()

        # HOW MANY ARE STILL OPEN, so the owner knows what "may finish" covers.
        still_open = open_trades(db)
        return {"trade": wanted, "open_trades": int(still_open)}, 200

    def set_receipts():
        """
        Pay kills only against the server's count of the fight, or not (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: switch
            schema:
              type: object
              required: [on]
              properties:
                "on": {type: boolean, description: "true pays a kill only with an agreed death in the books"}
        responses:
          200:
            description: The switch, as it now stands, and the oldest build allowed in
          400:
            description: No 'on' field
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        # OWNER, like PvP and trade: a rule about every kill in the world. See
        # RECEIPTS_KEY in app.py. NOT ANNOUNCED: it changes nothing a player
        # does, and a kill it refuses says why in the game.
        payload = request.get_json(silent=True) or {}
        if "on" not in payload or not isinstance(payload.get("on"), bool):
            return bad_request('send {"on": true} or {"on": false}')

        wanted = payload["on"]
        db = get_db()
        set_setting(receipts_key, json.dumps({"on": wanted}), g.user["username"])
        log_staff_action(g.user, "receipts", "server", None, "on" if wanted else "off")
        db.commit()
        # THE BUILD GATE, said back: a game older than the receipts' build is
        # told to update on every kill the books have not counted yet, so the
        # GM panel says when the minimum is still below it.
        return {"kill_receipts": wanted, "min_client_build": int(min_client_build())}, 200

    def co_owners_payload():
        """The co-owners' switch as the owner's panel shows it: whether they are
        in, who it lets in, who threw it and when, and what the row says."""
        raw = get_setting(co_owners_key)
        try:
            stored = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            stored = {}
        row = data.stamp(get_db(), co_owners_key)
        return {
            "on": co_owners_on(),
            # Who the switch lets in: ELUSION_CO_OWNERS, as the .env names them.
            "names": list(co_owner_names()),
            "by": row["updated_by"] if row is not None else "",
            "at": int(row["updated_at"] or 0) if row is not None else 0,
            "stored_on": bool(stored.get("on")) if isinstance(stored, dict) else False,
        }

    def read_co_owners():
        """
        Whether the co-owners are in, and who they are (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: The switch, and the names ELUSION_CO_OWNERS gives it
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner or a co-owner
        """
        return co_owners_payload(), 200

    def set_co_owners():
        """
        Let the co-owners in, or take them out (the owner alone)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: switch
            schema:
              type: object
              required: [on]
              properties:
                "on": {type: boolean, description: "true gives the accounts ELUSION_CO_OWNERS names the owner's powers"}
        responses:
          200:
            description: The switch as it now stands, and who it lets in
          400:
            description: No 'on' field
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner - a co-owner included
          409:
            description: On, with nobody named in the server's ELUSION_CO_OWNERS
        """
        # THE OWNER'S ALONE (require_the_owner): a co-owner who could throw it
        # could keep it on. Off takes effect on their next request - role_for()
        # asks the switch every time - so there is nothing to sign out.
        payload = request.get_json(silent=True) or {}
        if "on" not in payload or not isinstance(payload.get("on"), bool):
            return bad_request('send {"on": true} or {"on": false}')
        wanted = payload["on"]
        if wanted and not co_owner_names():
            return {
                "error": "Conflict",
                "message": "Nobody is named to be a co-owner. Put ELUSION_CO_OWNERS=<name> in the"
                           " server's .env and restart it, then switch this on.",
            }, 409
        db = get_db()
        set_setting(co_owners_key, json.dumps({"on": wanted}), g.user["username"])
        log_staff_action(g.user, "coowners", "server", None,
                         ("on: " + ", ".join(co_owner_names())) if wanted else "off")
        db.commit()
        # Asked again, not the answer cached before the switch moved.
        co_owners_changed()
        return co_owners_payload(), 200

    def send_broadcast():
        """
        Say something to everyone playing (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [body]
              properties:
                body: {type: string, description: "the announcement, max 200 chars"}
                kind: {type: string, description: "system (default) or shout"}
        responses:
          200:
            description: The announcement as stored
          400:
            description: Empty or missing body
          404:
            description: Not the owner
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        text = str(payload.get("body", "")).strip()
        if text == "":
            return bad_request("body must be a non-empty message")

        kind = str(payload.get("kind", "system")).strip().lower() or "system"
        if kind not in ("system", "shout"):
            return bad_request("kind must be 'system' or 'shout'")

        db = get_db()
        new_id = post_broadcast(text, kind, g.user["username"])
        db.commit()

        return {
            "id": new_id,
            "kind": kind,
            "body": text[:broadcast_limit],
            "by": g.user["username"],
        }, 200

    def owner(view):
        return require_auth(require_owner(view))

    def owner_asked_again(view):
        return require_auth(require_owner(require_fresh_password(view)))

    app.add_url_rule("/api/status", endpoint="server_status", view_func=server_status, methods=["GET"])
    app.add_url_rule("/api/server/maintenance", endpoint="set_maintenance",
                     view_func=owner_asked_again(set_maintenance), methods=["POST"])
    app.add_url_rule("/api/server/minbuild", endpoint="set_min_build",
                     view_func=owner_asked_again(set_min_build), methods=["POST"])
    app.add_url_rule("/api/server/pvp", endpoint="set_pvp",
                     view_func=owner_asked_again(set_pvp), methods=["POST"])
    app.add_url_rule("/api/server/trade", endpoint="set_trade",
                     view_func=owner_asked_again(set_trade), methods=["POST"])
    app.add_url_rule("/api/server/receipts", endpoint="set_receipts",
                     view_func=owner_asked_again(set_receipts), methods=["POST"])
    app.add_url_rule("/api/server/coowners", endpoint="read_co_owners",
                     view_func=owner(read_co_owners), methods=["GET"])
    app.add_url_rule("/api/server/coowners", endpoint="set_co_owners",
                     view_func=require_auth(require_the_owner(set_co_owners)), methods=["POST"])
    app.add_url_rule("/api/server/broadcast", endpoint="send_broadcast",
                     view_func=owner(send_broadcast), methods=["POST"])
