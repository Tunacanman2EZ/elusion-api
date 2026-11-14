"""minigames/routes.py - the edge (game 0.93.3; the kill 0.93.4).

    GET  /api/minigames         every minigame and your wait
    POST /api/minigames/enter   going in: 409 while your wait runs

register() is handed app.py's own pieces - it never imports app.py:

    require_auth   the token check (puts the account on g.user)
    get_db         the request's database connection
    bad_request    app.py's 400 answer
    skips_wait     (username) -> whether this account skips every wait: the
                   owner and a co-owner while the switch is on

AND FOR A MINIGAME'S KILLS (game 0.93.4):

    POST /api/minigames/kill    an Endless elite or boss killed: its loot,
                                rolled here, into the character's chest

    parse_slot     a raw slot -> its int, or None
    max_slot       the highest slot, for the 400's words
    catalogue      gamedata: ENDLESS and FORGE (the runs' schedules), ENEMIES and
                   roll_kill_rewards() - asked on every call, so a suite's
                   seeded die is heard
    class_of       (db, user_id, slot) -> the character's class_id, or None
                   for an empty slot
    material_scale (db) -> the owner's material_drop_scale (economyrules/)
    store_in_chest (db, user_id, slot, game, contents) -> (cells used, cut):
                   loot/'s, no commit
    clear_chests   (db, user_id, game): loot/'s, at the next Go; no commit
    post_broadcast (body, kind, by): a mythic's notice; no commit
    mythic_text    (username, item_id, enemy_id) -> the notice (kill/'s)
    mythic_kind    the notice's kind (kill/'s)

AND THE FORGE (game 0.120.0): its fire sprites pay into the chest through
/kill (gamedata's FORGE, the run's schedule), and a run's time is kept:

    POST /api/minigames/best    how long a run held, kept when it is your best

AND THE HEALER'S RUN (game 0.99.0): three Go's a day, and its gold at Leave:

    POST /api/minigames/finish  a run's end - how far it got - and its gold,
                                paid once into the character's purse

    gold_delta     app.py's gold_delta(): the only way gold changes, ledger
                   and balance together; no commit

AND A DAY'S MINIGAME GOLD (14 Nov, services.py): every run's gold and every
chest's coins counted against one allowance an account a day:

    gold_a_day     () -> the cap, ELUSION_MINIGAME_GOLD_DAY's (0: none); None
                   is no cap (a bare app)
    money_worth    (item_id) -> the gold one of it cashes into, 0 for anything
                   that is not gold (app.py's money_item_worth())
"""
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, skips_wait, parse_slot, max_slot, catalogue,
             class_of, material_scale, store_in_chest, clear_chests, post_broadcast, mythic_text,
             mythic_kind, gold_delta=None, gold_a_day=None, money_worth=None):
    """Add the five routes."""

    def cap_now():
        """The day's allowance for the caller: none for the owner and a co-owner
        while let in, who skip the waits too (they test the minigames)."""
        if gold_a_day is None or skips_wait(g.user["username"]):
            return 0
        return int(gold_a_day())

    def day_paid(db, now):
        return data.gold_since(db, g.user["id"], now - services.DAY)

    def held_words(held):
        return " (%d held back: the day's minigame gold is full)" % held if held > 0 else ""

    def listing(db, now):
        entries = data.entries_of(db, g.user["id"])
        bests = data.bests_of(db, g.user["id"])
        skip = bool(skips_wait(g.user["username"]))
        games = []
        for game_id, game in services.GAMES.items():
            entered = entries.get(game_id, 0)
            plays = services.plays_of(game)
            if plays > 1:
                starts = data.starts_since(db, g.user["id"], game_id, now - game["wait"])
                left = 0 if skip else services.ready_in_plays(starts, plays, game["wait"], now)
                plays_left = plays if skip else max(0, plays - len(starts))
            else:
                left = 0 if skip else services.ready_in(entered, game["wait"], now)
                plays_left = 0 if left > 0 else 1
            row = {"id": game_id, "name": game["name"], "wait_seconds": game["wait"],
                   "entered_at": entered, "ready_in": left, "plays": plays, "plays_left": plays_left}
            # A timed minigame's best (game 0.120.0, the Forge's): seconds, 0 for none.
            if game.get("times"):
                row["best"] = bests.get(game_id, 0)
            games.append(row)
        return {"games": games, "skips_wait": skip}

    def minigames_list():
        """
        Every minigame, and how long until you may go in again
        ---
        tags:
          - Minigames
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: "games: id, name, wait_seconds, entered_at (0 never), ready_in (0 now), and a timed one's best (seconds); skips_wait"
          401:
            description: Not logged in
        """
        return listing(get_db(), int(time.time())), 200

    def minigames_enter():
        """
        Go into a minigame - the wait after it starts now, and a second time inside it is refused
        ---
        tags:
          - Minigames
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
              required: [game]
              properties:
                game: {type: string, description: "a minigame's id: endless, healerrun, forge, cloth"}
        responses:
          200:
            description: "in: game, entered_at, ready_in (the next wait)"
          400:
            description: "not a minigame"
          409:
            description: "your wait is still running: ready_in seconds, and the message says how long"
          401:
            description: Not logged in
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return bad_request("body must be a JSON object")
        game_id = payload.get("game")
        game = services.GAMES.get(game_id) if isinstance(game_id, str) else None
        if game is None:
            return bad_request("no such minigame")
        db = get_db()
        now = int(time.time())
        skip = bool(skips_wait(g.user["username"]))
        if services.plays_of(game) > 1:
            return enter_a_run(db, game_id, game, payload, now, skip)
        left = services.ready_in(data.entries_of(db, g.user["id"]).get(game_id, 0), game["wait"], now)
        if left > 0 and not skip:
            return {"error": "Conflict", "ready_in": left,
                    "message": "%s opens for you again in %s." % (game["name"], services.wait_words(left))}, 409
        data.enter(db, g.user["id"], game_id, now)
        # THE CHEST IS KEPT UNTIL YOUR NEXT GO (game 0.93.4): a new day starts
        # with it empty. The Minigames card warns before Leave with loot in it.
        clear_chests(db, g.user["id"], game_id)
        db.commit()
        return {"ok": True, "game": game_id, "entered_at": now,
                "ready_in": 0 if skip else int(game["wait"])}, 200

    def enter_a_run(db, game_id, game, payload, now, skip):
        """A Go into a game played several times a day: a run of its own for
        the character in `slot`, while the account has one left."""
        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        if class_of(db, g.user["id"], slot) is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404
        plays = services.plays_of(game)
        starts = data.starts_since(db, g.user["id"], game_id, now - game["wait"])
        left = services.ready_in_plays(starts, plays, game["wait"], now)
        if left > 0 and not skip:
            return {"error": "Conflict", "ready_in": left, "plays_left": 0,
                    "message": "You have run %s %d times today. Your next run is in %s."
                               % (game["name"], plays, services.wait_words(left))}, 409
        run_id = data.start_run(db, g.user["id"], slot, game_id, now)
        data.enter(db, g.user["id"], game_id, now)
        db.commit()
        plays_left = plays if skip else max(0, plays - len(starts) - 1)
        return {"ok": True, "game": game_id, "entered_at": now, "run": run_id, "plays_left": plays_left,
                "ready_in": 0 if skip or plays_left > 0 else services.ready_in_plays(
                    starts + [now], plays, game["wait"], now),
                "pays": services.pays_for(game_id)}, 200

    def minigames_finish():
        """
        The end of a Healer's Run or a Cut the Cloth run - how far it got - and its gold, paid once into the character's purse
        ---
        tags:
          - Minigames
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
              required: [run, waves]
              properties:
                run:   {type: integer, description: "the run's id, from its Go"}
                waves: {type: integer, description: "the Healer's Run: waves passed, 0-24 (0-25 before game 0.100.0)"}
                rounds: {type: integer, description: "Cut the Cloth (game 0.131.0): rounds cleared"}
                bosses: {type: integer, description: "bosses beaten, 0-3 (game 0.100.0's tiers)"}
                boss:  {type: boolean, description: "before game 0.100.0: the gate's boss beaten"}
                goal:  {type: boolean, description: "the goal marker reached"}
        responses:
          200:
            description: "gold paid, the waves, boss and goal believed (Cut the Cloth: the rounds believed), and the purse after (gold)"
          400:
            description: "a bad body"
          404:
            description: "no such run of yours"
          409:
            description: "that run was already finished, or is too old to pay"
          401:
            description: Not logged in
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return bad_request("body must be a JSON object")
        run_id = payload.get("run")
        if isinstance(run_id, bool) or not isinstance(run_id, int):
            return bad_request("run must be a whole number")
        db = get_db()
        now = int(time.time())
        run = data.run_of(db, run_id)
        if run is None or int(run["user_id"]) != int(g.user["id"]):
            return {"error": "Not Found", "message": "No such run."}, 404
        if str(run["game"]) == "cloth":
            return finish_cloth(db, run, payload, now)
        waves = payload.get("waves")
        if isinstance(waves, bool) or not isinstance(waves, int) or waves < 0:
            return bad_request("run and waves must be whole numbers")
        seconds = now - int(run["started_at"])
        if int(run["finished_at"]) != 0:
            return {"error": "Conflict", "message": "That run is already finished."}, 409
        # A game from 0.100.0 says how many bosses it beat (the tiers); one
        # from before says whether it beat the one boss, and is paid by the
        # old table.
        tiered = "bosses" in payload
        if tiered:
            bosses_said = payload.get("bosses")
            if isinstance(bosses_said, bool) or not isinstance(bosses_said, int) or bosses_said < 0:
                return bad_request("bosses must be a whole number")
            believed_waves, bosses, goal = services.healer_tier_believed(waves, bosses_said,
                                                                         payload.get("goal") is True, seconds)
            gold = services.healer_tier_gold(believed_waves, bosses, goal)
            boss = bosses == services.HEALER_TIERS
        else:
            believed_waves, boss, goal = services.healer_believed(waves, payload.get("boss") is True,
                                                                  payload.get("goal") is True, seconds)
            gold = services.healer_gold(believed_waves, boss, goal)
            bosses = 1 if boss else 0
        if seconds > services.HEALER_RUN_KEPT_SECONDS:
            gold = 0
        # A DAY'S MINIGAME GOLD: what is left of the account's allowance.
        gold, held = services.gold_allowed(gold, day_paid(db, now), cap_now())
        if not data.finish_run(db, run_id, g.user["id"], now, believed_waves, bosses, goal, gold):
            return {"error": "Conflict", "message": "That run is already finished."}, 409
        purse = None
        if gold > 0 and gold_delta is not None:
            beaten = (", %d of %d bosses" % (bosses, services.HEALER_TIERS) if bosses else "") if tiered \
                else (", the boss" if boss else "")
            purse = gold_delta(db, g.user["id"], int(run["slot"]), gold, "minigame",
                               "%s run %d: %d waves%s%s%s" % (run["game"], run_id, believed_waves,
                                                              beaten, ", the goal" if goal else "",
                                                              held_words(held)))
            if purse is None:
                gold = 0
        db.commit()
        answer = {"ok": True, "gold": gold, "waves": believed_waves, "boss": boss, "goal": goal,
                  "held_back": held, "status": {"gold": int(purse)} if purse is not None else {}}
        if tiered:
            answer["bosses"] = bosses
        return answer, 200

    def finish_cloth(db, run, payload, now):
        """A Cut the Cloth run's end (game 0.131.0): the rounds it cleared,
        held to the time since its Go, and their gold paid - once."""
        rounds = payload.get("rounds")
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
            return bad_request("rounds must be a whole number")
        if int(run["finished_at"]) != 0:
            return {"error": "Conflict", "message": "That run is already finished."}, 409
        seconds = now - int(run["started_at"])
        believed = services.cloth_believed(rounds, seconds)
        gold = services.cloth_gold(believed)
        if seconds > services.HEALER_RUN_KEPT_SECONDS:
            gold = 0
        gold, held = services.gold_allowed(gold, day_paid(db, now), cap_now())
        bosses = believed // services.CLOTH_BOSS_EVERY
        if not data.finish_run(db, int(run["id"]), g.user["id"], now, believed, bosses, False, gold):
            return {"error": "Conflict", "message": "That run is already finished."}, 409
        purse = None
        if gold > 0 and gold_delta is not None:
            purse = gold_delta(db, g.user["id"], int(run["slot"]), gold, "minigame",
                               "cloth run %d: %d rounds%s" % (int(run["id"]), believed, held_words(held)))
            if purse is None:
                gold = 0
        db.commit()
        return {"ok": True, "gold": gold, "rounds": believed, "bosses": bosses, "held_back": held,
                "status": {"gold": int(purse)} if purse is not None else {}}, 200

    def minigames_kill():
        """
        An Endless elite or boss, or a Forge fire sprite, killed - its loot rolled into your chest (no XP)
        ---
        tags:
          - Minigames
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
              required: [game, slot, enemy_id]
              properties:
                game:     {type: string,  description: "endless or forge"}
                slot:     {type: integer, example: 0}
                enemy_id: {type: string,  example: lightsprite}
        responses:
          200:
            description: "the roll (added), what a full chest could not keep (cut), the chest's id and cells used, mythic, pet_won"
          400:
            description: "not a minigame that pays, a bad slot, or a monster the run never sends"
          404:
            description: That slot is empty
          409:
            description: "you have not gone in today: no Go inside the minigame's day"
          429:
            description: "more kills than the run could have sent since your Go"
          401:
            description: Not logged in
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return bad_request("body must be a JSON object")
        game_id = payload.get("game")
        game = services.GAMES.get(game_id) if isinstance(game_id, str) else None
        # Each paying game's schedule, gamedata.json's own (the Forge's since
        # game 0.120.0). A catalogue without one pays nothing for that game.
        schedules = {"endless": getattr(catalogue, "ENDLESS", {}), "forge": getattr(catalogue, "FORGE", {})}
        schedule = schedules.get(game_id, {}) if game is not None else {}
        if game is None or not schedule:
            return bad_request("no such minigame pays for a kill")
        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        enemy_id = str(payload.get("enemy_id", "")).strip()
        kind = services.sends(schedule, enemy_id)
        enemy = catalogue.ENEMIES.get(enemy_id)
        if not kind or enemy is None or not enemy.get("grants_rewards", True):
            return bad_request("'%s' is not a monster %s sends." % (enemy_id[:64], game["name"]))

        db = get_db()
        user_id = g.user["id"]
        class_id = class_of(db, user_id, slot)
        if class_id is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        # INSIDE YOUR DAY: after your Go, and within its wait. The Go is the
        # account's (the owner's is written too, though the owner skips the
        # wait), so this is the one place the day stops being a courtesy.
        now = int(time.time())
        entered = data.entries_of(db, user_id).get(game_id, 0)
        if not entered or now - entered >= int(game["wait"]):
            return {"error": "Conflict",
                    "message": "You have not gone into %s today." % game["name"]}, 409

        # NO MORE THAN THE RUN COULD HAVE SENT since the Go (services.py says
        # how that is counted). 429: "not yet", as the kill route says it.
        boss = kind == "boss"
        if kind == "sprite":
            ceiling = services.forge_ceiling(schedule, now - entered)
        else:
            ceiling = (services.bosses_ceiling if boss else services.elites_ceiling)(schedule, now - entered)
        if data.kills_since(db, user_id, game_id, entered, boss) >= ceiling:
            return {"error": "Too Many Requests",
                    "message": "More %s than %s could have sent since you went in."
                               % (services.KIND_WORDS.get(kind, "monsters"), game["name"])}, 429

        # LOOT ONLY (the owner, 28 Oct: "Loot only"): the monster's own table
        # as in the Field - gold, gear, materials, its pet and mythic odds -
        # and none of its XP. Into the chest, in this kill's transaction.
        # THE FORGE'S RARER PETS AND MYTHICS (game 0.121.0, services.py):
        # each one in N ten times longer, and one pet since the Go.
        forge = game_id == "forge"
        rewards = catalogue.roll_kill_rewards(
            enemy_id, class_id, 1.0, 1.0, material_drop_scale=material_scale(db),
            rare_odds_scale=services.FORGE_RARE_SCALE if forge else 1,
            pets=not forge or data.pets_since(db, user_id, game_id, entered) < services.FORGE_PETS_A_DAY)
        # A DAY'S MINIGAME GOLD: the coins only as far as the day's allowance
        # goes; the gear, materials and pet always.
        cap = cap_now()
        left = max(0, cap - day_paid(db, now)) if cap > 0 else None
        contents, coin_gold, held = services.coins_within(
            rewards["contents"], money_worth if money_worth is not None else (lambda _id: 0), left)
        used, cut = store_in_chest(db, user_id, slot, game_id, contents)
        data.record_kill(db, user_id, slot, game_id, enemy_id, boss, now, pet=bool(rewards.get("pet_won")),
                         gold=coin_gold)
        mythic = rewards.get("mythic", "")
        if mythic:
            post_broadcast(mythic_text(g.user["username"], mythic, enemy_id), mythic_kind, g.user["username"])
            app.logger.info("mythic: %s (slot %d, %s) found %s on %s in %s",
                            g.user["username"], slot, class_id, mythic, enemy_id, game_id)
        db.commit()
        return {"ok": True, "game": game_id, "enemy_id": enemy_id, "boss": boss,
                "added": contents, "cut": cut, "used": used, "held_back": held,
                "chest_id": "chest:%s:%d" % (game_id, slot),
                "mythic": mythic, "pet_won": bool(rewards.get("pet_won"))}, 200

    def minigames_best():
        """
        How long a run of a timed minigame held - the Forge's - kept when it is your best
        ---
        tags:
          - Minigames
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
              required: [game, seconds]
              properties:
                game:    {type: string,  description: "forge"}
                seconds: {type: integer, description: "how long the run held"}
        responses:
          200:
            description: "seconds (as believed), best (yours now) and new_best"
          400:
            description: "not a timed minigame, or seconds not a whole number"
          409:
            description: "you have not gone in today: no Go inside the minigame's day"
          401:
            description: Not logged in
        """
        # THE TIME IS THE GAME'S WORD, held to what can be true: no longer
        # than since your Go, nor services.FORGE_BEST_MOST, and only inside
        # your day. The honest limit: a modified game could claim that long.
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return bad_request("body must be a JSON object")
        game_id = payload.get("game")
        game = services.GAMES.get(game_id) if isinstance(game_id, str) else None
        if game is None or not game.get("times"):
            return bad_request("no such minigame keeps a time")
        seconds = payload.get("seconds")
        if isinstance(seconds, float) and seconds.is_integer():
            seconds = int(seconds)
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            return bad_request("seconds must be a whole number")
        db = get_db()
        now = int(time.time())
        entered = data.entries_of(db, g.user["id"]).get(game_id, 0)
        if not entered or now - entered >= int(game["wait"]):
            return {"error": "Conflict",
                    "message": "You have not gone into %s today." % game["name"]}, 409
        believed = services.best_believed(seconds, now - entered)
        best, new = data.keep_best(db, g.user["id"], game_id, believed, now)
        db.commit()
        return {"ok": True, "game": game_id, "seconds": believed, "best": best, "new_best": new}, 200

    app.add_url_rule("/api/minigames", endpoint="minigames_list",
                     view_func=require_auth(minigames_list), methods=["GET"])
    app.add_url_rule("/api/minigames/enter", endpoint="minigames_enter",
                     view_func=require_auth(minigames_enter), methods=["POST"])
    app.add_url_rule("/api/minigames/kill", endpoint="minigames_kill",
                     view_func=require_auth(minigames_kill), methods=["POST"])
    app.add_url_rule("/api/minigames/finish", endpoint="minigames_finish",
                     view_func=require_auth(minigames_finish), methods=["POST"])
    app.add_url_rule("/api/minigames/best", endpoint="minigames_best",
                     view_func=require_auth(minigames_best), methods=["POST"])
