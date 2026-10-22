"""The stirring lands (game 0.46.0): a world that changes on its own.

Every few hours one of the seven lands stirs: its monsters can hit harder,
have more health, move faster, drop loot more often and give more XP, and
resistance to it can count for more - each an owner's switch with an amount,
the whole thing one switch more. The server picks the land and pays the loot
and XP; the game is told the rest.

  W-1  the settings: defaults with no row, OFF when the row cannot be read,
       held to their ranges; the owner's route checks every value, is the
       owner's alone, and says when the lands go still or stir again
  W-2  the ticks: written by whoever asks first after one ends, the rhythm
       held while people play, a quiet spell restarting it; never the same
       land twice; the least hunted land; a tie settled the same way for the
       same tick; two writers of one tick make one row and one announcement
  W-3  the poll carries the world, and a stir begun by the poll is announced
       in the same answer; switched off, or broken, it is "no land" and the
       poll still answers
  W-4  a kill of the stirring land's monsters drops loot more often and gives
       more XP, as set; any other kill is ordinary
  W-5  the owner stirs a land by hand: a whole stir, announced; refused when
       switched off or not one of the seven
  W-6  the owner's view: settings, the stir now, the last few, each land's
       hunting
  W-7  the books (combatbook.py, presence.py): a stirring land's monster born
       with more health is counted at it, not flagged; any other is flagged

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_world.py
"""
import importlib.util
import json
import os
import random
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_world_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "worldowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

passed = 0
failed = 0
failures = []


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n--- %s ---" % title)


_ip = [0]


