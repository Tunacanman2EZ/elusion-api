"""equipment/routes.py - the edge: putting gear on and taking it off, as it was
in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    parse_slot          a raw slot -> its int, or None
    max_slot            the highest slot, for the 400's words
    parse_cell          (payload, capacity) -> (the cell it came from or None,
                        the refusal or None)
    carry_capacity      how many cells the carry has, hotbar included
    equip_slots         the names of the places gear goes (the catalogue's)
    catalogue           gamedata: which slot an item goes in, and whether this
                        class at this level may wear it - asked on every call
    derived_stats       a saves row -> its maxima WITH WHAT IT IS WEARING:
                        app.py's _derived_stats(), the one way to ask
    take_from_backpack  (user_id, slot, item_id, quantity, prefer=) -> True,
                        or False having written nothing
    add_to_backpack     (user_id, slot, item_id, quantity) -> cells, or None
    inventory_of        (user_id, slot) -> the carry, cell by cell

THE ENDPOINT NAMES ARE app.py's - equip_item, unequip_item - and routes.lock
holds them.
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, parse_cell, carry_capacity,
             equip_slots, catalogue, derived_stats, take_from_backpack, add_to_backpack, inventory_of):

    def apply_worn_maxima(db, user_id, slot):
        """
        Re-derive the maxima from what is NOW worn, in the caller's
        transaction, and bring hp and mana down to them if they sit above.
        Returns the four numbers for the response.

        WHY THE EQUIP ROUTES DO THIS AND NOT ONLY THE NEXT SAVE: taking off a
        Vitality amulet lowers max_hp this instant. Left for the next status
        write, the stored row would say 492/432 for however long that took,
        and anything reading it meanwhile - the character sheet, the healing
        reconciler's "before" - would be reading a character over their own
        ceiling.

        NEVER RAISES THE POOLS. Putting one on lifts the ceiling, not the
        health; regeneration fills the gap at the rate the new maximum allows.
        """
        derived = derived_stats(data.wearer(db, user_id, slot))
        if derived is None:
            return {}
        data.hold_to(db, user_id, slot, derived["max_hp"], derived["max_mana"])
        return data.pools(db, user_id, slot)

    def equip_item():
        """
        Move one item from the backpack onto the character
        ---
        tags: [Character]
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
              required: [slot, item_id]
              properties:
                slot:     {type: integer, example: 0}
                item_id:  {type: string,  example: cobaltrobe}
                position: {type: integer, example: 4, description: "The carried cell it came from (0-19 bag, 20-29 hotbar). Taken from first when it holds the item."}
        responses:
          200: {description: "Equipped. Returns the new equipment map, the bag layout, and stats: the hp/mana ceilings what is now worn implies"}
          400: {description: Bad slot or item_id}
          403: {description: Wrong slot for this item, wrong class, or too low a level}
          404: {description: No character in that slot, or you are not carrying that}
          409: {description: The piece coming off has nowhere to go}
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
        row = data.wearer(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # THE SLOT IS DERIVED FROM THE ITEM, never taken from the request.
        # There is then nothing for the two to disagree about, and
        # equipmentpanel.gd already works this way - "the square's name is
        # ignored on purpose".
        target = catalogue.equip_slot_for(item_id)
        if not target:
            if not catalogue.has_item(item_id):
                return bad_request("No such item '%s'." % item_id)
            return {"error": "Forbidden", "message": "That is not equipment."}, 403

        # THE SERVER'S OWN LEVEL AND CLASS, never the body's. Same reason
        # parse_equipment() takes them as arguments rather than reading a
        # payload: a client that could name its own level could wear anything.
        verdict = catalogue.equip_check(item_id, target, str(row["class_id"]), int(row["level"]))
        if not verdict.get("ok", False):
            if verdict.get("reason", "") == "unknown":
                return bad_request("No such item '%s'." % item_id)
            return services.refusal(verdict)

        worn = services.worn_of(row["equipment"])
        coming_off = str(worn.get(target, ""))

        # ---- everything above this line is validation; everything below commits --

        # TAKE FIRST, THEN PUT BACK, and the order is load-bearing rather than
        # tidy. Taking the new item may free the cell the old one needs -
        # swapping your only chest piece for another should always work, and
        # it only does if the bag is measured after the incoming item has left
        # it.
        if not take_from_backpack(user_id, slot, item_id, 1, prefer=position):
            return {"error": "Not Found", "message": "You are not carrying that."}, 404

        if coming_off:
            if add_to_backpack(user_id, slot, coming_off, 1) is None:
                # ALL OR NOTHING. The take above is undone rather than left
                # standing - an item that left the bag and reached neither the
                # doll nor the floor is the worst outcome available here.
                add_to_backpack(user_id, slot, item_id, 1)
                return {"error": "Conflict",
                        "message": "Your bag is full - nowhere to put what you are wearing."}, 409

        worn[target] = item_id
        data.wear(db, user_id, slot, worn, int(time.time()))
        pools = apply_worn_maxima(db, user_id, slot)
        db.commit()

        return {
            "slot": slot,
            "equipped": item_id,
            "equip_slot": target,
            "unequipped": coming_off,
            "equipment": worn,
            # THE CEILINGS THE SERVER NOW HOLDS, so a client can see its own
            # recompute agrees rather than finding out on the next save.
            "stats": pools,
            # THE AUTHORITATIVE LAYOUT, handed back so the client renders what
            # the server did rather than guessing at it. /api/loot/take does the
            # same.
            "inventory": inventory_of(user_id, slot),
        }

    def unequip_item():
        """
        Move one item off the character and back into the backpack
        ---
        tags: [Character]
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
              required: [slot, equip_slot]
              properties:
                slot:       {type: integer, example: 0}
                equip_slot: {type: string,  example: chest}
        responses:
          200: {description: "Unequipped. Returns the new equipment map, the bag layout, and stats: the hp/mana ceilings what is now worn implies"}
          400: {description: Bad slot or equip_slot}
          404: {description: No character in that slot, or nothing worn there}
          409: {description: The bag is full}
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        target = str(payload.get("equip_slot", "")).strip().lower()
        if target not in equip_slots:
            return bad_request("equip_slot must be one of: %s" % ", ".join(equip_slots))

        db = get_db()
        row = data.wearer(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        worn = services.worn_of(row["equipment"])
        item_id = str(worn.get(target, ""))
        if not item_id:
            # 404 RATHER THAN A CHEERFUL 200. Taking off a slot that is already
            # empty means the client and the server disagree about what is
            # worn, and answering "done" would let that disagreement persist
            # silently.
            return {"error": "Not Found", "message": "Nothing is worn there."}, 404

        # ---- everything above this line is validation; everything below commits --

        if add_to_backpack(user_id, slot, item_id, 1) is None:
            return {"error": "Conflict",
                    "message": "Your bag is full."}, 409

        worn.pop(target, None)
        data.wear(db, user_id, slot, worn, int(time.time()))
        pools = apply_worn_maxima(db, user_id, slot)
        db.commit()

        return {
            "slot": slot,
            "unequipped": item_id,
            "equip_slot": target,
            "equipment": worn,
            "stats": pools,
            "inventory": inventory_of(user_id, slot),
        }

    app.add_url_rule("/api/character/equip", endpoint="equip_item",
                     view_func=require_auth(equip_item), methods=["POST"])
    app.add_url_rule("/api/character/unequip", endpoint="unequip_item",
                     view_func=require_auth(unequip_item), methods=["POST"])
