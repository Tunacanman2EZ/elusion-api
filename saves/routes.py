"""saves/routes.py - the edge: a character's slots, its whole read, and
deleting it, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth      the token check (puts the account on g.user)
    get_db            the request's database connection
    bad_request       a message -> app.py's 400
    parse_slot        a raw slot -> its int, or None
    max_slot          the highest slot, for the 400's words
    clean_text        (raw, limit) -> one line of text fit to show other
                      players: clean_player_text(), the one rule for every
                      name and line typed
    owns_item         (user_id, slot, item_id) -> whether the carry or the
                      bank holds it: the only pet that can be out
    stored_json       (raw, fallback) -> a JSON column read back, forgiving
                      anything unreadable
    derived_stats     a saves row -> its maxima WITH WHAT IT IS WEARING:
                      app.py's _derived_stats(), the one way to ask
    catalogue         gamedata: what the first level costs - asked on every
                      call
    take_snapshot     (db, user_id, slot, reason) -> the save history's next
                      snapshot, or None when it is too soon or nothing changed
    status_of         (user_id, slot) -> the vitals, as the status read
                      answers them, or None
    inventory_of      (user_id, slot) -> the carry, cell by cell
    skills_of         (user_id, slot) -> {skill: {"level", "xp"}}
    find_open_trade   (db, user_id) -> the account's open trade, or None
    gold_delta        (db, user_id, slot, delta, reason, detail) - the
                      carried gold, through the ledger
    deletions_kept    how many deleted characters an account keeps a copy of
    delete_reason     the gold ledger's reason for a deleted character's purse

THE ENDPOINT NAMES ARE app.py's - list_saves, write_save, read_character,
character_delete - and routes.lock holds them.
"""
import json
import time

from flask import g, request