def register(name):
    _ip[0] += 1
    res = client.post("/api/auth/register", json={"username": name, "password": "password123"},
                      environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert res.status_code == 201, res.get_json()
    return res.get_json()["token"]


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def ticks():
    return [dict(r) for r in sql("SELECT * FROM world_ticks ORDER BY tick")]


def world_messages():
    return [dict(r) for r in sql("SELECT * FROM broadcasts WHERE kind = 'world' ORDER BY id")]


def poll(token, since=0):
    return client.get("/api/server/broadcasts?since=%d" % since, headers=auth(token))


def reset_world():
    sql("DELETE FROM world_ticks")
    sql("DELETE FROM broadcasts")
    sql("DELETE FROM server_settings WHERE key = ?", (app_module.WORLD_KEY,))
    sql("DELETE FROM kill_reports")


def in_app(fn):
    with app_module.app.app_context():
        db = app_module.get_db()
        out = fn(db)
        db.commit()
        return out


def end_current(seconds_ago=1):
    """Make the stir going on have ended `seconds_ago`."""
    last = sql("SELECT tick FROM world_ticks ORDER BY tick DESC LIMIT 1")
    if last:
        sql("UPDATE world_ticks SET ends_at = ? WHERE tick = ?", (int(time.time()) - seconds_ago, last[0]["tick"]))


owner = register("worldowner")
player = register("worldplayer")
client.put("/api/save", headers=auth(player), json={"slot": 0, "class_id": "warrior", "name": "W"})
owner_id = int(sql("SELECT id FROM users WHERE username = 'worldowner'")[0]["id"])
player_id = int(sql("SELECT id FROM users WHERE username = 'worldplayer'")[0]["id"])

ELEMENT_OF = {e["enemy_id"]: int(e.get("element", 0)) for e in gamedata.ENEMIES.values()}
BY_LAND = {}
for enemy_id, element in sorted(ELEMENT_OF.items()):
    enemy = gamedata.ENEMIES[enemy_id]
    if element in app_module.LANDS and enemy.get("grants_rewards") and float(enemy.get("bag_drop_chance", 0)) > 0 \
            and float(enemy.get("bag_drop_chance", 0)) < 1:
        BY_LAND.setdefault(element, enemy_id)

# =============================================================================
section("W-1  the settings")
check("the seven lands are Element.Type 1 to 7, named as the game names them",
      app_module.LANDS == {1: "dark", 2: "light", 3: "ice", 4: "wind", 5: "earth", 6: "fire", 7: "water"})
check("every land has monsters that pay and can drop a bag", sorted(BY_LAND) == list(range(1, 8)), BY_LAND)
with app_module.app.app_context():
    defaults = app_module.world_settings()
check("with no row: on, 3-hour stirs, hits +25%, health +50%, speed +15%, loot 1.5x, resistance 1.5x, more XP there but off",
      defaults == {"on": True, "hours": 3, "hit_on": True, "hit_percent": 25, "health_on": True, "health_percent": 50,
                   "speed_on": True, "speed_percent": 15, "bags_on": True, "bags_times": 1.5,
                   "resist_on": True, "resist_times": 1.5, "xp_on": False, "xp_percent": 25}, defaults)
sql("INSERT OR REPLACE INTO server_settings (key, value) VALUES (?, ?)", (app_module.WORLD_KEY, "{not json"))
with app_module.app.app_context():
    broken = app_module.world_settings()
check("a row that cannot be read: OFF - a broken setting never makes the game harder", broken["on"] is False)
sql("INSERT OR REPLACE INTO server_settings (key, value) VALUES (?, ?)", (app_module.WORLD_KEY,
    json.dumps({"hours": 99, "hit_percent": -5, "bags_times": "lots", "resist_times": 1.75, "xp_on": "yes",
                "health_percent": 900, "speed_percent": 80, "speed_on": 1})))
with app_module.app.app_context():
    held = app_module.world_settings()
check("values out of range are held to it, values of the wrong kind are the default",
      held["hours"] == 24 and held["hit_percent"] == 0 and held["bags_times"] == 1.5 and held["resist_times"] == 1.75
      and held["xp_on"] is False and held["on"] is True and held["health_percent"] == 200 and held["speed_percent"] == 50
      and held["speed_on"] is True, held)
reset_world()

for body, why in [({}, "nothing"), ({"color": "red"}, "unknown"), ({"on": "yes"}, "switch not a bool"),
                  ({"hours": 0}, "hours under 1"), ({"hours": 2.5}, "hours not whole"), ({"hit_percent": 101}, "hit over 100"),
                  ({"bags_times": 3.5}, "bags over 3"), ({"resist_times": 0.5}, "resist under 1"),
                  ({"xp_percent": True}, "a bool for a number"), ({"health_percent": 201}, "health over 200"),
                  ({"speed_percent": 51}, "speed over 50"), ({"speed_percent": 10.5}, "speed not whole"),
                  ({"health_on": "on"}, "health switch not a bool")]:
    res = client.post("/api/server/world", headers=auth(owner), json=body)
    check("  refused with 400: %s" % why, res.status_code == 400, (res.status_code, res.get_json()))
check("nobody but the owner sees or changes it - the same bare 404 as no route",
      client.get("/api/server/world", headers=auth(player)).status_code == 404
      and client.post("/api/server/world", headers=auth(player), json={"on": False}).status_code == 404
      and client.post("/api/server/world/stir", headers=auth(player), json={"element": 6}).status_code == 404)
check("  and both changes ask for the password again on a hardened session (STEP-UP)",
      getattr(app_module.set_world, "_elusion_fresh_password", False)
      and getattr(app_module.stir_world, "_elusion_fresh_password", False))
res = client.post("/api/server/world", headers=auth(owner),
                  json={"hours": 2, "hit_percent": 40, "xp_on": True, "bags_times": 2.0})
body = res.get_json() or {}
check("the owner's change is saved, and answered with the world",
      res.status_code == 200 and body["settings"]["hours"] == 2 and body["settings"]["hit_percent"] == 40
      and body["settings"]["xp_on"] is True and body["settings"]["bags_times"] == 2.0 and "state" in body, body)
logged = sql("SELECT action, detail FROM staff_actions WHERE action = 'world' ORDER BY id DESC LIMIT 1")
check("  and written in the moderation log", logged and "hit_percent=40" in logged[0]["detail"], [dict(r) for r in logged])
sql("DELETE FROM broadcasts")
client.post("/api/server/world", headers=auth(owner), json={"on": False})
check("switched off: 'The lands grow still.'", [m["body"] for m in world_messages()] == ["The lands grow still."])
sql("DELETE FROM broadcasts")
client.post("/api/server/world", headers=auth(owner), json={"on": True})
said = world_messages()
check("  and on again: a stir, announced once", len(said) == 1 and "lands stir for 2 hours" in said[0]["body"], said)
reset_world()

# =============================================================================
section("W-2  the ticks")
settings = dict(app_module.WORLD_DEFAULTS)
t0 = 1_800_000_000
first = in_app(lambda db: app_module.current_world_tick(db, settings, now=t0))
check("the first ask writes stir 1, three hours long",
      first["tick"] == 1 and first["started_at"] == t0 and first["ends_at"] == t0 + 3 * 3600 and first["chosen_by"] == "", first)
again = in_app(lambda db: app_module.current_world_tick(db, settings, now=t0 + 3600))
check("  asked again before it ends: the same stir, nothing new written", again == first and len(ticks()) == 1)
second = in_app(lambda db: app_module.current_world_tick(db, settings, now=t0 + 3 * 3600 + 60))
check("after it ends: stir 2 begins where stir 1 ended - the rhythm holds",
      second["tick"] == 2 and second["started_at"] == t0 + 3 * 3600, second)
check("  and it is never the same land twice running", second["element"] != first["element"])
late = in_app(lambda db: app_module.current_world_tick(db, settings, now=t0 + 20 * 3600))
check("after a quiet spell longer than a stir: the next begins when somebody asks",
      late["tick"] == 3 and late["started_at"] == t0 + 20 * 3600, late)
said = world_messages()
check("each stir is announced once, by the world, in its own kind",
      len(said) == 3 and all(m["created_by"] == "" for m in said)
      and said[0]["body"].startswith("The %s lands stir for 3 hours" % first["name"]), said)

seen = [r["element"] for r in ticks()]
now_t = t0 + 20 * 3600
for _ in range(40):
    now_t += 3 * 3600
    in_app(lambda db: app_module.current_world_tick(db, settings, now=now_t))
seen = [r["element"] for r in ticks()]
check("forty stirs on: no land twice running, and every land has stirred",
      all(a != b for a, b in zip(seen, seen[1:])) and set(seen) == set(range(1, 8)), seen)

reset_world()
now = int(time.time())
# Every land hunted but ice (3) and wind (4); wind hunted once, ice never.
for element, enemy_id in BY_LAND.items():
    for _ in range(0 if element == 3 else (1 if element == 4 else 5)):
        sql("INSERT INTO kill_reports (user_id, slot, enemy_id, at) VALUES (?, 0, ?, ?)", (player_id, enemy_id, now - 60))
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, at) VALUES (?, 0, ?, ?)", (player_id, BY_LAND[3], now - 3 * 86400))
hunts = in_app(lambda db: app_module.land_hunts(db, now))
check("each land's hunting is its paid kills in the last day, older ones forgotten",
      hunts[3] == 0 and hunts[4] == 1 and hunts[6] == 5, hunts)
