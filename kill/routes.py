"""kill/routes.py - the edge: a kill reported and paid for, as it was in
app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth       the token check (puts the account on g.user)
    get_db             the request's database connection
    bad_request        a message -> app.py's 400
    parse_slot         a raw slot -> its int, or None
    max_slot           the highest slot, for the 400's words
    catalogue          gamedata: every monster, how many are placed, the roll,
                       the XP curve, the item names - asked on every call, so a
                       suite's seeded die or its held roll is heard
    stir_for           (db, enemy_id) -> (bag scale, XP scale): the stirring
                       lands (world/'s, registered later - a lookup)
    derived_stats      (row, level=) -> the maxima a level brings
    levelup_grant_id   the id the level-up's refill is noted under
    note_refill        (db, user_id, slot, item_id, at) - the refill written
                       where the healing check reads it (status/'s, the one
                       writer); no commit
    proficient_amount  (class_id, skill_id, raw) -> the XP with the class's
                       specialty applied
    grant_skill_xp     (user_id, slot, skill_id, gained) -> (level, xp, levels
                       gained); no commit (skills/'s, registered later)
    post_broadcast     (body, kind, by): the server's voice; no commit
    create_loot_bag    (user_id, slot, enemy_id, contents) -> (bag_id, stored):
                       the bag stored and committed (loot/'s create_bag(),
                       registered later)

THE ENDPOINT NAME IS app.py's - combat_kill - and routes.lock holds it.
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, catalogue, stir_for, derived_stats,
             levelup_grant_id, proficient_amount, grant_skill_xp, post_broadcast, create_loot_bag,
             note_refill):

    def combat_kill():
        """
        Report a kill and receive its rewards
        ---
        tags:
          - Combat
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
              required: [slot, enemy_id]
              properties:
                slot:     {type: integer, example: 0}
                enemy_id: {type: string,  example: poisonslimesmall}
        responses:
          200:
            description: XP granted, level-ups applied, and whatever the loot roll produced
          400:
            description: Bad slot, unknown enemy_id, or an enemy that awards nothing
          404:
            description: That slot is empty
          429:
            description: Kills are arriving faster than the cooldown allows
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        enemy_id = str(payload.get("enemy_id", "")).strip()
        if not enemy_id or len(enemy_id) > 64:
            return bad_request("enemy_id must be 1-64 characters")

        enemy = catalogue.ENEMIES.get(enemy_id)
        if enemy is None:
            # Deliberately names the id back. This is not user input in the hostile
            # sense - it is the client and the server disagreeing about the roster,
            # which in practice means gamedata.json is stale on one side. Saying
            # which id was not found is the difference between a five-minute fix
            # and an afternoon.
            return bad_request("Unknown enemy_id '%s'. Is gamedata.json current?" % enemy_id)

        # The large poison slime is the case this exists for: it never dies, it
        # splits, and everything it is worth walks away as four smalls. A client
        # reporting it as a kill is either broken or lying, and either way there is
        # nothing to pay.
        if not enemy.get("grants_rewards", True):
            return bad_request("'%s' awards no rewards - it cannot be killed for profit." % enemy_id)

        db = get_db()
        row = data.character(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        now_ms = int(time.time() * 1000)

        # Refill first, then spend - see services.tokens_now().
        tokens = services.tokens_now(row["kill_tokens"], row["last_kill_at"], now_ms)

        if tokens < 1.0:
            # 429 rather than 400: nothing about the request is malformed, it simply
            # arrived faster than the bucket allows. An honest client can read that
            # and retry; a 400 would tell it to give up.
            return {
                "error": "Too Many Requests",
                "message": "Kills are arriving faster than %g per second." % services.KILL_TOKENS_PER_SECOND,
            }, 429

        tokens -= 1.0

        # THE SPAWN CEILING. Read services.KILL_WINDOW_SECONDS for the arithmetic; this is
        # the half that asks whether this many of THIS enemy could have died.
        #
        # Counted out of kill_reports rather than a new table: it already holds one
        # row per PAID kill, indexed on (user_id, at), and a refused kill leaves no
        # row - so the count is exactly "kills this account was paid for" and
        # cannot drift from what was granted.
        #
        # FAILS OPEN TWICE, both deliberate. A gamedata.json exported before the
        # spawn tally existed knows nothing about the world and must not refuse
        # every kill on a half-upgraded server. And a placed_count of 0 means
        # "this comes from somewhere I cannot see" - the poison slime's smalls are
        # spawned by its own script and appear in no scene - where refusing would
        # break a real fight. See gamedata.spawn_count_for().
        placed = catalogue.spawn_count_for(enemy_id) if catalogue.SPAWNS_EXPORTED else 0
        if placed > 0:
            window_start = int(time.time()) - services.KILL_WINDOW_SECONDS
            recent = data.paid_recently(db, user_id, enemy_id, window_start)
            ceiling = services.spawn_ceiling(placed)
            if recent >= ceiling:
                # 429, matching the bucket above: this is "not yet", not "never".
                # The message names the world rather than a policy, because that is
                # where the number came from and a player reading it can check it.
                return {
                    "error": "Too Many Requests",
                    "message": "Only %d '%s' exist in the world - that is more kills "
                               "than could have respawned in the last %d minutes."
                               % (placed, enemy_id, services.KILL_WINDOW_SECONDS // 60),
                }, 429

        # ---- everything above this line is validation; everything below commits --

        # The killer's class goes in for the mythic roll: a win is their own
        # class's piece. See gamedata.roll_mythic().
        # THE STIRRING LANDS: a monster of the land stirring now drops loot more
        # often and gives more XP, as the owner has set it (world/'s stir_for()).
        bag_scale, xp_scale = stir_for(db, enemy_id)
        rewards = catalogue.roll_kill_rewards(enemy_id, row["class_id"], bag_scale, xp_scale)

        level, xp, xp_to_next, levels_gained = catalogue.apply_xp(
            int(row["level"]), int(row["xp"]), int(row["xp_to_next"]), rewards["xp"]
        )

        # A LEVEL-UP MOVES THE MAXIMA WITH IT.
        #
        # Without this the level changed here and max_hp did not, so a character who
        # levelled from a kill carried the previous level's maximum until something
        # happened to write a status - and /api/player/status GET returns the stored
        # row, so the player would simply see the wrong number.
        #
        # Found by a test asserting max_hp against the curve rather than against a
        # hardcoded 180: the moment a kill-burst test started levelling the
        # character up, the stale value stopped matching.
        derived = derived_stats(row, level=level) if levels_gained else None

        if levels_gained:
            # THE REFILL THAT COMES WITH A LEVEL, recorded so the healing
            # reconciler does not read it as a cheat. player.gd::level_up() fills
            # all three pools, which is a jump from wherever you were to full in no
            # time at all - indistinguishable, from the outside, from a client that
            # simply claimed it.
            #
            # Written here rather than where the client syncs, because this is the
            # moment the server DECIDED the level-up happened. Same table and same
            # reasoning as the revive grant.
            note_refill(db, user_id, slot, levelup_grant_id, int(time.time()))

        # ONE UPDATE. The XP, the level and the cooldown stamp move together or not
        # at all - the same reasoning as the bank gold transfer. Two statements
        # would leave a window where a crash could bank the XP and lose the level,
        # or stamp the cooldown for a kill that was never paid.
        data.bank(db, user_id, slot, level, xp, xp_to_next, derived, now_ms, tokens, int(time.time()))

        # ATTACK XP IS BANKED HERE, not returned for the client to apply.
        #
        # The number was always the server's - rewards["attack_xp"] comes from the
        # enemy's own profile and the client never names it. What used to happen is
        # that it was handed back and the client was trusted to add it up and PUT
        # the total to /api/character/skills, so an authored reward became a claim
        # somewhere in the round trip. Granting it at the moment of the kill removes
        # the trip the lie lived in.
        #
        # BEFORE THE COMMIT, AND THAT IS NOT INCIDENTAL. grant_skill_xp() does not
        # commit - it expects its caller's transaction, the way the fishing and
        # cooking routes use it. Called after this commit it inserts into a
        # transaction nothing ever finishes, and the row is discarded at the end of
        # the request: the response still reports a level, the database never sees
        # it. Sharing the commit also means the character's XP and the attack XP
        # from one kill can never be stored apart from each other.
        #
        # THE CLASS SPECIALTY APPLIES. A warrior trains attack half again as fast
        # (SKILL_PROFICIENCY), and this is the only place attack trains.
        attack_gained = proficient_amount(row["class_id"], "attack", rewards["attack_xp"])
        attack_level, attack_xp, attack_levels = grant_skill_xp(
            user_id, slot, "attack", attack_gained
        )

        # Recorded in the same transaction, for the same reason the grant is: a kill
        # that paid out and left no trace, or a trace for a kill that was rolled
        # back, would both make the log a thing you cannot reason from.
        # AS PAID: the attack XP the character actually banked, specialty included.
        data.record_kill(db, user_id, slot, enemy_id, dict(rewards, attack_xp=attack_gained),
                         level, now_ms // 1000)

        db.commit()

        # THE BAG IS STORED, and its id goes back with the contents. The client
        # spawns a node to render it, but the node is a picture: taking anything out
        # of it goes through /api/loot/take, against these rows.
        #
        # An empty bag_id means nothing dropped, and the client spawns nothing.
        #
        # `stored` is the roll with a position stamped on every entry - which is
        # what the client renders into its grid and what it sends back to take
        # anything out. The roll itself is never returned: position is not a detail
        # the two sides should be inferring separately.
        # A MYTHIC IS TOLD TO EVERYONE ONLINE, through the broadcast poll every
        # client already runs, as kind "mythic". The game draws that as a red
        # banner, and the finder, whose game is already celebrating, keeps only
        # the chat line (it matches "by" to its own name).
        #
        # Before create_loot_bag(), so the notice and the bag land in one commit
        # (post_broadcast() leaves the commit to its caller). It cannot announce
        # a piece the bag then dropped: rarest_first() ranks a mythic with the pet,
        # ahead of everything LOOT_BAG_CAPACITY could cut.
        mythic = rewards.get("mythic", "")
        if mythic:
            post_broadcast(services.mythic_find_text(catalogue, g.user["username"], mythic, enemy_id),
                           services.MYTHIC_BROADCAST_KIND, g.user["username"])
            app.logger.info("mythic: %s (slot %d, %s) found %s on %s",
                            g.user["username"], slot, row["class_id"], mythic, enemy_id)

        bag_id, stored = create_loot_bag(user_id, slot, enemy_id, rewards["contents"])

        return {
            "bag_id": bag_id,
            "enemy_id": enemy_id,
            "xp_gained": rewards["xp"],
            # WHAT WAS BANKED, specialty included, and where it left the skill -
            # the client sets its attack bar to these rather than adding up its
            # own, so the screen and the record cannot drift apart.
            "attack_xp_gained": attack_gained,
            "attack_level": attack_level,
            "attack_xp": attack_xp,
            "attack_xp_to_next": catalogue.xp_needed_for_skill_level("attack", attack_level),
            "attack_levelled_up": attack_levels > 0,
            "levels_gained": levels_gained,
            "level": level,
            "xp": xp,
            "xp_to_next": xp_to_next,
            "pet_won": rewards["pet_won"],
            # The mythic item_id in this bag, or "". The game celebrates on it.
            "mythic": mythic,
            "contents": stored,
        }, 200

    app.add_url_rule("/api/combat/kill", endpoint="combat_kill",
                     view_func=require_auth(combat_kill), methods=["POST"])
