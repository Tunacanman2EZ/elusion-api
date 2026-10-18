"""The land insights (game 0.50.0): what the world's data says about its lands.

The owner's "Behavior Insights": for each land, recent players and kills, time
since its last event, whether it is busy (overfarmed), quiet or ignored; a
next stir with a reason that weighs new players against veterans; and who is
online by hour. Built like playerstats/ and events/, so each layer is held on
its own:

  I-1  services.py, the rules: counts per land, a land's status, the next
       stir (never where a new player is learning, while another will do) and
       its reason, the busiest hours
  I-2  data.py, the SQL: reads only, and nothing from a database without the
       tables
  I-3  routes.py, the edge: the owner's alone, every land's numbers
  I-4  the world: its own pick passes over a land a new player is learning
       in; the owner's "next stir" is what it then picks; a stir event's land
       is the next when one is on; a rule that breaks is the old rule

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_insights.py
"""
import importlib.util
import os
import random
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_insights_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "insightsowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

from insights import data, services  # noqa: E402

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


def in_app(fn):
    with app_module.app.app_context():
        db = app_module.get_db()
        out = fn(db)
        db.commit()
        return out


LANDS = app_module.LANDS
DAY = services.DAY
HOUR = services.HOUR
BY_LAND = {}
for enemy_id, enemy in sorted(gamedata.ENEMIES.items()):
    element = int(enemy.get("element", 0) or 0)
    if element in LANDS:
        BY_LAND.setdefault(element, enemy_id)


def element_of(enemy_id):
    return int((gamedata.ENEMIES.get(enemy_id) or {}).get("element", 0) or 0)


# =============================================================================
section("I-1  services.py - the rules")
rows = [(BY_LAND[6], 5), (BY_LAND[6], 2), (BY_LAND[3], 1), ("no such monster", 9)]
check("kills per land: every land there, a monster of no land not counted",
      services.per_land(rows, element_of, LANDS) == {1: 0, 2: 0, 3: 1, 4: 0, 5: 0, 6: 7, 7: 0})
check("  people per land: each person once",
      services.people_per_land([(BY_LAND[6], 1), (BY_LAND[6], 1), (BY_LAND[6], 2), (BY_LAND[3], 2)], element_of, LANDS)[6] == 2)
check("a land's status from its share of the week: none ignored, under half an even share quiet, twice busy",
      [services.status(0, 70, 7), services.status(4, 70, 7), services.status(10, 70, 7), services.status(20, 70, 7)]
      == ["ignored", "quiet", "healthy", "busy"])
hunts = {1: 5, 2: 5, 3: 0, 4: 1, 5: 5, 7: 5}
land, passed_over = services.choose([1, 2, 3, 4, 5, 7], hunts, {}, random.Random(1))
check("the next stir, with nobody new about: the least hunted (ice, never hunted)", (land, passed_over) == (3, []))
land, passed_over = services.choose([1, 2, 3, 4, 5, 7], hunts, {3: 1}, random.Random(1))
check("  a new player learning in ice: wind instead, and ice passed over for it", (land, passed_over) == (4, [3]))
land, passed_over = services.choose([1, 2, 3, 4], {1: 5, 2: 5, 3: 0, 4: 1}, {1: 1, 3: 1}, random.Random(1))
check("  passed over: only the lands hunted less than the pick - not every land with a new player",
      (land, passed_over) == (4, [3]), (land, passed_over))
land, passed_over = services.choose([1, 2, 3], {1: 5, 2: 2, 3: 0}, {1: 1, 2: 1, 3: 2}, random.Random(1))
check("  new players in every land: the least hunted of all", (land, passed_over) == (3, []))
picks = {services.choose([1, 2, 3], {}, {}, random.Random(9))[0] for _ in range(5)}
check("  a tie is the caller's seeded Random: the same seed, the same land", len(picks) == 1)
check("its reason, in a sentence",
      services.reason(3, hunts, {}, [], [1, 2, 3, 4, 5, 7], LANDS) == "Ice: the least hunted in the last day (0 kills)."
      and services.reason(4, hunts, {3: 1}, [3], [1, 2, 3, 4, 5, 7], LANDS)
      == "Wind: the least hunted in the last day (1 kill) without new players in it - passing over Ice (1 new player), where they are learning."
      and services.reason(3, {3: 0}, {1: 1, 3: 2}, [], [1, 3], LANDS)
      == "Ice: the least hunted in the last day (0 kills) - every land has new players in it, so the least hunted of all.")