check("the least hunted land stirs", in_app(lambda db: app_module.pick_land(db, 1, 0, now)) == 3)
check("  unless it just stirred: then the next least", in_app(lambda db: app_module.pick_land(db, 1, 3, now)) == 4)
sql("DELETE FROM kill_reports")
picks = {in_app(lambda db: app_module.pick_land(db, 77, 0, now)) for _ in range(5)}
check("a tie (a quiet server) is settled by the tick: the same tick always picks the same land", len(picks) == 1, picks)
check("  and different ticks spread over the lands",
      len({in_app(lambda db, t=t: app_module.pick_land(db, t, 0, now)) for t in range(1, 40)}) >= 5)

reset_world()
def two_writers(db):
    a = app_module._begin_stir(db, 1, 5, now, settings)
    b = app_module._begin_stir(db, 1, 6, now, settings)
    return a, b
wrote = in_app(two_writers)
check("two asking at once write one stir and one announcement: the first in keeps it",
      wrote == (True, False) and [r["element"] for r in ticks()] == [5] and len(world_messages()) == 1, (wrote, ticks()))

# =============================================================================
section("W-3  the poll")
reset_world()
res = poll(player)
body = res.get_json() or {}
world = body.get("world") or {}
check("the broadcast poll carries the world: land, name, when, and the numbers the game applies",
      res.status_code == 200 and world.get("on") is True and world.get("tick") == 1 and world.get("element") in app_module.LANDS
      and world.get("name") == app_module.LANDS[world["element"]] and world["ends_at"] - world["started_at"] == 3 * 3600
      and world["hit"] == 0.25 and world["resist"] == 1.5 and world["bags"] == 1.5 and world["xp"] == 0.0
      and world["health"] == 0.5 and world["speed"] == 0.15, world)
