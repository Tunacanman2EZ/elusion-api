"""moderation/routes.py - the edge: the sanctions, the reports and the mutes,
as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth            the token check (puts the account on g.user)
    require_role            a staff rank -> its decorator (404 below it)
    require_fresh_password  the password again, on a hardened session
    get_db                  the request's database connection
    bad_request             a message -> app.py's 400
    parse_stat              a raw value -> a whole number >= 0, or None
    moderation_target       payload -> (account, None) or (None, the
                            refusal): an account within the caller's reach,
                            the same 404 for nobody and for out of reach
    find_user               a name -> the users row, or None
    can_act_on              (actor, target) -> whether actor outranks target
    role_for                a users row -> its effective rank
    role_at_least           (row, rank) -> whether it is that rank or above
    roles                   the ladder, lowest first
    settable_roles          the ranks a request may give (never the owner's)
    log_staff_action        (actor, kind, target, target_id, detail) - the
                            moderation log, in the same transaction
    can_read_row            (db, user_id, chat row) -> whether they were shown
                            it: a report is only for a line you could read
    wait_words              seconds -> "10 minutes", the way a person says it
    default_channel         the channel a line with none was said in
    reports_per_hour        () -> how many reports one account may file an hour
    report_lines_per_player () -> how many lines a Reports card shows
    mute_max_minutes_mod    () -> a mod's longest mute

The last three are app.py's tunables, ASKED ON EVERY USE: a suite turns them
down (test_chatsafety, test_moderation).

and HANDS BACK the two counts the broadcast poll shows staff - open_lines()
and open_players() - which app.py keeps as _open_report_count() and
_open_report_players().

THE ENDPOINT NAMES ARE app.py's - ban_account, kick_account, unban_account,
set_account_role, report_chat_line, report_player, staff_reports,
resolve_reports, mute_account, unmute_account - and routes.lock holds them.
"""
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services
from .services import (MAX_MOD_BAN_DAYS, MUTE_MAX_MINUTES, REPORT_KEEP_SECONDS, REPORT_LINES_SHOWN,
                       REPORT_OUTCOMES, REPORT_PLAYERS_SHOWN, REPORT_REASONS, TOO_MANY_REPORTS)


