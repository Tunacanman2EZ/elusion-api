"""trade/routes.py - the edge: the trade routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    require_role        a staff rank -> its decorator (staff_trades)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    parse_slot          a raw slot -> its int, or None
    max_slot            the highest slot, for the 400's words
    slot_exists         (user_id, slot) -> whether a character is there
    ignores             (db, a, b) -> whether account a ignores account b
    presence_for        (db, ids) -> {id: last seen}
    online_window       seconds since last seen that still count as on
    playing_slot        (db, user_id) -> the character they are playing, or None
    trades_enabled      () -> the owner's switch: may a NEW trade open?
    take_from_backpack  (user_id, slot, item_id, quantity, keys_last=) -> True,
                        or False having written nothing
    add_to_backpack     (user_id, slot, item_id, quantity) -> cells, or None
    gold_delta          (db, user_id, slot, delta, reason, detail) - the
                        gold ledger: the tax, and nothing else in a trade
    inventory_of        (user_id, slot) -> the carry, cell by cell
    mark_resync         (db, trade) - flag both characters' bags as changed
    take_resync         (db, user_id) -> a flagged character's new bag, once
    name_hue_of         users row -> the colour they chose, or None
    moderation_target   payload -> (account, None) or (None, the refusal):
                        an account within the caller's reach
    query_int           (name, default, low, high) -> the query's int, or None
    staff_page          (default, most) a staff page holds
    catalogue           gamedata: what an item is and is worth, and the tax
    tax_terms           (db) -> the owner's trade tax, as keyword arguments for
                        catalogue.trade_tax() - {} when the owner has set none
                        (economyrules/, 0.74.0). Left out, the tax is the
                        catalogue's own, as it was before the owner could set it.

THE ENDPOINT NAMES ARE app.py's - trade_offer, trade_current, trade_update,
trade_confirm, trade_cancel, staff_trades, trade_history - and routes.lock
holds them.

THE ONLY PLACE TWO PLAYERS' PROPERTY CHANGES HANDS, and no route will let a
client say WHICH trade: every one starts from the caller (data.find_open,
data.find_recent - test_ownership.py, O-2).
"""
import secrets
import time

from flask import g, request

from . import data, services
from .services import (STAFF_TRADE_STATES, TRADE_HISTORY_SHOWN, TRADE_MAX_ITEM_TYPES)


