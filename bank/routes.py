"""bank/routes.py - the edge: the account-shared bank and what the account
holds, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth           the token check (puts the account on g.user)
    get_db                 the request's database connection
    bad_request            a message -> app.py's 400
    parse_slot             a raw slot -> its int, or None
    max_slot               the highest slot, for the 400's words
    parse_stat             a raw value -> a whole number >= 0, or None
    parse_cell             (payload, capacity) -> (the cell dragged or None,
                           the refusal or None)
    parse_grid_cell        (payload, key, capacity) -> (a required cell, or a
                           400)
    parse_item_id          payload -> (item_id, None) or (None, a 400)
    parse_positional_items (cells, field) -> ([(position, item_id,
                           quantity)], None) or (None, a 400)
    slot_exists            (user_id, slot) -> whether a character is there
    ensure_account         user_id -> the accounts row, made if missing -
                           asked on every call, so a suite's stand-in is heard
    has_owner_powers       a username -> the owner or a co-owner, switch on
    catalogue              gamedata: how high an item stacks
    capacity               how many cells the bank has
    carry_capacity         how many the carry has, hotbar included
    backpack_capacity      how many of those are the bag, for the 409's words
    quantity_ceiling       the most one request may move
    take_from_cells        (rows, item_id, quantity, prefer=) -> (changes,
                           None) or (None, how many there were): the grids' one
                           rule for taking, the backpack's too
    apply_cell_changes     (table, key columns, key values, changes) - write it
    grid_move              (table, keys, values, from, to, item_id) ->
                           "moved", or "stale" having written nothing
    grid_discard           (table, keys, values, cell, item_id) -> ("discarded",
                           the stack) or ("stale", None)
    add_to_backpack        (user_id, slot, item_id, quantity) -> cells, or None
    inventory_of           (user_id, slot) -> the carry, cell by cell

and HANDS BACK account_of() - the account as GET /api/account answers it -
which app.py keeps as account_payload() (cashing a pile of lusions answers
with it).

THE ENDPOINT NAMES ARE app.py's - read_account, write_bank, write_lusions,
move_bank_items, move_bank_gold, move_banked, discard_banked - and routes.lock
holds them.
"""
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services
from .services import OPS


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, parse_stat, parse_cell,
             parse_grid_cell, parse_item_id, parse_positional_items, slot_exists, ensure_account,
             has_owner_powers, catalogue, capacity, carry_capacity, backpack_capacity, quantity_ceiling,
             take_from_cells, apply_cell_changes, grid_move, grid_discard, add_to_backpack, inventory_of):

    def account_of(user_id):
        """The account as GET /api/account answers it: lusions, bank gold,
        the score, and the bank's cells."""
        row = ensure_account(user_id)
        cells = services.grid(data.bank_rows(get_db(), user_id), capacity)
        return services.account_view(row, cells, capacity)

    def add_to_bank(user_id, item_id, quantity):
        """
        Put quantity of item_id into the bank and return the cells touched, or
        None when it does not fit - writing NOTHING unless the whole quantity
        fits. The bank's _add_to_backpack(): see services.pack().
        """
        db = get_db()
        stackable, max_stack = services.stacking(catalogue.item_row(item_id))
        occupied = {int(r["position"]): (r["item_id"], int(r["quantity"]))
                    for r in data.bank_rows(db, user_id)}
        writes = services.pack(occupied, item_id, quantity, max_stack, capacity, stackable)
        if writes is None:
            return None
        data.put_cells(db, user_id, writes)
        return [position for position, _, _ in writes]

    def resync(user_id):
        """The bank route's 409 for an out-of-date picture: the account as the
        server holds it, under `account`, so the game reloads the grid."""
        return {"error": "Conflict",
                "message": "The bank changed on the server. It has been reloaded.",
                "account": account_of(user_id)}, 409

    def read_account():
        """
        Account-shared state: lusions, bank gold, bank inventory
        ---
        tags:
          - Account
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Everything shared across this user's characters
          401:
            description: Missing, invalid or expired token
        """
        # NO slot parameter, deliberately. That absence is the fix.
        return account_of(g.user["id"]), 200

    def write_bank():
        """
        Replace the bank's contents
        ---
        tags:
          - Account
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
              required: [bank_inventory]
              properties:
                bank_inventory:
                  type: array
                  description: "Positional; null for an empty cell. At most 50 entries."
        responses:
          200:
            description: The account as stored
          400:
            description: Oversized array, or a malformed entry
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]
        ensure_account(user_id)

        cells = payload.get("bank_inventory")
        if not isinstance(cells, list):
            return bad_request("bank_inventory must be an array")
        if len(cells) > capacity:
            return bad_request("bank_inventory has %d entries, capacity is %d" % (len(cells), capacity))

        parsed, error = parse_positional_items(cells, "bank_inventory")
        if error is not None:
            return error

        # A PLAYER'S BANK IS NOT WRITTEN HERE ANY MORE, for the backpack's
        # reasons - see app.py's ONE CELL AT A TIME and write_inventory().
        # Items come and go through POST /api/bank/items, and rearranging is
        # POST /api/bank/move; a whole bank from the client was only ever
        # trimmed to the record (E-1), never derived from it. Ignored rather
        # than refused, with the bank the server holds in the answer; the owner
        # still writes it whole, for tooling.
        #
        # THE OWNER, NOT "MOD AND UP". A whole write can hold any item, so it
        # is an item grant by another name, and the grant itself is owner-only
        # now - see staff_grant(). A mod's bank is ignored exactly like a
        # player's.
        if not has_owner_powers(g.user["username"]):
            body = account_of(user_id)
            body["ignored"] = ["bank_inventory"]
            return body, 200

        db = get_db()
        data.replace_bank(db, user_id, parsed)
        db.commit()

        return account_of(user_id), 200

    def write_lusions():
        """
        Read the account's lusion balance (writes are ignored)
        ---
        tags:
          - Account
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
            required: false
            schema:
              type: object
              properties:
                lusions: {type: integer, example: 40}
        responses:
          200:
            description: The account as the SERVER holds it; any lusions sent are ignored
          401:
            description: Missing, invalid or expired token
        """
        # THIS ENDPOINT USED TO STORE WHATEVER ARRIVED, and that was the
        # finding.
        #
        #     lusions = parse_stat(payload.get("lusions"))
        #     UPDATE accounts SET lusions = ? WHERE user_id = ?
        #
        # An absolute figure, from the client, with no check beyond "is it a
        # non-negative integer". The same mistake as gold on the status
        # endpoint (E-8), one endpoint over.
        #
        # WHY IT MATTERED MORE THAN THE NUMBER SUGGESTS. Lusions have exactly
        # one sink in the game: reviving after death, at GameConstants.revive_cost
        # a go. So the balance IS the death penalty, and a client that could
        # write it could make dying free - which it did, because gameover.gd
        # deducted the cost locally and restored the health itself too.
        #
        # NOTHING HONEST NEEDS TO WRITE THIS ANY MORE. Lusions are created by
        # the server when a duplicate pet converts (see /api/loot/take) and
        # spent by the server at /api/character/revive. Both ends are now inside
        # the building.
        #
        # KEPT RATHER THAN DELETED, and answering 200. An un-updated client
        # still PUTs its lusion figure on a routine sync, and a 404 or a 400
        # would turn an honest save into an error the player sees. It reads
        # back the server's own number instead, which is also how the client
        # learns it was corrected.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]
        ensure_account(user_id)

        body = account_of(user_id)
        if "lusions" in payload:
            body["ignored"] = ["lusions"]
        return body, 200

    def move_bank_items():
        """
        Move an item between a character's backpack and the shared bank
        ---
        tags:
          - Account
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
              required: [slot, op, item_id]
              properties:
                slot:     {type: integer, example: 0}
                op:       {type: string,  enum: [deposit, withdraw]}
                item_id:  {type: string,  example: "tinyhealthpotion"}
                quantity: {type: integer, example: 5}
                position: {type: integer, example: 3, description: "The source cell dragged - a carried cell for a deposit, a bank cell for a withdrawal. Taken from first when it holds the item."}
        responses:
          200:
            description: Both grids afterwards, plus the account
          400:
            description: Bad slot, op, item id or quantity, or not enough on the source side
          404:
            description: That slot is empty
          409:
            description: The destination is full
          401:
            description: Missing, invalid or expired token
        """
        # CONSERVES THE TOTAL, which a whole-array replace cannot.
        #
        # The server holds both sides of this - carry_items and bank_items are
        # both its rows - so a transfer is the one shape where it can check
        # that what left one side is exactly what arrived at the other. PUT
        # /api/account/bank takes two arrays on trust and has no way to tell a
        # legal pair from an invented one; two arrays that do not add up look
        # exactly like two that do.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        if not slot_exists(user_id, slot):
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        op = str(payload.get("op", "")).strip().lower()
        if op not in OPS:
            return bad_request("op must be 'deposit' or 'withdraw'")

        item_id = str(payload.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return bad_request("item_id must be 1-64 characters")

        quantity, refusal = services.quantity_refusal(payload.get("quantity", 1), quantity_ceiling)
        if refusal is not None:
            return bad_request(refusal)

        position, error = parse_cell(payload, carry_capacity if op == "deposit" else capacity)
        if error is not None:
            return error

        db = get_db()

        if op == "deposit":
            source_rows = data.carry_rows(db, user_id, slot)
        else:
            source_rows = data.bank_rows(db, user_id)

        changes, available = take_from_cells(source_rows, item_id, quantity, prefer=position)
        if changes is None:
            where = "carried" if op == "deposit" else "banked"
            return bad_request(
                "Cannot move %d %s - only %d %s." % (quantity, item_id, available, where)
            )

        # REMOVE FIRST, THEN ADD, THEN CHECK. The add is the half that can fail
        # on capacity, and rolling back is how the item gets home - not an "if
        # it fits" test beforehand, which would be a second implementation of
        # the packing rule and would disagree with the real one the first time
        # a stack was part-used.
        if op == "deposit":
            apply_cell_changes("carry_items", ("user_id", "slot"), (user_id, slot), changes)
            written = add_to_bank(user_id, item_id, quantity)
            full_message = "The bank is full (%d slots)." % capacity
        else:
            apply_cell_changes("bank_items", ("user_id",), (user_id,), changes)
            written = add_to_backpack(user_id, slot, item_id, quantity)
            full_message = "Your backpack is full (%d slots)." % backpack_capacity

        if written is None:
            # Nothing was committed, so the rollback puts the source rows back
            # exactly as they were. The item is never in neither place.
            db.rollback()
            return {"error": "Conflict", "message": full_message}, 409

        db.commit()

        result = account_of(user_id)
        result["slot"] = slot
        result["op"] = op
        result["item_id"] = item_id
        result["quantity"] = quantity
        result["inventory"] = inventory_of(user_id, slot)
        return result, 200

    def move_bank_gold():
        """
        Move gold between a character's pocket and the shared bank
        ---
        tags:
          - Account
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
              required: [slot, op, amount]
              properties:
                slot:   {type: integer, example: 0}
                op:     {type: string,  enum: [deposit, withdraw]}
                amount: {type: integer, example: 500}
        responses:
          200:
            description: Carried gold and the account afterwards
          400:
            description: Bad slot, op or amount, or not enough gold on the relevant side
          404:
            description: That slot is empty
          401:
            description: Missing, invalid or expired token
        """
        # STILL TAKES A SLOT, and still an op rather than a replace.
        #
        # The slot is the CHARACTER whose pocket the gold comes out of - that
        # half is genuinely per-character. The bank half is not, and now comes
        # from accounts rather than from saves.bank_gold.
        #
        # It stays an op because this is the one operation on this whole server
        # that can be verified: it holds both balances, so it can enforce that
        # the total is conserved.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        op = str(payload.get("op", "")).strip().lower()
        if op not in OPS:
            return bad_request("op must be 'deposit' or 'withdraw'")

        amount = parse_stat(payload.get("amount"))
        if amount is None or amount <= 0:
            return bad_request("amount must be a positive integer")

        db = get_db()
        carried = data.purse(db, user_id, slot)
        if carried is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        account = ensure_account(user_id)
        carried, banked, refusal = services.transfer(op, amount, carried, int(account["bank_gold"]))
        if refusal is not None:
            return bad_request(refusal)

        data.set_balances(db, user_id, slot, carried, banked, int(time.time()))
        db.commit()

        result = account_of(user_id)
        result["slot"] = slot
        result["carried_gold"] = carried
        return result, 200

    def move_banked():
        """
        Move, merge or swap one bank cell onto another
        ---
        tags:
          - Account
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
              required: [from, to, item_id]
              properties:
                from:    {type: integer, example: 0}
                to:      {type: integer, example: 12}
                item_id: {type: string, example: ironsword, description: "What the game saw in `from`"}
        responses:
          200:
            description: The account afterwards, bank included. The same move, merge and swap rules as the backpack.
          400:
            description: Bad cell or item id
          409:
            description: "`from` does not hold that item. Carries the account under `account`; nothing moved."
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]
        ensure_account(user_id)
        item_id, error = parse_item_id(payload)
        if error is not None:
            return error
        source, error = parse_grid_cell(payload, "from", capacity)
        if error is not None:
            return error
        target, error = parse_grid_cell(payload, "to", capacity)
        if error is not None:
            return error
        if grid_move("bank_items", ("user_id",), (user_id,), source, target, item_id) == "stale":
            return resync(user_id)
        get_db().commit()
        return account_of(user_id), 200

    def discard_banked():
        """
        Destroy the whole stack in one bank cell
        ---
        tags:
          - Account
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
              required: [position, item_id]
              properties:
                position: {type: integer, example: 9}
                item_id:  {type: string, example: rawshrimp}
        responses:
          200:
            description: The account afterwards, and what was destroyed
          400:
            description: Bad cell or item id
          409:
            description: "The cell does not hold that item. Carries the account; nothing was destroyed."
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]
        ensure_account(user_id)
        item_id, error = parse_item_id(payload)
        if error is not None:
            return error
        position, error = parse_grid_cell(payload, "position", capacity)
        if error is not None:
            return error
        outcome, held = grid_discard("bank_items", ("user_id",), (user_id,), position, item_id)
        if outcome == "stale":
            return resync(user_id)
        get_db().commit()
        body = account_of(user_id)
        body["discarded"] = {"item_id": held[0], "quantity": held[1]}
        return body, 200

    app.add_url_rule("/api/account", endpoint="read_account",
                     view_func=require_auth(read_account), methods=["GET"])
    app.add_url_rule("/api/account/bank", endpoint="write_bank",
                     view_func=require_auth(write_bank), methods=["PUT"])
    app.add_url_rule("/api/account/lusions", endpoint="write_lusions",
                     view_func=require_auth(write_lusions), methods=["PUT"])
    app.add_url_rule("/api/bank/items", endpoint="move_bank_items",
                     view_func=require_auth(move_bank_items), methods=["POST"])
    app.add_url_rule("/api/bank/gold", endpoint="move_bank_gold",
                     view_func=require_auth(move_bank_gold), methods=["POST"])
    app.add_url_rule("/api/bank/move", endpoint="move_banked",
                     view_func=require_auth(move_banked), methods=["POST"])
    app.add_url_rule("/api/bank/discard", endpoint="discard_banked",
                     view_func=require_auth(discard_banked), methods=["POST"])

    return SimpleNamespace(account_of=account_of, add_to_bank=add_to_bank)