check("  and the stir this poll began is announced in this poll's own messages",
      any(m["kind"] == "world" and m["body"].startswith("The %s lands stir" % world["name"]) for m in body["messages"]),
      body.get("messages"))
said = [m["body"] for m in body["messages"] if m["kind"] == "world"]
check("  saying what it does: harder hits, more health, faster, more loot, resistance counting more",
      said and said[0] == "The %s lands stir for 3 hours: %s monsters hit 25%% harder, have 50%% more health, "
      "move 15%% faster and drop loot 1.5x as often, and %s resistance counts 1.5x." % (world["name"], world["name"], world["name"]),
      said)
client.post("/api/server/world", headers=auth(owner), json={"hit_on": False, "resist_on": False, "health_on": False,
                                                          "speed_on": False})
world = poll(player).get_json()["world"]
check("an effect switched off is sent as nothing: hits +0, health +0, speed +0, resistance 1x",
      world["hit"] == 0.0 and world["resist"] == 1.0 and world["health"] == 0.0 and world["speed"] == 0.0, world)
client.post("/api/server/world", headers=auth(owner), json={"on": False})
world = poll(player).get_json()["world"]
check("switched off: no land", world == {"on": False, "tick": 0, "element": 0}, world)
client.post("/api/server/world", headers=auth(owner), json={"on": True, "hit_on": True, "resist_on": True,
                                                          "health_on": True, "speed_on": True})
sql("DROP TABLE world_ticks")
res = poll(player)
check("the table gone: no land, and the poll - the heartbeat - still answers",
      res.status_code == 200 and (res.get_json() or {}).get("world", {}).get("element") == 0, res.status_code)
# PUT BACK BY world/'s OWN CREATE (world/schema.py, since 19 Oct), not a copy
# of it here: a copy is a second table definition that can drift from the one
# a server builds.
import world.schema  # noqa: E402

with app_module.app.app_context():
    world.schema.create(app_module.get_db())

# =============================================================================
section("W-4  kills")
reset_world()
captured = []
real_roll = gamedata.roll_kill_rewards


def spy(enemy_id, class_id="", bag_scale=1.0, xp_scale=1.0, material_drop_scale=1.0):
    # material_drop_scale (0.74.0) is the owner's economy number, not the
    # stirring lands': passed through as the kill route sent it.
    captured.append((enemy_id, bag_scale, xp_scale))
    return real_roll(enemy_id, class_id, bag_scale, xp_scale, material_drop_scale=material_drop_scale)


def kill(enemy_id):
    sql("UPDATE saves SET kill_tokens = 50, last_kill_at = 0 WHERE user_id = ?", (player_id,))
    return client.post("/api/combat/kill", headers=auth(player), json={"slot": 0, "enemy_id": enemy_id})


land = poll(player).get_json()["world"]["element"]
other = next(e for e in app_module.LANDS if e != land)
gamedata.roll_kill_rewards = spy
try:
    kill(BY_LAND[land])
    kill(BY_LAND[other])
    client.post("/api/server/world", headers=auth(owner), json={"xp_on": True, "xp_percent": 50})
    kill(BY_LAND[land])
    client.post("/api/server/world", headers=auth(owner), json={"bags_on": False})
    kill(BY_LAND[land])
    client.post("/api/server/world", headers=auth(owner), json={"on": False})
    kill(BY_LAND[land])
finally:
    gamedata.roll_kill_rewards = real_roll
scales = [(c[1], c[2]) for c in captured]
check("a kill of the stirring land's monster: loot 1.5x as often, XP as it was (more XP is off)",
      scales[0] == (1.5, 1.0), scales)
