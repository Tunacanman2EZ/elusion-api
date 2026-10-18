"""guilds/routes.py - the edge: the ten routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth       the token check (puts the account on g.user)
    require_role       a staff rank -> its decorator (staff_guild, guild_list)
    get_db             the request's database connection
    bad_request        a message -> app.py's 400
    parse_slot         a raw slot -> its int, or None
    find_user          a name -> its users row, or None
    ensure_account     user_id -> the account row (bank_gold), made if missing
    gold_delta         (db, user_id, slot, delta, reason, detail) -> the purse
                       after, through the gold ledger
    bank_gold_delta    (db, user_id, delta, reason, detail) -> the bank after
    insufficient_gold  the exception either raises when a balance would go
                       below nothing
    log_staff_action   (user, action, target, detail=) - the moderation log
    has_owner_powers   username -> whether they hold the owner's powers
    role_for           users row -> the rank a list shows
    name_hue_of        users row -> the colour they chose, or None
    playing_slot_sql   app.py's PLAYING_SLOT_SQL: which character an account
                       is playing
    online_window      seconds since last seen that still count as on

THE ENDPOINT NAMES ARE app.py's - guild_read, guild_create, guild_invite,
guild_respond, guild_leave, guild_kick, guild_set_rank, guild_disband,
staff_guild, guild_list - and routes.lock holds them.

EVERY ROUTE HERE ANSWERS "WHO ARE YOU IN THIS GUILD" FIRST. That is one
lookup - seat() - and the rank it returns decides the rest. The alternative,
each route working it out from the request, is how one of them ends up trusting
a guild name the caller sent.

A REFUSAL IS A 404 WHEN IT WOULD OTHERWISE CONFIRM SOMETHING. "No such guild"
and "you are not allowed to touch that guild" read the same from outside,
exactly as the staff routes already do.
"""
import re
import time

from flask import g, request

from . import data, services
from .data import log_guild_event
from .services import (GUILD_FOUND_COST, GUILD_NAME_PATTERN, GUILD_RANKS, MAX_GUILD_INVITES,
                       MAX_GUILD_MEMBERS, MAX_GUILD_NAME, guild_rank_at_least)


