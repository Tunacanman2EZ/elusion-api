"""
Minigames and their wait. Run: python3 test_minigames.py

The owner: "we need to get rid of ladder and make this a button in hud that
says minigames this dungeon will have a 1 day wait timer for players", and the
day starts when you press Go. minigames/ (game 0.93.3).

  MG-1  THE LIST: every minigame, its wait, and yours - ready at first.
  MG-2  GO STARTS THE DAY: the first Go is let in, a second inside the day is
        refused (409, how long is left, in words), and the list says so.
  MG-3  A DAY LATER IT OPENS AGAIN; per account, so another character does not
        start a fresh day, and another account has a day of its own.
  MG-4  THE OWNER SKIPS THE WAIT, and is written all the same.
  MG-5  NOT A MINIGAME, NOT JSON, NO LOGIN: 400, 400, 401.

The Endless's chest (game 0.93.4): an elite or boss killed in a run pays its
own loot - no XP, the owner's "Loot only" - into the character's chest
(loot/'s), held to what the run's schedule could have sent since the Go.

  MG-6  A KILL PAYS INTO THE CHEST: only inside your day, only a monster the
        run sends; the roll lands in the chest and nowhere else - no XP, no
        attack XP, no kill record, no bag - and the chest is read and taken
        from like a bag.
  MG-7  THE SCHEDULE IS THE CEILING: elites and bosses since the Go, counted
        from gamedata.json's "endless", the most alive allowed at once, no run
        believed past STAGES_BELIEVED; past it, 429.
  MG-8  THE NEXT GO EMPTIES IT, and a day over pays nothing.
  MG-9  A FULL CHEST keeps the rarest: folds what stacks, and a new find takes
        the least cell's place only if it is worth more.
  MG-10 DELETING THE CHARACTER takes its chest; another account's chest is
        nobody else's to read or take from.
  MG-14 THE FORGE (game 0.120.0): once a day; every fire sprite slain pays
        its loot into the Forge's chest, held to its schedule (gamedata.json's
        "forge", six runs a day at most); a run's time kept when it is your
        best, believed no longer than since the Go.
  MG-15 THE FORGE'S PETS AND MYTHICS (game 0.121.0): each fire sprite's
        pet and mythic odds ten times rarer than the Field's, and one pet a
        Go - the Endless and the Field unchanged.
  MG-13 THE HEALER'S RUN'S TIERS (game 0.100.0): 3 tiers of 8 waves, a boss
        each, then the goal - up to 20,000 (5000 before 0.104.0), 700 of it by
        the second boss; each
        boss believed only with its tier's waves and the time; a game that
        sends `boss` is paid as before (1000 at most).
"""
import importlib.util, math, os, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_minigames_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "mgowner"
os.environ.pop("ELUSION_GAMEDATA", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)