from . import data, services
from .services import MAX_EXPLORED_BYTES, VALID_CLASSES


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, clean_text, owns_item, stored_json,
             derived_stats, catalogue, take_snapshot, status_of, inventory_of, skills_of, find_open_trade,
             gold_delta, deletions_kept, delete_reason):

    def slot_view(row, buffs=None):
        """One slot as the game lists it - see services.slot_view(). `buffs` is
        data.food_buffs() for the account, {slot: row}."""
        return services.slot_view(row, stored_json(row["equipment"], {}), stored_json(row["explored"], {}),
                                  services.food_buff_view((buffs or {}).get(int(row["slot"]))))

    def list_saves():
        """
        List the account's character slots
        ---
        tags:
          - Save
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Occupied slots only; an empty account returns an empty list
          401:
            description: Missing, invalid or expired token
        """
        rows = data.all_of(get_db(), g.user["id"])
        # EACH CHARACTER'S MEAL IN FORCE (0.74.0), or null: one read for all of
        # them, beside the rows.
        buffs = data.food_buffs(get_db(), g.user["id"], int(time.time()))

        return {
            "username": g.user["username"],
            "slots": [slot_view(r, buffs) for r in rows],
        }, 200

    def write_save():
        """
        Create or overwrite one character slot
        ---
        tags:
          - Save
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
              required: [slot, class_id, name]
              properties:
                slot:     {type: integer, example: 0}
                class_id: {type: string,  example: warrior}
                name:     {type: string,  example: Tunacan}
                level:    {type: integer, example: 12}
                area:     {type: string,  example: elusion}
                active_pet_id:
                  type: string
                  example: petpoisonslimesmall
                  description: "Pet currently out, as the client's item_id. Empty string means none. Omitted leaves the stored value unchanged."
        responses:
          200:
            description: Slot written
          400:
            description: Invalid slot, class, name or active_pet_id
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        class_id = str(payload.get("class_id", "")).strip().lower()
        if class_id not in VALID_CLASSES:
            return bad_request("class_id must be one of: %s" % ", ".join(sorted(VALID_CLASSES)))

        # Shown to other players - the online list, a guild's roster, a trade - so
        # held to the same one-line rule as chat. The client names characters
        # after their class; only a modified one could send anything else.
        raw_name = str(payload.get("name", "")).strip()
        name = clean_text(raw_name, 20)
        if not name or len(raw_name) > 20:
            return bad_request("name must be 1-20 characters")

        # NOT from the payload. level is server-owned (see SERVER_OWNED_STATS): a
        # new character starts at 1, and an existing one keeps whatever the server
        # has. Taking it from the client here would have left an open door beside
        # the one /api/player/status just closed.
        level = 1
        # OMITTED means "leave it alone", exactly like active_pet_id below.
        #
        # THIS USED TO DEFAULT TO "elusion" ON EVERY WRITE, which meant any caller
        # that did not happen to send an area silently moved the character back to
        # the starting zone. Nothing noticed for a long time because nothing on the
        # client set the field at all - every character in the game just read
        # "elusion" forever. The moment the client started reporting where it
        # actually was, a single save from a slot loaded before the field existed
        # would have yanked that character home.
        #
        # "elusion" now applies only to a brand new character, through VALUES.
        area_key_sent = "area" in payload
        area = str(payload.get("area", "")).strip() if area_key_sent else ""
        if not area:
            area = "elusion"
            area_key_sent = False
        if len(area) > 64:
            return bad_request("area must be 64 characters or fewer")

        # OMITTED means "leave it alone", not "clear it".
        #
        # The client writes this slot on character creation and again on every save,
        # and not every one of those callers knows or cares which pet is out. If a
        # missing key cleared the field, one save from a caller that doesn't send it
        # would silently unequip the player's pet - and a pet is a 1-in-216 drop, so
        # that is the last thing in the game that should vanish by omission.
        #
        # Sending "" explicitly is how you clear it. That's a deliberate act.
        pet_key_sent = "active_pet_id" in payload
        active_pet_id = services.pet_id(payload.get("active_pet_id")) if pet_key_sent else ""
        if pet_key_sent and active_pet_id is None:
            return bad_request("active_pet_id must be a string of at most 64 characters")

        # ONLY A PET YOU HOLD CAN BE OUT. This took any string, so a modified
        # client could walk out a pet it had never won; decided on day 1 to close
        # it. HELD means in this character's carry or the account's bank - the
        # same _owns_item() the drop roll asks - and not "named in the catalogue":
        # the server still keeps no list of pets, so a pet added to the game needs
        # no server deploy before it can be summoned. A pet the player does not
        # hold is not stored; the save clears it instead, and says so in `ignored`,
        # the way gold and equipment are said.
        pet_ignored = False
        if pet_key_sent and active_pet_id and not owns_item(g.user["id"], slot, active_pet_id):
            active_pet_id = ""
            pet_ignored = True

        explored_key_sent = "explored" in payload
        explored = services.explored(payload.get("explored")) if explored_key_sent else {}
        if explored_key_sent and explored is None:
            return bad_request(
                "explored must be an object of {area: {w, h, ox, oy, bits}} and "
                "under %d bytes" % MAX_EXPLORED_BYTES)

        # THE HOTBAR IS NOT A SAVE FIELD ANY MORE. It used to be a list of item ids,
        # one per key, pointing into the backpack. Now each key holds the item
        # itself, as a carry_items row past the bag (see CARRY_CAPACITY), and moves
        # with PUT /api/character/inventory like any other cell. Ignored rather
        # than refused, for the reason given for equipment below.
        hotbar_ignored = "hotbar" in payload

        now = int(time.time())

        db = get_db()

        # Read BEFORE the upsert, because afterwards there is no way to tell a
        # character that was just created from one that already existed - and they
        # need different treatment below.
        existing = data.level_of(db, g.user["id"], slot)
        is_new = existing is None

        # EQUIPMENT: IGNORED, NOT REFUSED - and this is the line that makes equipment
        # server-owned rather than merely server-moved.
        #
        # /api/character/equip and /unequip take the item OUT of the backpack and
        # put it back, in one transaction, and the take IS the ownership check.
        # All of that is worth nothing if a client can then PUT whatever equipment
        # map it likes here: it would simply skip the endpoints and dress itself,
        # in the column combat reads to decide what a hit is worth. That is E-1
        # one column over, and E-8 one field over.
        #
        # IGNORED rather than 400, exactly like gold. An un-updated client still
        # sends `equipment` on every save, and refusing an otherwise honest sync
        # over a field it is no longer allowed to set would break saving for
        # anyone who had not restarted their game. It is named in `ignored` so a
        # client can see it is being dropped rather than silently disagreeing with
        # the server forever.
        #
        # parse_equipment(), which once checked a whole map sent here, is gone
        # (18 Oct): the equip routes ask gamedata.equip_check() for one item at a
        # time, and nothing else writes the column.
        equipment_ignored = "equipment" in payload

        # ALWAYS 0, and that is the whole point rather than a leftover.
        #
        # The upsert below reads this as "did the caller supply equipment" and
        # writes excluded.equipment when it is true. Setting it from the payload
        # while forcing `equipment` to {} - which is what the first draft of this
        # change did - would write an EMPTY map over the character's gear on every
        # save. That is the same stripping bug this whole endpoint pair exists to
        # fix, reintroduced by the fix.
        #
        # Zero means the CASE keeps saves.equipment, which is what the equip
        # endpoints wrote and the only thing that should ever have written it.
        equip_key_sent = 0
        equipment = {}

        # ON CONFLICT rather than DELETE-then-INSERT: an upsert is one statement,
        # so there is no window where the slot exists in neither state.
        #
        # active_pet_id uses excluded.* only when the caller actually sent the key;
        # otherwise it keeps whatever the row already holds. On a fresh INSERT there
        # is nothing to keep, so it lands as the '' default.
        data.upsert(db, g.user["id"], slot, class_id, name, level, area, active_pet_id,
                    json.dumps(equipment), json.dumps(explored), now,
                    area_key_sent, pet_key_sent, equip_key_sent, explored_key_sent)
        db.commit()

        # DERIVED STATS BELONG ON A NEW CHARACTER TOO, not only on the next status
        # write. Without this a freshly created warrior sat at the table's DEFAULT 10
        # hp until something happened to push a status - and if the client had been
        # trusted for max_hp, nobody would ever have noticed, because it would have
        # overwritten the 10 on its first save.
        #
        # Recomputed on every save rather than only on creation: max_hp is a pure
        # function of class and level, so there is no state where storing anything
        # else is correct.
        #
        # AND OF WHAT IS WORN, read back from the row rather than the request -
        # equipment is server-owned and this route never writes it.
        stored = data.wearer(db, g.user["id"], slot)
        derived = derived_stats(stored, class_id=class_id)

        if derived is not None:
            if is_new:
                # A new character starts with full pools. Only on creation - doing it
                # on every save would refill the player's health for free.
                # The table's DEFAULT 100 is the old curve's first level; a
                # new character is told what the current curve says.
                data.fill(db, g.user["id"], slot, derived,
                          catalogue.xp_needed_for_level(int(stored["level"])), int(time.time()))
            else:
                # THE POOLS COME DOWN WITH THE CEILING, never up. A maximum can fall
                # now - an amulet came off - and a stored hp above it would be a
                # character the status route then has to argue with.
                data.hold_to(db, g.user["id"], slot, derived)
            db.commit()

        # THE SAVE HISTORY. At most one snapshot every SNAPSHOT_EVERY_SECONDS, and
        # none when nothing has changed - see SAVE SNAPSHOTS AND ROLLBACK. Not for
        # a character created by this very request: there is nothing to go back to.
        if not is_new and take_snapshot(db, g.user["id"], slot, "save") is not None:
            db.commit()

        # active_pet_id is echoed back only when the caller actually set it, so a
        # client can tell "you stored this" apart from "we left yours alone".
        result = {"slot": slot, "updated_at": now}
        if pet_key_sent:
            result["active_pet_id"] = active_pet_id

        # SAID OUT LOUD, the way gold is. A client that keeps sending `equipment`
        # is not doing anything wrong - it simply predates the endpoints - but a
        # field that is silently dropped is a client and a server disagreeing
        # forever with nothing to notice it by.
        ignored = []
        if pet_ignored:
            ignored.append("active_pet_id")
        if equipment_ignored:
            ignored.append("equipment")
        if hotbar_ignored:
            ignored.append("hotbar")
        if ignored:
            result["ignored"] = ignored
        if equipment_ignored:
            result["equipment"] = stored_json(data.equipment_of(get_db(), g.user["id"], slot), {})
        return result, 200

    def read_character():
        """
        Everything one character is, in a single call
        ---
        tags:
          - Character
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: query
            name: slot
            type: integer
            required: true
        responses:
          200:
            description: Identity, vitals, backpack, skills and the meal in force (food_buff, or null) together
          400:
            description: Bad slot
          404:
            description: That slot is empty
          401:
            description: Missing, invalid or expired token
        """
        # ONE CALL, NOT FOUR. Loading a character used to mean status, then
        # inventory, then skills, then bank - four round trips on a screen the
        # player is staring at. They are all keyed on the same (user_id, slot) and
        # none of them is useful without the others.
        slot = parse_slot(request.args.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        user_id = g.user["id"]
        status = status_of(user_id, slot)
        if status is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # equipment IS IN THE SELECT (data.identity()). sqlite3.Row raises
        # IndexError for a column the query did not fetch, so adding a key to the
        # payload below without adding it there turns a read into a 500. Third
        # time that trap has been stepped in today; it is always the same two
        # lines apart.
        row = data.identity(get_db(), user_id, slot)

        return {
            "slot": slot,
            "class_id": row["class_id"],
            "name": row["name"],
            "area": row["area"],
            "active_pet_id": row["active_pet_id"],
            "status": status,
            "inventory": inventory_of(user_id, slot),
            # EQUIPMENT TRAVELS WITH THE BAG, and the two being split across
            # endpoints is what cost a character its gear on every login.
            #
            # GET /api/save carried equipment and no inventory; this carried
            # inventory and no equipment. The client merges both, and
            # characterdata.gd::_sanitize_character_slot() reconciled the gear
            # against the bag - so it ran with equipment from one response and an
            # EMPTY inventory from the other, concluded the player owned none of
            # what they were wearing, and cleared every slot. Two endpoints each
            # correct on its own, and a reconciliation firing when only one half
            # had arrived.
            #
            # The prune is going away with the move to server-owned equipment, but
            # a read that answers half a question is worth fixing on its own terms:
            # the next thing to ask this endpoint what a character is wearing
            # should not have to know to ask somewhere else.
            "equipment": stored_json(row["equipment"], {}),
            "skills": skills_of(user_id, slot),
            # THE MEAL IN FORCE, or null (0.74.0) - what serverstorage.gd reads
            # at login, so a pie eaten before a relog is still on the HUD after
            # it, with the time the server gave it rather than a fresh clock.
            "food_buff": services.food_buff_view(
                data.food_buffs(get_db(), user_id, int(time.time())).get(slot)),
        }, 200

    def character_delete():
        """
        Delete one of your characters, for good
        ---
        tags:
          - Character
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
              required: [slot, confirm]
              properties:
                slot:    {type: integer, example: 0}
                confirm: {type: string, example: warrior, description: "The character's name, typed by the player. Any case."}
        responses:
          200:
            description: The character, its bag, its gold and its skills are gone; the account's bank is not
          400:
            description: Bad slot, or confirm is not the character's name
          404:
            description: No character in that slot
          409:
            description: An open trade names this character
        """
        payload = request.get_json(silent=True) or {}
        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer between 0 and %d" % max_slot)

        user_id = g.user["id"]
        db = get_db()
        row = data.row_of(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # THE NAME, TYPED. The game asks for it before it sends this, and the
        # server asks again, so a stray call - a stale button, a script, a replayed
        # request - cannot take a character. A character's name is its class, so
        # this is "type WARRIOR", which is exactly the deliberate act it should be.
        if not services.confirmed(payload.get("confirm", ""), row["name"]):
            return bad_request("Type the character's name to delete it.")

        # NOT IN THE MIDDLE OF A TRADE. An open trade holds items offered FROM this
        # character's bag and names it as the one receiving; running it after the
        # rows are gone would move things to and from a character that no longer
        # exists. Finishing or cancelling it is one click; this is the one place it
        # has to come first.
        if services.in_trade(find_open_trade(db, user_id), user_id, slot):
            return {"error": "Conflict",
                    "message": "This character is in a trade. Finish or cancel it first."}, 409

        # ---- everything above this line is validation; everything below commits --

        carry = data.carried(db, user_id, slot)
        skills = data.skills_of(db, user_id, slot)
        now = int(time.time())
        gold = int(row["gold"] or 0)
        data.keep_copy(db, user_id, slot, row, gold, now, services.deletion_copy(row, carry, skills),
                       deletions_kept)

        # THROUGH THE LEDGER, before the row goes: the purse is in saves, and a
        # bare DELETE would take the gold out of SUM(saves.gold) with no burn row,
        # and the supply invariant would be off by it for ever. The note on
        # gold_ledger in the schema says the same about deleting a user.
        if gold > 0:
            gold_delta(db, user_id, slot, -gold, delete_reason,
                       "carried gold of a deleted %s in slot %d" % (row["class_id"], slot))

        # AND ITS SAVE HISTORY: a snapshot names a slot, not a character, and the
        # next character made in this slot is somebody else. The copy above is
        # what is kept of this one.
        # And its kill record: the next character in this slot starts its own.
        # The items in its loot bags go with the bags: loot_bag_items cascades.
        data.drop(db, user_id, slot)
        db.commit()

        app.logger.info("character deleted: %s slot %d (%s, level %s, %d gold, %d cells)",
                        g.user["username"], slot, row["class_id"], row["level"], gold, len(carry))
        return {"slot": slot, "deleted": True, "gold_lost": gold, "items_lost": len(carry)}, 200

    app.add_url_rule("/api/save", endpoint="list_saves",
                     view_func=require_auth(list_saves), methods=["GET"])
    app.add_url_rule("/api/save", endpoint="write_save",
                     view_func=require_auth(write_save), methods=["PUT"])
    app.add_url_rule("/api/character", endpoint="read_character",
                     view_func=require_auth(read_character), methods=["GET"])
    app.add_url_rule("/api/character/delete", endpoint="character_delete",
                     view_func=require_auth(character_delete), methods=["POST"])