check("  a kill of any other land's: ordinary", scales[1] == (1.0, 1.0), scales)
check("  more XP switched on at 50%: 1.5x XP", scales[2] == (1.5, 1.5), scales)
check("  loot switched off: 1x loot, the XP kept", scales[3] == (1.0, 1.5), scales)
check("  the lands switched off: an ordinary kill", scales[4] == (1.0, 1.0), scales)
client.post("/api/server/world", headers=auth(owner), json={"on": True, "bags_on": True, "xp_on": True, "xp_percent": 50})
enemy = BY_LAND[land]
paid = (kill(enemy).get_json() or {}).get("xp_gained")
check("the XP paid is the server's, with the stir in it",
      paid == int(round(gamedata.ENEMIES[enemy]["xp_reward"] * 1.5)), (paid, gamedata.ENEMIES[enemy]["xp_reward"]))

kept_rng = gamedata._rng
try:
    # THE BAG ROLL ALONE: since 0.74.0 a monster's materials make a bag of
    # their own (gamedata.roll_material_drops()), on their own odds that no
    # stir touches, so they are switched off here to measure what a stir
    # scales - the bag roll - and nothing else.
    def bag_rate(scale, enemy_id, n=4000):
        gamedata._rng = random.Random(12345)
        return sum(1 for _ in range(n)
                   if gamedata.roll_kill_rewards(enemy_id, "", scale, material_drop_scale=0.0)["contents"]) / n
    plain, stirred = bag_rate(1.0, "darkslime"), bag_rate(1.5, "darkslime")
    check("the roll: a 30%% monster drops near 45%% at 1.5x (%.2f, %.2f)" % (plain, stirred),
          0.26 < plain < 0.34 and 0.41 < stirred < 0.49, (plain, stirred))
    check("  a boss's certain bag stays certain, and a monster that drops none still drops none",
          bag_rate(2.0, "fireboss", 200) == 1.0 and bag_rate(3.0, "darkslimelarge", 200) == 0.0)
finally:
    gamedata._rng = kept_rng

# =============================================================================
section("W-5  stirring a land by hand")
reset_world()
start = poll(player).get_json()["world"]
pick = 6 if start["element"] != 6 else 2
sql("DELETE FROM broadcasts")
res = client.post("/api/server/world/stir", headers=auth(owner), json={"element": pick})
rows = ticks()
check("the owner stirs a land: the stir going on ends now, and the owner's begins - a whole stir long",
      res.status_code == 200 and len(rows) == 2 and rows[0]["ends_at"] <= int(time.time())
      and rows[1]["element"] == pick and rows[1]["chosen_by"] == "worldowner"
      and rows[1]["ends_at"] - rows[1]["started_at"] == 3 * 3600, rows)
said = world_messages()
check("  announced, signed by the owner", len(said) == 1 and said[0]["created_by"] == "worldowner"
      and said[0]["body"].startswith("The %s lands stir" % app_module.LANDS[pick]), said)
check("  and the game hears it on its next poll", poll(player).get_json()["world"]["element"] == pick)
for bad in (0, 8, "6", True, None):
    res = client.post("/api/server/world/stir", headers=auth(owner), json={"element": bad})
    check("  not one of the seven lands (%r): 400" % (bad,), res.status_code == 400, res.status_code)
client.post("/api/server/world", headers=auth(owner), json={"on": False})
check("switched off: stirring by hand is a 409, not a surprise",
      client.post("/api/server/world/stir", headers=auth(owner), json={"element": 3}).status_code == 409)
client.post("/api/server/world", headers=auth(owner), json={"on": True})

# =============================================================================
section("W-6  the owner's view")
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, at) VALUES (?, 0, ?, ?)", (player_id, BY_LAND[7], int(time.time())))
view = client.get("/api/server/world", headers=auth(owner)).get_json() or {}
check("the settings, the stir now, the last few, each land's hunting, and the ranges",
      set(view) >= {"settings", "state", "recent", "lands", "limits", "now"}
      and view["state"]["element"] in app_module.LANDS and view["recent"][0]["tick"] == view["state"]["tick"]
      and [l["name"] for l in view["lands"]] == ["dark", "light", "ice", "wind", "earth", "fire", "water"]
      and view["lands"][6]["kills"] >= 1 and view["limits"]["hours"] == [1, 24]
      and view["limits"]["health_percent"] == [0, 200] and view["limits"]["speed_percent"] == [0, 50], view)

