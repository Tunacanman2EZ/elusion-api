"""staffdesk/routes.py - the edge: the staff list, the moderation log, a note,
and one account's page, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    require_role        a staff rank -> its decorator (404 below it)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    query_int           (name, default, low, high) -> the query's int, or None
    moderation_target   payload -> (account, None) or (None, the refusal):
                        an account within the caller's reach
    can_act_on          (actor, target) -> whether actor outranks target
    role_for            a users row -> its effective rank
    rank_sql            role_for() as SQL: (alias) -> (sql, params), asked
                        on every use - it reads the co-owners as they are now
    roles               the ladder, lowest first
    default_role        the plain player's rank
    ban_state           a users row -> its live ban, or None
    mute_state          (row, now) -> its live chat mute, or None
    log_staff_action    (actor, kind, target, target_id, detail) - a note is
                        a line in the moderation log, written by app.py's
    online_window       seconds since a heartbeat that still count as online
    playing_slot        (db, user_id) -> the character they are playing
    linked_accounts     (db, user_id) -> (accounts seen from their addresses,
                        whether the list was cut short)
    crowded_at          how many accounts on one address make a link weak
    row_int             (row, column) -> its int, 0 for a missing column

and HANDS BACK the two query builders - users_query() and log_query() - which
app.py keeps as _staff_users_query() and _staff_log_query(), the ones
test_moderation.py asks SQLite for the plan of.

THE ENDPOINT NAMES ARE app.py's - list_accounts, staff_action_log, staff_note,
staff_read_user - and routes.lock holds them.
"""
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services
from .services import (RECENT_SHOWN, STAFF_ACTION_GROUPS, STAFF_ACTION_KINDS, STAFF_LIST_SHOWS,
                       STAFF_NOTE_MAX, STAFF_PAGE_DEFAULT, STAFF_PAGE_MAX, STAFF_PRIVATE_KINDS)