busiest = services.busiest_hour([(BY_LAND[6], 20, 4), (BY_LAND[6], 3, 9), (BY_LAND[3], 12, 1)], element_of, LANDS)
check("each land's busiest hour of the day (UTC); none for a land nobody hunts",
      busiest[6] == 3 and busiest[3] == 12 and busiest[1] is None, busiest)
now_b = 20000 * DAY
first = now_b // 300 - 24 * 12
hours = services.online_by_hour({first + 12 * 4 + k: 2 for k in range(12)}, first, now_b)
check("who is online by hour: two players all of 04:00 UTC, a five minutes with no row is nobody, a day counted",
      hours["days"] == 1.0 and hours["players"][4] == 2.0 and sum(1 for v in hours["players"] if v > 0) == 1, hours)
hours = services.online_by_hour({first + 12 * 4: 2}, first, now_b)
check("  two players for five minutes of an hour: a twelfth of that hour", hours["players"][4] == round(2 / 12.0, 2), hours)
check("  nothing counted yet: nothing said", services.online_by_hour({}, None, now_b) == {"players": [0.0] * 24, "days": 0.0})
check("when an event last covered each land: every monster's, its own, a stir of it - not the stirring land's",
      services.last_event_for([("xp", 0, 100), ("loot", 6, 300), ("stir", 3, 250), ("xp", -1, 900)], LANDS)
      == {1: 100, 2: 100, 3: 250, 4: 100, 5: 100, 6: 300, 7: 100})

# =============================================================================
section("I-2  data.py - reads only")
mem = sqlite3.connect(":memory:")
check("a database without the tables answers with nothing",
      data.kills_by_enemy(mem, 0) == [] and data.newcomers_by_enemy(mem, 0, 10) == [] and data.last_stirred(mem) == {}
      and data.events_begun(mem, 0, 1) == [] and data.online_samples(mem, 0, 1) == ({}, None))
source = open(os.path.join(HERE, "insights", "data.py"), encoding="utf-8").read().upper()
check("data.py only reads: no INSERT, UPDATE, DELETE, REPLACE, DROP, CREATE, ALTER or commit",
      not any(word in source for word in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "DROP ", "CREATE ", "ALTER ", ".COMMIT(")))

# =============================================================================
section("I-3  routes.py - the edge")
owner = register("insightsowner")
player = register("insightsnew")
veteran = register("insightsvet")
devtok = register("insightsdev")
sql("UPDATE users SET role = 'dev' WHERE username = 'insightsdev'")
ids = {r["username"]: int(r["id"]) for r in sql("SELECT id, username FROM users")}
views = app_module.app.view_functions
codes = [client.get("/api/server/insights", headers=auth(t)).status_code for t in (player, devtok)]
check("a player and a dev get the same bare 404 as no route", codes == [404, 404], codes)
check("  and nobody without a login", client.get("/api/server/insights").status_code == 401)
check("  it only reads, so it asks for no password again",
      not getattr(views["read_insights"], "_elusion_fresh_password", False)
      and getattr(views["read_insights"], "_elusion_min_role", None) == "coowner")
now = int(time.time())


def kill(user, land, at, level):
    sql("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 0, ?, 10, 0, ?, ?)",
        (ids[user], BY_LAND[land], level, at))


sql("DELETE FROM kill_reports")
for _ in range(12):
    kill("insightsvet", 6, now - 3 * HOUR, 20)          # fire: busy, a veteran's
kill("insightsvet", 6, now - 3 * DAY, 20)
kill("insightsnew", 3, now - 600, 3)                    # ice: a new player, now
kill("insightsnew", 3, now - 3 * HOUR, 3)
kill("insightsdev", 4, now - 600, 1)                    # wind: staff, not a player
kill("insightsowner", 5, now - 600, 1)                  # earth: the owner, not a player
kill("insightsvet", 2, now - 10 * DAY, 20)              # light: over a week ago
kill("insightsnew", 7, now - 3 * HOUR, 3)                # water: the new player, three hours ago
res = client.get("/api/server/insights", headers=auth(owner))
body = res.get_json() or {}
by_land = {row["element"]: row for row in body.get("lands", [])}
check("the owner: every land, in order", res.status_code == 200 and [r["element"] for r in body["lands"]] == sorted(LANDS), body)
check("  kills today and this week, and who played - players only, the owner and staff left out",
      by_land[6]["kills_day"] == 12 and by_land[6]["kills_week"] == 13 and by_land[6]["players_week"] == 1
      and by_land[4]["kills_week"] == 1 and by_land[4]["players_week"] == 0 and by_land[5]["players_week"] == 0
      and by_land[2]["kills_week"] == 0, by_land)
