"""smith/routes.py - the edge: the anvil's four routes (0.74.0).

register() is handed app.py's own pieces - it never imports app.py:

    require_auth       the token check (puts the account on g.user)
    get_db             the request's database connection
    bad_request        a message -> app.py's 400
    parse_slot         a raw slot -> its int, or None
    max_slot           the highest slot, for the 400's words
    catalogue          gamedata: what a piece is (item_row()), what tempering
                       makes of its roll (temper_roll()), its worth, its name,
                       the smith's constants - asked on every call
    rule               (db, key) -> the owner's economy number in force
                       (economyrules.value(): temper_gold_scale and
                       certification_gold_scale)
    gold_delta         (db, user_id, slot, delta, reason, detail) -> the new
                       balance: the carried gold, through the ledger
    take_from_backpack (user_id, slot, item_id, quantity, keys_last=) -> True,
                       or False having written nothing (carry/'s grant)
    inventory_of       (user_id, slot) -> the carry, cell by cell
    carry_request      () -> (payload, slot, item_id, None) or an error: what a
                       one-cell carry route opens with (carry/'s)
    parse_grid_cell    (payload, key, capacity) -> (the cell, or None and a 400)
    carry_cells        every carried cell, the hotbar's keys after the bag
    grid_cells         (table, key columns, key values, positions) -> what the
                       named cells hold (carry/'s)
    grid_rewrite       (table, key columns, key values, positions, after) - the
                       named cells replaced (carry/'s)
    resync             (user_id, slot) -> the 409 with the carry and the purse
                       the server holds (carry/'s)
    move_trade_hold    (db, user_id, old id, new id) - a held find's hold moved
                       to the id tempering gave it (trade/'s)

THE ENDPOINT NAMES ARE THIS FILE'S - smith_status, smith_quote, smith_temper,
smith_certify - and routes.lock holds them.
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, catalogue, rule, gold_delta,
             take_from_backpack, inventory_of, carry_request, parse_grid_cell, carry_cells, grid_cells,
             grid_rewrite, resync, move_trade_hold):

    def name_of(item_id):
        return str((catalogue.item_row(item_id) or {}).get("display_name") or item_id)

    def rank_of(earned):
        """The character's rank: the highest certification it holds, 0 for none."""
        return max(earned) if earned else 0

    def status_for(db, user_id, slot, purse):
        earned = data.earned(db, user_id, slot)
        return services.status_view(catalogue.CONSTANTS, catalogue.QUALITY_HIGH, rank_of(earned), earned,
                                    data.held(db, user_id, slot), rule(db, "temper_gold_scale"),
                                    rule(db, "certification_gold_scale"), purse)

    def missing_character(slot):
        return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

    def take_all(user_id, slot, materials):
        """Every material, bag before keys, or False having written nothing
        the caller keeps (the caller rolls back). [(item_id, quantity)]."""
        for item_id, quantity in materials:
            if not take_from_backpack(user_id, slot, item_id, quantity, keys_last=True):
                return False
        return True

    def smith_status():
        """
        The smith: this character's Royal Certification, every rank and what it costs, and the temper costs by tier
        ---
        tags:
          - Smith
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
            description: "rank and rank_name; ranks (each with gold scaled, materials with have, earned); temper_costs (each tier's gold_rate, materials with have, certification); temper_step, temper_cap; gold, the purse"
          400:
            description: Bad slot
          404:
            description: That slot is empty
          401:
            description: Missing, invalid or expired token
        """
        slot = parse_slot(request.args.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        user_id = g.user["id"]
        db = get_db()
        row = data.character(db, user_id, slot)
        if row is None:
            return missing_character(slot)
        return status_for(db, user_id, slot, int(row["gold"] or 0)), 200

    def appraisal():
        """What quote and temper both open with: the request, the cell, and
        the smith's verdict on what is in it. (answer, refusal, context) or
        (None, an error response, None)."""
        payload, slot, item_id, error = carry_request()
        if error is not None:
            return None, error, None
        position, error = parse_grid_cell(payload, "position", carry_cells)
        if error is not None:
            return None, error, None

        # ONE CELL AND THE ITEM THE GAME SAW IN IT (ONE CELL AT A TIME, like
        # the shop's sale): a cell that holds something else is the game's
        # picture being out of date, and it is sent the server's.
        user_id = g.user["id"]
        keys = (user_id, slot)
        cell = grid_cells("carry_items", ("user_id", "slot"), keys, (position,)).get(position)
        if cell is None or cell[0] != item_id:
            return None, resync(user_id, slot), None

        db = get_db()
        row = data.character(db, user_id, slot)
        earned = data.earned(db, user_id, slot)
        held = data.held(db, user_id, slot)
        answer, refusal = services.appraise(
            catalogue.item_row(item_id), item_id, catalogue.CONSTANTS, catalogue.QUALITY_HIGH, rank_of(earned),
            held, int(row["gold"] or 0), rule(db, "temper_gold_scale"), catalogue.temper_roll,
            services.certifications(catalogue.CONSTANTS), name_of)
        return answer, refusal, {"slot": slot, "position": position, "cell": cell, "keys": keys}

    def smith_quote():
        """
        What tempering the piece in one carried cell would make of it and cost - nothing is done
        ---
        tags:
          - Smith
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
              required: [slot, position, item_id]
              properties:
                slot:     {type: integer, example: 0}
                position: {type: integer, example: 4, description: "The carried cell"}
                item_id:  {type: string,  example: "jadesword~d104p96", description: "What the game saw in that cell"}
        responses:
          200:
            description: "item_id, new_item_id, before and after ({field: percent}), gold, materials (with have), certification; can, and the reason it cannot"
          400:
            description: Bad slot, cell or item
          404:
            description: That slot is empty
          409:
            description: "The cell does not hold that item. Carries resync; nothing was done."
          401:
            description: Missing, invalid or expired token
        """
        answer, refusal, context = appraisal()
        if context is None:
            return refusal
        return answer, 200

    def smith_temper():
        """
        Temper the piece in one carried cell: every rolled stat up, for materials and gold
        ---
        tags:
          - Smith
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
              required: [slot, position, item_id]
              properties:
                slot:     {type: integer, example: 0}
                position: {type: integer, example: 4, description: "The carried cell"}
                item_id:  {type: string,  example: "jadesword~d104p96", description: "What the game saw in that cell"}
        responses:
          200:
            description: "Tempered. item_id is the new id, in the same cell (position); gold the purse after; status the smith's; inventory the carry; consumed the materials"
          400:
            description: Bad slot, cell or item, or not enough gold
          403:
            description: The tier needs a Royal Certification this character has not earned
          404:
            description: That slot is empty, or a material is not carried
          409:
            description: "Not rolled gear, already perfect or fully tempered, a tier the smith does not temper - or the cell does not hold that item (carries resync)"
          401:
            description: Missing, invalid or expired token
        """
        # A SINK FOR GOLD AND MATERIALS (0.74.0). The owner: "a new smith spends
        # materials plus gold to upgrade gear". The gold is DESTROYED through
        # gold_delta(), reason smith_temper; the materials leave through the
        # carry's grant; the piece is rewritten in its own cell. One
        # transaction, under the write lock every POST takes, so nothing is
        # read twice and nothing is paid for that was not made.
        answer, refusal, context = appraisal()
        if context is None:
            return refusal
        if refusal is not None:
            status, message = refusal
            title = {400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict"}[status]
            return {"error": title, "message": message}, status

        user_id = g.user["id"]
        slot, position, cell, keys = context["slot"], context["position"], context["cell"], context["keys"]
        old_id, new_id = answer["item_id"], answer["new_item_id"]
        cost = services.temper_costs(catalogue.CONSTANTS)[int(catalogue.item_row(old_id)["tier"])]
        db = get_db()

        # ---- everything above this line is validation; below it commits --------
        if not take_all(user_id, slot, cost["materials"]):
            # Cannot happen under the write lock - the counts above were read in
            # this transaction - but if it does, nothing is kept.
            db.rollback()
            return resync(user_id, slot)
        remaining = gold_delta(db, user_id, slot, -answer["gold"], "smith_temper",
                               "%s -> %s" % (old_id, new_id))
        # THE NEW ROLL, IN THE SAME CELL: the player's grid is laid out by hand,
        # and the piece they put on the anvil comes back where it was.
        grid_rewrite("carry_items", ("user_id", "slot"), keys, (position,), {position: (new_id, cell[1])})
        # A HELD FIND STAYS HELD (trade/'s holds are on the id): the 48 hours a
        # fresh mythic or Perfect waits do not end at the anvil.
        move_trade_hold(db, user_id, old_id, new_id)
        db.commit()

        return {
            "item_id": new_id,
            "previous_item_id": old_id,
            "position": position,
            "before": answer["before"],
            "after": answer["after"],
            "paid": answer["gold"],
            "gold": int(remaining),
            "status": status_for(db, user_id, slot, int(remaining)),
            "inventory": inventory_of(user_id, slot),
            "consumed": [{"item_id": item_id, "quantity": quantity} for item_id, quantity in cost["materials"]],
        }, 200

    def smith_certify():
        """
        Earn the next Royal Certification: its materials and gold, at its level
        ---
        tags:
          - Smith
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
              required: [slot, rank]
              properties:
                slot: {type: integer, example: 0}
                rank: {type: integer, example: 1, description: "The next rank: the character's own plus one"}
        responses:
          200:
            description: "Earned. The new status (as GET /api/smith/status), with inventory, and consumed the materials"
          400:
            description: Bad slot or rank, or not enough gold
          403:
            description: Not the next rank, or the character's level is too low
          404:
            description: That slot is empty, or a material is not carried
          409:
            description: That certification is already held
          401:
            description: Missing, invalid or expired token
        """
        # THE STEP TO THE HIGHER TIERS (0.74.0). The owner: "upgrades that cost
        # materials, plus a 'Royal Certification' step to reach higher tiers".
        # The gold - scaled by the owner's certification_gold_scale - is
        # DESTROYED, reason royal_certification, and the materials go with it.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        certs = services.certifications(catalogue.CONSTANTS)
        raw = payload.get("rank")
        try:
            wanted = int(raw) if not isinstance(raw, bool) and raw is not None and float(raw) == int(raw) else None
        except (TypeError, ValueError, OverflowError):
            wanted = None
        cert = next((c for c in certs if c["rank"] == wanted), None)
        if cert is None:
            return bad_request("rank must be one of %s" % ", ".join(str(c["rank"]) for c in certs))

        db = get_db()
        row = data.character(db, user_id, slot)
        if row is None:
            return missing_character(slot)

        earned = data.earned(db, user_id, slot)
        rank = rank_of(earned)
        if wanted <= rank:
            return {"error": "Conflict", "message": "You are already a %s." % services.rank_name(certs, rank)}, 409
        if wanted != rank + 1:
            # THE NEXT RANK ONLY: a Royal Smith is a Master Smith first.
            return {"error": "Forbidden",
                    "message": "Earn the %s first." % services.rank_name(certs, rank + 1)}, 403
        if int(row["level"] or 1) < cert["level"]:
            return {"error": "Forbidden", "message": "You need level %d." % cert["level"]}, 403
        held = data.held(db, user_id, slot)
        missing = services.first_missing(cert["materials"], held)
        if missing is not None:
            return {"error": "Not Found", "message": "You are missing %s." % name_of(missing)}, 404
        gold = services.certification_gold(cert["gold"], rule(db, "certification_gold_scale"))
        purse = int(row["gold"] or 0)
        if purse < gold:
            return bad_request("That costs %d gold and you have %d." % (gold, purse))

        # ---- everything above this line is validation; below it commits --------
        if not take_all(user_id, slot, cert["materials"]):
            db.rollback()
            return resync(user_id, slot)
        # gold_delta() writes no zero (the owner may set the scale to 0), and
        # answers None for one, so the purse is what it was.
        if gold > 0:
            purse = int(gold_delta(db, user_id, slot, -gold, "royal_certification",
                                   "rank %d, %s" % (cert["rank"], cert["name"])))
        data.earn(db, user_id, slot, cert["rank"], int(time.time()))
        db.commit()

        answer = status_for(db, user_id, slot, purse)
        answer["inventory"] = inventory_of(user_id, slot)
        answer["consumed"] = [{"item_id": item_id, "quantity": quantity} for item_id, quantity in cert["materials"]]
        return answer, 200

    app.add_url_rule("/api/smith/status", endpoint="smith_status",
                     view_func=require_auth(smith_status), methods=["GET"])
    app.add_url_rule("/api/smith/quote", endpoint="smith_quote",
                     view_func=require_auth(smith_quote), methods=["POST"])
    app.add_url_rule("/api/smith/temper", endpoint="smith_temper",
                     view_func=require_auth(smith_temper), methods=["POST"])
    app.add_url_rule("/api/smith/certify", endpoint="smith_certify",
                     view_func=require_auth(smith_certify), methods=["POST"])