def register(app, *, require_auth, require_role, get_db, bad_request, query_int, moderation_target, can_act_on,
             role_for, rank_sql, roles, default_role, ban_state, mute_state, log_staff_action, online_window,
             playing_slot, linked_accounts, crowded_at, row_int):

    def users_query(q, show, after, limit, now):
        """The staff list's SQL: (page_sql, page_params, count_sql,
        count_params)."""
        return data.users_query(q, show, after, limit, now, rank_sql=rank_sql,
                                default_level=roles.index(default_role), online_window=online_window)

    def log_query(viewer, player="", staff="", kind="", before=0, limit=STAFF_PAGE_DEFAULT):
        """The moderation log's SQL, as `viewer` may read it: (page_sql,
        page_params, summary_sql, summary_params)."""
        return data.log_query(get_db(), roles.index(role_for(viewer)), player, staff, kind, before, limit,
                              rank_sql=rank_sql, default_level=roles.index(default_role),
                              groups=STAFF_ACTION_GROUPS)

    def list_accounts():
        """
        One page of accounts, with rank, ban state, record and whether they are online
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: query
            name: q
            type: string
            description: "Any part of a username, any case."
          - in: query
            name: show
            type: string
            description: "all (default), online, banned or staff"
          - in: query
            name: after
            type: string
            description: "The previous page's next_after."
          - in: query
            name: limit
            type: integer
            description: "1-200, default 50"
        responses:
          200:
            description: The page, whether there is more, and how many matched
          400:
            description: An unknown filter, or a limit out of range
          401:
            description: Missing, invalid or expired token
        """
        q = request.args.get("q", "").strip()
        show = request.args.get("show", "").strip().lower() or "all"
        after = request.args.get("after", "").strip()
        limit = query_int("limit", STAFF_PAGE_DEFAULT, 1, STAFF_PAGE_MAX)

        if show not in STAFF_LIST_SHOWS:
            return bad_request("show must be one of: %s" % ", ".join(STAFF_LIST_SHOWS))
        if limit is None:
            return bad_request("limit must be a whole number 1-%d" % STAFF_PAGE_MAX)
        if len(q) > 64 or len(after) > 64:
            return bad_request("q and after are at most 64 characters")

        db = get_db()
        now = int(time.time())

        page_sql, page_params, count_sql, count_params = users_query(q, show, after, limit, now)
        rows = db.execute(page_sql, page_params).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]

        # PRESENCE AND RECORD FOR THIS PAGE, one query each - not one per
        # account, and not for the whole server.
        ids = [int(row["id"]) for row in rows]
        seen = data.last_seen(db, ids, now)
        record = data.records(db, ids)

        accounts = []
        for row in rows:
            ban = ban_state(row)
            last_seen = seen.get(int(row["id"]), 0)
            actionable = can_act_on(g.user, row)
            accounts.append({
                "id": row["id"],
                "username": row["username"],
                "role": role_for(row),
                "banned": ban is not None,
                "ban": ban,
                "mute": mute_state(row, now),
                # Whether YOU can act on this person, so a client can grey out
                # the buttons rather than offering them and being refused.
                "actionable": actionable,
                # See app.py's ONLINE_WINDOW_SECONDS. 0 means no live session
                # at all.
                "online": last_seen > 0 and now - last_seen <= online_window,
                "last_seen_at": last_seen,
                "record": services.visible_tally(record.get(int(row["id"]), {}), actionable),
            })

        return {
            "accounts": accounts,
            # THE SERVER'S CLOCK, so "last seen 4 min ago" is worked out against
            # the same clock that wrote last_seen_at - a client whose own clock
            # is off by an hour would otherwise say so about everyone.
            "now": now,
            "more": more,
            "next_after": accounts[-1]["username"] if more else None,
            "matched": data.count(db, count_sql, count_params),
            "online": data.online_now(db, now, online_window),
            "show": show,
            "q": q,
            "limit": limit,
        }, 200

    def staff_action_log():
        """
        The moderation log, newest first, one page at a time
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: query
            name: player
            type: string
            description: "Only entries about this account (any case)."
          - in: query
            name: staff
            type: string
            description: "Only entries by this member of staff (any case)."
          - in: query
            name: action
            type: string
            description: "Only this kind - one of the response's `kinds` - or a set by name, one of `groups` (moderation)."
          - in: query
            name: before
            type: integer
            description: "The previous page's next_before."
          - in: query
            name: limit
            type: integer
            description: "1-200, default 50"
        responses:
          200:
            description: A page of entries, whether there is more, and with `player` a tally of that record
          400:
            description: An unknown action, or a cursor or limit out of range
          401:
            description: Missing, invalid or expired token
        """
        # THE REASON THIS EXISTS. staff_actions has recorded every moderation
        # act since the first day, and the only way to read it was sqlite3 on
        # the box holding elusion.db - or twenty rows at a time through one
        # account's view. "What happened while I was away", "has anybody
        # warned this player before", "what has the new mod been doing" were
        # all unanswerable for anybody but the owner at a terminal. A log
        # nobody can read is not the review that makes delegating safe; it is
        # a promise of one.
        player = request.args.get("player", "").strip()
        staff = request.args.get("staff", "").strip()
        kind = request.args.get("action", "").strip().lower()
        before = query_int("before", 0, 0, 2 ** 62)
        limit = query_int("limit", STAFF_PAGE_DEFAULT, 1, STAFF_PAGE_MAX)

        if kind and kind not in STAFF_ACTION_KINDS and kind not in STAFF_ACTION_GROUPS:
            return bad_request("action must be one of: %s" % ", ".join(
                list(STAFF_ACTION_GROUPS) + list(STAFF_ACTION_KINDS)))
        if before is None:
            return bad_request("before must be a whole number, 0 or more")
        if limit is None:
            return bad_request("limit must be a whole number 1-%d" % STAFF_PAGE_MAX)
        if len(player) > 64 or len(staff) > 64:
            return bad_request("player and staff are at most 64 characters")

        db = get_db()
        page_sql, page_params, summary_sql, summary_params = log_query(
            g.user, player, staff, kind, before, limit)

        rows = db.execute(page_sql, page_params).fetchall()
        more = len(rows) > limit
        entries = [services.log_entry(r) for r in rows[:limit]]

        body = {
            "actions": entries,
            "more": more,
            "next_before": entries[-1]["id"] if more else None,
            "kinds": list(STAFF_ACTION_KINDS),
            "groups": {name: list(group) for name, group in STAFF_ACTION_GROUPS.items()},
            "now": int(time.time()),
        }

        if player:
            # THE RECORD AT A GLANCE: "2 bans, 1 kick, 3 warnings" answers "is
            # this the first time" before anybody reads a line of it.
            body["summary"] = {
                r["action"]: int(r["n"])
                for r in db.execute(summary_sql, summary_params).fetchall()
            }

        return body, 200

    def staff_note():
        """
        Write a staff-only note or warning onto an account's record
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
              required: [username, text]
              properties:
                username: {type: string}
                text:     {type: string, description: "1-500 characters"}
                kind:     {type: string, description: "note (default) or warn"}
        responses:
          200:
            description: The entry as recorded
          400:
            description: Empty or over-long text, or an unknown kind
          404:
            description: No such account, one you may not act on, or not staff
          401:
            description: Missing, invalid or expired token
        """
        # THE SANCTION BELOW A KICK, and the memory between shifts. A mod who
        # told somebody to stop had nowhere to write it down, so the next mod
        # started from nothing and a player could collect a first warning from
        # every one of them. A warning here is a record that one was given -
        # the player is not sent anything, because what they were told was
        # said to them already, by a person, in the game.
        #
        # REACH, like every sanction: moderation_target is can_act_on(), so a
        # note is only ever written by somebody who could read it back.
        payload = request.get_json(silent=True) or {}

        target, error = moderation_target(payload)
        if error is not None:
            return error

        kind = str(payload.get("kind", "") or "note").strip().lower()
        if kind not in STAFF_PRIVATE_KINDS:
            return bad_request("kind must be one of: %s" % ", ".join(STAFF_PRIVATE_KINDS))

        text = services.one_line(payload.get("text", ""))
        if not text or len(text) > STAFF_NOTE_MAX:
            return bad_request("text must be 1-%d characters" % STAFF_NOTE_MAX)

        db = get_db()
        # THE ONE COMPUTED KIND in the log, and checked just above against the
        # private kinds (test_moderation.py, M-8).
        log_staff_action(g.user, kind, target["username"], target["id"], text)
        entry_id = data.last_entry_id(db)
        db.commit()

        return {
            "entry": {
                "id": entry_id,
                "at": int(time.time()),
                "by": g.user["username"],
                "action": kind,
                "target": target["username"],
                "detail": text,
            },
        }, 200

    def staff_read_user(username):
        """
        Everything staff may know about one account
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: path
            name: username
            type: string
            required: true
        responses:
          200:
            description: The account, its characters, and its recent activity
          401:
            description: Missing, invalid or expired token
          403:
            description: Not staff
          404:
            description: No such account
        """
        # THE READ SIDE OF MODERATION, and the reason the two log tables are
        # worth having. login_attempts and kill_reports answer real questions
        # - is this account being brute-forced, is it reporting kills faster
        # than a person could - but until now the only way to ask was sqlite3
        # on the box holding elusion.db. A ban decision made without being
        # able to look is a guess.
        #
        # WHAT IS NEVER HERE: password_hash and session tokens. Not redacted,
        # not included - a SELECT * on users would carry the hash into this
        # payload the moment someone stopped reading carefully, so the columns
        # are named.
        db = get_db()
        row = data.account(db, username)
        if row is None:
            return {"error": "Not Found", "message": "No such account."}, 404

        # IP ADDRESSES ARE GATED HIGHER THAN THE REST, deliberately.
        #
        # Rank, ban state and characters are what the owner panel's view
        # button needs, and any mod may see them for anyone. An address is
        # different: it is personal data about a real person, and the honest
        # test for whether a mod should have it is whether they could act on
        # that person at all. So it follows can_act_on() - strictly above -
        # which means a mod sees a player's addresses and not another mod's,
        # and nobody sees the owner's.
        #
        # The COUNT is always shown. "Seen from 4 addresses" is the shape of
        # the answer a mod needs for "is this account shared"; the addresses
        # themselves are what they need only if they are going to act.
        may_see_addresses = can_act_on(g.user, row)

        characters = []
        # WHICH ONE THEY ARE ON, so a moderator reading four characters knows
        # which of them was in the fight being reported - the same rule the
        # trade window and the lists use (app.py's _playing_slot()).
        playing = playing_slot(db, int(row["id"]))
        for save in data.characters(db, row["id"]):
            characters.append({
                "slot": int(save["slot"]),
                "class_id": save["class_id"],
                "name": save["name"],
                "level": int(save["level"]),
                "area": save["area"],
                "updated_at": int(save["updated_at"] or 0),
                "playing": int(save["slot"]) == playing,
            })

        # LOGINS. Recent rows for the timeline, plus a summary so a long quiet
        # history does not hide a burst - a page of the last twenty attempts
        # looks identical whether the account has had twenty failures or two
        # thousand.
        logins = []
        for attempt in data.login_attempts(db, row["username"], RECENT_SHOWN):
            entry = {
                "ok": bool(attempt["ok"]),
                "reason": attempt["reason"],
                "at": int(attempt["at"]),
            }
            if may_see_addresses:
                entry["ip"] = attempt["ip"]
            logins.append(entry)

        totals = data.login_totals(db, row["username"])
        good = int(totals["good"] or 0)
        total = int(totals["n"] or 0)

        # KILLS, grouped rather than listed. A thousand rows is not a view;
        # "boss x412, most recent 9 seconds ago" is.
        kills = []
        for kill in data.kills(db, row["id"]):
            kills.append({
                "enemy_id": kill["enemy_id"],
                "count": int(kill["n"]),
                "xp_total": int(kill["xp"] or 0),
                "first_at": int(kill["earliest"] or 0),
                "last_at": int(kill["latest"] or 0),
            })

        # WHAT STAFF HAVE ALREADY DONE TO THEM. A second mod arriving at the
        # same account should see the first one's decision before making their
        # own.
        #
        # Notes and warnings only for somebody who could act on the account -
        # the rule GET /api/staff/actions applies, see STAFF_PRIVATE_KINDS.
        # Without it this route was a side door: a mod looking up another mod
        # read what a dev had written about them.
        history = []
        for entry in data.staff_history(db, row["id"], may_see_addresses, RECENT_SHOWN):
            history.append({
                "action": entry["action"],
                "by": entry["actor_name"],
                "detail": entry["detail"],
                "at": int(entry["created_at"]),
            })

        # WHERE THIS ACCOUNT IS SIGNED IN RIGHT NOW.
        #
        # login_attempts above answers "who has been trying"; this answers "who
        # is holding a key". They are different questions and the second one
        # was not askable at all - a mod deciding whether to kick had no way to
        # see whether there was anything to kick, and after a ban no way to
        # confirm the person was actually out.
        #
        # NO TOKENS. Not truncated, not masked - absent. A staff view is
        # exactly where a readable token would be most damaging, because the
        # people reading it are the ones with reach. What is useful is the
        # SHAPE: how many keys exist, how old they are, when they run out.
        #
        # EXPIRED ROWS EXCLUDED. user_for_token() refuses them and deletes them
        # on sight, so a row past its expiry grants nothing - listing it would
        # invent a session that is not there. _prune_sessions() clears them on
        # the login path anyway; this simply does not count on that having
        # run.
        # NO issued_at, BECAUSE THE TABLE DOES NOT HOLD ONE. sessions is
        # (token_hash, user_id, expires_at, presence) and nothing else, so
        # "when did they sign in" would have to be derived as expires_at -
        # TOKEN_TTL - which is right only for rows issued under the CURRENT ttl
        # and silently wrong for every row issued before it last changed. A
        # migration could add the column; it is not worth one, because
        # expires_at already carries the same information against a fixed ttl:
        # a session with 29 of its 30 days left is minutes old, and one with
        # two days left is nearly a month old.
        now_secs = int(time.time())
        sessions = []
        for s in data.live_sessions(db, row["id"], now_secs, RECENT_SHOWN):
            sessions.append({
                "expires_at": int(s["expires_at"]),
                "expires_in": int(s["expires_at"]) - now_secs,
            })

        # LINKED ACCOUNTS, behind the same gate as the addresses themselves.
        #
        # "Who else plays from this person's connection" is a statement about
        # other people, several of whom are not the subject of this lookup and
        # have done nothing. If a mod may not see this account's addresses,
        # they certainly may not see a list of strangers derived from them -
        # so this follows can_act_on() rather than the mod rank that opens the
        # route.
        #
        # THIS IS A READ, AND ONLY A READ. Nothing here bans, flags or scores
        # an account. It exists because the evader's NEXT account used to be
        # invisible until somebody already suspected its name.
        linked, linked_truncated = ([], False)
        if may_see_addresses:
            linked, linked_truncated = linked_accounts(db, row["id"])

        return {
            "id": row["id"],
            "username": row["username"],
            "role": role_for(row),
            "created_at": int(row["created_at"] or 0),
            "ban": ban_state(row),
            "mute": mute_state(row),
            "actionable": may_see_addresses,
            # ALWAYS SHOWN, unlike the addresses and the linked accounts.
            #
            # The gate on those two is that they are facts about OTHER PEOPLE
            # - an address identifies a household, a link names strangers who
            # share it. A session count is a fact about this account only, and
            # it is the one number a mod needs before deciding between a kick
            # and a ban. Hiding it would gate the sanction rather than the
            # personal data.
            "sessions": {
                "active": len(sessions),
                "list": sessions,
            },
            "linked_accounts": {
                "visible": may_see_addresses,
                "accounts": linked,
                "truncated": linked_truncated,
                # Named in the payload so a client does not have to hard-code
                # the threshold to explain its own "weak" badge.
                "crowded_at": crowded_at,
            },
            "lusions": data.lusions(db, row["id"]),
            "characters": characters,
            "logins": {
                "recent": logins,
                "total": total,
                "failed": total - good,
                "addresses": int(totals["addresses"] or 0),
                "addresses_visible": may_see_addresses,
                "locked_until": row_int(row, "lockout_until"),
                "consecutive_failures": row_int(row, "failed_logins"),
            },
            "kills": kills,
            "staff_history": history,
        }, 200

    mod = require_role("mod")
    app.add_url_rule("/api/staff/users", endpoint="list_accounts",
                     view_func=require_auth(mod(list_accounts)), methods=["GET"])
    app.add_url_rule("/api/staff/actions", endpoint="staff_action_log",
                     view_func=require_auth(mod(staff_action_log)), methods=["GET"])
    app.add_url_rule("/api/staff/note", endpoint="staff_note",
                     view_func=require_auth(mod(staff_note)), methods=["POST"])
    app.add_url_rule("/api/staff/user/<username>", endpoint="staff_read_user",
                     view_func=require_auth(mod(staff_read_user)), methods=["GET"])

    return SimpleNamespace(users_query=users_query, log_query=log_query)
