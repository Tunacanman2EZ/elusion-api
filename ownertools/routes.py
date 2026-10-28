"""ownertools/routes.py - the edge: the owner's nine tools, as they were in
app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth, require_owner, require_role, require_fresh_password
                         the token check, the owner's 404, a rank's 404, the
                         password asked again
    get_db               the request's database connection
    bad_request          a message -> app.py's 400
    parse_slot, max_slot a raw slot -> its int or None, and the highest one
    slot_exists          (user_id, slot) -> whether that character exists
    catalogue            gamedata: the XP curves, what a roll is, rolling and
                         perfecting a piece - asked on every call
    ensure_account       user_id -> the account row, made if missing (a
                         lookup: test_concurrency holds it open)
    gold_delta           (db, user_id, slot, delta, reason, detail): the purse,
                         through the gold ledger; raises insufficient_gold
    bank_gold_delta      (db, user_id, delta, reason, detail): the bank, the same
    insufficient_gold    the exception either raises below zero
    record_gift          (db, actor, receiver_id, receiver_name, slot, item_id,
                         quantity, source) -> its worth: an item into
                         owner_gifts; no commit
    record_gold_gift     (db, actor, receiver_id, receiver_name, slot, amount):
                         gold the same way; no commit
    find_user            a name -> the users row, or None
    owner_may_touch      (actor, target) -> whether a co-owner may act on them
    owner_untouchable    the 403 a co-owner gets for naming the owner
    playing_slot         (db, user_id) -> the character they are playing
    playing_area         (db, user_id) -> where that character is
    take_snapshot        (db, user_id, slot, reason) -> the snapshot's id; no
                         commit
    character_state      (db, user_id, slot) -> what a snapshot holds of it now
    stored_json          (raw, fallback) -> a JSON column, read safely
    derived_stats        (row, level=) -> the maxima a level and the gear give
    levelup_grant_id     the id a level's refill is noted under
    note_refill          (db, user_id, slot, item_id, at) - that refill, written
                         where the healing check reads it (status/'s); no commit
    log_staff_action     the moderation log's writer
    valid_skills         every skill the game has
    max_skill_level      the highest level any grant sets
    stack_limit          item_id -> how many sit in one cell
    add_to_backpack      (user_id, slot, item_id, quantity) -> the cells
                         written, or None having written nothing
    bag_cells            the backpack's size, for "full"
    inventory_of         (user_id, slot) -> the carry, cell by cell
    mark_gift            (db, user_id, slot, by, item_id, quantity): flag the
                         bag so their game reloads it and says who gave what
    online_window        ONLINE_WINDOW_SECONDS
    gifts_since_key      the setting that says when the gifts ledger began
    snapshots_kept, snapshot_every
                         how many snapshots a character keeps, and how often a
                         save takes one
    find_open_trade      (db, user_id) -> their open trade, or None (trade/'s)
    carry_cells          every carried cell, the hotbar's after the bag's
    owns_item            (user_id, slot, item_id) -> whether the account holds it
    has_owner_powers     a name -> whether it is the owner or a co-owner
    queue_teleport       (db, user_id, area, x, y, index, total, by, now): one
                         teleport queued for the poll to hand out
    teleport_spacing     how far apart "everyone" are set down
    post_broadcast       (body, kind, by): the server's voice; no commit
    moderation_target    (payload) -> (the account, None) or (None, the 404),
                         within the caller's reach

THE ENDPOINT NAMES ARE app.py's - staff_gold, staff_level, staff_skill,
staff_grant, staff_gifts, staff_snapshots, staff_rollback, staff_teleport,
acknowledge_teleport - and routes.lock holds them, with each one's rank and
its asking for the password again.
"""
import json
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, require_owner, require_role, require_fresh_password, get_db, bad_request,
             parse_slot, max_slot, slot_exists, catalogue, ensure_account, gold_delta, bank_gold_delta,
             insufficient_gold, record_gift, record_gold_gift, find_user, owner_may_touch, owner_untouchable,
             playing_slot, playing_area, take_snapshot, character_state, stored_json, derived_stats,
             levelup_grant_id, log_staff_action, valid_skills, max_skill_level, stack_limit, add_to_backpack,
             bag_cells, inventory_of, mark_gift, online_window, gifts_since_key, snapshots_kept, snapshot_every,
             find_open_trade, carry_cells, owns_item, has_owner_powers, queue_teleport, teleport_spacing,
             post_broadcast, moderation_target, note_refill):

    def staff_gold():
        """
        Give yourself gold for testing (owner only)
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
            name: grant
            schema:
              type: object
              required: [slot, amount]
              properties:
                slot:   {type: integer, example: 0}
                amount: {type: integer, example: 5000}
                bank:   {type: boolean, description: "Put it in the bank instead"}
        responses:
          200:
            description: The new balances
          400:
            description: Bad slot, bad amount, or it would take you below zero
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner, or no character in that slot
        """
        # WHY THIS EXISTS AT ALL, stated plainly: a server nobody can put gold on
        # is a server whose shops, trades, bank, revive cost and guild founding
        # cannot be tested by the person who wrote them. The game's debug keys did
        # this job, but they were gated on OS.is_debug_build() and so disappeared
        # the moment the game was exported - which is exactly when it most needs
        # exercising. (Since game 0.7.1 there are no keys at all; the GM panel does
        # their work.)
        #
        # OWNER ONLY, NOT MOD. Granting an item is a test fixture; minting the
        # currency is the economy itself, and the Kingdom Tax board is built on
        # the assumption that gold is scarce. One person can be trusted with that
        # because it is their server.
        #
        # THROUGH THE LEDGER, LIKE EVERYTHING ELSE. A bare UPDATE would mint gold
        # the supply invariant cannot see, and /api/economy/supply would start
        # reporting drift that is not drift. The row says 'staff_gold', so every
        # coin conjured for a test is visible as exactly that forever.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        if not slot_exists(user_id, slot):
            return {"error": "Not Found",
                    "message": "No character in slot %d." % slot}, 404

        raw = payload.get("amount", 0)
        if isinstance(raw, bool):
            return bad_request("amount must be a whole number")
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            return bad_request("amount must be a whole number")
        if amount == 0:
            return bad_request("amount must not be zero")
        if abs(amount) > services.DEBUG_GOLD_MAX:
            return bad_request("one grant is at most %d gold" % services.DEBUG_GOLD_MAX)

        db = get_db()
        to_bank = bool(payload.get("bank", False))
        detail = "owner debug grant"

        # THE ACCOUNTS ROW HAS TO EXIST FIRST, and this line is here because the
        # first version did not have it. accounts rows are created lazily, on the
        # first time somebody banks anything - so an owner who has never used the
        # bank has no row, bank_gold_delta's UPDATE matched nothing, and it
        # returned None rather than raising. The grant reported success, wrote no
        # ledger row, and moved no gold. Silent, and the supply invariant stayed
        # balanced throughout because nothing had happened at all.
        ensure_account(user_id)

        try:
            if to_bank:
                if bank_gold_delta(db, user_id, amount, "staff_gold", detail) is None:
                    db.rollback()
                    return bad_request("that bank grant did not go through")
            else:
                if gold_delta(db, user_id, slot, amount, "staff_gold", detail) is None:
                    return {"error": "Not Found",
                            "message": "No character in slot %d." % slot}, 404
        except insufficient_gold:
            db.rollback()
            return bad_request("that would take the balance below zero")

        # THE GIFTS LEDGER: the owner's own gold is a gift too (one taken back is a
        # negative one). Same transaction as the gold.
        record_gold_gift(db, g.user, user_id, g.user["username"], None if to_bank else slot, amount)
        db.commit()

        account = ensure_account(user_id)
        purse = data.purse(db, user_id, slot)
        return {
            "slot": slot,
            "granted": amount,
            "where": "bank" if to_bank else "carried",
            "gold": int(purse["gold"] or 0) if purse is not None else 0,
            "bank_gold": int(account["bank_gold"] or 0),
        }, 200


    def staff_level():
        """
        Set a character's level - yours, or the one a player is playing (owner only)
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
            schema:
              type: object
              required: [level]
              properties:
                slot:     {type: integer, example: 0, description: "Your own character (no username)"}
                username: {type: string, description: "Somebody else: the character they are playing"}
                level:    {type: integer, example: 22}
        responses:
          200:
            description: The character's new level, XP and maxima
          400:
            description: Bad slot or level
          401:
            description: Missing, invalid or expired token
          403:
            description: A co-owner naming the owner
          404:
            description: Not the owner, no such account, or no character
        """
        # WHY THIS EXISTS. Day 2: tier 6 weapons that need level 22, and eight
        # hours of play between a new character and level 22. The person who has
        # to test them cannot, and the gold grant above is the precedent: the owner
        # may set up his own server's test fixtures.
        #
        # OWNER ONLY. It was self only, like the gold grant, until 10 Oct (0.21.0):
        # the owner, testing with first-time players who "want to try the new
        # weapons", "i also need the ability to set a players level so they can try
        # out the game". A `username` that is not your own is THE CHARACTER THEY ARE
        # PLAYING, exactly as a give finds it (_playing_slot()), never a slot from
        # the request; a snapshot first ("before-level"), so the save history can
        # put it back; a "level" line about THEM; and their sessions ended, as a
        # rollback ends them, because their game holds its own copy of the level
        # and nothing short of a reload replaces it. A co-owner may name anyone but
        # the owner (owner_may_touch()). Your own name, or none, is your own slot.
        #
        # A LEVEL SET IS A LEVEL-UP OR A LEVEL-DOWN, done the way the kill route
        # does one: XP to zero and the curve's xp_to_next for the new level, the
        # maxima derived from the level and what is worn, and the pools full. The
        # refill is recorded as a level-up grant, so the healing check does not
        # read the client's full bars as a cheat. Logged as "level", in the same
        # transaction, like every staff action.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]
        db = get_db()

        raw = payload.get("level")
        if isinstance(raw, bool):
            return bad_request("level must be a whole number")
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return bad_request("level must be a whole number")
        if level < 1 or level > services.STAFF_LEVEL_MAX:
            return bad_request("level must be 1-%d" % services.STAFF_LEVEL_MAX)

        target = None
        wanted = str(payload.get("username", "") or "").strip()
        if wanted and wanted.casefold() != str(g.user["username"]).casefold():
            target = find_user(wanted)
            if target is None:
                return {"error": "Not Found", "message": "No such account."}, 404
            if not owner_may_touch(g.user, target):
                return owner_untouchable
            user_id = int(target["id"])
            slot = playing_slot(db, user_id)
            if slot is None:
                return {"error": "Not Found",
                        "message": "%s has no character." % target["username"]}, 404
        else:
            slot = parse_slot(payload.get("slot"))
            if slot is None:
                return bad_request("slot must be an integer 0-%d" % max_slot)

        row = data.character(db, user_id, slot)
        if row is None:
            return {"error": "Not Found",
                    "message": "No character in slot %d." % slot}, 404

        if target is not None:
            take_snapshot(db, user_id, slot, "before-level")
        was = int(row["level"] or 1)
        derived = derived_stats(row, level=level)
        if derived is None:
            return bad_request("that character's class has no level curve")
        xp_to_next = int(catalogue.xp_needed_for_level(level))

        data.set_level(db, user_id, slot, level, xp_to_next, derived, int(time.time()))
        note_refill(db, user_id, slot, levelup_grant_id, int(time.time()))
        if target is not None:
            # THEIR GAME RELOADS, as after a rollback: counted first, like a kick.
            ended = data.end_sessions(db, user_id, int(time.time()))
            name = row["name"] or row["class_id"]
            # "player_level", not "level": done to a player, so it is on their
            # record with the gives and rollbacks (STAFF_ACTION_GROUPS).
            log_staff_action(
                g.user, "player_level", target["username"], user_id,
                "%s (slot %d): level %d -> %d; %d session(s) ended" % (name, slot, was, level, ended),
            )
            db.commit()
            return {
                "username": target["username"],
                "character": name,
                "slot": slot,
                "level": level,
                "was": was,
                "xp_to_next": xp_to_next,
                "max_hp": derived["max_hp"],
                "sessions_ended": ended,
            }, 200

        log_staff_action(
            g.user, "level", g.user["username"], g.user["id"],
            "slot %d: level %d -> %d" % (slot, was, level),
        )
        db.commit()

        return {
            "slot": slot,
            "level": level,
            "was": was,
            "xp": 0,
            "xp_to_next": xp_to_next,
            "max_hp": derived["max_hp"],
            "max_mana": derived["max_mana"],
            "max_stamina": derived["max_stamina"],
            # What is stored now, so the client copies the server's pools rather
            # than refilling its own.
            "hp": derived["max_hp"],
            "mana": derived["max_mana"],
            "stamina": derived["max_stamina"],
        }, 200


    def staff_skill():
        """
        Set your own character's skill levels, for testing (owner only)
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
            schema:
              type: object
              required: [slot, skill, level]
              properties:
                slot:  {type: integer, example: 0}
                skill: {type: string, example: "attack", description: 'One skill, or "all"'}
                level: {type: integer, example: 50}
        responses:
          200:
            description: Each skill set, with its new level, XP and next threshold
          400:
            description: Bad slot, skill or level
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner, or no character in that slot
        """
        # WHY THIS EXISTS. The owner, 6 Oct: "i want full control of my stats in
        # admin panel so i can do more testing". Skills train on hours of play -
        # attack at the kill, the rest through /api/skill/train at a capped rate -
        # so gear and recipes that need a skill level could not be tested without
        # those hours. The level tool above is the precedent.
        #
        # OWNER ONLY, SELF ONLY, like the gold grant and the level tool: a slot, a
        # skill and a level, applied to THE CALLER's character; a username in the
        # payload is ignored.
        #
        # A SET, NOT A GRANT. The skill lands on the level asked for with no XP
        # into it, up or down, the way the level tool sets a level. MAX_SKILL_LEVEL
        # is the ceiling every server grant already observes. Logged as "skill", in
        # the same transaction.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        skill = str(payload.get("skill", "")).strip().lower()
        if skill == "all":
            chosen = list(services.STAFF_SKILL_ORDER)
        elif skill in valid_skills:
            chosen = [skill]
        else:
            return bad_request("skill must be one of %s, or all"
                               % ", ".join(services.STAFF_SKILL_ORDER))

        raw = payload.get("level")
        if isinstance(raw, bool):
            return bad_request("level must be a whole number")
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return bad_request("level must be a whole number")
        if level < 1 or level > max_skill_level:
            return bad_request("level must be 1-%d" % max_skill_level)

        db = get_db()
        if data.character(db, user_id, slot) is None:
            return {"error": "Not Found",
                    "message": "No character in slot %d." % slot}, 404

        before = data.skill_levels(db, user_id, slot)
        answer = {}
        changes = []
        for skill_id in chosen:
            was = before.get(skill_id, 1)
            data.set_skill(db, user_id, slot, skill_id, level)
            answer[skill_id] = {
                "level": level,
                "xp": 0,
                "xp_next": int(catalogue.xp_needed_for_skill_level(skill_id, level)),
                "was": was,
            }
            changes.append("%s %d -> %d" % (skill_id, was, level))

        log_staff_action(
            g.user, "skill", g.user["username"], g.user["id"],
            "slot %d: %s" % (slot, ", ".join(changes)),
        )
        db.commit()

        return {"slot": slot, "level": level, "skills": answer}, 200


    def staff_grant():
        """
        Give an item to yourself or to a player (owner only)
        ---
        tags:
          - Staff
        consumes:
          - application/json
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
              required: [item_id]
              properties:
                slot: {type: integer, example: 0, description: "Your own character's slot. Needed unless username names somebody else."}
                username: {type: string, example: someplayer, description: "Give it to this account instead, into the character it is playing. Your own name, or none, is yourself."}
                item_id: {type: string, example: "petsniper"}
                quantity: {type: integer, example: 1}
                quality:
                  type: string
                  enum: [plain, roll, perfect]
                  description: "plain (the default) is the catalogue piece at 100%; roll rolls it like a drop or a purchase; perfect gives the Perfect roll. store is read as plain."
        responses:
          200:
            description: "Your own: the backpack as stored, after the grant. Somebody else's: who, which character, what arrived and where."
          400:
            description: Bad slot, item id or quantity
          404:
            description: That slot is empty, no such account or it has no character, or you are not the owner
          409:
            description: The backpack it was going into is full
          401:
            description: Missing, invalid or expired token
        """
        # THE DEBUG KEYS, MOVED TO WHERE THEY CAN BE ENFORCED.
        #
        # F1-F7 and the P O I U Y T pet row used to add items to the client's own
        # inventory, which then pushed the whole bag here on the next save. The rank
        # check lived in the client - so a patched build that set Api.role to
        # "owner" got the keys back, and could have skipped them entirely and just
        # written the item into the array it was going to send anyway.
        #
        # Now the client asks and the SERVER decides. require_owner above is the real
        # gate. Since game 0.7.1 the keys are gone altogether, and the GM panel's
        # Give item and item catalogue are what call this.
        #
        # OWNER ONLY, NOT MOD AND UP (6 Oct 2026). It was require_role("mod"), and a
        # mod could give themselves anything - a Perfect mythic, a stack of potions
        # to sell - and trade it on. A rank is trust with PLAYERS; nothing about
        # judging a chat report needs the power to create items, and "Item spawning
        # is gated on the SERVER, not on a rank" in CLAUDE.md said where this was
        # going. The two whole-write doors that leaned on this one being open (the
        # bag in write_inventory() and the bank in write_bank()) closed with it, so
        # the owner is now the only account that can make an item from nothing.
        #
        # TO SOMEBODY ELSE, SINCE 6 OCT 2026 (the owner: "give player item might be
        # useful"). It said "SELF ONLY ... there should not be one until there is a
        # reason": granting to someone else is a different action with different
        # consequences. The reason came, and the consequences are what is below:
        #
        # - THE CHARACTER THEY ARE PLAYING (PLAYING_SLOT_SQL, the trade offer's
        #   rule), never a slot the owner names - the panel knows a name, not
        #   which of four characters is at the keyboard. Offline, their last save.
        # - THEIR GAME IS TOLD. The bag is flagged (_mark_gift(), the trade flag's
        #   column) and the broadcast poll hands the game the bag the server holds
        #   and who gave what - the way a trade that finished without them watching
        #   reaches them. Offline, they load the bag at login and read the line.
        # - A SNAPSHOT FIRST ("before-give"), so the gift can be taken back with
        #   the save history like any other change.
        # - ITS OWN LINE IN THE LOG, "give", about THEM - on their record, where a
        #   mod looking at them will see it, not filed under the owner's testing.
        #
        # Still owner only, and still no can_act_on(): the owner outranks everyone,
        # and nobody else reaches this line.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        target = None
        wanted = str(payload.get("username", "") or "").strip()
        if wanted and wanted.casefold() != str(g.user["username"]).casefold():
            target = find_user(wanted)
            if target is None:
                return {"error": "Not Found", "message": "No such account."}, 404
            if not owner_may_touch(g.user, target):
                return owner_untouchable
            slot = playing_slot(get_db(), int(target["id"]))
            if slot is None:
                return {"error": "Not Found",
                        "message": "%s has no character to give it to." % target["username"]}, 404
        else:
            slot = parse_slot(payload.get("slot"))
            if slot is None:
                return bad_request("slot must be an integer 0-%d" % max_slot)
            if not slot_exists(user_id, slot):
                return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        item_id = str(payload.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return bad_request("item_id must be 1-64 characters")
        # AN ID IS STILL NOT WHITELISTED, BUT A ROLL IS CHECKED. A grant of an
        # unknown id stores it as typed (see "Item ids are not whitelisted" in
        # CLAUDE.md); one that LOOKS like a roll and is not one - a stat the piece
        # does not have, 300% - would be a piece every other route reads as nothing.
        if catalogue.VARIANT_MARK in item_id and not catalogue.has_item(item_id):
            return bad_request("'%s' is not a roll of any item" % item_id)

        raw_quantity = payload.get("quantity", 1)
        if isinstance(raw_quantity, bool):
            return bad_request("quantity must be a positive integer")
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            return bad_request("quantity must be a positive integer")
        if quantity <= 0:
            return bad_request("quantity must be a positive integer")

        # WHAT THE PIECE ROLLS. "plain" is the catalogue at 100%, the only way to
        # have one now that the shop rolls what it sells; "roll" is what a kill or
        # a purchase would hand over; "perfect" is the 1 in QUALITY_PERFECT_ODDS,
        # so the owner can see one without granting a hundred - armour with its
        # resistance at the top of its range, as a Perfect drop. "store" is read as
        # "plain": it was the name for the 100% piece while the shop sold one, and
        # the build-3 game that is out sends it. Only a plain id is rolled: an id
        # that already names a roll is granted as named. Anything with no stats to
        # roll is granted as it is.
        quality = str(payload.get("quality", "plain") or "plain").strip().lower()
        if quality == "store":
            quality = "plain"
        if quality not in ("plain", "roll", "perfect"):
            return bad_request("quality must be plain, roll or perfect")
        if quality != "plain" and catalogue.VARIANT_MARK not in item_id:
            item_id = (catalogue.roll_quality(item_id) if quality == "roll"
                       else catalogue.perfect_id(item_id, resist=True))

        # The same ceiling an ordinary write gets. Staff is not a reason to be
        # allowed to create a cell holding a billion potions - that is a corrupt
        # row, not a privilege, and it would be this endpoint's fault.
        limit = stack_limit(item_id)
        if quantity > limit:
            return bad_request(
                "quantity is %d, the most %s stacks to is %d" % (quantity, item_id, limit)
            )

        db = get_db()
        if target is not None:
            return give_to_player(db, target, slot, item_id, quantity)

        written = add_to_backpack(user_id, slot, item_id, quantity)
        if written is None:
            return {
                "error": "Conflict",
                "message": "Your backpack is full (%d slots)." % bag_cells,
            }, 409

        # IN THE SAME TRANSACTION AS THE GRANT. An item that appears with no line in
        # the log is exactly what this endpoint exists to prevent, and "log it
        # afterwards" is how that happens the first time something raises in
        # between.
        log_staff_action(
            g.user, "grant", g.user["username"], g.user["id"],
            "%d x %s into slot %d" % (quantity, item_id, slot),
        )
        worth = record_gift(db, g.user, user_id, g.user["username"], slot, item_id, quantity, "grant")
        db.commit()

        return {
            "slot": slot,
            "granted_item_id": item_id,
            "granted_quantity": quantity,
            "carry_positions": written,
            "inventory": inventory_of(user_id, slot),
            # What it is worth (THE GIFTS LEDGER), so the panel can say it.
            "worth": worth,
        }, 200


    def give_to_player(db, target, slot, item_id, quantity):
        """
        The half of POST /api/staff/grant that puts an item in somebody else's bag:
        `target` is their users row, `slot` the character they are playing. A
        snapshot first, then the item, the flag their game reads, and the log line,
        in one transaction. See TO SOMEBODY ELSE in staff_grant().
        """
        receiver = int(target["id"])
        take_snapshot(db, receiver, slot, "before-give")
        written = add_to_backpack(receiver, slot, item_id, quantity)
        if written is None:
            db.rollback()
            return {
                "error": "Conflict",
                "message": "%s's backpack is full (%d slots)." % (target["username"], bag_cells),
            }, 409

        mark_gift(db, receiver, slot, g.user["username"], item_id, quantity)
        character = data.character(db, receiver, slot)
        name = (character["name"] or character["class_id"]) if character is not None else "?"
        log_staff_action(
            g.user, "give", target["username"], receiver,
            "%d x %s into %s (slot %d)" % (quantity, item_id, name, slot),
        )
        worth = record_gift(db, g.user, receiver, target["username"], slot, item_id, quantity, "give")
        db.commit()

        now = int(time.time())
        online = data.heard_lately(db, receiver, now - online_window, now)
        return {
            "username": target["username"],
            "slot": slot,
            "character": name,
            "granted_item_id": item_id,
            "granted_quantity": quantity,
            "carry_positions": written,
            # So the panel can say WHEN they will see it: within a poll, or at
            # their next login.
            "online": online,
            # And what it is worth (THE GIFTS LEDGER): 999 Piles of Gold is
            # 24,975,000 gold, which nobody should find out from the ledger.
            "worth": worth,
        }, 200


    def staff_gifts():
        """
        What the owner and the co-owners have given away, and what it comes to
        ---
        tags:
          - Staff
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: query
            name: username
            type: string
            description: Only what this account was given
          - in: query
            name: recent
            type: integer
            description: How many of the newest gifts to list (1-100, default 20)
        responses:
          200:
            description: Totals, to players and to yourself, by player, by item, the newest, and the economy beside them
          404:
            description: Not the owner, or no such account
        """
        db = get_db()
        user_id = None
        wanted = str(request.args.get("username", "") or "").strip()
        if wanted:
            target = find_user(wanted)
            if target is None:
                return {"error": "Not Found", "message": "No such account."}, 404
            user_id = int(target["id"])
        try:
            recent = min(100, max(1, int(request.args.get("recent", 20))))
        except (TypeError, ValueError):
            recent = 20
        report = data.gift_report(db, gifts_since_key, user_id, recent)
        report["username"] = wanted or None
        return report, 200


    def rollback_target(username):
        """The account named, or (None, 404). The owner may name anyone, themselves
        included; a co-owner anyone but the owner (owner_may_touch()); a name that
        is not an account is the same 404 as any refusal."""
        username = str(username or "").strip()
        if not username:
            return None, bad_request("username is required")
        target = find_user(username)
        if target is None:
            return None, ({"error": "Not Found", "message": "No such account."}, 404)
        if not owner_may_touch(g.user, target):
            return None, owner_untouchable
        return target, None


    def staff_snapshots():
        """
        Read a player's save history (owner only)
        ---
        tags:
          - Staff
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: query
            name: username
            type: string
            required: true
          - in: query
            name: slot
            type: integer
            description: "Which character. Left out, the one they are playing."
        responses:
          200:
            description: Their characters, the chosen one as it is now, and its snapshots newest first
          400:
            description: No username, or a bad slot
          404:
            description: No such account, no character there, or you are not the owner
          401:
            description: Missing, invalid or expired token
        """
        # A READ, SO NOT LOGGED. Looking is not an action on the player; the
        # rollback it may lead to is, and that is logged with what it changed.
        target, refusal = rollback_target(request.args.get("username"))
        if refusal is not None:
            return refusal
        db = get_db()
        user_id = int(target["id"])

        characters = data.characters_of(db, user_id)
        if not characters:
            return {"error": "Not Found",
                    "message": "%s has no characters." % target["username"]}, 404

        raw_slot = request.args.get("slot")
        if raw_slot is None or str(raw_slot).strip() == "":
            slot = playing_slot(db, user_id)
        else:
            slot = parse_slot(raw_slot)
            if slot is None:
                return bad_request("slot must be an integer 0-%d" % max_slot)
        now = character_state(db, user_id, slot) if slot is not None else None
        if now is None:
            return {"error": "Not Found", "message": "No character in slot %s." % raw_slot}, 404

        snapshots = [services.snapshot_row(r) for r in data.snapshots_of(db, user_id, slot)]
        return {
            "username": target["username"],
            "slot": slot,
            "playing": playing_slot(db, user_id),
            "characters": characters,
            "now": {"name": now["name"], "class_id": now["class_id"], "level": now["level"],
                    "gold": now["gold"], "items": len(now["carry"])},
            "snapshots": snapshots,
            "kept": snapshots_kept,
            "every_seconds": snapshot_every,
        }, 200


    def staff_rollback():
        """
        Put a player's character back to a snapshot (owner only)
        ---
        tags:
          - Staff
        consumes:
          - application/json
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
              required: [username, snapshot_id]
              properties:
                username:    {type: string, example: someplayer}
                snapshot_id: {type: integer, example: 42, description: "From GET /api/staff/snapshots"}
        responses:
          200:
            description: "Restored: what it was and is now, the snapshot that undoes it, and how many sessions ended"
          400:
            description: No username, or a snapshot_id that is not a whole number
          404:
            description: No such account, no such snapshot of theirs, the character is gone, or you are not the owner
          409:
            description: The character is in an open trade, or its slot now holds another class
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        target, refusal = rollback_target(payload.get("username"))
        if refusal is not None:
            return refusal
        raw_id = payload.get("snapshot_id")
        if isinstance(raw_id, bool):
            return bad_request("snapshot_id must be a whole number")
        try:
            snapshot_id = int(raw_id)
        except (TypeError, ValueError):
            return bad_request("snapshot_id must be a whole number")
        if isinstance(raw_id, float) and raw_id != snapshot_id:
            return bad_request("snapshot_id must be a whole number")

        db = get_db()
        user_id = int(target["id"])
        # BY ID AND ACCOUNT TOGETHER: the name in the request and the snapshot have
        # to agree, so a stale panel holding somebody else's id cannot roll the
        # wrong player back.
        snap = data.snapshot(db, snapshot_id, user_id)
        if snap is None:
            return {"error": "Not Found", "message": "No such snapshot of %s." % target["username"]}, 404
        slot = int(snap["slot"])
        state = stored_json(snap["snapshot"], {})
        row = data.character(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "That character no longer exists."}, 404
        # A SLOT IS A CLASS (one per class), so this only fails if the character
        # was deleted and made again since - and the delete takes the snapshots
        # with it. Checked anyway: a warrior's bag and skills on a mage is not a
        # state anything in this file expects.
        if state.get("class_id") != row["class_id"]:
            return {"error": "Conflict",
                    "message": "That snapshot is of a %s; the slot holds a %s now." % (
                        state.get("class_id", "?"), row["class_id"])}, 409
        # NOT IN THE MIDDLE OF A TRADE - the delete route's rule, for its reason:
        # an open trade names items in this bag, and a rollback changes the bag
        # under it. It ends on its own at TRADE_EXPIRY_SECONDS.
        trade = find_open_trade(db, user_id)
        if trade is not None and (
                (trade["a_user"] == user_id and trade["a_slot"] == slot)
                or (trade["b_user"] == user_id and trade["b_slot"] == slot)):
            return {"error": "Conflict",
                    "message": "%s is in a trade. Try again when it has finished or timed out." % (
                        target["username"])}, 409

        # ---- everything above this line is validation; everything below commits --

        was = character_state(db, user_id, slot)
        undo = take_snapshot(db, user_id, slot, "before-rollback")
        now = int(time.time())

        data.restore_cells_and_skills(db, user_id, slot,
                                      services.carried_rows(state, carry_cells),
                                      services.skill_rows(state))

        # THE PURSE THROUGH THE LEDGER, by the difference, never written outright.
        gold_now = int(row["gold"] or 0)
        gold_then = max(0, int(state.get("gold", 0)))
        if gold_then != gold_now:
            gold_delta(db, user_id, slot, gold_then - gold_now, services.ROLLBACK_REASON,
                       "rollback to snapshot #%d by %s" % (snapshot_id, g.user["username"]))

        # THE PET ONLY IF IT IS STILL HELD - the save route's rule. It may have
        # been in this bag then and in nobody's now.
        pet = str(state.get("active_pet_id", "") or "")
        if pet and not owns_item(user_id, slot, pet):
            pet = ""
        equipment = state.get("equipment", {}) if isinstance(state.get("equipment"), dict) else {}
        level = max(1, int(state.get("level", 1)))
        data.put_back(db, user_id, slot, level, max(0, int(state.get("xp", 0))),
                      int(catalogue.xp_needed_for_level(level)), json.dumps(equipment), pet, now)
        # THE MAXIMA FOLLOW THE OLD LEVEL AND GEAR; THE POOLS ONLY COME DOWN.
        derived = derived_stats(data.character(db, user_id, slot))
        if derived is not None:
            data.bring_pools_down(db, user_id, slot, derived, now)

        # THEIR GAME RELOADS - see the section header. Counted first, like a kick,
        # because "were they playing" is the useful number.
        ended = data.end_sessions(db, user_id, now)

        after = character_state(db, user_id, slot)
        log_staff_action(
            g.user, "rollback", target["username"], user_id,
            "%s (slot %d) to snapshot #%d of %s UTC: level %d -> %d, gold %d -> %d, %d -> %d"
            " carried; undo is #%s; %d session(s) ended" % (
                row["name"], slot, snapshot_id,
                time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(snap["taken_at"]))),
                was["level"], after["level"], was["gold"], after["gold"],
                len(was["carry"]), len(after["carry"]),
                undo if undo is not None else "-", ended),
        )
        db.commit()

        return {
            "username": target["username"],
            "slot": slot,
            "character": row["name"],
            "restored": snapshot_id,
            "taken_at": int(snap["taken_at"]),
            "undo_snapshot": undo,
            "was": {"level": was["level"], "gold": was["gold"], "items": len(was["carry"])},
            "now": {"level": after["level"], "gold": after["gold"], "items": len(after["carry"])},
            "pet_cleared": bool(state.get("active_pet_id")) and pet == "",
            "sessions_ended": ended,
        }, 200


    def staff_teleport():
        """
        Move a player, or everyone, to a place
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
              properties:
                username: {type: string, description: "who to move; omit when everyone is true"}
                everyone: {type: boolean, description: "move everyone online - owner only"}
                area:     {type: string, description: "destination area; defaults to yours"}
                x:        {type: number}
                y:        {type: number}
        responses:
          200:
            description: How many moves were queued, and where
          400:
            description: Neither a username nor everyone
          403:
            description: Only the owner may move everyone
          409:
            description: That player is not online
          404:
            description: No such account, one you may not act on, or not staff
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        everyone = bool(payload.get("everyone", False))

        db = get_db()
        now = int(time.time())

        # WHERE. Defaults to the caller's own area, because "come here" is the
        # thing a dev wants nine times out of ten and typing your own location is
        # a step with nothing to gain.
        #
        # THE AREA OF THE CHARACTER BEING PLAYED. This read `LIMIT 1` with no
        # ORDER BY, which is whichever save SQLite happened to return first - not
        # even the latest - so "bring everyone to me" could send them to where an
        # alt was parked.
        area = str(payload.get("area", "") or playing_area(db, g.user["id"]) or "elusion").strip()
        if area == "":
            area = "elusion"

        try:
            x = float(payload.get("x", 0) or 0)
            y = float(payload.get("y", 0) or 0)
        except (TypeError, ValueError):
            return bad_request("x and y must be numbers")

        # ---------------------------------------------------------------- everyone
        if everyone:
            # MOVING THE WHOLE SERVER IS AN OWNER'S ACT. A dev may reach for one
            # player; emptying the world into one spot is the kind of thing that
            # wants the narrowest rank in the game behind it.
            if not has_owner_powers(g.user["username"]):
                return {
                    "error": "Forbidden",
                    "message": "Only the owner or a co-owner may teleport everyone.",
                }, 403

            # ONLY WHO IS ONLINE (game 0.93.4): a move waits two minutes at most
            # (TELEPORT_KEEP_SECONDS in app.py), so queuing one for somebody
            # away only ever moved them on a later sign-in, by surprise.
            rows = data.online_accounts(db, now - online_window, now)
            total = len(rows)
            for slot, row in enumerate(rows):
                queue_teleport(db, row["id"], area, x, y, slot, total, g.user["username"], now)

            log_staff_action(g.user, "teleport", "everyone", None,
                             "%d players -> %s (%.0f, %.0f)" % (total, area, x, y))
            post_broadcast("Everyone has been moved to %s." % area, "system", g.user["username"])
            db.commit()

            return {
                "moved": total, "area": area, "x": x, "y": y,
                "everyone": True, "spacing": teleport_spacing,
            }, 200

        # ------------------------------------------------------------ one player
        username = str(payload.get("username", "")).strip()
        if username == "":
            return bad_request('send {"username": "..."} or {"everyone": true}')

        # can_act_on via the same helper every sanction uses, so a mod cannot move
        # somebody who outranks them and nobody can move the owner.
        target, refusal = moderation_target({"username": username})
        if refusal is not None:
            return refusal
        # NOBODY AWAY IS MOVED (game 0.93.4): it would wait for their next
        # sign-in, and move them then, wherever they meant to be.
        if not data.heard_lately(db, target["id"], now - online_window, now):
            return {
                "error": "Conflict",
                "message": "%s is not online - a teleport is only for somebody playing." % target["username"],
            }, 409

        queue_teleport(db, target["id"], area, x, y, 0, 1, g.user["username"], now)
        log_staff_action(g.user, "teleport", target["username"], target["id"],
                         "-> %s (%.0f, %.0f)" % (area, x, y))
        db.commit()

        return {
            "moved": 1, "username": target["username"], "area": area,
            "x": x, "y": y, "everyone": False,
        }, 200


    def acknowledge_teleport():
        """
        Confirm a pending teleport was carried out
        ---
        tags:
          - Status
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
              required: [id]
              properties:
                id: {type: integer, description: "the id the poll handed you"}
        responses:
          200:
            description: Cleared, or already gone
          401:
            description: Missing, invalid or expired token
        """
        # THE CLIENT CLEARS IT, NOT THE SERVER ON DELIVERY. A teleport removed the
        # moment it was handed out is a teleport lost if the client never applied
        # it - the window closed, the game crashed, the player alt-tabbed into a
        # scene change. It stays queued until somebody says they actually moved.
        #
        # Matched on id so a stale ack cannot clear a NEWER teleport issued in the
        # meantime, which would strand the player where they were sent from.
        payload = request.get_json(silent=True) or {}
        try:
            teleport_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            return bad_request("id must be a whole number")

        db = get_db()
        cleared = data.clear_teleport(db, g.user["id"], teleport_id)
        db.commit()
        return {"cleared": cleared}, 200

    def owner_asked_again(view):
        return require_auth(require_owner(require_fresh_password(view)))

    app.add_url_rule("/api/staff/gold", endpoint="staff_gold",
                     view_func=owner_asked_again(staff_gold), methods=["POST"])
    app.add_url_rule("/api/staff/level", endpoint="staff_level",
                     view_func=owner_asked_again(staff_level), methods=["POST"])
    app.add_url_rule("/api/staff/skill", endpoint="staff_skill",
                     view_func=owner_asked_again(staff_skill), methods=["POST"])
    app.add_url_rule("/api/staff/grant", endpoint="staff_grant",
                     view_func=owner_asked_again(staff_grant), methods=["POST"])
    app.add_url_rule("/api/staff/gifts", endpoint="staff_gifts",
                     view_func=require_auth(require_owner(staff_gifts)), methods=["GET"])
    app.add_url_rule("/api/staff/snapshots", endpoint="staff_snapshots",
                     view_func=require_auth(require_owner(staff_snapshots)), methods=["GET"])
    app.add_url_rule("/api/staff/rollback", endpoint="staff_rollback",
                     view_func=owner_asked_again(staff_rollback), methods=["POST"])
    app.add_url_rule("/api/staff/teleport", endpoint="staff_teleport",
                     view_func=require_auth(require_role("dev")(staff_teleport)), methods=["POST"])
    app.add_url_rule("/api/teleport/ack", endpoint="acknowledge_teleport",
                     view_func=require_auth(acknowledge_teleport), methods=["POST"])
