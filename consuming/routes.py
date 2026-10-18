"""consuming/routes.py - the edge: using one consumable, as it was in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    parse_slot          a raw slot -> its int, or None
    max_slot            the highest slot, for the 400's words
    parse_cell          (payload, capacity) -> (the cell used or None, the
                        refusal or None)
    carry_capacity      how many cells the carry has, hotbar included
    catalogue           gamedata: whether this character may use the item,
                        and what it restores - asked on every call, so a
                        suite's stand-in for consume_check() is heard
    skills_of           (user_id, slot) -> {skill: {"level", "xp"}}
    valid_skills        every skill the game has
    take_from_backpack  (user_id, slot, item_id, quantity, prefer=) -> True,
                        or False having written nothing
    inventory_of        (user_id, slot) -> the carry, cell by cell

THE ENDPOINT NAME IS app.py's - character_consume - and routes.lock holds it.
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, parse_cell, carry_capacity,
             catalogue, skills_of, valid_skills, take_from_backpack, inventory_of):

    def character_consume():
        """
        Use one consumable from the backpack
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
              required: [slot, item_id]
              properties:
                slot:     {type: integer, example: 0}
                item_id:  {type: string,  example: smallhealthpotion}
                position: {type: integer, example: 21, description: "The carried cell used (0-19 bag, 20-29 hotbar). Spent first when it holds the item."}
        responses:
          200:
            description: The item was destroyed; apply its effect
          400:
            description: Bad slot or item_id
          403:
            description: Character level or skill level too low
          404:
            description: That slot is empty, or you are not carrying that item
          409:
            description: That item is not something you can consume
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        item_id = str(payload.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return bad_request("item_id must be 1-64 characters")

        position, error = parse_cell(payload, carry_capacity)
        if error is not None:
            return error

        db = get_db()
        level = data.level_of(db, user_id, slot)
        if level is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # WHAT YOU HOLD IS CHECKED BEFORE WHETHER YOU MAY USE IT, and the order
        # is not arbitrary. "You are not carrying that" is true regardless of
        # level, and answering 403 first would tell a client which items it
        # does NOT hold are gated and at what level - the same shape as the
        # 404-not-403 decision in require_role(), for the same reason.
        held = data.carried(db, user_id, slot, item_id)
        if held <= 0:
            return {"error": "Not Found", "message": "You are not carrying that."}, 404

        levels = {
            skill: int(value["level"])
            for skill, value in skills_of(user_id, slot).items()
        }

        # VALID_SKILLS IS PASSED IN, and it is the whole difference between a
        # gate and a bypass. Skill rows are written when a skill is first
        # trained, so a new character has none at all - and without this set,
        # "cooking is missing from your levels" reads as "cooking is not a
        # skill" and every fresh account walks through every skill gate in the
        # game. See consume_check().
        verdict = catalogue.consume_check(item_id, level, levels, known_skills=valid_skills)

        if verdict.get("unknown_skill"):
            # Loud and permissive, exactly as the client is. A skill name the
            # game does not have is a typo in the .tres, and refusing would
            # take a working item away from every player over an editor slip.
            # Logged, because a gate that has quietly stopped gating is the
            # outcome worth avoiding.
            app.logger.error(
                "consume: '%s' requires unknown skill '%s' - check its .tres",
                item_id, verdict["unknown_skill"],
            )

        if not verdict["ok"]:
            return services.refusal(verdict)

        # ---- everything above this line is validation; everything below commits --

        if not take_from_backpack(user_id, slot, item_id, 1, prefer=position):
            # Only reachable if the stack vanished between the count above and
            # here. All-or-nothing, so nothing is written.
            return {"error": "Not Found", "message": "You are not carrying that."}, 404

        # The amount comes from the server's own catalogue, never from the
        # request.
        target, amount = catalogue.restore_for(item_id)
        data.grant(db, user_id, slot, item_id, target, amount, int(time.time()))
        db.commit()

        return {
            "slot": slot,
            "item_id": item_id,
            "consumed": 1,
            "remaining": held - 1,
            # Returned so the client applies the server's number rather than
            # its own copy. It still owns the effect - hp is client-written, see
            # E-9 - but there is no reason for the two to disagree about the
            # size of it.
            "restores": {"target": target, "amount": int(amount)},
            # THE BAG AFTERWARDS, like every other route that changes it. The
            # game used to take one off its own copy and save the whole bag;
            # the bag is the server's now, so it adopts this instead.
            "inventory": inventory_of(user_id, slot),
        }, 200

    app.add_url_rule("/api/character/consume", endpoint="character_consume",
                     view_func=require_auth(character_consume), methods=["POST"])