check("  new players there now: the one in ice; not the dev or the owner at level 1; not where they were three hours ago",
      by_land[3]["newcomers_now"] == 1 and by_land[4]["newcomers_now"] == 0 and by_land[5]["newcomers_now"] == 0
      and by_land[7]["newcomers_now"] == 0, by_land)
check("  each land's status: fire busy, light and dark ignored; a rough read under %d kills in the week" % services.FEW_KILLS,
      by_land[6]["status"] == "busy" and by_land[2]["status"] == "ignored" and by_land[1]["status"] == "ignored"
      and body["total_week"] == 18 and body["few"] is True, [(e, r["status"]) for e, r in by_land.items()])
check("  and its busiest hour (UTC), and who is online by hour",
      by_land[6]["busiest_hour"] == ((now - 3 * HOUR) // 3600) % 24 and len(body["hours"]["players"]) == 24
      and body["new_player_level"] == services.NEW_PLAYER_LEVEL, by_land[6])

# =============================================================================
section("I-4  the world's own pick")
sql("DELETE FROM world_ticks")
sql("DELETE FROM broadcasts")
sql("DELETE FROM kill_reports")
for land_id in LANDS:
    for _ in range(0 if land_id == 3 else (2 if land_id == 4 else 5)):
        kill("insightsvet", land_id, now - 3600, 20)        # ice never hunted, wind twice
check("nobody new about: the least hunted stirs (ice)",
      in_app(lambda db: app_module.pick_land(db, 1, 0, now)) == 3)
kill("insightsvet", 3, now - 600, 20)
check("  a veteran hunting there: still ice (1 kill against wind's 2)",
      in_app(lambda db: app_module.pick_land(db, 1, 0, now)) == 3)
sql("DELETE FROM kill_reports WHERE enemy_id = ? AND level_at = 20", (BY_LAND[3],))
kill("insightsnew", 3, now - 600, 3)
check("a new player learning in ice: the world passes it over - wind stirs",
      in_app(lambda db: app_module.pick_land(db, 1, 0, now)) == 4)
kept = services.choose


def broken(*_args, **_kwargs):
    raise ValueError("a rule that breaks")


services.choose = broken
try:
    fallback = in_app(lambda db: app_module.pick_land(db, 1, 0, now))
    page = client.get("/api/server/insights", headers=auth(owner))
finally:
    services.choose = kept
check("a rule that breaks: the old rule, the least hunted - new players or not - and the owner's page still answers",
      fallback == 3 and page.status_code == 200, (fallback, page.status_code))
first_row = in_app(lambda db: app_module.current_world_tick(db, None, now))
upcoming = client.get("/api/server/world", headers=auth(owner)).get_json().get("next")
check("the owner's next stir: the land, when, and why",
      upcoming is not None and upcoming["at"] == first_row["ends_at"] and upcoming["reason"].startswith(
          "%s: the least hunted in the last day" % LANDS[upcoming["element"]].capitalize()), upcoming)
# THE SAME MOMENT'S DATA: a new player still in ice when the stir ends.
kill("insightsnew", 3, first_row["ends_at"] - 600, 3)
upcoming = in_app(lambda db: app_module.world_next_stir(db, first_row["ends_at"] - 1))
picked = in_app(lambda db: app_module.current_world_tick(db, None, first_row["ends_at"] + 5))
check("  and it is the land the world then picks", picked["element"] == upcoming["element"], (picked, upcoming))
sql("INSERT INTO world_events (name, kind, amount, land, starts_at, ends_at, created_at) VALUES ('The Water Lands Stir', 'stir', 1, 7, ?, ?, ?)",
    (picked["ends_at"] - 60, picked["ends_at"] + 2 * HOUR, now))
upcoming = in_app(lambda db: app_module.world_next_stir(db, picked["started_at"] + 5))
check("a stir event on when this stir ends: the next is its land, and it says so",
      upcoming["element"] == 7 and upcoming["reason"] == "Water: The Water Lands Stir is on then.", upcoming)
sql("DELETE FROM world_events")
client.post("/api/server/world", headers=auth(owner), json={"on": False})
check("the lands switched off: no next stir",
      client.get("/api/server/world", headers=auth(owner)).get_json().get("next") is None)
client.post("/api/server/world", headers=auth(owner), json={"on": True})

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
