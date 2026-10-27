"""carry/routes.py - the edge: the carry's four routes, as they were in app.py,
and the grants and grid steps every other area that moves an item is handed.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    parse_slot          a raw slot -> its int, or None
    max_slot            the highest slot, for the 400's words
    slot_exists         (user_id, slot) -> whether that character exists
    parse_grid_cell     (payload, key, capacity) -> (the cell, or None and a 400)
    parse_item_id       payload -> (the item it names, or None and a 400)
    has_owner_powers    username -> whether this is the owner (or a co-owner),
                        who alone may write a carry whole
    catalogue           gamedata: an item's row (how high it stacks), and the
                        lusion pile's id - asked on every call
    bag_cells           the bag's cells (INVENTORY_CAPACITY) - where a grant
                        lands, and what "your backpack is full" is about
    carry_cells         every carried cell, the hotbar's keys after the bag
                        (CARRY_CAPACITY)
    quantity_ceiling    the most of an item the server does not know a cell may
                        hold (QUANTITY_CEILING)
    take_resync         (db, user_id, slot) -> the trade's resync for this
                        carry, cleared as it is delivered, or None
    money_item_worth    item_id -> ("gold" | "lusions", each) or (None, 0):
                        what a carried pile cashes into
    lusions_item_id     () -> the lusion pile's id when the catalogue names none
    ensure_account      user_id -> the accounts row, made if it is missing -
                        asked on every call, so a suite's stand-in is heard
    gold_delta          (db, user_id, slot, delta, reason, detail) - the
                        carried gold, through the ledger
    lusion_delta        (db, user_id, slot, delta, reason, detail) - lusions
    account_of          user_id -> the account view, for the lusions a pile
                        leaves

register() hands back what the other areas move items with - the grants
(add_to_backpack, take_from_backpack, owns_item), the carry as the game holds
it (inventory_of, fingerprint), how high a cell stacks and what a positional
array may hold (stack_limit, parse_positional_items), the grid steps the bank
shares (take_from_cells, apply_cell_changes, grid_cells, grid_rewrite,
grid_move, grid_discard), and what a one-cell carry route opens and refuses
with (carry_request, resync) - the shop's sale is one. app.py keeps every one
callable under the name it always had.

THE ENDPOINT NAMES ARE app.py's - write_inventory, move_carried,
discard_carried, cash_carried_pile - and routes.lock holds them.
"""
from types import SimpleNamespace

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, slot_exists, parse_grid_cell,
             parse_item_id, has_owner_powers, catalogue, bag_cells, carry_cells, quantity_ceiling, take_resync,
             money_item_worth, lusions_item_id, ensure_account, gold_delta, lusion_delta, account_of):

    # ---- what a cell may hold ------------------------------------------------

    def stack_limit(item_id):
        """How many of item_id may sit in one cell."""
        return services.stack_limit(catalogue.item_row(item_id), quantity_ceiling)

    def parse_positional_items(cells, field):
        """A positional item array, the backpack's or the bank's: (parsed,
        None) or (None, an error response) - see services.positional_items()."""
        parsed, message = services.positional_items(cells, field, stack_limit)
        if message is not None:
            return None, bad_request(message)
        return parsed, None

    # ---- either grid, one cell at a time (the bank is handed these) ----------

    def apply_cell_changes(table, key_columns, key_values, changes):
        """Write back what take_from_cells() worked out. Zero means delete the row."""
        data.apply_cell_changes(get_db(), table, key_columns, key_values, changes)

    def grid_cells(table, key_columns, key_values, positions):
        """{position: (item_id, quantity)} for the named cells that hold anything."""
        return data.grid_cells(get_db(), table, key_columns, key_values, positions)

    def grid_rewrite(table, key_columns, key_values, positions, after):
        """Replace the named cells with `after` ({position: (item_id, quantity)})."""
        data.grid_rewrite(get_db(), table, key_columns, key_values, positions, after)

    def grid_move(table, key_columns, key_values, source, target, item_id):
        """
        The drag the game has always drawn, done on the server's copy: onto an
        empty cell it MOVES, onto the same stackable item it MERGES up to the stack
        limit (the rest stays where it was), onto anything else it SWAPS.

        Returns "moved", or "stale" when `source` does not hold `item_id` - see ONE
        CELL AT A TIME. Nothing is written for a stale move.
        """
        db = get_db()
        cells = data.grid_cells(db, table, key_columns, key_values, (source, target))
        moving = cells.get(source)
        if moving is None or moving[0] != item_id:
            return "stale"
        if source == target:
            return "moved"
        after = services.move_after(source, target, moving, cells.get(target), item_id, stack_limit(item_id))
        data.grid_rewrite(db, table, key_columns, key_values, (source, target), after)
        return "moved"

    def grid_discard(table, key_columns, key_values, position, item_id):
        """Destroy the whole stack in one cell. "discarded", or "stale" when the
        cell does not hold `item_id`. Returns the stack it removed as well."""
        db = get_db()
        held = data.grid_cells(db, table, key_columns, key_values, (position,)).get(position)
        if held is None or held[0] != item_id:
            return "stale", None
        data.grid_rewrite(db, table, key_columns, key_values, (position,), {})
        return "discarded", held

    # ---- the carry, and the server's grants ----------------------------------

    def inventory_of(user_id, slot):
        """Everything carried, as the game holds it - see services.cells_of()."""
        return services.cells_of(data.cells(get_db(), user_id, slot), carry_cells)

    def owns_item(user_id, slot, item_id):
        """
        True when this account already holds item_id in the backpack or the bank.

        Only pets ask. The bank is account-scoped and the backpack is per-character,
        which is deliberate: a pet is a collectible, and having caught one on your
        warrior should stop it dropping again for your mage - which is exactly what
        the bank half covers, since that is where a collection ends up.
        """
        return data.owns(get_db(), user_id, slot, item_id)

    def take_from_backpack(user_id, slot, item_id, quantity, prefer=None, keys_last=False):
        """
        Remove quantity of item_id from the backpack. Returns True, or False when
        the player does not hold that many - in which case NOTHING is written.

        The mirror of add_to_backpack() below, and all-or-nothing for the same
        reason: a half-completed consume is an ingredient that left the bag without
        producing anything. Which cells go first - the highest, the one the player
        pointed at (`prefer`), the bag before the keys (`keys_last`) - is
        services.backpack_take().
        """
        quantity = int(quantity)
        if quantity <= 0:
            return True

        db = get_db()
        changes = services.backpack_take(data.held(db, user_id, slot, item_id), quantity, prefer, keys_last,
                                         bag_cells)
        if changes is None:
            return False
        data.take(db, user_id, slot, changes)
        return True

    def add_to_backpack(user_id, slot, item_id, quantity):
        """
        Put quantity of item_id into the backpack the way the CLIENT would, and
        return the cells touched - or None when it does not fit, having written
        nothing. Where it lands - existing stacks topped up first, the bag only,
        never a hotbar key - is services.placement().
        """
        db = get_db()
        occupied = {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in data.cells(db, user_id, slot)}
        writes = services.placement(occupied, item_id, quantity, catalogue.item_row(item_id), bag_cells)
        if writes is None:
            return None
        data.put(db, user_id, slot, writes)
        return [position for position, _, _ in writes]

    def starter_keys(user_id, slot, items):
        """A NEW CHARACTER'S STARTER KIT on its first hotbar keys (game 0.92.0):
        [(item_id, quantity)], one key each from the first, onto empty keys
        only, each held to the item's stack. The one place anything is put on
        a key, and only for a character the save route has just made - whose
        keys nobody has chosen yet. Returns the keys written."""
        db = get_db()
        taken = {int(r["position"]) for r in data.cells(db, user_id, slot)}
        writes = []
        position = bag_cells
        for item_id, quantity in items:
            if position >= carry_cells or catalogue.item_row(item_id) is None:
                continue
            while position in taken and position < carry_cells:
                position += 1
            if position >= carry_cells:
                break
            writes.append((position, item_id, min(int(quantity), stack_limit(item_id))))
            position += 1
        data.put(db, user_id, slot, writes)
        return [p for p, _, _ in writes]

    # ---- the routes ----------------------------------------------------------

    def write_inventory():
        """
        Replace a character's backpack
        ---
        tags:
          - Character
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
              required: [slot, inventory]
              properties:
                slot: {type: integer, example: 0}
                inventory:
                  type: array
                  description: "Positional; null for an empty cell. Cells 0-19 are the bag, 20-29 the hotbar's keys. At most 30 entries. The bag is always replaced; a hotbar cell past the end of a shorter array is left as it is."
                based_on:
                  type: string
                  description: "bag_fingerprint() of the bag the client last had from the server when it built this one. Optional; when it no longer matches, the write is refused with 409."
        responses:
          200:
            description: The backpack as stored
          409:
            description: "The bag changed on the server since this was built (a trade, or any route that changes the carry). Carries resync: the bag and purse the server holds."
          400:
            description: Bad slot, oversized array, or a malformed entry
          404:
            description: That slot is empty
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

        # A BAG BUILT BEFORE A TRADE MOVED THINGS IS NOT A BAG TO SAVE. This route
        # replaces the whole backpack, so a client still showing what it had before
        # a trade finished would delete what the trade just gave it - reproduced,
        # not supposed: accept first, let the other side accept, drag a potion, and
        # the sword you traded for was gone. See _mark_resync().
        #
        # REFUSED, AND THE REFUSAL CARRIES THE ANSWER. The 409 hands back the bag
        # and purse the server holds so the client can adopt them in the same
        # breath; the flag is cleared because the truth has now been delivered. A
        # write that arrives after that is built on the new bag and goes through.
        stale = take_resync(get_db(), user_id, slot)
        if stale is not None:
            return {"error": "Conflict",
                    "message": "Your backpack changed on the server - a trade finished. "
                               "It has been reloaded.",
                    "resync": stale}, 409
        # A BAG BUILT BEFORE ANY OTHER CHANGE IS NOT A BAG TO SAVE EITHER. The same
        # rule as the trade above, for every route that changes the carry: a loot
        # take, a cook, a catch, a purchase, an equip. Day 1: cooking a stack of
        # twelve, a save built after cook five and sent during cook six landed
        # after it, deleted the fish cook six had made (a loss, so it went
        # through), and the next save's copy of that fish was then trimmed as a
        # gain. One cooked fish in every few was gone.
        #
        # `based_on` is the fingerprint of the bag the client last had from the
        # server when it built this one (bag_fingerprint()). Anything else on the
        # server since means this write would undo it, so it is refused with the
        # bag the server holds, the same 409 the trade rule sends. A client from
        # before the field sends none and is let through as before.
        based_on = payload.get("based_on")
        if based_on is not None:
            if not isinstance(based_on, str) or len(based_on) > 64:
                return bad_request("based_on must be a bag fingerprint")
            held = inventory_of(user_id, slot)
            if based_on != services.fingerprint(held):
                gold = data.purse(get_db(), user_id, slot) or 0
                return {"error": "Conflict",
                        "message": "Your backpack changed on the server since this was saved. "
                                   "It has been reloaded.",
                        "resync": {"slot": slot, "gold": gold,
                                   "inventory": held, "trade": None, "reason": "stale_save"}}, 409

        cells = payload.get("inventory")
        if not isinstance(cells, list):
            return bad_request("inventory must be an array")
        if len(cells) > carry_cells:
            return bad_request("inventory has %d entries, capacity is %d" % (len(cells), carry_cells))

        # THE HOTBAR IS REPLACED ONLY BY AN ARRAY THAT REACHES IT. The bag cells
        # are always replaced, exactly as before the hotbar held items - a short
        # array, [] included, still means "the bag holds this and nothing else".
        # The hotbar's cells are replaced only as far as the array goes; past its
        # end they are left as they are, and left out of the ledger the claim is
        # checked against.
        #
        # A client that sends twenty cells - one written before the keys held
        # items, or one whose hotbar was not attached when it saved - is not saying
        # "empty my hotbar", it is saying nothing about it. Replacing every row
        # would delete up to ten stacks the player can see on screen.
        #
        # AND THE LEDGER HAS TO SHRINK WITH IT, or the rule becomes a duplication
        # bug: five potions on a key the write does not touch, plus a claim of five
        # potions in the bag checked against a ledger that still counts the key's
        # five, is ten potions. Counting only the covered cells means a write can
        # rearrange what it covers and nothing else.
        covered = max(len(cells), bag_cells)

        # THE SHARED VALIDATOR, not a second copy of it. This loop used to be
        # written out here as well as in _parse_positional_items(), twenty identical
        # lines in two places - so a rule added to one of them (the stack ceiling
        # was exactly that) would silently not apply to the other.
        #
        # It validates the whole array before the caller writes any of it: a bad
        # entry at index 17 must not leave the first seventeen stored.
        items, error = parse_positional_items(cells, "inventory")
        if error is not None:
            return error

        # A PLAYER'S BAG IS NOT WRITTEN HERE ANY MORE - see ONE CELL AT A TIME.
        # Every change a player makes is its own request now (move, discard, use a
        # pile, and the routes that always were: loot, the bank, the shop, trades,
        # equip, consume, catches and cooking), so this array is never the truth
        # about anything. It used to be trimmed to what the server had granted
        # (E-1), which caught a gain and took the arrangement and every loss on
        # the client's word.
        #
        # IGNORED, NOT REFUSED, like gold and lusions before it: a game from before
        # the change still sends its whole bag on a routine save, and a 400 would
        # turn that into an error on screen. It gets the bag the server holds, and
        # `ignored` says why its own did not stick.
        #
        # THE OWNER STILL WRITES IT WHOLE, for tooling and tests. It used to be
        # any staff rank, on the reasoning that a mod could already put anything
        # in a bag with POST /api/staff/grant, so leaving this open closed no door.
        # That held only while the grant was mod and up. The grant is owner-only
        # now (6 Oct 2026), so this is too: otherwise a mod denied the grant would
        # write the same Perfect mythic in here instead. A mod's bag is ignored
        # exactly like a player's.
        if not has_owner_powers(g.user["username"]):
            return {"slot": slot, "inventory": inventory_of(user_id, slot),
                    "ignored": ["inventory"]}, 200

        parsed = [(user_id, slot, index, item_id, quantity)
                  for index, item_id, quantity in items]

        db = get_db()
        # DELETE then INSERT, in one transaction - see data.replace().
        data.replace(db, user_id, slot, covered, parsed)
        db.commit()

        return {"slot": slot, "inventory": inventory_of(user_id, slot)}, 200

    def resync(user_id, slot, reason="stale_save"):
        """The 409 a carry route sends when the client's picture is out of date:
        the bag and purse the server holds, in the shape apply_server_carry()
        already adopts from a stale save."""
        return {"error": "Conflict",
                "message": "Your backpack changed on the server. It has been reloaded.",
                "resync": {"slot": slot, "gold": data.purse(get_db(), user_id, slot) or 0,
                           "inventory": inventory_of(user_id, slot), "trade": None,
                           "reason": reason}}, 409

    def carry_request():
        """slot and item_id, shared by every one-cell carry route: (payload, slot,
        item_id, None) or (..., an error response)."""
        payload = request.get_json(silent=True) or {}
        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return payload, None, None, bad_request("slot must be an integer 0-%d" % max_slot)
        if not slot_exists(g.user["id"], slot):
            return payload, None, None, ({"error": "Not Found",
                                          "message": "No character in slot %d." % slot}, 404)
        item_id, error = parse_item_id(payload)
        return payload, slot, item_id, error

    def move_carried():
        """
        Move, merge or swap one carried cell onto another
        ---
        tags:
          - Character
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
              required: [slot, from, to, item_id]
              properties:
                slot:    {type: integer, example: 0}
                from:    {type: integer, example: 3, description: "The cell dragged (0-19 bag, 20-29 hotbar)"}
                to:      {type: integer, example: 21, description: "The cell it was dropped on"}
                item_id: {type: string, example: smallhealthpotion, description: "What the game saw in `from`"}
        responses:
          200:
            description: "The carry afterwards: onto an empty cell it moved, onto the same stackable item it merged up to the stack limit, onto anything else it swapped"
          400:
            description: Bad slot, cell or item id
          404:
            description: That slot is empty
          409:
            description: "`from` does not hold that item - the game's picture is out of date. Carries resync: the bag the server holds. Nothing moved."
          401:
            description: Missing, invalid or expired token
        """
        payload, slot, item_id, error = carry_request()
        if error is not None:
            return error
        source, error = parse_grid_cell(payload, "from", carry_cells)
        if error is not None:
            return error
        target, error = parse_grid_cell(payload, "to", carry_cells)
        if error is not None:
            return error
        user_id = g.user["id"]
        if grid_move("carry_items", ("user_id", "slot"), (user_id, slot),
                     source, target, item_id) == "stale":
            return resync(user_id, slot)
        get_db().commit()
        return {"slot": slot, "inventory": inventory_of(user_id, slot)}, 200

    def discard_carried():
        """
        Destroy the whole stack in one carried cell
        ---
        tags:
          - Character
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
              required: [slot, position, item_id]
              properties:
                slot:     {type: integer, example: 0}
                position: {type: integer, example: 7}
                item_id:  {type: string, example: rawshrimp, description: "What the game saw in that cell"}
        responses:
          200:
            description: The carry afterwards, and what was destroyed
          400:
            description: Bad slot, cell or item id
          404:
            description: That slot is empty
          409:
            description: "The cell does not hold that item. Carries resync; nothing was destroyed."
          401:
            description: Missing, invalid or expired token
        """
        payload, slot, item_id, error = carry_request()
        if error is not None:
            return error
        position, error = parse_grid_cell(payload, "position", carry_cells)
        if error is not None:
            return error
        user_id = g.user["id"]
        outcome, held = grid_discard("carry_items", ("user_id", "slot"), (user_id, slot),
                                     position, item_id)
        if outcome == "stale":
            return resync(user_id, slot)
        get_db().commit()
        return {"slot": slot, "inventory": inventory_of(user_id, slot),
                "discarded": {"item_id": held[0], "quantity": held[1]}}, 200

    def cash_carried_pile():
        """
        Turn a carried pile of gold coins, or of lusions, into the balance
        ---
        tags:
          - Character
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
              required: [slot, position, item_id]
              properties:
                slot:     {type: integer, example: 0}
                position: {type: integer, example: 4}
                item_id:  {type: string, example: goldcoin}
        responses:
          200:
            description: The carry, the purse and the lusions afterwards
          400:
            description: Bad slot, cell or item id
          404:
            description: That slot is empty
          409:
            description: "That is not money, or the cell does not hold it (then with resync)"
          401:
            description: Missing, invalid or expired token
        """
        # THE ONE CLIENT PATH THAT USED TO ADD GOLD LOCALLY. inventoryscreen.gd
        # added the pile's worth to its own purse and emptied the cell; the server
        # has ignored a client's gold since E-8, so the cell's loss was saved and
        # the gold was not - using a pile destroyed it. Now the server empties the
        # cell and credits the balance in one transaction, through the ledgers.
        # Piles reach a bag only through a staff grant or a trade of one.
        payload, slot, item_id, error = carry_request()
        if error is not None:
            return error
        position, error = parse_grid_cell(payload, "position", carry_cells)
        if error is not None:
            return error
        premium = catalogue.CONSTANTS.get("lusions_item_id", lusions_item_id())
        currency, per = money_item_worth(item_id)
        if currency is None:
            return {"error": "Conflict", "message": "That is not money."}, 409

        user_id = g.user["id"]
        db = get_db()
        outcome, held = grid_discard("carry_items", ("user_id", "slot"), (user_id, slot),
                                     position, item_id)
        if outcome == "stale":
            return resync(user_id, slot)
        worth = int(held[1]) * per
        detail = "%s x%d" % (item_id, held[1])
        if item_id == premium:
            ensure_account(user_id)
            lusion_delta(db, user_id, slot, worth, "pile", detail)
        else:
            gold_delta(db, user_id, slot, worth, "pile", detail)
        db.commit()

        gold = data.purse(db, user_id, slot) or 0
        account = account_of(user_id)
        return {"slot": slot, "inventory": inventory_of(user_id, slot),
                "gold": gold, "lusions": int(account.get("lusions", 0)),
                "cashed": {"item_id": item_id, "quantity": held[1],
                           "currency": "lusions" if item_id == premium else "gold",
                           "amount": worth}}, 200

    app.add_url_rule("/api/character/inventory", endpoint="write_inventory",
                     view_func=require_auth(write_inventory), methods=["PUT"])
    app.add_url_rule("/api/character/inventory/move", endpoint="move_carried",
                     view_func=require_auth(move_carried), methods=["POST"])
    app.add_url_rule("/api/character/inventory/discard", endpoint="discard_carried",
                     view_func=require_auth(discard_carried), methods=["POST"])
    app.add_url_rule("/api/character/inventory/cash", endpoint="cash_carried_pile",
                     view_func=require_auth(cash_carried_pile), methods=["POST"])

    return SimpleNamespace(
        stack_limit=stack_limit, parse_positional_items=parse_positional_items,
        fingerprint=services.fingerprint, inventory_of=inventory_of, owns_item=owns_item,
        take_from_backpack=take_from_backpack, add_to_backpack=add_to_backpack, starter_keys=starter_keys,
        take_from_cells=services.take_from_cells, apply_cell_changes=apply_cell_changes,
        grid_cells=grid_cells, grid_rewrite=grid_rewrite, grid_move=grid_move, grid_discard=grid_discard,
        resync=resync, carry_request=carry_request)
