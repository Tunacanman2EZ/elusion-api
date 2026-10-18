"""gathering/routes.py - the edge: the two routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    get_db              the request's database connection
    parse_slot          a raw slot -> its int, or None
    max_slot            the highest slot, for the 400's words
    bad_request         a message -> app.py's 400
    slot_exists         (user_id, slot) -> whether a character is there
    item_row            an item_id -> its catalogue row, or None
    roll_catch          (rod tier, fishing level) -> {item_id, quantity, xp},
                        or None for a catalogue with no fish in it
    roll_cook           (item_id, cooking level) -> {ok, reason, needs, burnt,
                        output, xp}
    skills_of           (user_id, slot) -> {skill: {level, xp, ...}}
    take_from_backpack  (user_id, slot, item_id, quantity) -> True, or False
                        having written nothing
    add_to_backpack     (user_id, slot, item_id, quantity) -> the cells
                        written, or None having written nothing
    grant_skill_xp      (user_id, slot, skill_id, xp) -> (level, xp, levels
                        gained)
    status_of           (user_id, slot) -> the status block every carry
                        answer hands back
    inventory_of        (user_id, slot) -> the carry, cell by cell
    bag_cells           the backpack's size, for "Your backpack is full"

THE GRANTS ARE app.py's: the loot bag, the shop, trades, the bank and gifts
pay with the same three (take, add, grant), and this file asks for them.

THE ENDPOINT NAMES ARE app.py's - fishing_catch, cooking_cook - and
routes.lock holds them.
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, parse_slot, max_slot, bad_request, slot_exists,
             item_row, roll_catch, roll_cook, skills_of, take_from_backpack, add_to_backpack,
             grant_skill_xp, status_of, inventory_of, bag_cells):

    def backpack_full():
        return {
            "error": "Conflict",
            "message": "Your backpack is full (%d slots)." % bag_cells,
        }, 409

    def fishing_catch():
        """
        Land a fish
        ---
        tags:
          - Fishing
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
              required: [slot]
              properties:
                slot: {type: integer, example: 0}
        responses:
          200:
            description: What was caught, and the state it landed in
          400:
            description: Bad slot
          403:
            description: No rod, or no bait
          404:
            description: That slot is empty
          409:
            description: Backpack full
          429:
            description: Casting faster than the bucket allows
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        db = get_db()
        row = data.cast_bucket(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # THE ROD AND THE BAIT ARE CHECKED AGAINST carry_items, not against
        # anything the client said. fishingspot.gd runs the same two checks so
        # it can refuse early and explain why, but that copy is a courtesy -
        # this one decides.
        rod_tier = services.best_rod_tier(data.carried_ids(db, user_id, slot), item_row)
        if rod_tier <= 0:
            return {"error": "Forbidden", "message": "You need a fishing rod."}, 403

        if data.held(db, user_id, slot, services.FISHING_BAIT_ID) <= 0:
            return {"error": "Forbidden", "message": "You need worms for bait."}, 403

        # Refill then spend, exactly as /api/combat/kill does.
        now_ms = int(time.time() * 1000)
        tokens = services.refilled(row["cast_tokens"], row["last_cast_at"], now_ms)
        if tokens < 1.0:
            return {
                "error": "Too Many Requests",
                "message": "Casts are arriving faster than %g per second." % services.CAST_TOKENS_PER_SECOND,
            }, 429
        tokens -= 1.0

        skills = skills_of(user_id, slot)
        fishing_level = int(skills.get("fishing", {}).get("level", 1))

        catch = roll_catch(rod_tier, fishing_level)
        if catch is None:
            # No FISH-typed items in the catalogue at all. A server
            # misconfiguration rather than a player problem, and it must not
            # eat their bait.
            return {
                "error": "Conflict",
                "message": "There is nothing to catch here.",
            }, 409

        # ---- everything above this line is validation; everything below commits
        if not take_from_backpack(user_id, slot, services.FISHING_BAIT_ID, 1):
            # Re-checked inside the transaction. The count above was read
            # before the roll, and two casts racing would both have seen the
            # last worm.
            return {"error": "Forbidden", "message": "You need worms for bait."}, 403

        written = add_to_backpack(user_id, slot, catch["item_id"], catch["quantity"])
        if written is None:
            # The bait is not spent: nothing has been committed, and returning
            # here abandons the transaction rather than charging for a fish
            # that had nowhere to go.
            return backpack_full()

        level, _xp, levels_gained = grant_skill_xp(user_id, slot, "fishing", catch["xp"])

        data.spend_cast(db, user_id, slot, now_ms, tokens)
        db.commit()

        return services.caught(catch, written, levels_gained, level, status_of(user_id, slot),
                               inventory_of(user_id, slot), skills_of(user_id, slot)), 200

    def cooking_cook():
        """
        Cook one raw fish
        ---
        tags:
          - Cooking
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
              required: [slot, item_id]
              properties:
                slot:    {type: integer, example: 0}
                item_id: {type: string,  example: rawmudfish}
        responses:
          200:
            description: What happened, and the state it landed in
          400:
            description: Bad slot or item_id
          403:
            description: Cooking level too low
          404:
            description: That slot is empty, or you are not carrying that fish
          409:
            description: Backpack full, or the item is not cookable
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        if not slot_exists(user_id, slot):
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        item_id = str(payload.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return bad_request("item_id must be 1-64 characters")

        # THE INPUT IS NAMED BY THE CLIENT AND NOTHING ELSE IS. Which of their
        # own fish to cook is a choice, not a claim - the recipe, the level gate
        # and the burn roll are all read from data the server holds.
        db = get_db()
        if data.held(db, user_id, slot, item_id) <= 0:
            return {"error": "Not Found", "message": "You are not carrying that."}, 404

        skills = skills_of(user_id, slot)
        cooking_level = int(skills.get("cooking", {}).get("level", 1))

        outcome = roll_cook(item_id, cooking_level)
        if not outcome["ok"]:
            if outcome["reason"] == "level":
                return {
                    "error": "Forbidden",
                    "message": "You need cooking level %d." % outcome["needs"],
                }, 403
            return {
                "error": "Conflict",
                "message": "That cannot be cooked.",
            }, 409

        # ---- everything above this line is validation; everything below commits
        if not take_from_backpack(user_id, slot, item_id, 1):
            return {"error": "Not Found", "message": "You are not carrying that."}, 404

        written = []
        if not outcome["burnt"]:
            written = add_to_backpack(user_id, slot, outcome["output"], 1)
            if written is None:
                # The raw fish is not consumed - nothing is committed.
                return backpack_full()

        level, _xp, levels_gained = grant_skill_xp(user_id, slot, "cooking", outcome["xp"])
        db.commit()

        return services.cooked(item_id, outcome, written, levels_gained, level, status_of(user_id, slot),
                               inventory_of(user_id, slot), skills_of(user_id, slot)), 200

    app.add_url_rule("/api/fishing/catch", endpoint="fishing_catch",
                     view_func=require_auth(fishing_catch), methods=["POST"])
    app.add_url_rule("/api/cooking/cook", endpoint="cooking_cook",
                     view_func=require_auth(cooking_cook), methods=["POST"])
