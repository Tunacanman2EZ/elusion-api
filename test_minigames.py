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
"""
import importlib.util, os, sqlite3, sys, time

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
assert kill(dora, ELITE).status_code == 200
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

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
sys.exit(1 if failed else 0)