def register(app, *, require_auth, require_role, require_fresh_password, get_db, bad_request, parse_stat,
             moderation_target, find_user, can_act_on, role_for, role_at_least, roles, settable_roles,
             log_staff_action, can_read_row, wait_words, default_channel, reports_per_hour,
             report_lines_per_player, mute_max_minutes_mod):

    def close_reports_about(db, user_id, outcome, now):
        """Close every open report about one account, as the caller. Returns
        how many LINES that was. Called by the Reports tab's per-player
        buttons, and by a mute, kick or ban - acting on somebody answers what
        they were reported for, so staff do not have to come back and tidy the
        tab afterwards."""
        return data.close_about(db, g.user["username"], user_id, outcome, now)

    def prune(db, now):
        data.prune(db, now - REPORT_KEEP_SECONDS)

    def report_lines(db, rows, most):
        """Report rows, newest first, folded into one entry per reported line -
        at most `most` lines."""
        out = []
        users = {}
        for key, group in services.fold_lines(rows, most):
            first = group[-1]
            name = first["reported_name"]
            if name not in users:
                users[name] = find_user(name)
            reported = users[name]
            out.append({
                "message_id": key,
                "reported": name,
                "reported_role": role_for(reported) if reported is not None else "player",
                "channel": first["channel"],
                "body": first["body"],
                "image_id": first["image_id"],
                "said_at": int(first["said_at"]),
                "reports": len(group),
                "reporters": sorted({row["reporter_name"] for row in group}),
                "reasons": services.reason_counts(group),
                "first_at": int(first["created_at"]),
                "resolved_at": int(group[0]["resolved_at"]),
                "resolved_by": group[0]["resolved_by"],
                "outcome": group[0]["outcome"],
                # Whether YOU may close it or act on who said it. A mod does not
                # judge reports about another mod - or about themselves.
                "actionable": reported is not None and can_act_on(g.user, reported),
                # Still in chat, or already gone - deleted, or trimmed by age.
                "line_exists": data.line_exists(db, key),
            })
        return out

    def report_players(db, state):
        """One card per reported player: who, how many lines and how many
        people, every reason, and their newest lines.

        WORST FIRST: the most different people reporting them, then the most
        recent report. Ten players reporting one person outranks one player
        reporting ten lines - the second is as likely a grudge as a problem."""
        players = []
        for head in data.reported_players(db, state, REPORT_PLAYERS_SHOWN):
            reported_id = int(head["reported_id"])
            shown = report_lines_per_player()
            lines = report_lines(db, data.rows_about(db, reported_id, state, shown * 20), shown)
            reported = find_user(head["reported_name"])
            players.append({
                "reported": head["reported_name"],
                "reported_role": role_for(reported) if reported is not None else "player",
                "actionable": reported is not None and can_act_on(g.user, reported),
                "line_count": int(head["lines"]),
                "reports": int(head["reports"]),
                "people": int(head["people"]),
                "reporters": data.reporters_of(db, reported_id, state),
                "reasons": data.reasons_about(db, reported_id, state),
                "first_at": int(head["first_at"]),
                "last_at": int(head["last_at"]),
                # The newest REPORT_LINES_PER_PLAYER of them; line_count says how
                # many there are in all.
                "lines": lines,
            })
        return players

    def resolve_reports_about(db, username, outcome):
        """Every open report about one player, closed together - the Dismiss on
        their card. ONE line in the log for the lot, not one per reported
        line: the log is read by people, and twenty "dismissed" lines say one
        thing."""
        not_found = ({"error": "Not Found", "message": "No open report about that player."}, 404)
        if not username or len(username) > 64:
            return not_found
        reported = find_user(username)
        # THE SAME 404 for nobody, nothing open, and somebody out of your
        # reach, as the one-line close gives.
        if reported is None or not can_act_on(g.user, reported):
            return not_found
        newest = data.newest_open_about(db, reported["id"])
        if newest is None:
            return not_found
        now = int(time.time())
        lines = close_reports_about(db, reported["id"], outcome, now)
        log_staff_action(g.user, "report", reported["username"], reported["id"],
                         "%s %d line%s, newest: %s" % (outcome, lines, "" if lines == 1 else "s",
                                                       str(newest["body"] or "(a picture)")[:100]))
        prune(db, now)
        db.commit()
        return {"username": reported["username"], "outcome": outcome, "lines": lines}, 200

    # =========================================================================
    # THE SANCTIONS: a kick, a bounded ban, a permanent one, and a rank
    # =========================================================================
    # Every route here is require_role("mod") at the door and can_act_on()
    # inside (through moderation_target), and the two do different jobs: the
    # decorator says whether you are staff at all, can_act_on() says whether
    # this particular person is within your reach.
    #
    # Refusals are 404, and the same 404 whether the account does not exist or
    # simply outranks you. A mod who could tell those apart could map out who
    # is above them by guessing names.

    def ban_account():
        """
        Ban an account
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username, reason]
              properties:
                username: {type: string}
                reason:   {type: string}
                days:     {type: integer, description: "Omit for a permanent ban."}
        responses:
          200:
            description: The ban as stored
          400:
            description: Missing reason, or a days value out of range
          403:
            description: A mod attempting a permanent or over-long ban
          404:
            description: No such account, or one you may not act on
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}

        target, error = moderation_target(payload)
        if error is not None:
            return error

        reason = str(payload.get("reason", "")).strip()
        if not reason or len(reason) > 500:
            # REQUIRED, not optional. A ban with no reason is one nobody can
            # review later, including the person who issued it.
            return bad_request("reason must be 1-500 characters")

        raw_days = payload.get("days")
        permanent = raw_days is None

        expires_at = None
        if not permanent:
            days = parse_stat(raw_days)
            if days is None or days < 1 or days > 3650:
                return bad_request("days must be an integer 1-3650, or omitted for a permanent ban")
            expires_at = int(time.time()) + days * 86400

        # A PERMANENT BAN IS A HIGHER PERMISSION THAN A TEMPORARY ONE.
        if not role_at_least(g.user, "dev"):
            if permanent:
                return {
                    "error": "Forbidden",
                    "message": "Only a dev or the owner can ban permanently.",
                }, 403
            if parse_stat(raw_days) > MAX_MOD_BAN_DAYS:
                return {
                    "error": "Forbidden",
                    "message": "A mod may ban for at most %d days." % MAX_MOD_BAN_DAYS,
                }, 403

        db = get_db()
        now = int(time.time())

        data.ban(db, target["id"], expires_at, reason, g.user["username"], now)

        # IN THE SAME TRANSACTION. Crossing the name off the list does nothing
        # about the person already inside - a banned player holding a live
        # token keeps playing until it expires, which on this server is thirty
        # days.
        data.end_sessions(db, target["id"])

        closed = close_reports_about(db, target["id"], "actioned", now)
        log_staff_action(
            g.user, "ban", target["username"], target["id"],
            services.ban_words(None if permanent else parse_stat(raw_days), reason)
            + services.closed_words(closed),
        )
        db.commit()

        return {
            "username": target["username"],
            "banned": True,
            "permanent": permanent,
            "expires_at": expires_at,
            "reason": reason,
            "banned_by": g.user["username"],
            "reports_closed": closed,
        }, 200

    def kick_account():
        """
        Sign an account out everywhere, without banning it
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string, example: someplayer}
                reason:   {type: string, example: "suspected shared account"}
        responses:
          200:
            description: Every session for that account was destroyed
          401:
            description: Missing, invalid or expired token
          403:
            description: Not staff, or the target outranks you
          404:
            description: No such account
        """
        # THE SANCTION BETWEEN "NOTHING" AND "BANNED", and the reason it needs
        # to exist separately is that /api/staff/ban already deletes sessions -
        # so the only way to get somebody out of the game was to ban them. That
        # makes the smallest available response to "this account is behaving
        # oddly" the largest one, and a mod who only wants to interrupt
        # something has to choose between overreacting and doing nothing.
        #
        # WHAT IT IS FOR: a shared account to re-secure, a session left open on
        # a machine somebody no longer controls, a stuck client, or buying a
        # minute to look at a report before deciding. It revokes access; it
        # does not revoke permission. The account logs straight back in.
        #
        # THAT IS THE WHOLE POINT and also its honest limit: against someone
        # actively cheating this is a speed bump, because they still hold the
        # password. It is not a ban and must not be presented as one.
        #
        # SAME REACH AS EVERY OTHER SANCTION. can_act_on() via
        # moderation_target, so a mod cannot kick another mod and nobody can
        # kick the owner - a weaker gate here would make this the cheap way to
        # harass staff.
        #
        # LOGGED LIKE A BAN. A session that ends for no visible reason is a
        # support ticket; staff_actions is what answers it.
        payload = request.get_json(silent=True) or {}
        target, refusal = moderation_target(payload)
        if refusal is not None:
            return refusal

        reason = str(payload.get("reason", "")).strip()[:200]

        db = get_db()
        # COUNTED BEFORE THE DELETE, because rowcount after a DELETE is the
        # number removed and the useful number is "how many places were they
        # signed in" - which a mod reads as evidence. Zero is a real answer: the
        # account holds no live session and the kick changed nothing.
        live = data.live_sessions(db, target["id"], int(time.time()))

        data.end_sessions(db, target["id"])

        closed = close_reports_about(db, target["id"], "actioned", int(time.time()))
        log_staff_action(
            g.user, "kick", target["username"], target["id"],
            "%d session(s)%s%s" % (live, (": " + reason) if reason else "", services.closed_words(closed)),
        )
        db.commit()

        return {
            "username": target["username"],
            "sessions_ended": live,
            "banned": False,
            "by": g.user["username"],
            "reports_closed": closed,
        }, 200

    def unban_account():
        """
        Lift a ban
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string}
        responses:
          200:
            description: The account is no longer banned
          404:
            description: No such account, or one you may not act on
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}

        target, error = moderation_target(payload)
        if error is not None:
            return error

        db = get_db()
        data.unban(db, target["id"])
        log_staff_action(g.user, "unban", target["username"], target["id"])
        db.commit()

        # Idempotent on purpose: unbanning someone who is not banned is a 200
        # that changed nothing. The caller wanted them not-banned, and they are
        # not.
        return {"username": target["username"], "banned": False}, 200

    def set_account_role():
        """
        Change an account's rank
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username, role]
              properties:
                username: {type: string}
                role:     {type: string, description: "player, mod or dev"}
        responses:
          200:
            description: The rank as stored
          400:
            description: Not a settable rank
          403:
            description: Granting a rank at or above your own
          404:
            description: No such account, or one you may not act on
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}

        target, error = moderation_target(payload)
        if error is not None:
            return error

        new_role = str(payload.get("role", "")).strip().lower()
        if new_role not in settable_roles:
            return bad_request(
                "role must be one of: %s. 'owner' and 'coowner' come from the environment."
                % ", ".join(settable_roles)
            )

        # YOU CANNOT GRANT A RANK AT OR ABOVE YOUR OWN.
        #
        # Without this, one dev promotes another dev, or promotes a player to
        # dev, and a single compromised staff account spreads sideways for as
        # long as nobody is looking. Only the owner makes a dev.
        if roles.index(new_role) >= roles.index(role_for(g.user)):
            return {
                "error": "Forbidden",
                "message": "You cannot grant a rank at or above your own.",
            }, 403

        was = target["role"]

        db = get_db()
        data.set_role(db, target["id"], new_role)
        log_staff_action(
            g.user, "role", target["username"], target["id"], "%s -> %s" % (was, new_role)
        )
        db.commit()

        return {"username": target["username"], "role": new_role, "was": was}, 200

    # =========================================================================
    # REPORT, AND MUTE
    # =========================================================================
    #   REPORT is how a player hands a line - or a player - to staff. The line
    #   is copied into the report, so it survives being deleted - which is
    #   usually what a report leads to. Staff see open reports in their panel
    #   and a count on the poll.
    #
    #   MUTE is staff's, and smaller than a ban: the account keeps playing and
    #   cannot speak until the time runs out. Every channel, pictures included.

    def report_chat_line():
        """
        Tell staff about a chat line
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [id, reason]
              properties:
                id:     {type: integer}
                reason: {type: string, enum: [spam, harassment, hate, cheating, other]}
        responses:
          200:
            description: Reported (or already reported by you - "already" says so)
          400:
            description: A bad id or reason, or your own line
          404:
            description: No such line, or one you could not have read
          429:
            description: Too many reports this hour
        """
        payload = request.get_json(silent=True) or {}
        try:
            message_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            message_id = 0
        if message_id <= 0:
            return bad_request("id must be a whole number")
        reason = str(payload.get("reason", "")).strip().lower()
        if reason not in REPORT_REASONS:
            return bad_request("reason must be one of: %s" % ", ".join(REPORT_REASONS))
        db = get_db()
        me = int(g.user["id"])
        row = data.chat_line(db, message_id)
        # A LINE YOU WERE NEVER SHOWN IS NOT THERE. The same 404 for both, so
        # the route cannot be used to find out which ids are somebody's
        # whispers.
        if row is None or not can_read_row(db, me, row):
            return {"error": "Not Found", "message": "No such message."}, 404
        if int(row["user_id"] or 0) == me:
            return bad_request("That is your own message.")
        if data.report_by(db, message_id, me) is not None:
            return {"id": message_id, "reported": True, "already": True}, 200
        now = int(time.time())
        if data.filed_since(db, me, now - 3600) >= reports_per_hour():
            return dict(TOO_MANY_REPORTS), 429
        prune(db, now)
        data.file_line_report(db, message_id, me, g.user["username"], row,
                              str(row["channel"] or default_channel), reason, now)
        db.commit()
        return {"id": message_id, "reported": True}, 200

    def report_player():
        """
        Tell staff about a player - not one line, the player (game 0.43.0)
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username, reason]
              properties:
                username: {type: string}
                reason:   {type: string, enum: [spam, harassment, hate, cheating, other]}
                area:     {type: string, description: "Where you saw them, an area id - for the staff card"}
        responses:
          200:
            description: Reported (or already reported by you and still open - "already" says so)
          400:
            description: A bad reason, or yourself
          404:
            description: No such account
          429:
            description: Too many reports this hour
        """
        # THE OWNER: "clicking over a person should have add friend inv to
        # guild or party and report button that syncs to our mod/dev/owner". A
        # report about a player is a row in chat_reports beside the reported
        # lines, so it lands on the same card on the staff Reports tab, counts
        # in the same badge, and is closed by the same buttons. Its channel is
        # "player", and its message_id - the key a line's reports share - is
        # NEGATIVE (services.player_report_key()).
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("username", "")).strip()
        reason = str(payload.get("reason", "")).strip().lower()
        if reason not in REPORT_REASONS:
            return bad_request("reason must be one of: %s" % ", ".join(REPORT_REASONS))
        target = find_user(name) if name and len(name) <= 64 else None
        if target is None:
            return {"error": "Not Found", "message": "No such account."}, 404
        me = int(g.user["id"])
        if int(target["id"]) == me:
            return bad_request("That is you.")
        where = services.where_seen(payload.get("area", ""))
        key = services.player_report_key(target["id"], me)
        db = get_db()
        now = int(time.time())
        row = data.report_by(db, key, me)
        if row is not None and int(row["resolved_at"] or 0) == 0:
            return {"username": target["username"], "reported": True, "already": True}, 200
        if data.filed_since(db, me, now - 3600) >= reports_per_hour():
            return dict(TOO_MANY_REPORTS), 429
        body = "Reported %s" % where
        if row is not None:
            data.reopen_player_report(db, key, me, target["username"], body, reason, now)
        else:
            prune(db, now)
            data.file_player_report(db, key, me, g.user["username"], int(target["id"]), target["username"],
                                    body, reason, now)
        db.commit()
        return {"username": target["username"], "reported": True}, 200

    def staff_reports():
        """
        Reported chat lines - one card per reported player, and one entry per line
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: query
            name: state
            type: string
            description: "open (the default) or all"
        responses:
          200:
            description: '{"players": [...], "reports": [...], "open": n, "open_players": n, "now": t}'
          400:
            description: A state that is neither open nor all
        """
        state = str(request.args.get("state", "open")).strip().lower()
        if state not in ("open", "all"):
            return bad_request("state must be open or all")
        db = get_db()
        rows = data.report_rows(db, state)
        # ONE ENTRY PER LINE - the shape every build before the per-player
        # cards reads, kept so a staff member on an older build still sees
        # their tab.
        out = report_lines(db, rows, REPORT_LINES_SHOWN)
        return {
            "players": report_players(db, state),
            "reports": out,
            "open": data.open_line_count(db),
            "open_players": data.open_player_count(db),
            "now": int(time.time()),
        }, 200

    def resolve_reports():
        """
        Close every open report on one line, or every open report about one player
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [outcome]
              properties:
                message_id: {type: integer, description: "One line. Give this or username."}
                username:   {type: string, description: "Everything open about this player - their card on the Reports tab."}
                outcome:    {type: string, enum: [dismissed, actioned]}
        responses:
          200:
            description: Closed
          400:
            description: A bad id or outcome
          404:
            description: No open report on that line or player, or one about somebody you cannot act on
        """
        payload = request.get_json(silent=True) or {}
        outcome = str(payload.get("outcome", "")).strip().lower()
        if outcome not in REPORT_OUTCOMES:
            return bad_request("outcome must be dismissed or actioned")
        db = get_db()
        if "username" in payload and "message_id" not in payload:
            return resolve_reports_about(db, str(payload.get("username") or "").strip(), outcome)
        try:
            message_id = int(payload.get("message_id", 0))
        except (TypeError, ValueError):
            message_id = 0
        # NEGATIVE IS A PLAYER, not a line (report_player(), game 0.43.0).
        if message_id == 0 or isinstance(payload.get("message_id"), bool):
            return bad_request("message_id must be a whole number")
        first = data.first_open_on_line(db, message_id)
        not_found = ({"error": "Not Found", "message": "No open report on that line."}, 404)
        if first is None:
            return not_found
        reported = find_user(first["reported_name"])
        if reported is None or not can_act_on(g.user, reported):
            return not_found
        now = int(time.time())
        closed = data.close_line(db, message_id, g.user["username"], outcome, now)
        log_staff_action(g.user, "report", first["reported_name"], first["reported_id"],
                         "%s: %s" % (outcome, str(first["body"] or "(a picture)")[:100]))
        prune(db, now)
        db.commit()
        return {"message_id": message_id, "outcome": outcome, "closed": closed}, 200

    def mute_account():
        """
        Stop an account talking in chat for a while
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username, minutes, reason]
              properties:
                username: {type: string}
                minutes:  {type: integer, description: "1 to 1440 for a mod; up to 30 days for dev and the owner"}
                reason:   {type: string}
        responses:
          200:
            description: The mute as stored
          400:
            description: Missing reason, or minutes out of range
          403:
            description: A mod muting for longer than a day
          404:
            description: No such account, or one you may not act on
        """
        payload = request.get_json(silent=True) or {}
        target, error = moderation_target(payload)
        if error is not None:
            return error
        reason = str(payload.get("reason", "")).strip()
        if not reason or len(reason) > 500:
            # Required, like a ban's: a mute nobody can explain later is one
            # nobody can review.
            return bad_request("reason must be 1-500 characters")
        minutes = parse_stat(payload.get("minutes"))
        if minutes is None or minutes < 1 or minutes > MUTE_MAX_MINUTES:
            return bad_request("minutes must be a whole number 1-%d" % MUTE_MAX_MINUTES)
        longest_for_a_mod = mute_max_minutes_mod()
        if minutes > longest_for_a_mod and not role_at_least(g.user, "dev"):
            return {"error": "Forbidden",
                    "message": "A mod may mute for at most %s." % wait_words(longest_for_a_mod * 60)}, 403
        db = get_db()
        until = int(time.time()) + minutes * 60
        data.mute(db, target["id"], until, reason, g.user["username"])
        closed = close_reports_about(db, target["id"], "actioned", int(time.time()))
        log_staff_action(g.user, "mute", target["username"], target["id"],
                         "%s: %s%s" % (wait_words(minutes * 60), reason, services.closed_words(closed)))
        db.commit()
        return {"username": target["username"], "muted": True, "until": until,
                "reason": reason, "muted_by": g.user["username"], "reports_closed": closed}, 200

    def unmute_account():
        """
        Let an account talk again
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string}
        responses:
          200:
            description: Not muted
          404:
            description: No such account, or one you may not act on
        """
        payload = request.get_json(silent=True) or {}
        target, error = moderation_target(payload)
        if error is not None:
            return error
        db = get_db()
        data.unmute(db, target["id"])
        log_staff_action(g.user, "unmute", target["username"], target["id"])
        db.commit()
        return {"username": target["username"], "muted": False}, 200

    mod = require_role("mod")
    app.add_url_rule("/api/staff/ban", endpoint="ban_account",
                     view_func=require_auth(mod(require_fresh_password(ban_account))), methods=["POST"])
    app.add_url_rule("/api/staff/kick", endpoint="kick_account",
                     view_func=require_auth(mod(kick_account)), methods=["POST"])
    app.add_url_rule("/api/staff/unban", endpoint="unban_account",
                     view_func=require_auth(mod(require_fresh_password(unban_account))), methods=["POST"])
    app.add_url_rule("/api/staff/role", endpoint="set_account_role",
                     view_func=require_auth(mod(require_fresh_password(set_account_role))), methods=["PUT"])
    app.add_url_rule("/api/chat/report", endpoint="report_chat_line",
                     view_func=require_auth(report_chat_line), methods=["POST"])
    app.add_url_rule("/api/report/player", endpoint="report_player",
                     view_func=require_auth(report_player), methods=["POST"])
    app.add_url_rule("/api/staff/reports", endpoint="staff_reports",
                     view_func=require_auth(mod(staff_reports)), methods=["GET"])
    app.add_url_rule("/api/staff/reports/resolve", endpoint="resolve_reports",
                     view_func=require_auth(mod(resolve_reports)), methods=["POST"])
    app.add_url_rule("/api/staff/mute", endpoint="mute_account",
                     view_func=require_auth(mod(mute_account)), methods=["POST"])
    app.add_url_rule("/api/staff/unmute", endpoint="unmute_account",
                     view_func=require_auth(mod(unmute_account)), methods=["POST"])

    return SimpleNamespace(open_lines=data.open_line_count, open_players=data.open_player_count)