def register(app, *, require_auth, require_role, get_db, bad_request, parse_slot, max_slot, slot_exists,
             ignores, presence_for, online_window, playing_slot, trades_enabled, take_from_backpack,
             add_to_backpack, gold_delta, inventory_of, mark_resync, take_resync, name_hue_of,
             moderation_target, query_int, staff_page, catalogue, tax_terms=None):

    # THE OWNER'S TAX (0.74.0): the rate and the floor the owner set on the
    # server (economyrules/), asked by the quote and by the charge alike - so
    # the number on the confirm button is the number taken. Nothing set,
    # nothing passed: the game's own 5% and 1.
    def tax(db, value):
        return catalogue.trade_tax(value, **(dict(tax_terms(db) or {}) if tax_terms is not None else {}))

    def trade_payload(db, trade):
        """
        The trade as both sides' panels draw it.

        THE TAX IS QUOTED HERE, from the same catalogue.trade_tax() the
        execution charges. A panel that computed its own would be a panel that
        could show a number the server then disagrees with, and the one moment a
        player must not be surprised is the moment they press confirm.

        Quoted, not promised: either side changing their offer re-quotes it, and
        the figure that is actually charged is computed again at execution
        against the offer as it stands then.
        """
        a_items = data.items_of(db, trade["trade_id"], "a")
        b_items = data.items_of(db, trade["trade_id"], "b")
        a_value = services.value(catalogue, a_items, int(trade["a_gold"]))
        b_value = services.value(catalogue, b_items, int(trade["b_gold"]))

        a_user, b_user = int(trade["a_user"]), int(trade["b_user"])

        # ONE QUERY FOR BOTH SIDES' PRESENCE, not one each - this payload is
        # built for every poll of every open trade window.
        now = int(time.time())
        seen = presence_for(db, [a_user, b_user])

        def who(user_id, slot):
            # The character as well as the account, and the name the way every
            # other list draws it: in the colour its owner chose, with the rank
            # as a badge. See nametag.gd.
            row = data.person(db, user_id, slot)
            if row is None:
                return {"username": "?", "role": "player", "name_hue": None, "name": "", "level": 1}
            return {
                "username": row["username"],
                "role": row["role"],
                "name_hue": name_hue_of(row),
                "name": row["name"] or "",
                "level": int(row["level"] or 1),
            }

        def side(prefix, items, offered, receives_value):
            user_id, slot = int(trade["%s_user" % prefix]), int(trade["%s_slot" % prefix])
            return {
                **who(user_id, slot),
                "slot": slot,
                "items": items,
                "gold": int(trade["%s_gold" % prefix]),
                "confirmed": bool(int(trade["%s_confirmed" % prefix])),
                "offering_value": offered,
                # What this side will pay: tax on what it RECEIVES, which is the
                # other side's offer.
                "tax": tax(db, receives_value) if receives_value is not None else None,
                # STILL THERE? A trade with somebody who has closed the game can
                # only ever be cancelled, and the window should say so rather
                # than leave you waiting for an accept that is not coming.
                "online": int(seen.get(user_id, 0)) > now - online_window,
            }

        return {
            "trade_id": trade["trade_id"],
            "state": trade["state"],
            # WHAT A CONFIRM MUST NAME. See trade_confirm(): accepting is
            # agreeing to THIS revision of the offer, and the server refuses an
            # accept that names any other.
            "revision": int(trade["revision"]),
            "a": side("a", a_items, a_value, b_value),
            "b": side("b", b_items, b_value, a_value),
        }

    def refuse(db, trade_id, body, status):
        """
        Undo a failed execution and put the trade back to nobody-has-agreed.

        A BARE ROLLBACK IS NOT ENOUGH, AND THE ASYMMETRY IT LEAVES IS BACKWARDS.
        The confirm that triggered execution is written in THIS transaction; the
        other side's was committed in an earlier request. So a plain rollback
        undoes the confirmation of the player who just pressed the button and
        keeps the one from the player who did not - the panel then shows the
        other party as committed to an offer that has just been refused, while
        the person looking at it appears not to have agreed at all.

        It also quietly changes what "confirmed" means. Both sides agreed to a
        trade the world then refused; whatever was true when they agreed is not
        true now, so the honest state is that the agreement lapsed and has to be
        made again against the facts as they stand.

        Rolls back FIRST - every write the attempt made has to go, including the
        claim on the trade row - and only then writes the one fact worth
        keeping.
        """
        db.rollback()
        data.lapse_agreement(db, trade_id, int(time.time()))
        db.commit()
        return body, status

    def execute(db, trade):
        """
        Cross both sides, burn the tax, in ONE transaction. Returns (body,
        status).

        ORDER MATTERS AND IS NOT ARBITRARY:
          1. claim the trade, so two confirms racing can only fire once
          2. value both sides and compute both taxes
          3. check both purses survive the swap AND the tax
          4. take every item from both bags - all-or-nothing, so a missing item
             refuses before anything has moved
          5. add the crossed items - taking first is what frees the cells, so a
             one-for-one swap between two full backpacks still works
          6. move the gold as a TRANSFER, writing NO ledger rows
          7. burn the two taxes through gold_delta(), which does write rows
          8. record the taxes on the row, and flag both bags as changed
        Any refusal rolls the whole thing back; the caller does not commit on a
        non-200.

        STEP 6 IS THE ONE TO GET RIGHT. gold_delta() calls itself the only way
        gold may change, and for CREATION and DESTRUCTION it is. A transfer is
        neither: both purses count toward supply, so routing it through
        gold_delta() would write a -N and a +N that net to zero but leave the
        ledger claiming two events that never touched the money supply.
        gold_ledger's own comment says transfers write nothing. The tax is the
        only part of a trade that is a real ledger event, because it is the only
        part where gold stops existing.
        """
        trade_id = trade["trade_id"]

        # 1. CLAIM IT.
        if not data.claim(db, trade_id, int(time.time())):
            db.rollback()
            return {"error": "Conflict", "message": "That trade is no longer open."}, 409

        a_user, a_slot = int(trade["a_user"]), int(trade["a_slot"])
        b_user, b_slot = int(trade["b_user"]), int(trade["b_slot"])
        a_gold, b_gold = int(trade["a_gold"]), int(trade["b_gold"])

        a_items = data.items_of(db, trade_id, "a")
        b_items = data.items_of(db, trade_id, "b")

        # 2. VALUE AND TAX.
        a_value = services.value(catalogue, a_items, a_gold)
        b_value = services.value(catalogue, b_items, b_gold)
        if a_value is None or b_value is None:
            return refuse(db, trade_id,
                          *bad_request("This trade contains an item the server does not know."))

        a_tax = tax(db, b_value)   # A pays on what A receives
        b_tax = tax(db, a_value)

        # 3. PURSES. Read inside the transaction, never from the trade row: the
        # offer records what was PROMISED, and what is HELD is a different fact
        # that may have changed since.
        a_purse, b_purse = data.purse(db, a_user, a_slot), data.purse(db, b_user, b_slot)
        if a_purse is None or b_purse is None:
            return refuse(db, trade_id,
                          {"error": "Not Found",
                           "message": "One of these characters no longer exists."}, 404)

        a_final = a_purse - a_gold + b_gold - a_tax
        b_final = b_purse - b_gold + a_gold - b_tax
        if a_purse < a_gold or b_purse < b_gold:
            return refuse(db, trade_id,
                          *bad_request("Somebody no longer has the gold they offered."))
        if a_final < 0 or b_final < 0:
            return refuse(db, trade_id, *bad_request(
                "The kingdom's cut is %d and %d; one of you cannot cover it." % (a_tax, b_tax)))

        # 3b. HOLDS. A fresh mythic or Perfect may have arrived since the offer
        # was made, and the offer's own check is stale by now like the purses.
        for party, offered in ((a_user, a_items), (b_user, b_items)):
            waiting = data.hold_refusal(db, catalogue, party,
                                        {entry["item_id"]: entry["quantity"] for entry in offered})
            if waiting is not None:
                return refuse(db, trade_id, *bad_request(waiting))

        # 4. TAKE. Both bags emptied of what was promised before anything is
        # added.
        #
        # THE BAG BEFORE THE KEYS. An offer is a quantity, not a cell, so the
        # server chooses which cells it comes out of - and the take's own
        # order, highest position first, puts the hotbar (cells 20-29) ahead of
        # the whole bag. Trading away three of your ten potions emptied key 1
        # and left the seven in the bag, so the next fight started with nothing
        # on the key you press.
        for entry in a_items:
            if not take_from_backpack(a_user, a_slot, entry["item_id"], entry["quantity"],
                                      keys_last=True):
                return refuse(db, trade_id,
                              *bad_request("Somebody no longer has the items they offered."))
        for entry in b_items:
            if not take_from_backpack(b_user, b_slot, entry["item_id"], entry["quantity"],
                                      keys_last=True):
                return refuse(db, trade_id,
                              *bad_request("Somebody no longer has the items they offered."))

        # 5. CROSS.
        for entry in b_items:
            if add_to_backpack(a_user, a_slot, entry["item_id"], entry["quantity"]) is None:
                return refuse(db, trade_id,
                              {"error": "Conflict",
                               "message": "A backpack is too full for this trade."}, 409)
        for entry in a_items:
            if add_to_backpack(b_user, b_slot, entry["item_id"], entry["quantity"]) is None:
                return refuse(db, trade_id,
                              {"error": "Conflict",
                               "message": "A backpack is too full for this trade."}, 409)

        # 6. THE TRANSFER. Bare UPDATEs on purpose - see this function's header.
        now = int(time.time())
        if a_gold or b_gold:
            data.move_gold(db, a_user, a_slot, a_gold, b_gold, now)
            data.move_gold(db, b_user, b_slot, b_gold, a_gold, now)

        # 7. THE BURN. The only ledger events in a trade.
        if a_tax > 0:
            gold_delta(db, a_user, a_slot, -a_tax, "kingdom_tax",
                       "trade %s receiving %d" % (trade_id[:8], b_value))
        if b_tax > 0:
            gold_delta(db, b_user, b_slot, -b_tax, "kingdom_tax",
                       "trade %s receiving %d" % (trade_id[:8], a_value))

        # 8. THE RECORD, and the flag that makes both clients re-read their
        # bags. The taxes go on the row so the history can say what each side
        # paid without parsing the ledger's prose; see app.py's _mark_resync()
        # for the flag.
        data.note_taxes(db, trade_id, a_tax, b_tax)
        mark_resync(db, trade)

        db.commit()

        return {
            "trade_id": trade_id,
            "state": "done",
            "tax_paid": {"a": a_tax, "b": b_tax},
            "kingdom_take": a_tax + b_tax,
            "a": {"gold": a_final, "inventory": inventory_of(a_user, a_slot)},
            "b": {"gold": b_final, "inventory": inventory_of(b_user, b_slot)},
        }, 200

    def trade_offer():
        """
        Open a trade with another player
        ---
        tags:
          - Trade
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
              required: [slot, username]
              properties:
                slot:     {type: integer, example: 0}
                username: {type: string,  example: "otherplayer"}
                to_slot:  {type: integer, example: 0}
        responses:
          200: {description: The open trade}
          400: {description: Bad slot, unknown player, or trading with yourself}
          409: {description: One of you is already in a trade}
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        # THE SWITCH, FIRST. 503 and not 403, the reading maintenance_refusal()
        # makes: nothing is wrong with who is asking; the door is shut for now.
        # Only a NEW trade is refused - see TRADE_KEY.
        if not trades_enabled():
            return {"error": "Service Unavailable",
                    "message": "Trading is switched off for now. A trade already open can still finish.",
                    "trade": False}, 503

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        if not slot_exists(user_id, slot):
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        username = str(payload.get("username", "")).strip()
        if not username:
            return bad_request("username is required")

        db = get_db()
        other = data.account_by_name(db, username)
        if other is None:
            return bad_request("No player called '%s'." % username)

        other_id = int(other["id"])
        # THEIR NAME AS STORED, not as typed. users.username is COLLATE NOCASE,
        # so "BOBBY" finds bobby - and every message below should say bobby.
        username = other["username"]
        # REFUSED EXPLICITLY, not left to chance. A self-trade would run every
        # step of the execution against one backpack, and taking from the same
        # bag it adds to is precisely how a duplication bug is written.
        if other_id == user_id:
            return bad_request("You cannot trade with yourself.")
        if ignores(db, other_id, user_id):
            return {"error": "Forbidden",
                    "message": "%s is not taking trades from you." % username}, 403

        # SOMEBODY WHO IS THERE. A trade needs two people to press accept, and
        # one opened with a player who has closed the game can only sit there
        # until it expires - blocking both of you from trading with anyone else
        # for ten minutes, with nothing on screen to say why.
        if not presence_for(db, [other_id]).get(other_id, 0) > int(time.time()) - online_window:
            return bad_request("%s is not online." % username)

        # THE CHARACTER THEY ARE PLAYING, decided here and not by the caller.
        #
        # to_slot used to default to 0, and the trade window sent 0 whenever a
        # name was typed rather than picked - so typing a friend's name opened a
        # trade with their FIRST character, whoever they were actually playing.
        # Their window then listed the bag they were carrying while the server
        # checked a different one, and every offer they made was refused for
        # items they could see in front of them.
        #
        # A to_slot that is sent is still checked, and must name that same
        # character: an offer is to a person at the keyboard, not to a character
        # parked on their account.
        to_slot = playing_slot(db, other_id)
        if to_slot is None:
            return bad_request("%s has no character." % username)
        if payload.get("to_slot") is not None:
            asked = parse_slot(payload.get("to_slot"))
            if asked is None:
                return bad_request("to_slot must be an integer 0-%d" % max_slot)
            if asked != to_slot:
                return {"error": "Conflict",
                        "message": "%s is playing a different character now." % username}, 409

        # ONE OPEN TRADE EACH. Two concurrent trades could each validate against
        # the same sword and both pass, because neither holds it.
        for candidate in (user_id, other_id):
            existing = data.find_open(db, candidate)
            if existing is not None:
                if not services.expired(existing, time.time()):
                    return {
                        "error": "Conflict",
                        "message": "Already in a trade." if candidate == user_id
                                   else "%s is already in a trade." % username,
                    }, 409
                # Stale. Judged when it is in the way rather than by a sweeper.
                data.lapse(db, existing["trade_id"], int(time.time()))

        trade_id = secrets.token_urlsafe(16)
        data.open_trade(db, trade_id, user_id, slot, other_id, to_slot, int(time.time()))
        db.commit()

        return trade_payload(db, data.trade_row(db, trade_id)), 200

    def trade_current():
        """
        The trade this player is in, or null
        ---
        tags:
          - Trade
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200: {description: 'The open trade or null, and "resync" - a bag and purse to re-read, or null'}
        """
        db = get_db()
        user_id = g.user["id"]
        trade = data.find_open(db, user_id)

        # THE WINDOW IS THE FASTEST WAY THIS REACHES A CLIENT. It polls every
        # second and a half while open, and the person who accepted first is
        # usually still looking at it - so this is where they learn the trade
        # went through, and get the bag it went through with. See app.py's
        # _mark_resync().
        resync = take_resync(db, user_id)

        if trade is None or services.expired(trade, time.time()):
            # HOW THE LAST ONE ENDED, when there is no open one - so a window
            # that was watching a trade can say "complete" or "called off"
            # rather than guessing. The result itself may already have been
            # delivered by the broadcast poll; this is the fact that stays true
            # after that.
            recent = data.find_recent(db, user_id, 1, ("done", "cancelled"))
            last = None
            if recent:
                last = {"trade_id": recent[0]["trade_id"], "state": recent[0]["state"]}
            return {"trade": None, "resync": resync, "last": last}, 200
        return {"trade": trade_payload(db, trade), "resync": resync}, 200

    def trade_update():
        """
        Replace your side of the offer
        ---
        tags:
          - Trade
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
              properties:
                items: {type: array, description: "[{item_id, quantity}], replaces your whole side"}
                gold:  {type: integer, example: 500}
        responses:
          200: {description: The trade as it now stands, with both sides un-confirmed}
          400: {description: Bad item, quantity, or gold you do not have}
          404: {description: You are not in a trade}
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        db = get_db()
        trade = data.find_open(db, user_id)
        if trade is None or services.expired(trade, time.time()):
            return {"error": "Not Found", "message": "You are not in a trade."}, 404

        side = services.side_of(trade, user_id)
        slot = int(trade["%s_slot" % side])

        raw_gold = payload.get("gold", 0)
        if isinstance(raw_gold, bool) or not isinstance(raw_gold, int):
            return bad_request("gold must be an integer")
        if raw_gold < 0:
            return bad_request("gold must not be negative")

        held_gold = data.purse(db, user_id, slot)
        if held_gold is None:
            return {"error": "Not Found", "message": "That character no longer exists."}, 404
        if raw_gold > held_gold:
            return bad_request("You only have %d gold." % held_gold)

        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            return bad_request("items must be an array")
        if len(raw_items) > TRADE_MAX_ITEM_TYPES:
            return bad_request("At most %d different items per side." % TRADE_MAX_ITEM_TYPES)

        # MERGED BY ITEM ID BEFORE ANYTHING IS CHECKED. Two entries naming the
        # same item would otherwise each be checked against the full holding and
        # both pass, and the pair would then be taken at execution.
        merged = {}
        for entry in raw_items:
            if not isinstance(entry, dict):
                return bad_request("each item must be an object")
            item_id = str(entry.get("item_id", "")).strip()
            if not item_id or len(item_id) > 64:
                return bad_request("item_id must be 1-64 characters")
            if not catalogue.has_item(item_id):
                return bad_request("No such item '%s'." % item_id)
            raw_qty = entry.get("quantity", 1)
            if isinstance(raw_qty, bool) or not isinstance(raw_qty, int) or raw_qty <= 0:
                return bad_request("quantity must be a positive integer")
            merged[item_id] = merged.get(item_id, 0) + raw_qty

        # CHECKED NOW AND AGAIN AT EXECUTION. Here so the panel can refuse
        # immediately; there because this answer is already stale by the time
        # anybody presses confirm.
        held = data.carried(db, user_id, slot)
        for item_id, quantity in merged.items():
            if held.get(item_id, 0) < quantity:
                return bad_request("You only have %d x %s." % (held.get(item_id, 0), item_id))

        # A FRESH MYTHIC OR PERFECT WAITS (TRADE_HOLD_SECONDS). Checked here so
        # the window can say so at once, and again at execution with everything
        # else.
        waiting = data.hold_refusal(db, catalogue, user_id, merged)
        if waiting is not None:
            return bad_request(waiting)

        # THE SAME OFFER AGAIN IS NOT A CHANGE. Every write below withdraws both
        # acceptances and moves the revision, which is right for a real change
        # and wrong for a repeat: a window that re-sends what is already
        # standing - after a reconnect, or a quantity box set to the number it
        # already had - would silently un-accept the other player, who then has
        # to notice and press accept again for an offer nobody altered.
        standing = {entry["item_id"]: entry["quantity"]
                    for entry in data.items_of(db, trade["trade_id"], side)}
        if standing == merged and int(trade["%s_gold" % side]) == raw_gold:
            return trade_payload(db, trade), 200

        data.set_offer(db, trade["trade_id"], side, merged, raw_gold)
        data.touch(db, trade["trade_id"], int(time.time()))
        db.commit()

        return trade_payload(db, data.trade_row(db, trade["trade_id"])), 200

    def trade_confirm():
        """
        Confirm your side; the trade executes when both have
        ---
        tags:
          - Trade
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
              required: [revision]
              properties:
                revision: {type: integer, example: 3, description: "The revision of the offer you were shown"}
        responses:
          200: {description: The trade, or its result if that confirm completed it}
          400: {description: No revision, somebody cannot cover the tax, or no longer holds their offer}
          404: {description: You are not in a trade}
          409: {description: The offer changed since you saw it, the trade closed underneath you, or a backpack is full}
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]
        db = get_db()
        trade = data.find_open(db, user_id)
        if trade is None or services.expired(trade, time.time()):
            return {"error": "Not Found", "message": "You are not in a trade."}, 404

        # ACCEPTING IS AGREEING TO WHAT YOU SAW, AND ONLY THE CLIENT KNOWS WHAT
        # THAT WAS. So it has to say: the revision on the payload it drew.
        # REQUIRED, not optional - an accept that names nothing is an accept of
        # whatever happens to be standing when it lands, which is the scam this
        # closes. See data.touch() for the race.
        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            return bad_request("revision is required: the revision of the offer you are accepting")

        side = services.side_of(trade, user_id)
        # COMPARE-AND-SET, in the UPDATE itself. Checking the revision on the
        # row read above and then writing would leave a gap between the two for
        # the other side's change to land in; conditioning the write on it does
        # not.
        if not data.agree(db, trade["trade_id"], side, revision, int(time.time())):
            db.rollback()
            current = data.trade_row(db, trade["trade_id"])
            if current is None or current["state"] != "open":
                return {"error": "Conflict", "message": "That trade is no longer open."}, 409
            # THE CURRENT OFFER COMES BACK WITH THE REFUSAL, so the window can
            # redraw it at once rather than show a stale one for another poll.
            return {"error": "Conflict",
                    "message": "The offer changed. Look at it again before you accept.",
                    "trade": trade_payload(db, current)}, 409

        # RE-READ, don't reason about the row we started from: the other side
        # may have confirmed between our read and our write.
        trade = data.trade_row(db, trade["trade_id"])

        if int(trade["a_confirmed"]) and int(trade["b_confirmed"]):
            return execute(db, trade)

        db.commit()
        return trade_payload(db, trade), 200

    def trade_cancel():
        """
        Call off the trade
        ---
        tags:
          - Trade
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200: {description: Cancelled, or nothing to cancel}
        """
        db = get_db()
        trade = data.find_open(db, g.user["id"])
        if trade is None:
            return {"cancelled": False}, 200
        # EITHER SIDE MAY CANCEL AT ANY TIME, including after confirming. A
        # confirm that cannot be withdrawn is a trap: the other side changes
        # nothing, waits, and you are committed.
        data.call_off(db, trade["trade_id"], int(time.time()))
        db.commit()
        return {"cancelled": True, "trade_id": trade["trade_id"]}, 200

    def staff_trades():
        """
        Every trade an account has been in, newest first, one page at a time
        ---
        tags:
          - Moderation
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: query
            name: username
            type: string
            required: true
          - in: query
            name: before_at
            type: integer
            description: "The previous page's next_before.at"
          - in: query
            name: before_seq
            type: integer
            description: "The previous page's next_before.seq"
          - in: query
            name: limit
            type: integer
            description: "1-200, default 50"
        responses:
          200:
            description: A page of trades worded from that account's side, a tally by state, and the cursor for the next page
          400:
            description: No username, a half cursor, or a limit out of range
          404:
            description: No such account, or one outside your reach - the same answer for both
        """
        # "HE SCAMMED ME" IS THE REPORT THIS ANSWERS. Every finished trade is
        # kept for good for exactly this, and until now the only way to read one
        # was sqlite3 on the box holding the database. A moderator can page
        # through what an account gave, got and paid, with whom and on which
        # character.
        #
        # UNDER REACH, like every other staff view of a person: the account must
        # be one you could act on, and out of reach reads exactly like "no such
        # account" - see app.py's _moderation_target(). A trade WITH a mod still
        # shows up in the player's own history, which is the one being
        # investigated.
        target, refusal = moderation_target({"username": request.args.get("username", "")})
        if refusal is not None:
            return refusal

        page_default, page_max = staff_page
        limit = query_int("limit", page_default, 1, page_max)
        if limit is None:
            return bad_request("limit must be a whole number 1-%d" % page_max)
        before_at = query_int("before_at", -1, 0, 2 ** 62)
        before_seq = query_int("before_seq", -1, 0, 2 ** 62)
        if before_at is None or before_seq is None or (before_at == -1) != (before_seq == -1):
            return bad_request("before_at and before_seq come together, from next_before")
        before = (before_at, before_seq) if before_at != -1 else None

        db = get_db()
        user_id = int(target["id"])
        rows = data.find_page(db, user_id, limit + 1, STAFF_TRADE_STATES, before)
        more = len(rows) > limit
        rows = rows[:limit]
        trades = []
        for row in rows:
            record = data.record(db, row["trade_id"], user_id)
            if record is not None:
                trades.append(record)

        return {
            "username": target["username"],
            "trades": trades,
            "more": more,
            "next_before": {"at": int(rows[-1]["updated_at"]), "seq": int(rows[-1]["seq"])}
                           if more and rows else None,
            # THE TALLY, before anybody reads a line: twelve trades, three
            # called off. Counted off the two indexes rather than the rows.
            "summary": data.state_counts(db, user_id),
            "now": int(time.time()),
        }, 200

    def trade_history():
        """
        Your last few finished trades
        ---
        tags:
          - Trade
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200: {description: 'Newest first: who with, what you gave, what you got, and the kingdom''s cut'}
        """
        # "DID THAT GO THROUGH?" is the question every trade eventually gets
        # asked, usually a day later, and until now nothing could answer it but
        # a moderator reading the database. Finished trades only: one that was
        # called off moved nothing, and a list of those is a list of
        # conversations.
        db = get_db()
        user_id = g.user["id"]
        rows = data.find_recent(db, user_id, TRADE_HISTORY_SHOWN)
        trades = [data.record(db, row["trade_id"], user_id) for row in rows]
        # THE SERVER'S CLOCK, so "2 h ago" is aged against the same clock that
        # stamped the trade - not against a player's system time, which is
        # wrong often enough that the guild panel already learned this.
        return {"trades": [t for t in trades if t is not None], "now": int(time.time())}, 200

    app.add_url_rule("/api/trade/offer", endpoint="trade_offer",
                     view_func=require_auth(trade_offer), methods=["POST"])
    app.add_url_rule("/api/trade", endpoint="trade_current",
                     view_func=require_auth(trade_current), methods=["GET"])
    app.add_url_rule("/api/trade/update", endpoint="trade_update",
                     view_func=require_auth(trade_update), methods=["POST"])
    app.add_url_rule("/api/trade/confirm", endpoint="trade_confirm",
                     view_func=require_auth(trade_confirm), methods=["POST"])
    app.add_url_rule("/api/trade/cancel", endpoint="trade_cancel",
                     view_func=require_auth(trade_cancel), methods=["POST"])
    app.add_url_rule("/api/staff/trades", endpoint="staff_trades",
                     view_func=require_auth(require_role("mod")(staff_trades)), methods=["GET"])
    app.add_url_rule("/api/trade/history", endpoint="trade_history",
                     view_func=require_auth(trade_history), methods=["GET"])
