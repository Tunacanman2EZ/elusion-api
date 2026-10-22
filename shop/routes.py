"""shop/routes.py - the edge: buying from a vendor, selling to one, and what a
vendor stocks, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth      the token check (puts the account on g.user)
    get_db            the request's database connection
    bad_request       a message -> app.py's 400
    parse_slot        a raw slot -> its int, or None
    max_slot          the highest slot, for the 400's words
    slot_exists       (user_id, slot) -> whether that character exists
    catalogue         gamedata: the shops, what each stocks and charges and
                      pays, every item's row, and the roll a piece of gear is
                      handed over with - asked on every call, so a suite's
                      seeded die is heard
    stack_limit       item_id -> how many may sit in one cell (carry/'s)
    add_to_backpack   (user_id, slot, item_id, quantity) -> the cells written,
                      or None having written nothing (carry/'s grant)
    bag_cells         the bag's cells, for "your backpack is full"
    place_trade_hold  (db, user_id, item_id, quantity) - hold a fresh find of
                      a held kind from trade, as a drop is (trade/'s)
    gold_delta        (db, user_id, slot, delta, reason, detail) -> the new
                      balance: the carried gold, through the ledger
    inventory_of      (user_id, slot) -> the carry, cell by cell
    carry_request     () -> (payload, slot, item_id, None) or an error: what a
                      one-cell carry route opens with (carry/'s)
    parse_grid_cell   (payload, key, capacity) -> (the cell, or None and a 400)
    carry_cells       every carried cell, the hotbar's keys after the bag
    grid_cells        (table, key columns, key values, positions) -> what the
                      named cells hold (carry/'s)
    grid_rewrite      (table, key columns, key values, positions, after) - the
                      named cells replaced (carry/'s)
    resync            (user_id, slot) -> the 409 with the carry and the purse
                      the server holds (carry/'s)
    terms             (db, shop_id) -> the owner's numbers for this shop's
                      prices, as keyword arguments for the catalogue's three
                      pricing functions - {} when the owner has set none
                      (economyrules/, 0.74.0). Left out, every price is the
                      catalogue's own, as it was before the owner could set one.

THE ENDPOINT NAMES ARE app.py's - shop_buy, shop_sell, shop_catalogue - and
routes.lock holds them.
"""
from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, slot_exists, catalogue, stack_limit,
             add_to_backpack, bag_cells, place_trade_hold, gold_delta, inventory_of, carry_request, parse_grid_cell,
             carry_cells, grid_cells, grid_rewrite, resync, terms=None):

    # THE OWNER'S NUMBERS (0.74.0). The owner, of the fifth economy idea:
    # "Today, changing the trade tax or a price means a new version. If those
    # numbers lived on the server, you could adjust them in a minute." They do
    # now - economyrules/ - and every price below is asked with them: the till,
    # the buy-back and the shelf the panel draws, so the three can never
    # disagree. Asked per request, from economyrules/'s short cache, so a change
    # is at the till within its window. Nothing set, nothing passed: the
    # catalogue's own number, exactly as before.
    def shop_terms(db, shop_id):
        if terms is None:
            return {}
        return dict(terms(db, shop_id) or {})

    def shop_buy():
        """
        ---
        post:
          summary: Buy from a vendor
          description: >
            Server-authoritative purchase. The server checks the shop stocks the
            item, prices it from its OWN ItemData copy, takes the gold and grants
            the item in one transaction. The gold is DESTROYED, not moved - this is
            the first real sink in the economy.
          requestBody:
            content:
              application/json:
                schema:
                  type: object
                  required: [slot, shop_id, item_id]
                  properties:
                    slot:     {type: integer, example: 0}
                    shop_id:  {type: string,  example: generalstore}
                    item_id:  {type: string,  example: tinyhealthpotion}
                    quantity: {type: integer, example: 1}
          responses:
            200:
              description: >
                Bought. Returns the new gold balance and backpack. item_id is the
                piece that arrived - for gear, rolled on the spot ("jadesword~d104p96");
                stock_id is the shelf's id it was bought as. free is true for what
                the shop gives away: the plain piece, at no cost, one each.
            400:
              description: >
                Bad slot, unknown shop, item not stocked, not enough gold, or a
                free piece this character already has (carried, worn or banked)
            404:
              description: That slot is empty
            409:
              description: Backpack full
            401:
              description: Missing, invalid or expired token
        """
        # WHY THIS EXISTS AT ALL, rather than the client taking gold and adding an
        # item locally. The old bag ledger trimmed a client holding MORE of an
        # item than the server granted - but a client-side shop asserts both halves
        # of the trade, the item gained AND the gold spent, and the trim only
        # checks the first. A shop that ran on the client would be an item printer
        # with a receipt.
        #
        # It is also the sink. Every gold that leaves here leaves the economy for
        # good, which is the whole reason vendors sell rather than buy.
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        if not slot_exists(user_id, slot):
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        shop_id = str(payload.get("shop_id", "")).strip()
        if not shop_id or len(shop_id) > 64:
            return bad_request("shop_id must be 1-64 characters")

        item_id = str(payload.get("item_id", "")).strip()
        if not item_id or len(item_id) > 64:
            return bad_request("item_id must be 1-64 characters")

        raw_quantity = payload.get("quantity", 1)
        if isinstance(raw_quantity, bool):
            return bad_request("quantity must be a positive integer")
        try:
            quantity = int(raw_quantity)
        except (TypeError, ValueError):
            return bad_request("quantity must be a positive integer")
        if quantity <= 0:
            return bad_request("quantity must be a positive integer")

        # The same ceiling an ordinary write gets. One cell cannot hold more than
        # the item stacks to, and a purchase is not a reason to create a row that
        # breaks that rule everywhere else in the game.
        limit = stack_limit(item_id)
        if quantity > limit:
            return bad_request(
                "quantity is %d, the most %s stacks to is %d" % (quantity, item_id, limit)
            )

        if shop_id not in catalogue.SHOPS:
            # Named back deliberately, the same way an unknown enemy_id is: this is
            # the client and the server disagreeing about the world, which almost
            # always means gamedata.json is stale on one side.
            return bad_request("Unknown shop '%s'. Is gamedata.json current?" % shop_id)

        # PRICED BY THE SERVER, FROM THE SERVER'S CATALOGUE - at the owner's
        # markup when one is set (0.74.0). The request does not carry a price
        # and would not be believed if it did.
        db = get_db()
        unit_price = catalogue.shop_price(shop_id, item_id, **shop_terms(db, shop_id))
        if unit_price is None:
            return bad_request("'%s' does not stock '%s'." % (shop_id, item_id))

        total = unit_price * quantity

        # WHAT THE SHOP GIVES AWAY (its "free" list, 0.69.0): price 0, one each,
        # the plain piece. See gamedata.is_free() for why that is not a faucet.
        free = item_id in (catalogue.SHOPS[shop_id].get("free") or [])

        if free:
            name = (catalogue.item_row(item_id) or {}).get("display_name", item_id)
            if quantity != 1:
                return bad_request("The shop gives one %s to each character, not %d." % (name, quantity))
            # ONE EACH, counting the bag, what is worn and the account's bank.
            # Under the write lock every write route takes (app.py, THE WRITE
            # LOCK), so two claims at once cannot both read "none".
            where = data.where_held(db, user_id, slot, item_id)
            if where is not None:
                return bad_request(services.already_words(name, where))

        carried = data.purse(db, user_id, slot)
        if carried < total:
            return bad_request(
                "That costs %d gold and you have %d." % (total, carried)
            )

        # ---- everything above this line is validation; below it commits --------

        # THE ITEM FIRST, because this is the half that can still fail. It writes
        # nothing unless the whole quantity fits, so a refusal here leaves the
        # backpack exactly as it was and the gold untouched.
        # GEAR IS ROLLED AT THE TILL. The owner, 5 Oct: "item stats say ? and are
        # revealed upon buying in shop only". The shelf lists the catalogue piece
        # and its price; what is handed over rolls exactly as a drop does
        # (gamedata.roll_quality: 85-115% a stat, one in QUALITY_PERFECT_ODDS
        # Perfect). Same price whatever it rolls, so an average purchase is the
        # catalogue piece, as it was. A potion, a worm or a rod has nothing to roll
        # and comes as it is. Gear stacks to one, so quantity is 1 here.
        #
        # A FREE PIECE IS NOT ROLLED. The plain id is what marks it as the free
        # one (gamedata.is_free()), and a roll would make it a drop that sells;
        # it also means throwing it away and taking another re-rolls nothing.
        bought = item_id if free else catalogue.roll_quality(item_id)

        written = add_to_backpack(user_id, slot, bought, quantity)
        if written is None:
            db.rollback()
            return {
                "error": "Conflict",
                "message": "Your backpack is full (%d slots)." % bag_cells,
            }, 409

        if free:
            # NOTHING TO BURN AND NOTHING TO HOLD. gold_delta() writes no zero
            # (it answers None for one), and a free piece never trades, so a
            # trade hold on it would only be a row to prune.
            remaining = data.purse(db, user_id, slot)
        else:
            # A PERFECT FROM THE SHOP IS HELD FROM TRADE like one from a bag:
            # gold from cheated kills buys it as well as anything.
            place_trade_hold(db, user_id, bought, quantity)

            # THE BURN. Negative delta, through gold_delta() so it lands in the
            # ledger in the same transaction as the balance - see gold_ledger in
            # init_db(). This is gold leaving the world, not moving to a
            # vendor's pocket: nothing holds it afterwards, which is what makes
            # it a sink.
            remaining = gold_delta(
                db, user_id, slot, -total, "shop_buy",
                "%s x%d @%d from %s" % (bought, quantity, unit_price, shop_id),
            )

        db.commit()

        return {
            "slot": slot,
            "shop_id": shop_id,
            "item_id": bought,
            "stock_id": item_id,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_paid": total,
            "free": free,
            "gold": remaining,
            "inventory": inventory_of(user_id, slot),
        }, 200

    def shop_sell():
        """
        Sell what is in one carried cell to a vendor
        ---
        tags:
          - Shop
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
              required: [slot, shop_id, position, item_id]
              properties:
                slot:     {type: integer, example: 0}
                shop_id:  {type: string,  example: generalstore}
                position: {type: integer, example: 4, description: "The carried cell"}
                item_id:  {type: string,  example: jadesword, description: "What the game saw in that cell"}
                quantity: {type: integer, example: 1, description: "How many from the cell; the whole stack when left out"}
        responses:
          200:
            description: Sold. The carry and the purse afterwards, and what was paid.
          400:
            description: Bad slot, cell, item, quantity or shop, or the shop does not buy it
          404:
            description: That slot is empty
          409:
            description: "The cell does not hold that item, or not that many. Carries resync; nothing was sold."
          401:
            description: Missing, invalid or expired token
        """
        # THE SHOP BUYS NOW (5 Oct, the owner: "we should be able to sell items to
        # the shop"). Every piece of a tier was the same piece, so a second jade
        # sword was worth nothing to its finder; now it is worth something.
        #
        # ONE CELL AND THE ITEM THE GAME SAW IN IT, like every carry route (ONE
        # CELL AT A TIME): a sale names what it sells, the server checks the cell
        # still holds it, prices it from its own catalogue and pays through
        # gold_delta() in the same transaction. The request carries no price and
        # would not be believed if it did.
        #
        # IT IS A FAUCET, on purpose and small: the gold is MINTED, reason
        # shop_sell, so the supply report counts it and the invariant still holds.
        # sell_multiplier (the game's ShopData, 0.05) keeps an hour's junk worth
        # less than the hour's coin, and a set five hours away even for a player
        # who sells everything - test_pacing.py measures both.
        payload, slot, item_id, error = carry_request()
        if error is not None:
            return error
        position, error = parse_grid_cell(payload, "position", carry_cells)
        if error is not None:
            return error

        shop_id = str(payload.get("shop_id", "")).strip()
        if not shop_id or len(shop_id) > 64:
            return bad_request("shop_id must be 1-64 characters")
        if shop_id not in catalogue.SHOPS:
            return bad_request("Unknown shop '%s'. Is gamedata.json current?" % shop_id)

        # A FREE PIECE IS KEPT (gamedata.is_free(), 0.69.0), and said so: "the
        # shop does not buy that" about a Meteorite it buys when it dropped
        # would read as a bug.
        if catalogue.is_free(item_id):
            return bad_request(services.free_words(
                (catalogue.item_row(item_id) or {}).get("display_name", item_id)))
        unit_price = catalogue.shop_sell_price(shop_id, item_id, **shop_terms(get_db(), shop_id))
        if unit_price is None:
            return bad_request("The shop does not buy that.")

        raw_quantity = payload.get("quantity")
        quantity = None
        if raw_quantity is not None:
            if isinstance(raw_quantity, bool):
                return bad_request("quantity must be a positive integer")
            try:
                quantity = int(raw_quantity)
            except (TypeError, ValueError):
                return bad_request("quantity must be a positive integer")
            if quantity <= 0 or float(raw_quantity) != quantity:
                return bad_request("quantity must be a positive integer")

        user_id = g.user["id"]
        keys = (user_id, slot)
        held = grid_cells("carry_items", ("user_id", "slot"), keys, (position,)).get(position)
        if held is None or held[0] != item_id:
            return resync(user_id, slot)
        if quantity is None:
            quantity = held[1]
        if quantity > held[1]:
            # Asking for more than the cell holds is the game's picture being out
            # of date, the same as a cell holding something else.
            return resync(user_id, slot)

        db = get_db()
        left = held[1] - quantity
        grid_rewrite("carry_items", ("user_id", "slot"), keys, (position,),
                     {position: (item_id, left)} if left > 0 else {})
        total = unit_price * quantity
        remaining = gold_delta(
            db, user_id, slot, total, "shop_sell",
            "%s x%d @%d to %s" % (item_id, quantity, unit_price, shop_id),
        )
        db.commit()

        return {
            "slot": slot,
            "shop_id": shop_id,
            "position": position,
            "item_id": item_id,
            "quantity": quantity,
            "unit_price": unit_price,
            "total_received": total,
            "gold": remaining,
            "inventory": inventory_of(user_id, slot),
        }, 200

    def shop_catalogue(shop_id):
        """
        ---
        get:
          summary: What a vendor stocks, with prices
          description: >
            The prices the server will actually charge, so the panel cannot show a
            number the buy endpoint then disagrees with.
          responses:
            200:
              description: The vendor's stock
            404:
              description: No such shop
        """
        shop = catalogue.SHOPS.get(str(shop_id).strip())
        if shop is None:
            return {"error": "Not Found", "message": "No shop '%s'." % shop_id}, 404

        # PRICED HERE RATHER THAN IN THE CLIENT. The panel could multiply
        # ItemData.value by the shop's multiplier itself and usually get the same
        # answer - and the day the rounding rule changes, it would quietly get a
        # different one and show a price the server refuses. One source.
        #
        # WHAT IT GIVES AWAY FIRST (0.69.0), flagged free at price 0, so the
        # panel heads them on their own shelf above everything for gold.
        # AT THE OWNER'S NUMBERS (0.74.0), asked once for the whole shelf.
        owner_terms = shop_terms(get_db(), shop["shop_id"])
        stock = []
        for item_id in shop.get("free") or []:
            definition = catalogue.item_row(item_id)
            if definition is None:
                continue
            stock.append(services.stock_row(item_id, definition, 0, free=True))
        for item_id in shop.get("stock", []):
            definition = catalogue.item_row(item_id)
            if definition is None:
                continue
            stock.append(services.stock_row(item_id, definition,
                                            catalogue.shop_price(shop["shop_id"], item_id, **owner_terms)))

        # WHAT THE SHOP PAYS, for every item it would buy, from the same function
        # /api/shop/sell prices with - so the Sell list shows the number the till
        # pays, not one the panel worked out.
        #
        # BY CATALOGUE ID, so a price here is what ANY piece of it fetches -
        # vendor_pays(), not shop_sell_price(), because a rolled drop of
        # something the shop gives away still sells. The plain free pieces are
        # named in free_ids, and the panel leaves those out.
        sell_prices = {}
        for item_id in catalogue.ITEMS:
            price = catalogue.vendor_pays(shop["shop_id"], item_id, **owner_terms)
            if price is not None:
                sell_prices[item_id] = price
        free_ids = sorted({item_id for any_shop in catalogue.SHOPS.values()
                           for item_id in (any_shop.get("free") or [])})

        return {
            "shop_id": shop["shop_id"],
            "display_name": shop.get("display_name", "Shop"),
            "stock": stock,
            # THE TWO MULTIPLIERS IN FORCE, the owner's where set (0.74.0):
            # the panel says what share the shop pays, and whether the prices
            # are the usual ones.
            "sell_multiplier": float(owner_terms.get("sell_multiplier",
                                                     shop.get("sell_multiplier", 0.0) or 0.0)),
            "price_multiplier": float(owner_terms.get("price_multiplier",
                                                      shop.get("price_multiplier", 1.0))),
            "sell_prices": sell_prices,
            "free_ids": free_ids,
        }, 200

    app.add_url_rule("/api/shop/buy", endpoint="shop_buy",
                     view_func=require_auth(shop_buy), methods=["POST"])
    app.add_url_rule("/api/shop/sell", endpoint="shop_sell",
                     view_func=require_auth(shop_sell), methods=["POST"])
    app.add_url_rule("/api/shop/<shop_id>", endpoint="shop_catalogue",
                     view_func=require_auth(shop_catalogue), methods=["GET"])