# =============================================================================
section("W-7  the books")
import combatbook  # noqa: E402
spec_p = importlib.util.spec_from_file_location("elusion_presence_world", os.path.join(HERE, "presence.py"))
presence = importlib.util.module_from_spec(spec_p)
spec_p.loader.exec_module(presence)
AREA = next(iter(gamedata.AREAS)) if gamedata.AREAS else None
if AREA is None:
    print("  skip  the books: this catalogue has no areas' maps")
else:
    fire_id = next(e for e in sorted(gamedata.ENEMIES) if int(gamedata.ENEMIES[e].get("element", 0)) == 6
                   and gamedata.ENEMIES[e].get("resource"))
    water_id = next(e for e in sorted(gamedata.ENEMIES) if int(gamedata.ENEMIES[e].get("element", 0)) == 7
                    and gamedata.ENEMIES[e].get("resource"))

    class Leader:
        user_id = 1
        slot = 0
        reports_hits = True

    def born(book, monster_id, enemy_id, sent_max):
        enemy = gamedata.ENEMIES[enemy_id]
        book.world(Leader(), {"ev": [{"k": "spawn", "r": {"id": monster_id, "o": "", "x": 0, "y": 0,
                                                          "hp": sent_max, "mh": sent_max,
                                                          "p": {"ed": enemy["resource"]}}}]}, time.time())
        flags = [f for f in book.flags if f[2] == "max_hp_differs" and f[3].startswith(enemy_id + " ")]
        return book.monsters.get(monster_id), flags

    fire_max = int(gamedata.ENEMIES[fire_id]["max_hp"])
    water_max = int(gamedata.ENEMIES[water_id]["max_hp"])
    book = combatbook.AreaBook(AREA, gamedata.AREAS[AREA])
    book.stirred_lands = frozenset({6})
    m, flags = born(book, 101, fire_id, fire_max + fire_max // 2)
    check("a fire monster born with 50%% more health while fire stirs: counted at %d, not flagged" % (fire_max + fire_max // 2),
          m is not None and m.max_hp == fire_max + fire_max // 2 and m.hp == m.max_hp and not flags, (m and m.max_hp, flags))
    m, flags = born(book, 102, water_id, water_max * 2)
    check("  a water monster with more health while fire stirs: flagged, and counted at the catalogue's",
          m is not None and m.max_hp == water_max and flags, (m and m.max_hp, flags))
    book = combatbook.AreaBook(AREA, gamedata.AREAS[AREA])
    book.stirred_lands = frozenset({6})
    m, flags = born(book, 103, fire_id, fire_max * 4)
    check("  more than three times the catalogue's, stirring or not: flagged", m.max_hp == fire_max and flags,
          (m.max_hp, flags))
    book = combatbook.AreaBook(AREA, gamedata.AREAS[AREA])
    book.stirred_lands = frozenset({6})
    m, flags = born(book, 104, fire_id, fire_max - 1)
    check("  and LESS than the catalogue's - the way a cheat would go - is flagged as ever", m.max_hp == fire_max and flags,
          (m.max_hp, flags))
    reset_world()
    now = int(time.time())
    sql("INSERT INTO world_ticks (tick, element, started_at, ends_at) VALUES (1, 3, ?, ?)", (now - 9000, now - 5000))
    sql("INSERT INTO world_ticks (tick, element, started_at, ends_at) VALUES (2, 4, ?, ?)", (now - 5000, now - 600))
    sql("INSERT INTO world_ticks (tick, element, started_at, ends_at) VALUES (3, 6, ?, ?)", (now - 600, now + 3000))
    check("presence.py reads the lands stirring now and lately - not one long over",
          presence.stirred_lands(now) == frozenset({4, 6}), presence.stirred_lands(now))
    sql("DROP TABLE world_ticks")
    check("  and none from a database from before them", presence.stirred_lands(now) == frozenset())

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
