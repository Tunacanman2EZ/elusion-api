"""death/routes.py - the edge: the death screen's two exits, as they were in
app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    parse_slot          a raw slot -> its int, or None
    max_slot            the highest slot, for the 400's words
    ensure_account      user_id -> the accounts row (lusions, bank_gold),
                        made if it is missing - asked on every call, so a
                        suite's stand-in for it is heard
    catalogue           gamedata: what a gold revive costs, and a lusion one
    derived_stats       a saves row -> its maxima WITH WHAT IT IS WEARING:
                        app.py's _derived_stats(), the one way to ask
    gold_delta          (db, user_id, slot, delta, reason, detail) - the
                        carried gold, through the ledger
    bank_gold_delta     (db, user_id, delta, reason, detail) - the bank's
    lusion_delta        (db, user_id, slot, delta, reason, detail) - lusions
    lusion_gold_value   what one lusion is worth in gold, for the score
    revive_grant_id     the consume_grants item_id a revive writes
    respawn_grant_id    and a respawn - both read by the healing reconciler
    status_of           (user_id, slot) -> the character's status, as the
                        status read answers it
    note_refill         (db, user_id, slot, item_id, at, target=, amount=) - a
                        rise the server authorised, written where the healing
                        check reads it (status/'s, the one writer)
    owns_item           (user_id, slot, item_id) -> whether the bag or the
                        bank holds it (carry/'s) - a respawn's pet stays out
                        only if the bank still has one

THE ENDPOINT NAMES ARE app.py's - character_revive, character_respawn - and
routes.lock holds them.
"""
import time

from flask import g, request