def load_app(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app_module = load_app("elusion_app")
client = app_module.app.test_client()
import minigames.services as services  # noqa: E402

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

PW = "hunter2hunter2"
_ip = [60]
def register(name):
    _ip[0] += 1
    r = client.post("/api/auth/register", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert r.status_code == 201, r.get_json()
    return {"Authorization": "Bearer " + r.get_json()["token"]}

def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(sql, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()

def endless(headers):
    games = (client.get("/api/minigames", headers=headers).get_json() or {}).get("games", [])
    return next((g for g in games if g.get("id") == "endless"), {})

def go(headers, game="endless"):
    return client.post("/api/minigames/enter", headers=headers, json={"game": game})


ann = register("mgann")
bob = register("mgbob")
owner = register("mgowner")
uid = lambda name: raw("SELECT id FROM users WHERE username = ?", (name,))[0][0]

section("MG-1 THE LIST")
first = endless(ann)
check("the Endless is a minigame, a day's wait, and you may go in now",
      first.get("name") == "The Endless" and first.get("wait_seconds") == services.DAY == 86400
      and first.get("ready_in") == 0 and first.get("entered_at") == 0, first)

section("MG-2 GO STARTS THE DAY")
res = go(ann)
body = res.get_json() or {}
check("the first Go is let in, and says the next wait is a day", res.status_code == 200 and body.get("ok") is True
      and body.get("ready_in") == services.DAY, (res.status_code, body))
res = go(ann)
body = res.get_json() or {}
check("a second Go inside the day is refused (409), with how long is left, in words",
      res.status_code == 409 and services.DAY - 5 <= body.get("ready_in", 0) <= services.DAY
      and body.get("message", "").startswith("The Endless opens for you again in 2"), (res.status_code, body))
listed = endless(ann)
check("  and the list says the same", services.DAY - 5 <= listed.get("ready_in", 0) <= services.DAY
      and listed.get("entered_at", 0) > 0, listed)

section("MG-3 A DAY LATER, AND WHOSE DAY")
check("another account has a day of its own", go(bob).status_code == 200)
raw("UPDATE minigame_entries SET entered_at = entered_at - ? WHERE user_id = ?", (services.DAY - 90, uid("mgann")))
res = go(ann)
check("  with 90 seconds of the day left, still refused, '1m'", res.status_code == 409
      and "again in 1m" in (res.get_json() or {}).get("message", ""), res.get_json())
raw("UPDATE minigame_entries SET entered_at = entered_at - 100 WHERE user_id = ?", (uid("mgann"),))
check("  past the day it opens again", endless(ann).get("ready_in") == 0 and go(ann).status_code == 200)
check("the wait is the account's: one row per account and minigame, whichever character",
      raw("SELECT COUNT(*) FROM minigame_entries WHERE user_id = ?", (uid("mgann"),))[0][0] == 1
      and "slot" not in [r[1] for r in raw("PRAGMA table_info(minigame_entries)")])

section("MG-4 THE OWNER SKIPS THE WAIT")
check("the owner goes in twice running", go(owner).status_code == 200 and go(owner).status_code == 200)
check("  is never shown a wait, and is written all the same",
      endless(owner).get("ready_in") == 0 and endless(owner).get("entered_at", 0) > 0
      and (client.get("/api/minigames", headers=owner).get_json() or {}).get("skips_wait") is True)

section("MG-5 REFUSALS")
check("not a minigame: 400", go(ann, "bigfield").status_code == 400 and go(ann, 7).status_code == 400)
check("not JSON: 400", client.post("/api/minigames/enter", headers=ann, data="x").status_code == 400)
check("no login: 401", client.get("/api/minigames").status_code == 401
      and client.post("/api/minigames/enter", json={"game": "endless"}).status_code == 401)
check("wait_words: hours and minutes, never '0m'", services.wait_words(48000) == "13h 20m"
      and services.wait_words(3600) == "1h" and services.wait_words(59) == "under a minute")

import gamedata  # noqa: E402
import loot.services as chest_rules  # noqa: E402

def kill(headers, enemy_id, slot=0):
    return client.post("/api/minigames/kill", headers=headers,
                       json={"game": "endless", "slot": slot, "enemy_id": enemy_id})

def chest_of(headers, slot=0):
    return client.get("/api/loot/chest?game=endless&slot=%d" % slot, headers=headers).get_json() or {}

def held(answer):
    totals = {}
    for cell in answer.get("contents", []):
        totals[cell["item_id"]] = totals.get(cell["item_id"], 0) + cell["quantity"]
    return totals

ELITE = gamedata.ENDLESS["elites"][0]
BOSS = gamedata.ENDLESS["bosses"][0]
carl = register("mgcarl")
dora = register("mgdora")
for headers in (carl, dora):
    made = client.put("/api/save", headers=headers, json={"slot": 0, "class_id": "warrior", "name": "warrior"})
    assert made.status_code == 200, made.get_json()

section("MG-6 A KILL PAYS INTO THE CHEST")
check("the catalogue carries the run: its elites and bosses, a stage a minute, a boss every five",
      gamedata.ENDLESS.get("stage_seconds") == 60 and gamedata.ENDLESS.get("boss_every") == 5
      and "lightsprite" in gamedata.ENDLESS["elites"] and "boss" in gamedata.ENDLESS["bosses"]
      and len(gamedata.ENDLESS.get("stages", [])) >= services.STAGES_BELIEVED, gamedata.ENDLESS.get("bosses"))
res = kill(carl, ELITE)
check("before a Go: 409, nothing paid", res.status_code == 409 and not chest_of(carl).get("contents"),
      (res.status_code, res.get_json()))
go(carl)
before = raw("SELECT level, xp FROM saves WHERE user_id = ? AND slot = 0", (uid("mgcarl"),))[0]
added = {}
for _ in range(6):
    res = kill(carl, ELITE)
    assert res.status_code == 200, res.get_json()
    for entry in res.get_json()["added"]:
        added[entry["item_id"]] = added.get(entry["item_id"], 0) + entry["quantity"]
body = res.get_json()
chest = chest_of(carl)
check("six elites paid: the chest holds what they rolled, all of it, and says how many cells",
      held(chest) == added and body.get("used") == len(chest.get("contents", [])) and body.get("cut") == []
      and body.get("chest_id") == "chest:endless:0" and chest.get("cells") == 36 and chest.get("columns") == 6,
      (added, held(chest), body))
check("  loot only: no XP, no level, no kill record, no bag",
      raw("SELECT level, xp FROM saves WHERE user_id = ? AND slot = 0", (uid("mgcarl"),))[0] == before
      and "xp_gained" not in body and "bag_id" not in body
      and not raw("SELECT * FROM kill_reports WHERE user_id = ?", (uid("mgcarl"),))
      and not raw("SELECT * FROM loot_bags WHERE user_id = ?", (uid("mgcarl"),))
      and raw("SELECT COUNT(*) FROM minigame_kills WHERE user_id = ? AND boss = 0", (uid("mgcarl"),))[0][0] == 6)
first = chest["contents"][0]
took = client.post("/api/loot/take", headers=carl, json={"bag_id": chest["bag_id"], "position": first["position"]})
check("  a cell is taken through /api/loot/take, and is gone from the chest",
      took.status_code == 200 and took.get_json().get("item_id") == first["item_id"]
      and first["position"] not in [c["position"] for c in chest_of(carl).get("contents", [])],
      (took.status_code, took.get_json()))
again = client.post("/api/loot/take", headers=carl, json={"bag_id": chest["bag_id"], "position": first["position"]})
check("  twice is a 404 - one cell pays once", again.status_code == 404, again.status_code)
check("refused: a monster the run never sends (400), one nobody knows (400), an empty slot (404)",
      kill(carl, "skeletonelite").status_code == 400 and kill(carl, "nosuchthing").status_code == 400
      and kill(carl, ELITE, slot=2).status_code == 404)
check("  another game (400), not JSON (400), no login (401)",
      client.post("/api/minigames/kill", headers=carl,
                  json={"game": "bigfield", "slot": 0, "enemy_id": ELITE}).status_code == 400
      and client.post("/api/minigames/kill", headers=carl, data="x").status_code == 400
      and client.post("/api/minigames/kill", json={}).status_code == 401
      and client.get("/api/loot/chest?game=endless&slot=0").status_code == 401)

section("MG-7 THE SCHEDULE IS THE CEILING")
plan = gamedata.ENDLESS
def by_hand(seconds):
    total = 0
    for stage in plan["stages"][:min(services.STAGES_BELIEVED, seconds // 60 + 1)]:
        total += -(-60 // stage["gap"]) * stage["wave"]
    return total + plan["alive_most"]
check("elites: every stage reached, its waves as often and as big as the biggest party gets, plus the most alive",
      services.elites_ceiling(plan, 0) == plan["alive_most"] == 70
      and services.elites_ceiling(plan, 600) == by_hand(600)
      and services.elites_ceiling(plan, 86000) == by_hand(30 * 60) == services.elites_ceiling(plan, 30 * 60 - 1),
      (services.elites_ceiling(plan, 600), by_hand(600), services.elites_ceiling(plan, 86000)))
check("bosses: one each five stages reached (the fifth at 4:00), and one more",
      services.bosses_ceiling(plan, 0) == 1 and services.bosses_ceiling(plan, 239) == 1
      and services.bosses_ceiling(plan, 240) == 2 and services.bosses_ceiling(plan, 86000) == 7)
now = int(time.time())
conn = sqlite3.connect(DB_PATH)
conn.executemany("INSERT INTO minigame_kills (user_id, slot, game, enemy_id, boss, at) VALUES (?, 0, 'endless', ?, 0, ?)",
                 [(uid("mgcarl"), ELITE, now)] * (70 - 6))
conn.commit(); conn.close()
over = kill(carl, ELITE)
check("70 elites inside the first minute is all a run could have sent: the 71st is a 429, and pays nothing",
      over.status_code == 429 and "could have sent" in over.get_json().get("message", ""),
      (over.status_code, over.get_json()))
boss_one = kill(carl, BOSS)
boss_two = kill(carl, BOSS)
check("  a boss is counted apart: one, then 429", boss_one.status_code == 200 and boss_two.status_code == 429,
      (boss_one.status_code, boss_two.status_code))
raw("UPDATE minigame_entries SET entered_at = entered_at - 600 WHERE user_id = ?", (uid("mgcarl"),))
check("  ten minutes after the Go there is room again", kill(carl, ELITE).status_code == 200)

section("MG-8 THE NEXT GO EMPTIES IT")
raw("UPDATE minigame_entries SET entered_at = ? WHERE user_id = ?", (now - services.DAY - 1, uid("mgcarl")))
late = kill(carl, ELITE)
check("a day after the Go, a kill pays nothing (409)", late.status_code == 409, late.status_code)
had = len(chest_of(carl).get("contents", []))
go(carl)
check("  and the next Go empties the chest", had > 0 and chest_of(carl).get("contents") == [], had)

section("MG-9 A FULL CHEST KEEPS THE RAREST")
rows = {"ironsword": {"type_name": "WEAPON", "value": 10}, "jadesword": {"type_name": "WEAPON", "value": 90},
        "coppercoin": {"type_name": "CURRENCY", "value": 1, "stackable": True, "max_stack": 9999},
        "petmage": {"type_name": "PET", "value": 0}}
rank = lambda item_id: {"petmage": 0, "coppercoin": 2}.get(item_id, 1)
cells = {i: ["ironsword", 1] for i in range(35)}
cells[35] = ["coppercoin", 40]
cells, cut = chest_rules.chest_after(cells, [{"item_id": "coppercoin", "quantity": 5}], 36, rows.get, rank)
check("coins fold onto their cell, even full", cells[35] == ["coppercoin", 45] and cut == [])
cells, cut = chest_rules.chest_after(cells, [{"item_id": "petmage", "quantity": 1}], 36, rows.get, rank)
check("  a pet takes the coins' place - the least kept - and the coins are what is cut",
      cells[35] == ["petmage", 1] and cut == [{"item_id": "coppercoin", "quantity": 45}], (cells[35], cut))
cells, cut = chest_rules.chest_after(cells, [{"item_id": "jadesword", "quantity": 1},
                                             {"item_id": "ironsword", "quantity": 1}], 36, rows.get, rank)
check("  a better piece replaces a lesser one; an equal one is cut itself",
      sum(1 for c in cells.values() if c[0] == "jadesword") == 1
      and cut == [{"item_id": "ironsword", "quantity": 1}, {"item_id": "ironsword", "quantity": 1}], cut)
check("  the chest's id is read back, and nothing else is a chest",
      chest_rules.parse_chest_id("chest:endless:2") == ("endless", 2)
      and chest_rules.parse_chest_id("chest:endless:x") is None
      and chest_rules.parse_chest_id("k3Jx9_QpZ2mNvRt1") is None)

section("MG-10 WHOSE CHEST")
go(dora)
# An elite's roll can be nothing at all, so kill until one lands (it was one
# kill, and failed about one run in four).
for _ in range(20):
    assert kill(dora, ELITE).status_code == 200
    if chest_of(dora).get("contents"):
        break
dora_chest = chest_of(dora)
check("another account reads its own chest, never somebody else's, and cannot take from it",
      chest_of(carl).get("contents") == [] and len(dora_chest.get("contents", [])) > 0
      and client.post("/api/loot/take", headers=carl,
                      json={"bag_id": "chest:endless:0", "position": dora_chest["contents"][0]["position"]}
                      ).status_code == 404)
gone = client.post("/api/character/delete", headers=dora, json={"slot": 0, "confirm": "warrior"})
check("deleting the character takes its chest", gone.status_code == 200
      and not raw("SELECT * FROM loot_chest_items WHERE user_id = ?", (uid("mgdora"),)),
      (gone.status_code, gone.get_json()))

section("MG-11 THE HEALER'S RUN: THREE RUNS A DAY")
erin = register("mgerin")
assert client.put("/api/save", headers=erin, json={"slot": 0, "class_id": "warrior", "name": "warrior"}).status_code in (200, 201)
def healer(headers):
    games = (client.get("/api/minigames", headers=headers).get_json() or {}).get("games", [])
    return next((g for g in games if g.get("id") == "healerrun"), {})
def run_go(headers, slot=0):
    return client.post("/api/minigames/enter", headers=headers, json={"game": "healerrun", "slot": slot})
listed = healer(erin)
check("the Healer's Run is listed: three runs a day, all three left",
      listed.get("name") == "The Healer's Run" and listed.get("plays") == 3 and listed.get("plays_left") == 3
      and listed.get("ready_in") == 0, listed)
check("  and the Endless says it is one a day", endless(erin).get("plays") == 1 and endless(erin).get("plays_left") == 1)
runs = []
for n in range(3):
    res = run_go(erin)
    body = res.get_json() or {}
    runs.append(body.get("run"))
    check("  Go %d is let in with a run of its own, %d left" % (n + 1, 2 - n), res.status_code == 200
          and isinstance(body.get("run"), int) and body.get("plays_left") == 2 - n
          and body.get("pays", {}).get("most") == 1000, (res.status_code, body))
res = run_go(erin)
body = res.get_json() or {}
check("the fourth inside the day is refused (409) with when the next one is, in words",
      res.status_code == 409 and services.DAY - 5 <= body.get("ready_in", 0) <= services.DAY
      and "3 times today" in body.get("message", ""), (res.status_code, body))
raw("UPDATE minigame_runs SET started_at = started_at - ? WHERE id = ?", (services.DAY + 5, runs[0]))
check("  once the first is a day old, one is free again", healer(erin).get("plays_left") == 1
      and run_go(erin).status_code == 200)
check("refused: no slot (400), an empty slot (404)", run_go(erin, slot=None).status_code == 400
      and run_go(erin, slot=3).status_code == 404)
check("the owner runs it as often as they like", all(run_go(owner).status_code in (200, 404) for _ in range(4)))

section("MG-12 ITS GOLD, PAID AT LEAVE")
check("the gold by how far a run got: nothing, the first stretch, every wave, the boss, the goal",
      services.healer_gold(0, False, False) == 0 and services.healer_gold(5, False, False) == 50
      and services.healer_gold(25, False, False) == 600 and services.healer_gold(25, True, False) == 750
      and services.healer_gold(25, True, True) == 1000 and services.healer_gold(24, True, True) < 600)
def finish(headers, run_id, waves, boss=False, goal=False):
    return client.post("/api/minigames/finish", headers=headers,
                       json={"run": run_id, "waves": waves, "boss": boss, "goal": goal})
purse = lambda: raw("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (uid("mgerin"),))[0][0]
before = purse()
raw("UPDATE minigame_runs SET started_at = started_at - 600 WHERE id = ?", (runs[1],))
res = finish(erin, runs[1], 7)
body = res.get_json() or {}
check("Leave after seven waves pays their gold into the character's purse, in the ledger as minigame",
      res.status_code == 200 and body.get("gold") == 50 + 2 * 16 and purse() == before + 82
      and body.get("status", {}).get("gold") == purse()
      and raw("SELECT reason, delta FROM gold_ledger WHERE user_id = ? ORDER BY id DESC LIMIT 1",
              (uid("mgerin"),))[0] == ("minigame", 82), (res.status_code, body))
check("  once: a second Leave for the same run is refused (409)", finish(erin, runs[1], 7).status_code == 409
      and purse() == before + 82)
raw("UPDATE minigame_runs SET started_at = started_at - 30 WHERE id = ?", (runs[2],))
body = finish(erin, runs[2], 25, True, True).get_json() or {}
check("a run that says it reached the goal 30 seconds after its Go is believed only as far as the time allows",
      body.get("waves") == 10 and body.get("boss") is False and body.get("gold") == services.healer_gold(10, False, False),
      body)
raw("UPDATE minigame_runs SET started_at = started_at - ? WHERE id = ?", (services.DAY, runs[2]))
full = run_go(erin).get_json() or {}
raw("UPDATE minigame_runs SET started_at = started_at - 400 WHERE id = ?", (full.get("run", 0),))
body = finish(erin, full.get("run", 0), 25, True, True).get_json() or {}
check("  a whole run, the boss and the goal: 1000 gold", body.get("gold") == 1000 and body.get("goal") is True, body)
check("refused: somebody else's run (404), a bad body (400), not logged in (401)",
      finish(bob, runs[0], 3).status_code == 404 and finish(erin, "x", 3).status_code == 400
      and client.post("/api/minigames/finish", json={"run": runs[0], "waves": 3}).status_code == 401)
raw("UPDATE minigame_runs SET started_at = started_at - ? WHERE id = ?", (services.HEALER_RUN_KEPT_SECONDS + 10, runs[0]))
check("a run left too long pays nothing (closed without Leave)", (finish(erin, runs[0], 25, True, True).get_json() or {})
      .get("gold") == 0)

section("MG-13 THE HEALER'S RUN'S TIERS (game 0.100.0)")
tg = services.healer_tier_gold
check("the tiers' gold: 10, 25, 500 a wave by tier, bosses 120, 300, 6000, the goal 9300 - 20,000 in all, 700 by the second boss",
      tg(0, 0, False) == 0 and tg(8, 0, False) == 80 and tg(8, 1, False) == 200 and tg(16, 2, False) == 700
      and tg(24, 3, False) == 10700 and tg(24, 3, True) == 20000 and tg(24, 2, True) == 4700
      and services.healer_tier_pays()["most"] == 20000, [tg(8, 1, False), tg(16, 2, False), tg(24, 3, True)])
check("  a boss is not paid before its tier's waves are passed, nor more than three",
      tg(7, 1, False) == 70 and tg(9, 2, False) == 80 + 25 + 120 and tg(99, 9, True) == 20000)
tb = services.healer_tier_believed
check("believed only as far as the time allows: 3 s a wave, 15 s a boss",
      tb(24, 3, True, 9999) == (24, 3, True) and tb(24, 3, True, 30) == (10, 0, False)
      and tb(8, 1, False, 24) == (8, 0, False) and tb(8, 1, False, 39) == (8, 1, False)
      and tb(24, 3, True, 24 * 3 + 2 * 15) == (24, 2, False), [tb(24, 3, True, 30), tb(8, 1, False, 39)])
fay = register("mgfay")
assert client.put("/api/save", headers=fay, json={"slot": 0, "class_id": "healer", "name": "healer"}).status_code in (200, 201)
went = run_go(fay).get_json() or {}
check("Go hands the game the tiers' table beside the old one (a game from before reads the old)",
      went.get("pays", {}).get("tiers", {}).get("waves_per_tier") == 8
      and went.get("pays", {}).get("tiers", {}).get("boss_gold") == [120, 300, 6000]
      and went.get("pays", {}).get("waves_per_stretch") == 5, went.get("pays"))
fay_purse = lambda: raw("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (uid("mgfay"),))[0][0]
before = fay_purse()
raw("UPDATE minigame_runs SET started_at = started_at - 600 WHERE id = ?", (went.get("run", 0),))
res = client.post("/api/minigames/finish", headers=fay, json={"run": went.get("run", 0), "waves": 24, "bosses": 3,
                                                             "goal": True})
body = res.get_json() or {}
check("Leave from a whole tiered run: 20,000 gold, three bosses, the goal, in the ledger with the bosses",
      res.status_code == 200 and body.get("gold") == 20000 and body.get("bosses") == 3 and body.get("goal") is True
      and fay_purse() == before + 20000
      and "3 of 3 bosses" in raw("SELECT detail FROM gold_ledger WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                                 (uid("mgfay"),))[0][0]
      and raw("SELECT boss FROM minigame_runs WHERE id = ?", (went.get("run", 0),))[0][0] == 3, (res.status_code, body))
second = run_go(fay).get_json() or {}
raw("UPDATE minigame_runs SET started_at = started_at - 600 WHERE id = ?", (second.get("run", 0),))
body = (client.post("/api/minigames/finish", headers=fay, json={"run": second.get("run", 0), "waves": 12,
                                                                "bosses": 1, "goal": False}).get_json() or {})
check("  fallen in the second tier, the first boss beaten: 80 + 120 + four of 25", body.get("gold") == 80 + 120 + 100
      and body.get("bosses") == 1, body)
third = run_go(fay).get_json() or {}
check("  bosses must be a whole number (400)",
      client.post("/api/minigames/finish", headers=fay, json={"run": third.get("run", 0), "waves": 3,
                                                              "bosses": True}).status_code == 400
      and client.post("/api/minigames/finish", headers=fay, json={"run": third.get("run", 0), "waves": 3,
                                                                  "bosses": -1}).status_code == 400)

section("MG-14 THE FORGE (game 0.120.0): ONCE A DAY, ITS FIRE SPRITES PAY THE CHEST, ITS BEST TIME KEPT")
# The owner, 4 Nov: "Rewards + daily wait". forgerun.gd's schedule() says what
# a run could send at its fastest; gamedata.json carries it as "forge".
forge_plan = gamedata.FORGE
gus = register("mggus")
made = client.put("/api/save", headers=gus, json={"slot": 0, "class_id": "tank", "name": "tank"})
assert made.status_code == 200, made.get_json()
def forge_row(headers):
    games = (client.get("/api/minigames", headers=headers).get_json() or {}).get("games", [])
    return next((g for g in games if g.get("id") == "forge"), {})
def forge_kill(headers, enemy_id="firesprite", slot=0):
    return client.post("/api/minigames/kill", headers=headers, json={"game": "forge", "slot": slot, "enemy_id": enemy_id})
def forge_best(headers, seconds):
    return client.post("/api/minigames/best", headers=headers, json={"game": "forge", "seconds": seconds})
row = forge_row(gus)
check("the Forge is a minigame: a day's wait, ready now, no best yet",
      row.get("name") == "The Forge" and row.get("wait_seconds") == services.DAY and row.get("ready_in") == 0
      and row.get("best") == 0 and "best" not in endless(gus), row)
check("  the catalogue carries its schedule: fire sprites, eight floods, the volcano believed a minute, 50 alive at most",
      forge_plan.get("sprites") == ["firesprite"] and len(forge_plan.get("phases", [])) == 8
      and forge_plan.get("volcano_believed") == 60 and forge_plan.get("alive_most") == 50
      and forge_plan["phases"][0] == {"count": 1, "gap": 3.0, "seconds": 40.0}, forge_plan)
check("before a Go: a kill and a time are both 409, nothing paid",
      forge_kill(gus).status_code == 409 and forge_best(gus, 60).status_code == 409
      and not (client.get("/api/loot/chest?game=forge&slot=0", headers=gus).get_json() or {}).get("contents"))
check("  Go is let in once a day, as the Endless's", go(gus, "forge").status_code == 200
      and go(gus, "forge").status_code == 409 and forge_row(gus).get("ready_in", 0) > services.DAY - 10)
paid = forge_kill(gus)
body = paid.get_json() or {}
forge_chest = client.get("/api/loot/chest?game=forge&slot=0", headers=gus).get_json() or {}
check("a fire sprite slain pays its own loot into the Forge's chest - loot only, its own chest, not the Endless's",
      paid.status_code == 200 and body.get("chest_id") == "chest:forge:0" and "xp_gained" not in body
      and held(forge_chest) == {e["item_id"]: e["quantity"] for e in body.get("added", [])}
      and not (client.get("/api/loot/chest?game=endless&slot=0", headers=gus).get_json() or {}).get("contents")
      and raw("SELECT game, boss FROM minigame_kills WHERE user_id = ?", (uid("mggus"),)) == [("forge", 0)],
      (paid.status_code, body))
check("  a monster the Forge never sends is refused (400): an Endless elite, a boss",
      forge_kill(gus, "lightsprite").status_code == 400 and forge_kill(gus, "boss").status_code == 400)
def forge_by_hand(seconds):
    total, left = 0, float(seconds)
    for phase in forge_plan["phases"]:
        span = min(left, phase["seconds"])
        if span > 0:
            total += (math.ceil(span / phase["gap"]) + 1) * phase["count"]
        left -= span
    if left > 0:
        v = forge_plan["volcano"]
        total += (math.ceil(min(left, 60) / v["gap"]) + 1) * v["count"]
    return int(total)
whole = services.forge_run_sprites(forge_plan, services.forge_run_seconds(forge_plan))
check("its ceiling: every flood at its fastest, the volcano for a minute, the most alive - then back-to-back runs, six a day",
      services.forge_ceiling(forge_plan, 0) == 50 and services.forge_ceiling(forge_plan, 100) == forge_by_hand(100) + 50
      and whole == forge_by_hand(10 ** 6)
      and services.forge_ceiling(forge_plan, 86000) == whole * services.FORGE_RUNS_BELIEVED + 50
      and services.forge_ceiling(forge_plan, 2 * 288) > whole,
      (services.forge_ceiling(forge_plan, 100), forge_by_hand(100), whole, services.forge_ceiling(forge_plan, 86000)))
now = int(time.time())
conn = sqlite3.connect(DB_PATH)
conn.executemany("INSERT INTO minigame_kills (user_id, slot, game, enemy_id, boss, at) VALUES (?, 0, 'forge', 'firesprite', 0, ?)",
                 [(uid("mggus"), now)] * (50 - 1))
conn.commit(); conn.close()
over = forge_kill(gus)
check("  50 fire sprites at the Go is all there could be: the 51st is a 429 that names them",
      over.status_code == 429 and "fire sprites than The Forge" in (over.get_json() or {}).get("message", ""),
      (over.status_code, over.get_json()))
raw("UPDATE minigame_entries SET entered_at = entered_at - 400 WHERE user_id = ? AND game = 'forge'", (uid("mggus"),))
check("  400 seconds after the Go there is room again", forge_kill(gus).status_code == 200)
first = forge_best(gus, 270).get_json() or {}
worse = forge_best(gus, 200).get_json() or {}
check("a run's time is kept when it is your best: 4:30, then a 3:20 changes nothing",
      first.get("best") == 270 and first.get("new_best") is True and worse.get("best") == 270
      and worse.get("new_best") is False and worse.get("seconds") == 200 and forge_row(gus).get("best") == 270,
      (first, worse))
long = forge_best(gus, 5000).get_json() or {}
check("  no run is believed longer than since your Go", 400 <= long.get("seconds", 0) <= 410
      and long.get("best") == long.get("seconds"), long)
check("  refused: the Endless keeps no time (400), seconds not a whole number (400), no login (401)",
      client.post("/api/minigames/best", headers=gus, json={"game": "endless", "seconds": 60}).status_code == 400
      and forge_best(gus, "60").status_code == 400 and forge_best(gus, -1).status_code == 400
      and forge_best(gus, True).status_code == 400
      and client.post("/api/minigames/best", json={"game": "forge", "seconds": 60}).status_code == 401)
raw("UPDATE minigame_entries SET entered_at = ? WHERE user_id = ? AND game = 'forge'", (now - services.DAY - 1, uid("mggus")))
check("  a day after the Go: a kill and a time are 409",
      forge_kill(gus).status_code == 409 and forge_best(gus, 60).status_code == 409)
check("the next Go empties the Forge's chest", go(gus, "forge").status_code == 200
      and (client.get("/api/loot/chest?game=forge&slot=0", headers=gus).get_json() or {}).get("contents") == [])
import accountdelete.data as gone  # noqa: E402
check("deleting the account takes its best times and runs", "minigame_bests" in gone.OWN_TABLES
      and "minigame_runs" in gone.OWN_TABLES)

section("MG-15 THE FORGE'S PETS AND MYTHICS: TEN TIMES RARER, ONE PET A GO (game 0.121.0)")
# The owner: "one could farm lusions in these events" (a duplicate pet is
# lusions) and "lets fix pets and mythics". What the roll is asked, then a pet
# forced to win - the first is kept, the second never rolls.
hal = register("mghal")
made = client.put("/api/save", headers=hal, json={"slot": 0, "class_id": "tank", "name": "tank"})
assert made.status_code == 200, made.get_json()
check("  (Go into the Forge)", go(hal, "forge").status_code == 200)
asked = []
real_roll, real_pet, real_mythic = gamedata.roll_kill_rewards, gamedata.roll_pet, gamedata.roll_mythic
def spy_roll(*args, **kwargs):
    asked.append(dict(kwargs))
    return real_roll(*args, **kwargs)
gamedata.roll_kill_rewards = spy_roll
odds_seen = []
gamedata.roll_pet = lambda enemy: odds_seen.append(("pet", enemy.get("pet_odds"))) or True
gamedata.roll_mythic = lambda enemy, class_id="": odds_seen.append(("mythic", enemy.get("mythic_odds"))) or ""
try:
    first = forge_kill(hal).get_json() or {}
    second = forge_kill(hal).get_json() or {}
finally:
    gamedata.roll_kill_rewards, gamedata.roll_pet, gamedata.roll_mythic = real_roll, real_pet, real_mythic
sprite = gamedata.ENEMIES["firesprite"]
check("a fire sprite in the Forge rolls its pet and its mythic ten times rarer",
      services.FORGE_RARE_SCALE == 10 and asked and asked[0].get("rare_odds_scale") == 10
      and ("pet", int(sprite["pet_odds"]) * 10) in odds_seen
      and ("mythic", int(sprite["mythic_odds"]) * 10) in odds_seen, (asked[:1], odds_seen[:2]))
hal_chest = client.get("/api/loot/chest?game=forge&slot=0", headers=hal).get_json() or {}
pets_held = sum(q for item_id, q in held(hal_chest).items() if item_id.startswith("pet"))
check("  one pet a Go: the first kill's is kept, the second kill is not let roll one",
      first.get("pet_won") is True and second.get("pet_won") is False and pets_held == 1
      and asked[0].get("pets") is True and asked[1].get("pets") is False
      and raw("SELECT COUNT(*) FROM minigame_kills WHERE user_id = ? AND pet = 1", (uid("mghal"),))[0][0] == 1,
      (first.get("pet_won"), second.get("pet_won"), pets_held, asked[:2]))
plain = gamedata.roll_kill_rewards("firesprite", "tank")
check("  the Field's roll is unchanged: no scale, pets on (the defaults)",
      isinstance(plain, dict) and "pet_won" in plain
      and gamedata.roll_kill_rewards.__defaults__[-2:] == (1, True))
asked.clear()
gamedata.roll_kill_rewards = spy_roll
try:
    raw("UPDATE minigame_entries SET entered_at = ? WHERE user_id = ? AND game = 'endless'", (0, uid("mghal")))
    go(hal, "endless")
    client.post("/api/minigames/kill", headers=hal, json={"game": "endless", "slot": 0, "enemy_id": "lightsprite"})
finally:
    gamedata.roll_kill_rewards = real_roll
check("  the Endless is as it was: its elites roll at their own odds, pets on",
      asked and asked[0].get("rare_odds_scale") == 1 and asked[0].get("pets") is True, asked)

section("MG-15 CUT THE CLOTH (game 0.131.0): THREE RUNS A DAY, GOLD FOR EVERY ROUND CLEARED")
cg = services.cloth_gold
check("the rounds' gold: 100, 150, 200, 300, 600 ... 1,500 for the tenth - 5,050 for all ten; then 800 a round, "
      "1,600 each fifth; never past %d rounds" % services.CLOTH_ROUNDS_MOST,
      cg(0) == 0 and cg(1) == 100 and cg(5) == 1350 and cg(10) == 5050 and cg(11) == 5850
      and cg(15) == 5050 + 4 * 800 + 1600 and cg(99) == cg(services.CLOTH_ROUNDS_MOST)
      and services.cloth_pays()["most"] == cg(services.CLOTH_ROUNDS_MOST), [cg(5), cg(10), cg(15)])
cb = services.cloth_believed
check("  believed only as far as the time allows: %d s a round" % services.CLOTH_MIN_ROUND_SECONDS,
      cb(10, 9999) == 10 and cb(10, 60) == 4 and cb(3, 14) == 0 and cb(-2, 999) == 0 and cb(99, 99999) == services.CLOTH_ROUNDS_MOST)
ivy = register("mgivy")
assert client.put("/api/save", headers=ivy, json={"slot": 0, "class_id": "mage", "name": "mage"}).status_code in (200, 201)
def cloth_row(headers):
    games = (client.get("/api/minigames", headers=headers).get_json() or {}).get("games", [])
    return next((g for g in games if g.get("id") == "cloth"), {})
def cloth_go(headers, slot=0):
    return client.post("/api/minigames/enter", headers=headers, json={"game": "cloth", "slot": slot})
def cloth_finish(headers, run_id, rounds):
    return client.post("/api/minigames/finish", headers=headers, json={"run": run_id, "rounds": rounds})
row = cloth_row(ivy)
check("Cut the Cloth is listed: three runs a day, all three left", row.get("name") == "Cut the Cloth"
      and row.get("plays") == 3 and row.get("plays_left") == 3 and row.get("ready_in") == 0, row)
cloth_runs = []
for n in range(3):
    res = cloth_go(ivy)
    body = res.get_json() or {}
    cloth_runs.append(body.get("run"))
    check("  Go %d is let in with a run of its own and the rounds' table, %d left" % (n + 1, 2 - n),
          res.status_code == 200 and isinstance(body.get("run"), int) and body.get("plays_left") == 2 - n
          and body.get("pays", {}).get("rounds", [None])[0] == 100 and body.get("pays", {}).get("endless") == 800,
          (res.status_code, body))
res = cloth_go(ivy)
check("  the fourth inside the day is refused (409), in words", res.status_code == 409
      and "3 times today" in (res.get_json() or {}).get("message", ""), (res.status_code, res.get_json()))
ivy_purse = lambda: raw("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (uid("mgivy"),))[0][0]
before = ivy_purse()
raw("UPDATE minigame_runs SET started_at = started_at - 600 WHERE id = ?", (cloth_runs[0],))
res = cloth_finish(ivy, cloth_runs[0], 6)
body = res.get_json() or {}
check("Leave after six rounds pays their 1,750 gold into the purse, in the ledger as minigame, the boss counted",
      res.status_code == 200 and body.get("gold") == 1750 and body.get("rounds") == 6 and body.get("bosses") == 1
      and ivy_purse() == before + 1750 and body.get("status", {}).get("gold") == ivy_purse()
      and raw("SELECT reason, delta, detail FROM gold_ledger WHERE user_id = ? ORDER BY id DESC LIMIT 1",
              (uid("mgivy"),))[0] == ("minigame", 1750, "cloth run %d: 6 rounds" % cloth_runs[0]), (res.status_code, body))
check("  once: a second Leave for the same run is refused (409)", cloth_finish(ivy, cloth_runs[0], 6).status_code == 409
      and ivy_purse() == before + 1750)
raw("UPDATE minigame_runs SET started_at = started_at - 40 WHERE id = ?", (cloth_runs[1],))
body = cloth_finish(ivy, cloth_runs[1], 12).get_json() or {}
check("a run that says it cleared 12 rounds 40 seconds after its Go is believed for 2", body.get("rounds") == 2
      and body.get("gold") == 250, body)
check("refused: rounds missing or not a whole number (400), somebody else's run (404), a Healer's body (400)",
      client.post("/api/minigames/finish", headers=ivy, json={"run": cloth_runs[2]}).status_code == 400
      and cloth_finish(ivy, cloth_runs[2], True).status_code == 400 and cloth_finish(ivy, cloth_runs[2], -1).status_code == 400
      and cloth_finish(bob, cloth_runs[2], 3).status_code == 404
      and client.post("/api/minigames/finish", headers=ivy, json={"run": cloth_runs[2], "waves": 3}).status_code == 400)
raw("UPDATE minigame_runs SET started_at = started_at - ? WHERE id = ?", (services.HEALER_RUN_KEPT_SECONDS + 10, cloth_runs[2]))
check("  a run left too long pays nothing", (cloth_finish(ivy, cloth_runs[2], 5).get_json() or {}).get("gold") == 0)
check("the owner runs it as often as they like", all(cloth_go(owner).status_code in (200, 404) for _ in range(4)))

section("MG-16 A DAY'S MINIGAME GOLD (14 Nov): ONE ALLOWANCE AN ACCOUNT, RUNS AND CHEST COINS TOGETHER")
check("the cap's setting: unset or unreadable is %d, a whole number is itself, 0 is no cap" % services.GOLD_A_DAY,
      services.gold_a_day(None) == services.GOLD_A_DAY == 100000 and services.gold_a_day("") == 100000
      and services.gold_a_day(" 5000 ") == 5000 and services.gold_a_day("0") == 0
      and services.gold_a_day("-5") == 100000 and services.gold_a_day("lots") == 100000
      and services.gold_a_day("\uff11\uff12") == 100000)
ga = services.gold_allowed
check("  a run is paid what is left of the day, the rest held back; no cap pays it all",
      ga(1000, 0, 2000) == (1000, 0) and ga(1000, 1500, 2000) == (500, 500) and ga(1000, 2500, 2000) == (0, 1000)
      and ga(1000, 99999, 0) == (1000, 0))
worth = {"coppercoin": 1, "silvercoin": 50, "goldcoin": 1000}.get
roll = [{"item_id": "goldcoin", "quantity": 2}, {"item_id": "ironsword", "quantity": 1},
        {"item_id": "silvercoin", "quantity": 3}]
check("  a chest's coins as far as the day goes, in the order rolled; the gear always",
      services.coins_within(roll, lambda i: worth(i, 0), None) == (roll, 2150, 0)
      and services.coins_within(roll, lambda i: worth(i, 0), 1100) == (
          [{"item_id": "goldcoin", "quantity": 1}, {"item_id": "ironsword", "quantity": 1},
           {"item_id": "silvercoin", "quantity": 2}], 1100, 1050)
      and services.coins_within(roll, lambda i: worth(i, 0), 0) == ([{"item_id": "ironsword", "quantity": 1}], 0, 2150))

os.environ["ELUSION_MINIGAME_GOLD_DAY"] = "2000"
kai = register("mgkai")
assert client.put("/api/save", headers=kai, json={"slot": 0, "class_id": "warrior", "name": "warrior"}).status_code in (200, 201)
kai_purse = lambda: raw("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (uid("mgkai"),))[0][0]
first = run_go(kai).get_json() or {}
raw("UPDATE minigame_runs SET started_at = started_at - 600 WHERE id = ?", (first.get("run", 0),))
body = finish(kai, first.get("run", 0), 25, True, True).get_json() or {}
check("with the day's allowance at 2,000: a 1,000 run is paid whole", body.get("gold") == 1000
      and body.get("held_back") == 0 and kai_purse() == 1000, body)
cloth_first = cloth_go(kai).get_json() or {}
raw("UPDATE minigame_runs SET started_at = started_at - 600 WHERE id = ?", (cloth_first.get("run", 0),))
before = kai_purse()
body = cloth_finish(kai, cloth_first.get("run", 0), 6).get_json() or {}
check("  a 1,750 Cut the Cloth run after it - another minigame, the same day - is paid the 1,000 left",
      body.get("gold") == 1000 and body.get("held_back") == 750 and kai_purse() == before + 1000, body)
check("  and its ledger line says what was held back, and why",
      "750 held back: the day's minigame gold is full" in
      raw("SELECT detail FROM gold_ledger WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid("mgkai"),))[0][0])
second = run_go(kai).get_json() or {}
raw("UPDATE minigame_runs SET started_at = started_at - 600 WHERE id = ?", (second.get("run", 0),))
before = kai_purse()
lines_before = raw("SELECT COUNT(*) FROM gold_ledger WHERE user_id = ?", (uid("mgkai"),))[0][0]
body = finish(kai, second.get("run", 0), 25, True, True).get_json() or {}
check("  the day full, a run pays nothing and says so; no ledger line for nothing",
      body.get("gold") == 0 and body.get("held_back") == 1000 and kai_purse() == before
      and raw("SELECT COUNT(*) FROM gold_ledger WHERE user_id = ?", (uid("mgkai"),))[0][0] == lines_before, body)

# THE CHEST'S COINS count against the same allowance. The roll is fixed here:
# two gold coins and a sword.
real_roll = app_module.gamedata.roll_kill_rewards
app_module.gamedata.roll_kill_rewards = lambda *a, **k: {
    "contents": [{"item_id": "goldcoin", "quantity": 2}, {"item_id": "ironsword", "quantity": 1}],
    "pet_won": False, "mythic": ""}
try:
    assert go(kai).status_code == 200
    raw("UPDATE minigame_entries SET entered_at = entered_at - 600 WHERE user_id = ? AND game = 'endless'", (uid("mgkai"),))
    elite = next(e for e in app_module.gamedata.ENEMIES if services.sends(app_module.gamedata.ENDLESS, e) == "elite")
    body = kill(kai, elite).get_json() or {}
    check("a chest kill with the day full keeps its sword and none of its coins, and says what was held back",
          body.get("ok") is True and body.get("added") == [{"item_id": "ironsword", "quantity": 1}]
          and body.get("held_back") == 2000, body)
    raw("UPDATE minigame_runs SET finished_at = finished_at - ? WHERE user_id = ?", (services.DAY + 5, uid("mgkai")))
    raw("UPDATE minigame_kills SET at = at - ? WHERE user_id = ?", (services.DAY + 5, uid("mgkai")))
    body = kill(kai, elite).get_json() or {}
    check("  a day on, the allowance is whole again: the coins roll into the chest, and count",
          body.get("held_back") == 0 and {"item_id": "goldcoin", "quantity": 2} in body.get("added", [])
          and raw("SELECT gold FROM minigame_kills WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid("mgkai"),))[0][0] == 2000,
          body)
    body = kill(kai, elite).get_json() or {}
    check("  and they filled it: the next kill's coins are held back", body.get("held_back") == 2000, body)
    os.environ["ELUSION_MINIGAME_GOLD_DAY"] = "0"
    body = kill(kai, elite).get_json() or {}
    check("set to 0, there is no cap", body.get("held_back") == 0 and {"item_id": "goldcoin", "quantity": 2} in body.get("added", []),
          body)
    os.environ["ELUSION_MINIGAME_GOLD_DAY"] = "2000"
    assert client.put("/api/save", headers=owner, json={"slot": 0, "class_id": "warrior", "name": "warrior"}).status_code in (200, 201)
    go(owner)
    raw("UPDATE minigame_entries SET entered_at = entered_at - 600 WHERE user_id = ? AND game = 'endless'", (uid("mgowner"),))
    for _ in range(2):
        body = kill(owner, elite).get_json() or {}
    check("the owner, who skips the waits, skips the cap too", body.get("held_back") == 0, body)
finally:
    app_module.gamedata.roll_kill_rewards = real_roll
    os.environ.pop("ELUSION_MINIGAME_GOLD_DAY", None)

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
sys.exit(1 if failed else 0)
