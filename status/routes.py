"""status/routes.py - the edge: a character's live stats, read and written, as
they were in app.py, and the check that every rise in a pool was earned.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth      the token check (puts the account on g.user)
    get_db            the request's database connection
    bad_request       a message -> app.py's 400
    parse_slot        a raw slot -> its int, or None
    max_slot          the highest slot, for the 400's words
    parse_stat        a raw number -> a non-negative int up to the ceiling, or
                      None
    stat_ceiling      that ceiling, for the 400's words (STAT_CEILING)
    derived_stats     a saves row -> its maxima WITH WHAT IT IS WEARING:
                      app.py's _derived_stats(), the one way to ask
    catalogue         gamedata: how fast a pool of a given size regenerates
                      (regen_rate_for()) - asked on every check
    heal_margin       () -> HEAL_ALLOWANCE_MARGIN, the tight line: logged
    clamp_margin      () -> HEAL_CLAMP_MARGIN, the loose line: enforced
    clamp_enforced    () -> HEAL_CLAMP_ENFORCED, whether the loose line trims
    minimum_elapsed   () -> HEAL_MINIMUM_ELAPSED_SECONDS
    explain_window    () -> HEAL_EXPLAIN_WINDOW_SECONDS, how recent a grant
                      must be to explain a rise
    refill_grants     the grant ids that fill every pool - a revive, a respawn,
                      a level-up - so explain any rise at all

THE HEALING TUNABLES ARE app.py's AND ARE ASKED FOR ON EVERY CHECK. They are
switches someone flips on purpose - HEAL_CLAMP_ENFORCED has been off and on
again, each time for a reason written beside it - and the switch has to stay
in the one place it has always been, with its history. A copy handed in once
would be a second switch that the first one no longer moves.

register() hands back status_of(); app.py keeps it callable as
status_payload(), the name every area that answers with the vitals is handed.

THE ENDPOINT NAMES ARE app.py's - player_status, write_player_status - and
routes.lock holds them.
"""
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services
from .services import DERIVED_STATS, SERVER_OWNED_STATS, STATUS_FIELDS


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, parse_stat, stat_ceiling,
             derived_stats, catalogue, heal_margin, clamp_margin, clamp_enforced, minimum_elapsed,
             explain_window, refill_grants):

    def status_of(user_id, slot):
        """The character's live stats (services.view()), or None for no such
        character."""
        row = data.row_of(get_db(), user_id, slot)
        if row is None:
            return None
        return services.view(row)

    def reconcile_heals(db, user_id, slot, before, after):
        """
        Trim any rise in hp/mana/stamina that regeneration and authorised
        consumables cannot account for. Returns {field: corrected} for the caller
        to apply, empty when there is nothing to correct.

        RENAMED FROM _report_unexplained_heals, because it no longer only reports.
        It sat beside _reconcile_bank() and _reconcile_inventory() when it was
        named, did the same job as those two - compare the claim to the record and
        correct it - and is named for it. (Those two are gone: the bag and the bank
        are no longer claimed by the client at all. See ONE CELL AT A TIME.)

        TWO LINES, NOT ONE, AND THAT IS THE WHOLE DESIGN.

            HEAL_ALLOWANCE_MARGIN   1.25x   the TIGHT line: what regen plus
                                            consumes can honestly produce.
                                            Logged, never enforced.
            HEAL_CLAMP_MARGIN       3.00x   the LOOSE line: enforced.

        Enforcing at 1.25x on the evidence available would be the E-2 mistake - a
        threshold picked from eighteen simulated scenarios rather than from real
        traffic. Enforcing at 3x cannot plausibly catch an honest player: it is
        three times more healing than the game can produce, and a rise that large
        did not come from the game.

        Meanwhile every rise landing BETWEEN the two lines is logged as
        "tight-clamp would have caught this". That is the evidence for closing the
        gap later, gathered while a control is already running rather than instead
        of one. If that band stays empty across real play, 1.25x is safe and the
        log is what says so. If it fills up with honest players, the band is the
        proof that shipping the tight clamp would have broken them.

        CLAMPS, DOES NOT REFUSE. A 400 here fails the whole status write - all
        three pools together - so one suspicious figure would discard the honest
        two with it, and a client that keeps sending it would never save its pools
        again. Trimmed to what the player could have earned, the way the old bag
        ledger trimmed a bag, and the write proceeds.

        WHAT IT STILL DOES NOT DO. It bounds the RATE of unexplained healing, not
        its existence: measured against a 180 hp pool, roughly 7 hp per save slips
        under the tight line and ~14 full heals an hour under it. That is the same
        shape as the kill bucket, and the reason both are controls rather than
        proofs is the same - the server does not observe combat. See E-3.

        THE CHECK IS PER-POOL NOW, not per-character. The first version asked "did
        this character drink anything recently", which meant one cheap stamina
        potion explained an arbitrary jump in health - a hole exactly as wide as the
        smallest consumable in the game. It asked that because the server did not
        know what a potion restored. It does now, so a grant explains a rise in the
        pool it actually fills, up to the amount it actually gives.

        KNOWN SOURCE OF FALSE POSITIVES, named so whoever reads the log is not
        misled: **a level-up** raises the derived maximum in the same write. The
        rise in the CURRENT stat still has to come from somewhere, so this only
        misfires if the client refills on level-up as well.

        (Revive used to be the big one and no longer is - it goes through
        /api/character/revive and leaves a grant row of its own.)

        Anything that survives all of that is a client claiming health it did not
        earn.
        """
        # MEASURED FROM WHEN THE POOLS WERE LAST WRITTEN, not from updated_at. A
        # kill, a loot take, a skill tick or an equip all move updated_at without
        # touching hp or mana, and measuring from them gave a player who stood still
        # for fifteen seconds and then killed something two seconds' worth of
        # regeneration - found on day 1 as "mana +19 vs regen 8" while looting coins.
        # A row written before pools_at existed reads 0 there and falls back.
        since = int(before["pools_at"] or 0) if "pools_at" in before.keys() else 0
        elapsed = max(
            float(int(time.time()) - (since or int(before["updated_at"] or 0))),
            minimum_elapsed(),
        )

        risen = {}
        for field, cap_field in (("hp", "max_hp"), ("mana", "max_mana"), ("stamina", "max_stamina")):
            gained = int(after[field]) - int(before[field] or 0)
            if gained <= 0:
                continue

            ceiling = max(int(after[cap_field]), 1)

            # Capped at the headroom that existed, because regeneration cannot take
            # you past your own maximum however long you waited. Without this cap a
            # client that simply saves rarely is handed an unlimited allowance.
            #
            # ELAPSED IS THE WHOLE WINDOW, even though regen only runs after
            # REGEN_IDLE_THRESHOLD seconds of standing still and any action resets
            # it. The server cannot see movement, so it assumes the most generous
            # case. That makes this an upper bound rather than a tight one, which
            # is the right direction to be wrong in for a check that must never
            # fire on honest play.
            allowance = min(
                elapsed * catalogue.regen_rate_for(ceiling) * heal_margin(),
                float(ceiling - int(before[field] or 0)),
            )

            if gained > allowance:
                risen[field] = (gained, allowance)

        if not risen:
            return {}

        # WHAT THE SERVER AUTHORISED IN THE WINDOW, by pool.
        since = int(time.time()) - explain_window()
        granted = {"hp": 0.0, "mana": 0.0, "stamina": 0.0}
        for row in data.grants_since(db, user_id, slot, since):
            if row["item_id"] in refill_grants:
                # All three fill every pool to its maximum, so any of them explains
                # any rise in any of them. They are special cases rather than large
                # amounts because "as much as you had room for" is not a number the
                # server can write down in advance.
                return {}
            target = str(row["target"] or "").lower()
            if target in granted:
                granted[target] += float(row["amount"] or 0)
            elif row["amount"] == 0 and target == "":
                # A grant written before the target/amount columns existed, or a
                # potion from a gamedata.json that predates the export. Unknown
                # rather than zero: it explains nothing on its own, but it must not
                # be reported as a cheat either, because the server genuinely does
                # not know what it did.
                return {}

        still_unexplained = {
            field: (gained, allowance)
            for field, (gained, allowance) in risen.items()
            if gained > allowance + granted.get(field, 0.0)
        }
        if not still_unexplained:
            return {}

        # THE LINE SAYS WHAT WAS ALLOWED AND WHAT WAS GRANTED, per pool, because
        # "unexplained heal" on its own is not something anyone can act on. The old
        # message ended "(no consume in 30s)", which stopped being true the moment
        # a grant could be partial - a potion may well have been drunk; it just did
        # not cover the jump.
        app.logger.warning(
            "unexplained heal: user=%s slot=%s elapsed=%.1fs  %s  (window %ds)",
            user_id, slot, elapsed,
            "  ".join(
                "%s +%d vs regen %.0f + granted %.0f" % (field, gained, allowance, granted.get(field, 0.0))
                for field, (gained, allowance) in sorted(still_unexplained.items())
            ),
            explain_window(),
        )

        # THE ENFORCED LINE. Everything above this point is the 1.25x measurement
        # and is unchanged; this is the only part that acts.
        #
        # The ratio is taken against the TIGHT allowance, so widening the enforced
        # margin never widens what gets logged. The two numbers stay independent on
        # purpose: one is the honest estimate, the other is how much benefit of the
        # doubt is currently being given, and conflating them is how a temporary
        # margin quietly becomes the definition of honest.
        if not clamp_enforced():
            # Measured and reported above; nothing is corrected. See the constant.
            return {}

        corrections = {}
        for field, (gained, allowance) in still_unexplained.items():
            ceiling = allowance + granted.get(field, 0.0)
            if gained <= ceiling * (clamp_margin() / heal_margin()):
                # Between the lines. Logged above, deliberately not trimmed - this
                # is the band that decides whether the tight margin is safe.
                continue
            corrections[field] = int(before[field] or 0) + int(ceiling)

        if corrections:
            app.logger.warning(
                "heal clamped: user=%s slot=%s  %s",
                user_id, slot,
                "  ".join("%s %s -> %d" % (field, after[field], value)
                          for field, value in sorted(corrections.items())),
            )
        return corrections

    def player_status():
        """
        Live stat values for the HUD
        ---
        tags:
          - Player
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
            description: Character slot, 0-3
        responses:
          200:
            description: Current stats for that slot
          400:
            description: Missing or out-of-range slot
          404:
            description: That slot is empty
          401:
            description: Missing, invalid or expired token
        """
        slot = parse_slot(request.args.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        row = data.row_of(get_db(), g.user["id"], slot)

        # 404 is the right answer for an empty slot: the request was valid, the
        # resource does not exist. The client distinguishes this from a transport
        # failure and shows "no character here" instead of "server is down".
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        return services.view(row), 200

    def write_player_status():
        """
        Push current stat values for one character slot
        ---
        tags:
          - Player
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
              required: [slot]
              properties:
                slot:        {type: integer, example: 0}
                level:       {type: integer, example: 12}
                hp:          {type: integer, example: 88}
                max_hp:      {type: integer, example: 120}
                mana:        {type: integer, example: 30}
                max_mana:    {type: integer, example: 60}
                stamina:     {type: integer, example: 45}
                max_stamina: {type: integer, example: 50}
                gold:        {type: integer, example: 1450}
                xp:          {type: integer, example: 15320}
                xp_to_next:  {type: integer, example: 2100}
        responses:
          200:
            description: The full status after the write
          400:
            description: A field was not a non-negative integer, or hp exceeded max_hp
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

        row = data.row_of(get_db(), user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # PARTIAL UPDATE: only fields actually present are touched. A client that
        # knows nothing about stamina can still push hp without silently zeroing
        # everything it didn't mention.
        # RECOMPUTED BEFORE ANYTHING IS VALIDATED, so that hp is checked against the
        # maximum the server believes in rather than the one the client sent. A
        # client declaring max_hp = 999999 alongside hp = 999999 would otherwise pass
        # the paired check below on its own say-so.
        derived = derived_stats(row)

        updates = {}
        ignored = []
        for field in STATUS_FIELDS:
            if field not in payload:
                continue

            # Derived fields are dropped from the request for the same reason as
            # owned ones, but they are then written back from the curve - see below.
            if derived is not None and field in DERIVED_STATS:
                ignored.append(field)
                continue

            # IGNORED, NOT REFUSED. A 400 here would break every honest save: the
            # client sends its whole status block and has no way to know which
            # fields the server has taken ownership of. Dropping them silently and
            # reporting which ones were dropped lets an honest client carry on and
            # gives a dishonest one nothing.
            if field in SERVER_OWNED_STATS:
                ignored.append(field)
                continue

            value = parse_stat(payload[field])
            if value is None:
                return bad_request(
                    "%s must be a non-negative integer no greater than %d" % (field, stat_ceiling)
                )
            updates[field] = value

        if not updates and not ignored:
            return bad_request("no writable fields supplied")

        # Validate against the state AFTER the merge, not against what was sent.
        # Pushing hp=120 alone is illegal if stored max_hp is 100, but legal in the
        # same request that raises max_hp to 120 - and order of keys in JSON must
        # not decide which.
        merged = {field: row[field] for field in STATUS_FIELDS}
        merged.update(updates)

        if derived is not None:
            # The server's own numbers go in AFTER the client's, so they win, and
            # they are added to `updates` so they are actually written - a character
            # that levelled up needs its new maximum stored, not just enforced.
            merged.update(derived)
            updates.update(derived)

        for field, cap_field in STATUS_FIELDS.items():
            if cap_field is None:
                continue
            if merged[field] > merged[cap_field]:
                # CLAMPED, not refused, when the ceiling is one the server derived.
                #
                # A character at full health who levels DOWN - or whose class curve
                # is retuned downward between releases - legitimately arrives with
                # hp above the new max_hp. Refusing would make that character
                # unsaveable. Clamping is the honest reading: you have as much health
                # as the curve allows.
                if cap_field in DERIVED_STATS and derived is not None:
                    merged[field] = merged[cap_field]
                    updates[field] = merged[cap_field]
                    continue
                return bad_request(
                    "%s (%d) cannot exceed %s (%d)" % (field, merged[field], cap_field, merged[cap_field])
                )

        db = get_db()
        if updates:
            # BEFORE THE WRITE, because it compares the stored row to the merged
            # one and the stored row is about to stop existing.
            #
            # IT CORRECTS RATHER THAN REFUSING, and the corrections go into BOTH
            # `merged` and `updates`: merged is what the rest of this function
            # reasons about, updates is what actually reaches the UPDATE statement.
            # Writing only one of them is how a clamp becomes decorative - the log
            # would say "clamped" and the client's figure would be stored anyway.
            healed = reconcile_heals(db, user_id, slot, row, merged)
            for field, value in healed.items():
                merged[field] = value
                updates[field] = value

            # A DEATH IS A TRANSITION, NOT A STATE, and counting it as a state is
            # the bug worth not writing. A dead client keeps reporting its status
            # while the death screen is up, so "hp is 0" is true over and over for
            # as long as the player sits there deciding whether to pay. Counting
            # that would let anybody run their death total to a million by leaving
            # the screen open, and would make the figure meaningless for everybody
            # who did not.
            #
            # So: the STORED row still had hp above zero and the merged one does
            # not. That happens exactly once per death.
            #
            # AFTER _reconcile_heals(), because the reconciler can correct the hp
            # the client sent, and the corrected figure is the one that gets
            # stored - reading before it would count a death the save never
            # recorded.
            if "hp" in updates and int(row["hp"]) > 0 and int(updates["hp"]) <= 0:
                data.count_death(db, user_id)

            # pools_at moves only when a pool is written - see the migration.
            if any(f in updates for f in ("hp", "mana", "stamina")):
                updates["pools_at"] = int(time.time())
            data.write(db, user_id, slot, updates, int(time.time()))
            db.commit()

        result = status_of(user_id, slot)
        if ignored:
            # The server's own values are already in `result`; this just names what
            # was disregarded, so a client can see it is being corrected rather than
            # wondering why its level did not stick.
            result["ignored"] = ignored
        return result, 200

    app.add_url_rule("/api/player/status", endpoint="player_status",
                     view_func=require_auth(player_status), methods=["GET"])
    app.add_url_rule("/api/player/status", endpoint="write_player_status",
                     view_func=require_auth(write_player_status), methods=["PUT"])

    return SimpleNamespace(status_of=status_of, reconcile_heals=reconcile_heals)