from . import data, services
from .services import PAY_WITH


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, ensure_account, catalogue,
             derived_stats, gold_delta, bank_gold_delta, lusion_delta, lusion_gold_value, revive_grant_id,
             respawn_grant_id, status_of, note_refill, owns_item):

    def character_revive():
        """
        Revive a dead character, paying in lusions or in gold
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
              required: [slot]
              properties:
                slot: {type: integer, example: 0}
                pay:  {type: string,  example: gold, enum: [lusions, gold]}
        responses:
          200:
            description: Revived; the cost was taken and the character restored
          400:
            description: Bad slot
          402:
            description: Not enough lusions, or no gold at all
          404:
            description: No character in that slot
          409:
            description: That character is not dead
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        # CHECKED HERE, WITH THE SLOT, because it is a question about the
        # REQUEST rather than about the character. Validating it further down
        # meant a nonsense currency on a living character answered "that
        # character is not dead" - a true sentence about the wrong problem, and
        # a test caught it.
        method = str(payload.get("pay", "lusions")).strip().lower()
        if method not in PAY_WITH:
            return bad_request("pay must be 'lusions' or 'gold'")

        db = get_db()
        row = data.the_dead(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # ONLY THE DEAD MAY BE REVIVED, and the check is against the server's
        # own stored hp rather than anything the client asserts. Without it
        # this becomes "pay 20 lusions for a full heal at any moment", which is
        # a different feature with different balance consequences that nobody
        # designed.
        if int(row["hp"] or 0) > 0:
            return {
                "error": "Conflict",
                "message": "That character is not dead.",
            }, 409

        account = ensure_account(user_id)

        carry_gold = int(row["gold"] or 0)
        bank_gold = int(account["bank_gold"])

        if method == "gold":
            # A SHARE OF EVERYTHING WITH A FLOOR UNDER IT - see
            # GameConstants.REVIVE_GOLD_RATE for why a flat figure cannot sting
            # the same at level 3 and level 30, and revive_gold_cost() for why
            # the share alone left the bottom of the curve too cheap to matter.
            held = carry_gold + bank_gold
            cost = catalogue.revive_gold_cost(held)
            short = services.gold_shortfall(cost, held)
        else:
            cost = int(catalogue.CONSTANTS.get("revive_cost", 20))
            short = services.lusion_shortfall(cost, int(account["lusions"]))
        if short is not None:
            return short

        # ---- everything above this line is validation; everything below commits --

        derived = derived_stats(row)
        if derived is None:
            # A class the server has no curve for. Refusing is right: the
            # alternative is inventing a maximum and writing it into the save.
            app.logger.error("revive: no stat curve for class '%s'", row["class_id"])
            return {"error": "Conflict", "message": "That character cannot be revived."}, 409

        if method == "gold":
            # DESTROYED, NOT MOVED. Through gold_delta() and bank_gold_delta()
            # so the burn lands in the ledger in the same transaction as the
            # balance. A bare UPDATE here would break the supply invariant on
            # the first revive anybody paid for - the same mistake E-8 was.
            from_carry, from_bank = services.gold_split(carry_gold, cost)
            if from_carry:
                gold_delta(db, user_id, slot, -from_carry, "revive",
                           "revive from slot %d" % slot)
            if from_bank:
                bank_gold_delta(db, user_id, -from_bank, "revive",
                                "revive from slot %d" % slot)
        else:
            # THROUGH THE LEDGER, not a bare UPDATE, for the same reason the
            # gold burn is: a balance says what you have, and the kingdom board
            # needs to know what you GAVE. It is also the only record that a
            # lusion was ever spent on anything.
            lusion_delta(db, user_id, slot, -cost, "revive",
                         "revive from slot %d" % slot)

        # THE SCORE IS WHAT DYING TOOK, and it is the one number in the game
        # that goes up when you lose. BOTH PAYMENT PATHS COUNT - the 20-lusion
        # revive keeps everything you were carrying; the gold revive burns 80%
        # of what you hold - and the score is in gold (services.score_for()).
        #
        # IN THE SAME TRANSACTION as the payment above and the heal below, so a
        # crash cannot bank the score for a revive that did not happen or take
        # the payment for one that scored nothing.
        data.add_score(db, user_id, services.score_for(method, cost, lusion_gold_value))

        now = int(time.time())
        data.refill(db, user_id, slot, derived, now)

        # The marker the healing reconciler reads - an honest revive is a jump
        # from 0 to full in no time at all, and without it every legitimate
        # one would land in the log as a false positive.
        # The marker the healing check reads (status/'s note_refill()): a
        # revive fills every pool, so it names no pool and no amount.
        note_refill(db, user_id, slot, revive_grant_id, now)
        db.commit()

        account_after = ensure_account(user_id)
        return {
            "slot": slot,
            "revived": True,
            "paid_with": method,
            "cost": cost,
            "lusions": int(account_after["lusions"]),
            "bank_gold": int(account_after["bank_gold"]),
            "status": status_of(user_id, slot),
        }, 200

    def character_respawn():
        """
        Accept death: lose what was carried and worn, come back at full health
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
              required: [slot]
              properties:
                slot: {type: integer, example: 0}
        responses:
          200:
            description: Carry gold, carry items and worn gear destroyed, the pet put away unless the bank holds one, the three pools refilled to the bare maxima
          404:
            description: No character in that slot
          409:
            description: That character is not dead
        """
        # THE OTHER HALF OF A MIGRATION THAT ONLY EVER GOT DONE ONCE. The heal
        # and the penalty were both the client's: gameover.gd wrote full
        # pools into the save slot itself and zeroed the carry gold in the same
        # dictionary - so the healing reconciler clamped the heal to 52 hp, and
        # the gold, server-owned, came back in full on the next login. The
        # client sends what it DID ("I accepted death"), never what it now HAS;
        # both decisions are made here. The whole story is in death/__init__.py.
        payload = request.get_json(silent=True) or {}
        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer between 0 and %d" % max_slot)

        user_id = g.user["id"]
        db = get_db()
        row = data.the_dead(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # ONLY THE DEAD RESPAWN, the same check and the same reason as revive:
        # without it this route is "delete my carried gold and items for a free
        # full heal", which is a strange trade but a real one, and it would be
        # available at any moment rather than only after dying.
        if int(row["hp"] or 0) > 0:
            return {"error": "Conflict", "message": "That character is not dead."}, 409

        # WORN GEAR GOES TOO. Day 2, the owner: "gear is not dropping on full
        # death". It never had - this route took the bag and the purse and left
        # everything worn on, so a full death cost nothing to a character who
        # kept their best things on their back. His call: worn gear is lost with
        # the bag, and nothing is exempt, mythic weapons included. A paid
        # revive keeps it all; the bank is the only thing a full death cannot
        # reach.
        #
        # THE MAXIMA ARE DERIVED BARE, because that is what the character is
        # wearing once this commits. Deriving them from the row would refill to
        # a ceiling a Vitality amulet set and then leave hp above the new one.
        gear_lost = services.gear_lost(row["equipment"])
        derived = derived_stats({"class_id": row["class_id"], "level": row["level"],
                                 "equipment": "{}"})
        if derived is None:
            app.logger.error("respawn: no stat curve for class '%s'", row["class_id"])
            return {"error": "Conflict", "message": "That character cannot respawn."}, 409

        # ---- everything above this line is validation; everything below commits --

        # THROUGH THE LEDGER, NOT A BARE UPDATE. This is the single biggest
        # gold sink in the game - everything a player was carrying when they
        # died - and a bare `SET gold = 0` would break
        #
        #     SUM(gold_ledger.delta) == SUM(saves.gold) + SUM(accounts.bank_gold)
        #
        # on the first death anybody took. The kingdom board reads that ledger
        # to say what the realm has been given; a death is exactly the kind of
        # thing it should be able to say.
        burned = int(row["gold"] or 0)
        if burned > 0:
            gold_delta(db, user_id, slot, -burned, "death",
                       "carried gold lost on death in slot %d" % slot)

        # THE CARRY BAG, SERVER SIDE, in the same transaction as the gold, so a
        # crash between the two cannot take one and not the other.
        data.drop_carry(db, user_id, slot)

        # THE PET GOES WITH THE BAG, unless the bank holds one (0.56.2) - the
        # rule a pet is let out by (carry/'s owns_item()), asked after the bag
        # is gone and in the same transaction. The game cleared it on its own
        # side since 0.56.0 and the server kept it: a game closed before its
        # next save came back with the pet out and no item to put it away
        # with, and everyone else was shown it until then.
        pet = str(row["active_pet_id"] or "")
        pet_lost = pet != "" and not owns_item(user_id, slot, pet)
        if pet_lost:
            data.put_pet_away(db, user_id, slot)

        now = int(time.time())
        data.refill(db, user_id, slot, derived, now, strip_gear=True)

        # The marker the healing reconciler reads, exactly as the revive writes
        # one. Its absence is what turned an honest respawn into a clamped one,
        # and the log line said so in as many words: "granted 0".
        note_refill(db, user_id, slot, respawn_grant_id, now)
        db.commit()

        return {
            "slot": slot,
            "respawned": True,
            "gold_lost": burned,
            "gear_lost": gear_lost,
            "equipment": {},
            "pet_lost": pet_lost,
            "active_pet_id": "" if pet_lost else pet,
            "status": status_of(user_id, slot),
        }, 200

    app.add_url_rule("/api/character/revive", endpoint="character_revive",
                     view_func=require_auth(character_revive), methods=["POST"])
    app.add_url_rule("/api/character/respawn", endpoint="character_respawn",
                     view_func=require_auth(character_respawn), methods=["POST"])