def register(app, *, require_auth, require_role, get_db, bad_request, parse_slot, find_user,
             ensure_account, gold_delta, bank_gold_delta, insufficient_gold, log_staff_action,
             has_owner_powers, role_for, name_hue_of, playing_slot_sql, online_window):

    def seat():
        """(membership_row, guild_row, rank) for the caller, or (None, None, "")."""
        db = get_db()
        mine = data.membership(db, g.user["id"])
        if mine is None:
            return None, None, ""
        return mine, data.by_id(db, int(mine["guild_id"])), str(mine["rank"])

    def guild_dict(db, guild_row, now=None):
        """A guild as any route returns it."""
        if guild_row is None:
            return {}
        if now is None:
            now = int(time.time())
        guild_id = int(guild_row["id"])
        rows = data.roster_rows(db, guild_id)
        playing = data.playing_saves(db, [int(row["id"]) for row in rows], playing_slot_sql, now, online_window)
        members = services.roster(rows, playing, now, online_window, role_for, name_hue_of)
        return services.guild_answer(guild_row, members, data.activity(db, guild_id))

    def guild_read():
        """
        Your guild, its roster, and any invitation waiting for you
        ---
        tags:
          - Guilds
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: The guild you are in (or none), your rank, and your invites
          401:
            description: Missing, invalid or expired token
        """
        db = get_db()
        now = int(time.time())
        _mine, guild, rank = seat()

        # INVITATIONS ARE ANSWERED WHETHER OR NOT YOU ARE IN A GUILD, because
        # somebody who has just left should still see the one they were sent
        # yesterday rather than having it vanish silently.
        invites = []
        for row in data.invites_to(db, g.user["id"]):
            invites.append({
                "guild": row["name"],
                "by": row["invited_by"],
                "at": int(row["created_at"] or 0),
            })

        answer = {
            "in_guild": guild is not None,
            "rank": rank,
            "invites": invites,
            "now": now,
            "cost": GUILD_FOUND_COST,
            "capacity": MAX_GUILD_MEMBERS,
        }
        if guild is not None:
            answer["guild"] = guild_dict(db, guild, now)
        return answer, 200

    def guild_create():
        """
        Found a guild, paying the founding cost
        ---
        tags:
          - Guilds
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: guild
            schema:
              type: object
              required: [name, slot]
              properties:
                name: {type: string, example: "The Crowned"}
                slot: {type: integer, example: 0}
        responses:
          200:
            description: The new guild, with you as its leader
          400:
            description: Bad name, bad slot, already taken, or not enough gold
          401:
            description: Missing, invalid or expired token
          409:
            description: You are already in a guild
        """
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()

        if not re.fullmatch(GUILD_NAME_PATTERN, name):
            return bad_request(
                "a guild name is 3 to %d characters: letters, numbers, spaces,"
                " apostrophes and hyphens, starting with a letter or number"
                % MAX_GUILD_NAME)

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be a whole number")

        db = get_db()
        mine, _existing, _rank = seat()
        if mine is not None:
            return {
                "error": "Conflict",
                "message": "You are already in a guild. Leave it first.",
            }, 409

        if data.by_name(db, name) is not None:
            return bad_request("a guild by that name already exists")

        # WHAT THEY ACTUALLY HAVE, read before anything is spent - both piles.
        purse = data.carried_gold(db, g.user["id"], slot)
        if purse is None:
            return {"error": "Not Found", "message": "That slot is empty."}, 404

        account = ensure_account(g.user["id"])
        carry_gold = int(purse["gold"] or 0)
        bank_gold = int(account["bank_gold"] or 0)

        # BOTH PILES COUNT. THE FIRST VERSION LOOKED ONLY AT THE CARRIED PILE,
        # which meant somebody with 9000 in the bank and 40 in their pocket was
        # told they could not afford a guild. Their gold is their gold; where
        # they are keeping it is not a rule anybody was told about.
        if carry_gold + bank_gold < GUILD_FOUND_COST:
            # AND IT SAYS THE NUMBERS. "It costs 5000 gold" on its own is the
            # one answer nobody can act on - it does not say how short they
            # are, and it does not say which pile was looked in, which is
            # exactly the question somebody staring at a full bank is asking.
            return bad_request(
                "founding a guild costs %d gold. You have %d carried and %d in"
                " the bank." % (GUILD_FOUND_COST, carry_gold, bank_gold))

        # DESTROYED, NOT MOVED. Through gold_delta() and bank_gold_delta() so
        # the burn lands in the ledger in the same transaction as the balance -
        # a bare UPDATE here would break the supply invariant on the first
        # guild anybody founded. Carry first, then bank (founding_split()).
        from_carry, from_bank = services.founding_split(carry_gold)
        detail = "founded %s" % name[:60]

        left = carry_gold
        bank_left = bank_gold
        try:
            if from_carry:
                left = gold_delta(db, g.user["id"], slot, -from_carry,
                                  "guild_found", detail)
            if from_bank:
                bank_left = bank_gold_delta(db, g.user["id"], -from_bank, "guild_found", detail)
        except insufficient_gold:
            # The balance moved between the read above and the write - somebody
            # spending in two windows at once. Nothing has been committed, so
            # saying so and stopping is the whole recovery.
            db.rollback()
            return bad_request(
                "your gold changed while that was going through - try again")

        now = int(time.time())
        guild_id = data.found(db, name, g.user["username"], now)
        data.add_member(db, g.user["id"], guild_id, "leader", now)
        log_guild_event(db, guild_id, "founded", g.user["username"], now=now)

        # ANY INVITATION THEY WERE HOLDING IS GONE. They are in a guild now, and
        # a pending invite to a different one is an offer that can no longer be
        # accepted - leaving it would show an Accept button that always refuses.
        data.drop_invites_to(db, g.user["id"])
        db.commit()

        # BOTH BALANCES AFTER THE PAYMENT, so the client can show them. Gold is
        # the server's, so the game cannot work these out and must not try; on
        # day 1 it did not copy them either, and a founder who paid 1,000
        # carried and 4,000 banked went on seeing the old purse and bank until a
        # relog. carried_gold and bank_gold are the names /api/bank/gold answers
        # with; "gold" stays for anything that read it before.
        return {
            "guild": guild_dict(db, data.by_id(db, guild_id), now),
            "rank": "leader",
            "gold": left,
            "carried_gold": left,
            "bank_gold": int(bank_left if bank_left is not None else bank_gold),
            "paid": GUILD_FOUND_COST,
            "from_carried": from_carry,
            "from_bank": from_bank,
        }, 200

    def guild_invite():
        """
        Invite somebody to your guild (officer or leader)
        ---
        tags:
          - Guilds
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
            description: Already invited, already in a guild, or the guild is full
          401:
            description: Missing, invalid or expired token
          404:
            description: No such account, or you are not an officer
        """
        payload = request.get_json(silent=True) or {}
        who = str(payload.get("username", "")).strip()
        if who == "":
            return bad_request("username is required")

        _mine, guild, rank = seat()
        if guild is None or not guild_rank_at_least(rank, "officer"):
            return {"error": "Not Found", "message": "Not found."}, 404

        target = find_user(who)
        if target is None:
            return {"error": "Not Found", "message": "No such account."}, 404
        if int(target["id"]) == int(g.user["id"]):
            return bad_request("you are already in it")

        db = get_db()
        guild_id = int(guild["id"])

        if data.membership(db, int(target["id"])) is not None:
            return bad_request("%s is already in a guild" % target["username"])

        if len(data.member_ids(db, guild_id)) >= MAX_GUILD_MEMBERS:
            return bad_request("a guild holds %d people" % MAX_GUILD_MEMBERS)

        if data.invites_waiting(db, guild_id) >= MAX_GUILD_INVITES:
            return bad_request(
                "there are already %d invitations waiting" % MAX_GUILD_INVITES)

        # ONE LINE PER INVITATION, not per press. Inviting somebody twice makes
        # no second invitation, and it must not make a second line either - or
        # the history of a guild becomes a record of somebody's impatience.
        if data.invite(db, guild_id, int(target["id"]), g.user["username"], int(time.time())):
            log_guild_event(db, guild_id, "invited", g.user["username"], target["username"])
        db.commit()

        return {"username": target["username"], "guild": guild["name"],
                "state": "invited"}, 200

    def guild_respond():
        """
        Accept or turn down an invitation
        ---
        tags:
          - Guilds
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
              required: [guild, accept]
              properties:
                guild:  {type: string}
                accept: {type: boolean}
        responses:
          200:
            description: Joined, or the invitation is gone
          400:
            description: You are already in a guild, or that one is full
          401:
            description: Missing, invalid or expired token
          404:
            description: No invitation from that guild
        """
        payload = request.get_json(silent=True) or {}
        wanted = str(payload.get("guild", "")).strip()
        accept = bool(payload.get("accept", False))
        if wanted == "":
            return bad_request("guild is required")

        db = get_db()
        guild = data.by_name(db, wanted)
        if guild is None:
            return {"error": "Not Found", "message": "No such guild."}, 404

        if data.invite_of(db, int(guild["id"]), g.user["id"]) is None:
            return {"error": "Not Found",
                    "message": "No invitation from that guild."}, 404

        if not accept:
            # TURNED DOWN SILENTLY. The guild is not told, for the same reason a
            # declined friend request is not - being refused is not news
            # somebody is owed, and telling them invites a second try.
            data.drop_invite(db, int(guild["id"]), g.user["id"])
            db.commit()
            return {"guild": guild["name"], "state": "declined"}, 200

        if data.membership(db, g.user["id"]) is not None:
            return bad_request("you are already in a guild")

        if len(data.member_ids(db, int(guild["id"]))) >= MAX_GUILD_MEMBERS:
            return bad_request("that guild is full")

        now = int(time.time())
        data.add_member(db, g.user["id"], int(guild["id"]), "member", now)
        log_guild_event(db, int(guild["id"]), "joined", g.user["username"], now=now)
        # EVERY OTHER INVITATION GOES TOO, not just this one. They are in a
        # guild now and none of the others can be accepted.
        data.drop_invites_to(db, g.user["id"])
        db.commit()

        return {"guild": guild_dict(db, guild, now), "rank": "member",
                "state": "joined"}, 200

    def guild_leave():
        """
        Leave your guild
        ---
        tags:
          - Guilds
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: You are out
          400:
            description: The leader must hand the guild on or disband it
          401:
            description: Missing, invalid or expired token
          404:
            description: You are not in a guild
        """
        _mine, guild, rank = seat()
        if guild is None:
            return {"error": "Not Found", "message": "You are not in a guild."}, 404

        db = get_db()
        if rank == "leader" and len(data.member_ids(db, int(guild["id"]))) > 1:
            # A GUILD WITHOUT A LEADER CANNOT PROMOTE ANYBODY, so walking out
            # and leaving one behind would strand every member in a guild nobody
            # can invite to, rank or disband. Hand it on, or disband it on the
            # way out.
            return bad_request(
                "make somebody else the leader first, or disband the guild")

        data.remove_member(db, g.user["id"])
        # Written before the fold below. If they were the last one out the guild
        # goes, and its history goes with it (ON DELETE CASCADE) - there is
        # nobody left to read it.
        log_guild_event(db, int(guild["id"]), "left", g.user["username"])

        # THE LAST ONE OUT TAKES THE GUILD WITH THEM. An empty guild is a name
        # nobody can use and nobody can reclaim.
        folded = len(data.member_ids(db, int(guild["id"]))) == 0
        if folded:
            data.drop_guild(db, int(guild["id"]))
        db.commit()

        return {"guild": guild["name"], "state": "left", "disbanded": folded}, 200

    def guild_kick():
        """
        Remove somebody from your guild (officer or leader)
        ---
        tags:
          - Guilds
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
          401:
            description: Missing, invalid or expired token
          404:
            description: Not in your guild, or not yours to remove
        """
        payload = request.get_json(silent=True) or {}
        who = str(payload.get("username", "")).strip()
        if who == "":
            return bad_request("username is required")

        _mine, guild, rank = seat()
        if guild is None or not guild_rank_at_least(rank, "officer"):
            return {"error": "Not Found", "message": "Not found."}, 404

        target = find_user(who)
        if target is None:
            return {"error": "Not Found", "message": "Not found."}, 404
        if int(target["id"]) == int(g.user["id"]):
            return bad_request("use leave to remove yourself")

        db = get_db()
        their_seat = data.membership(db, int(target["id"]))
        if their_seat is None or int(their_seat["guild_id"]) != int(guild["id"]):
            return {"error": "Not Found", "message": "Not found."}, 404

        # STRICTLY ABOVE, the same rule can_act_on() applies to staff. An
        # officer cannot remove another officer, and nobody can remove the
        # leader.
        if GUILD_RANKS.index(str(their_seat["rank"])) >= GUILD_RANKS.index(rank):
            return {"error": "Not Found", "message": "Not found."}, 404

        data.remove_member(db, int(target["id"]))
        log_guild_event(db, int(guild["id"]), "removed", g.user["username"], target["username"])
        db.commit()
        return {"username": target["username"], "state": "removed"}, 200

    def guild_set_rank():
        """
        Change somebody's guild rank (leader only)
        ---
        tags:
          - Guilds
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: change
            schema:
              type: object
              required: [username, rank]
              properties:
                username: {type: string}
                rank:     {type: string, example: "officer"}
        responses:
          200:
            description: Their new rank
          400:
            description: Unknown rank
          401:
            description: Missing, invalid or expired token
          404:
            description: Not in your guild, or you are not the leader
        """
        payload = request.get_json(silent=True) or {}
        who = str(payload.get("username", "")).strip()
        wanted = str(payload.get("rank", "")).strip().lower()
        if who == "":
            return bad_request("username is required")
        if wanted not in GUILD_RANKS:
            return bad_request("rank must be one of: %s" % ", ".join(GUILD_RANKS))

        _mine, guild, rank = seat()
        if guild is None or rank != "leader":
            return {"error": "Not Found", "message": "Not found."}, 404

        target = find_user(who)
        if target is None:
            return {"error": "Not Found", "message": "Not found."}, 404
        if int(target["id"]) == int(g.user["id"]):
            return bad_request("you are the leader")

        db = get_db()
        their_seat = data.membership(db, int(target["id"]))
        if their_seat is None or int(their_seat["guild_id"]) != int(guild["id"]):
            return {"error": "Not Found", "message": "Not found."}, 404

        now = int(time.time())
        if wanted == "leader":
            # HANDING IT ON, WHICH IS A SWAP, NOT A PROMOTION. Two leaders is a
            # guild where either can remove the other, so the old leader steps
            # down in the same transaction.
            data.set_rank(db, int(target["id"]), "leader")
            data.set_rank(db, g.user["id"], "officer")
            log_guild_event(db, int(guild["id"]), "leader", g.user["username"],
                            target["username"], now=now)
            db.commit()
            return {"username": target["username"], "rank": "leader",
                    "you": "officer",
                    "guild": guild_dict(db, guild, now)}, 200

        data.set_rank(db, int(target["id"]), wanted)
        # UP OR DOWN IS READ OFF THE LADDER, not off the word asked for, and a
        # "change" to the rank they already hold is no line at all.
        was = str(their_seat["rank"])
        if was != wanted:
            log_guild_event(
                db, int(guild["id"]),
                "promoted" if GUILD_RANKS.index(wanted) > GUILD_RANKS.index(was) else "demoted",
                g.user["username"], target["username"], detail=wanted, now=now)
        db.commit()
        return {"username": target["username"], "rank": wanted,
                "guild": guild_dict(db, guild, now)}, 200

    def guild_disband():
        """
        Take a guild down - your own as its leader, or any of them as the owner
        ---
        tags:
          - Guilds
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: which
            schema:
              type: object
              properties:
                name:
                  type: string
                  description: "Owner only. Omit to disband the guild you lead."
        responses:
          200:
            description: The guild is gone, and everybody in it is out
          401:
            description: Missing, invalid or expired token
          404:
            description: No such guild, or not yours to take down
        """
        payload = request.get_json(silent=True) or {}
        named = str(payload.get("name", "")).strip()
        db = get_db()

        if named != "":
            # NAMING A GUILD IS AN OWNER'S POWER. Anyone else naming one - even
            # their own - gets the same 404 a stranger does, so the route never
            # confirms which guilds exist to somebody who may not ask.
            if not has_owner_powers(g.user["username"]):
                return {"error": "Not Found", "message": "Not found."}, 404
            guild = data.by_name(db, named)
            if guild is None:
                return {"error": "Not Found", "message": "No such guild."}, 404
        else:
            _mine, guild, rank = seat()
            if guild is None or rank != "leader":
                return {"error": "Not Found", "message": "Not found."}, 404

        guild_id = int(guild["id"])
        emptied = len(data.member_ids(db, guild_id))

        # THE MEMBERS AND THE INVITATIONS GO WITH IT (data.take_down()).
        data.take_down(db, guild_id)

        # AN OWNER TAKING DOWN SOMEBODY ELSE'S GUILD IS A STAFF ACTION, and it
        # was not being recorded. A leader disbanding their own is not - that is
        # a player doing something to a thing that is theirs, and logging it
        # would fill the review log with ordinary play.
        if named != "":
            log_staff_action(g.user, "guild_disband", guild["name"],
                             detail="%d member(s) removed" % emptied)
        db.commit()

        return {"guild": guild["name"], "state": "disbanded",
                "members_removed": emptied}, 200

    def staff_guild():
        """
        Rename or disband a guild whose name is a problem
        ---
        tags:
          - Staff
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
              required: [guild, action, reason]
              properties:
                guild:  {type: string, example: "the first"}
                action: {type: string, example: rename}
                name:   {type: string, example: "The First"}
                reason: {type: string, example: "name was a slur"}
        responses:
          200:
            description: Renamed, or disbanded
          400:
            description: Unknown action, bad replacement name, or a missing reason
          404:
            description: No such guild
          409:
            description: A guild already holds the replacement name
        """
        # A NAME IS PUBLIC AND IT FOLLOWS PEOPLE AROUND. It is about to be drawn
        # above every member's head, on every line they write in world chat and
        # on the kingdom board, which is exactly why a guild is worth naming
        # well and exactly why an unacceptable one needs a lever that is not
        # "ban fifty players".
        #
        # WHY NOT A WORD FILTER. Because it loses. A list of forbidden strings
        # is an invitation to find the spelling that gets through, it fires on
        # innocent names that happen to contain one, and it has to be
        # maintained by somebody in every language the server is played in. The
        # mechanism that works is the one moderation already uses here: a human
        # reads it, acts, and the action is on the record.
        #
        # RENAME BEFORE DISBAND. Disbanding takes a guild away from fifty people
        # because one of them chose a word; renaming takes away the word. The
        # smaller response exists so the harshest is not the only one - the
        # same argument that put a kick next to the ban (E-12).
        payload = request.get_json(silent=True) or {}
        wanted = str(payload.get("guild", "")).strip()
        action = str(payload.get("action", "")).strip().lower()
        reason = str(payload.get("reason", "")).strip()

        if wanted == "":
            return bad_request("guild is required")
        if action not in ("rename", "disband"):
            return bad_request("action must be 'rename' or 'disband'")
        # THE SAME RULE A BAN KEEPS, and for the same sentence: an action nobody
        # can review later is one the person who took it cannot defend either.
        if not reason or len(reason) > 500:
            return bad_request("reason must be 1-500 characters")

        db = get_db()
        guild = data.by_name(db, wanted)
        if guild is None:
            return {"error": "Not Found", "message": "No such guild."}, 404

        guild_id = int(guild["id"])
        old_name = guild["name"]

        if action == "rename":
            replacement = str(payload.get("name", "")).strip()
            if not re.fullmatch(GUILD_NAME_PATTERN, replacement):
                return bad_request(
                    "a guild name is 3 to %d characters: letters, numbers, spaces,"
                    " apostrophes and hyphens" % MAX_GUILD_NAME)
            # CASE-INSENSITIVELY UNIQUE, the same test creation uses, so staff
            # cannot make two guilds that look identical in a chat line.
            clash = data.by_name(db, replacement)
            if clash is not None and int(clash["id"]) != guild_id:
                return {"error": "Conflict",
                        "message": "a guild by that name already exists"}, 409

            # Both columns, in one statement - see data.rename().
            data.rename(db, guild_id, replacement)
            # CHAT LINES ARE LEFT ALONE. `chat_messages.guild` is a snapshot of
            # what somebody was flying when they spoke; rewriting history to
            # hide the old name would also rewrite the evidence of why it was
            # renamed. The window is minutes long and prunes itself.
            log_staff_action(g.user, "guild_rename", old_name,
                             detail="-> %s: %s" % (replacement, reason))
            # IN THE GUILD'S OWN HISTORY TOO, and WITHOUT the name of the member
            # of staff who did it. Members need to know their guild was renamed
            # and what it was called; they do not need somebody to be angry at.
            # The moderation log above keeps who, for the people who review it.
            log_guild_event(db, guild_id, "renamed", "", replacement, detail=old_name)
            db.commit()
            return {"guild": replacement, "was": old_name, "state": "renamed"}, 200

        emptied = len(data.member_ids(db, guild_id))
        data.take_down(db, guild_id)
        log_staff_action(g.user, "guild_disband", old_name,
                         detail="%d member(s) removed: %s" % (emptied, reason))
        db.commit()
        return {"guild": old_name, "state": "disbanded",
                "members_removed": emptied}, 200

    def guild_list():
        """
        Every guild on the server, for staff
        ---
        tags:
          - Guilds
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Each guild with its size and who founded it
          401:
            description: Missing, invalid or expired token
          404:
            description: Not staff
        """
        # STAFF CAN SEE THEM; ONLY THE OWNER CAN TAKE ONE DOWN. Two different
        # questions, and a mod being able to find a guild by name is what makes
        # reporting one to the owner possible.
        db = get_db()
        now = int(time.time())
        out = []
        for row in data.every_guild(db):
            leader = data.leader_of(db, row["id"])
            out.append({
                "name": row["name"],
                "founded_by": row["founded_by"],
                "created_at": int(row["created_at"] or 0),
                "size": int(row["size"] or 0),
                "leader": leader["username"] if leader is not None else "",
            })
        return {"guilds": out, "now": now,
                "may_disband": has_owner_powers(g.user["username"])}, 200

    app.add_url_rule("/api/guild", endpoint="guild_read",
                     view_func=require_auth(guild_read), methods=["GET"])
    app.add_url_rule("/api/guild/create", endpoint="guild_create",
                     view_func=require_auth(guild_create), methods=["POST"])
    app.add_url_rule("/api/guild/invite", endpoint="guild_invite",
                     view_func=require_auth(guild_invite), methods=["POST"])
    app.add_url_rule("/api/guild/respond", endpoint="guild_respond",
                     view_func=require_auth(guild_respond), methods=["POST"])
    app.add_url_rule("/api/guild/leave", endpoint="guild_leave",
                     view_func=require_auth(guild_leave), methods=["POST"])
    app.add_url_rule("/api/guild/kick", endpoint="guild_kick",
                     view_func=require_auth(guild_kick), methods=["POST"])
    app.add_url_rule("/api/guild/rank", endpoint="guild_set_rank",
                     view_func=require_auth(guild_set_rank), methods=["POST"])
    app.add_url_rule("/api/guild/disband", endpoint="guild_disband",
                     view_func=require_auth(guild_disband), methods=["POST"])
    # The two staff routes: require_auth outside require_role, as their
    # decorators were stacked in app.py.
    app.add_url_rule("/api/staff/guild", endpoint="staff_guild",
                     view_func=require_auth(require_role("mod")(staff_guild)), methods=["POST"])
    app.add_url_rule("/api/guild/list", endpoint="guild_list",
                     view_func=require_auth(require_role("mod")(guild_list)), methods=["GET"])
