"""loot/routes.py - the edge: the two routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    parse_stat          a raw cell number -> an int 0 or more, or None
    item_row            an item_id -> its catalogue row, or None
    owns_item           (user_id, slot, item_id) -> whether this account holds
                        it, in this character's bag or the shared bank
    lusions_item_id     what a duplicate pet turns into
    dupe_pet_lusions    () -> how many lusions it turns into (asked each time:
                        the catalogue's constant)
    gold_item_value     an item_id -> what one coin of it is worth, or 0
    gold_delta          (db, user_id, slot, delta, reason, detail) - the purse
                        and the gold ledger, in one transaction
    lusion_delta        (db, user_id, slot, delta, reason, detail) - the same
                        for lusions
    ensure_account      user_id -> the account row, made if missing
    add_to_backpack     (user_id, slot, item_id, quantity) -> the cells
                        written, or None having written nothing
    place_trade_hold    (db, user_id, item_id, quantity) - a mythic or a
                        perfect held from trade from the moment it lands
    status_of           (user_id, slot) -> the status block
    inventory_of        (user_id, slot) -> the carry, cell by cell
    bag_cells           the backpack's size, for "Your backpack is full"

THE ENDPOINT NAMES ARE app.py's - take_loot, read_loot_bag - and routes.lock
holds them.
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_stat, item_row, owns_item,
             lusions_item_id, dupe_pet_lusions, gold_item_value, gold_delta, lusion_delta,
             ensure_account, add_to_backpack, place_trade_hold, status_of, inventory_of, bag_cells):

    def take_loot():
        """
        Take one item out of a loot bag
        ---
        tags:
          - Loot
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
              required: [bag_id, position]
              properties:
                bag_id:   {type: string,  example: k3Jx9_QpZ2mNvRt1}
                position: {type: integer, example: 0}
        responses:
          200:
            description: What was taken, and the state it landed in
          400:
            description: Bad bag_id or position
          404:
            description: No such bag, not yours, or that cell is already empty
          409:
            description: Backpack full - the item stays in the bag
          410:
            description: The bag has expired
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        bag_id = str(payload.get("bag_id", "")).strip()
        if not bag_id or len(bag_id) > 64:
            return bad_request("bag_id must be 1-64 characters")

        # Bounded by the BAG's capacity, not the backpack's. They were the same
        # number by accident, and a cell index from a 6-cell grid validated
        # against a 20-cell one is a check that reads as if it is doing
        # something.
        position = parse_stat(payload.get("position"))
        if position is None or position >= services.LOOT_BAG_CAPACITY:
            return bad_request("position must be an integer 0-%d" % (services.LOOT_BAG_CAPACITY - 1))

        db = get_db()

        bag = data.bag_of(db, bag_id, user_id)
        if bag is None:
            return {"error": "Not Found", "message": "No such loot bag."}, 404

        if services.expired(bag["created_at"], time.time()):
            data.drop_bag(db, bag_id)
            db.commit()
            return {"error": "Gone", "message": "That loot bag has expired."}, 410

        item = data.cell(db, bag_id, position)
        if item is None:
            # Already taken, or never there. Same answer either way, and it is
            # the answer that makes a duplicate request harmless: the second
            # one finds nothing and changes nothing.
            return {"error": "Not Found", "message": "Nothing in that slot of the bag."}, 404

        slot = int(bag["slot"])
        item_id = item["item_id"]
        quantity = int(item["quantity"])
        definition = item_row(item_id) or {}
        kind = definition.get("type_name", "")

        # WHAT WAS IN THE BAG vs WHAT THE PLAYER ACTUALLY GETS. Those are the
        # same thing for everything except a pet you already own, and the
        # client needs both: item_id/quantity to know which cell to clear,
        # granted_* to know what to say in the notice.
        result = {"bag_id": bag_id, "position": position, "item_id": item_id, "quantity": quantity}
        granted_id = item_id
        granted_qty = quantity

        # A DUPLICATE PET BECOMES LUSIONS, AND THAT DECISION MOVED HERE.
        #
        # lootbaginventory.gd used to make it, in _load_contents(), by checking
        # the local inventory and the local bank. Both of those are now views
        # of rows this process owns, so the client was checking a copy to
        # decide what a pet was worth - and a client that decides what it is
        # owed is the thing this whole endpoint exists to stop.
        #
        # The panel still DISPLAYS the substitution so the player sees lusions
        # in the bag rather than a pet that turns into lusions. That is
        # cosmetic. This is the grant.
        if kind == "PET" and owns_item(user_id, slot, item_id):
            kind = "CURRENCY"
            granted_id = lusions_item_id
            granted_qty = int(dupe_pet_lusions())
            result["duplicate_pet"] = True

        if kind == "CURRENCY":
            # CURRENCY RESOLVES INTO A BALANCE RATHER THAN A BACKPACK CELL. This
            # is the whole point of the exercise: gold entering the game now
            # goes through a statement the server wrote, against a bag the
            # server rolled.
            coin_value = gold_item_value(granted_id)
            if coin_value > 0:
                # QUANTITY TIMES VALUE, not the quantity. Ten silver coins at
                # 50 each is 500 gold; crediting 10 would be the bug this line
                # exists to not have. See app.py's gold_item_value().
                credited = granted_qty * coin_value

                # THE ONLY PLACE GOLD IS CREATED. Through gold_delta() rather
                # than a bare UPDATE so the mint lands in the ledger in the same
                # transaction as the balance - see gold_ledger in init_db(). If
                # a second mint site is ever added it goes through there too, or
                # the invariant stops meaning anything.
                gold_delta(
                    db, user_id, slot, credited,
                    "loot", "%s x%d from bag %s" % (granted_id, granted_qty, bag_id),
                )
                result["credited"] = "gold"
                result["gold_amount"] = credited
            else:
                # Lusions, and anything else account-scoped that shows up later.
                #
                # THE ONLY PLACE LUSIONS ARE CREATED, and it goes through
                # lusion_delta() so the mint lands in lusion_ledger in the same
                # transaction as the balance - the same discipline the gold mint
                # two branches up has followed since the ledger existed.
                ensure_account(user_id)
                lusion_delta(db, user_id, slot, granted_qty, "duplicate_pet",
                             "%s converted from bag %s" % (granted_id, bag_id))
                result["credited"] = "lusions"
        else:
            written = add_to_backpack(user_id, slot, granted_id, granted_qty)
            if written is None:
                # 409, and the item stays where it is. Refusing beats dropping
                # it on the floor: the player can make room and ask again.
                # Nothing was written - add_to_backpack is all-or-nothing.
                return {
                    "error": "Conflict",
                    "message": "Your backpack is full (%d slots)." % bag_cells,
                }, 409

            result["credited"] = "inventory"
            result["carry_positions"] = written
            # A MYTHIC OR A PERFECT IS HELD FROM TRADE for TRADE_HOLD_SECONDS
            # from the moment it lands - see trade_holds in init_db().
            place_trade_hold(db, user_id, granted_id, granted_qty)

        result["granted_item_id"] = granted_id
        result["granted_quantity"] = granted_qty

        # Removed only after it has landed somewhere. If the insert above had
        # failed the transaction carries the delete with it, so an item cannot
        # evaporate between the two.
        data.empty_cell(db, bag_id, position)

        remaining = data.left_in(db, bag_id)
        if remaining == 0:
            data.drop_bag(db, bag_id)

        db.commit()

        result["bag_empty"] = remaining == 0

        # THE BALANCES GO BACK AS TOTALS, NOT AS THE DELTA THAT WAS JUST
        # APPLIED.
        #
        # The client still pushes `gold` on every save, so a client that added
        # the delta to its own figure and got it wrong once would overwrite this
        # row with the wrong number on the very next save - and the loss would
        # look like nothing at all. Handing back the balance it landed on means
        # a client that misses a response is corrected by the next one it does
        # get.
        result["status"] = status_of(user_id, slot)
        result["inventory"] = inventory_of(user_id, slot)
        result["lusions"] = int(ensure_account(user_id)["lusions"])
        return result, 200

    def read_loot_bag():
        """
        What is still in a loot bag
        ---
        tags:
          - Loot
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: query
            name: bag_id
            type: string
            required: true
        responses:
          200:
            description: The bag's remaining contents
          404:
            description: No such bag, or not yours
          401:
            description: Missing, invalid or expired token
        """
        # Exists so a client that reconnects, or one that is unsure whether a
        # take landed, can ask rather than guess. A bag the server has already
        # emptied is a 404, which is the same answer as never having existed -
        # and the right one, because in both cases there is nothing to collect.
        bag_id = str(request.args.get("bag_id", "")).strip()
        if not bag_id:
            return bad_request("bag_id is required")

        db = get_db()
        bag = data.bag_of(db, bag_id, g.user["id"])
        if bag is None:
            return {"error": "Not Found", "message": "No such loot bag."}, 404

        return services.contents_answer(bag_id, bag, data.contents(db, bag_id)), 200

    app.add_url_rule("/api/loot/take", endpoint="take_loot",
                     view_func=require_auth(take_loot), methods=["POST"])
    app.add_url_rule("/api/loot/bag", endpoint="read_loot_bag",
                     view_func=require_auth(read_loot_bag), methods=["GET"])
