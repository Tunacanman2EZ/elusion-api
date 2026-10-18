"""The player stats (game 0.47.0): the owner's numbers on the players.

The owner: "build this and add it in my gm menu as a button somewhere" - who
signs up, who comes back, where players stop, what gets played. Built the
BE105 way, so each layer is held on its own:

  S-1  services.py, the arithmetic: a made-up history with known answers -
       who is a player, the owner's days, the new players' road, where the
       ones who stopped stopped, day by day, the monsters, the classes
  S-2  data.py, the SQL: each read against a real database, a missing table
       answered with nothing, and not one write anywhere in the file
  S-3  routes.py, the edge: the owner's alone (the same bare 404 as no route),
       read only, no password again, staff never counted, a bad query held to
       the defaults - and the package never imports app.py

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_stats.py
"""
import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_stats_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "statsowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

from playerstats import data, services  # noqa: E402

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


DAY = services.DAY
NOW = 1_800_000_000          # a fixed clock for the pure checks


def acct(uid, name, created, role="player"):
    return {"id": uid, "username": name, "role": role, "created_at": created}


def char(level, area, updated, class_id="warrior", slot=0):
    return {"slot": slot, "class_id": class_id, "level": level, "area": area, "updated_at": updated}


# =============================================================================
section("S-1  services.py - the arithmetic")
kept, left = services.players([acct(1, "Ann", 0), acct(2, "Mo", 0, "mod"), acct(3, "Dev", 0, "dev"),
                               acct(4, "TheOwner", 0), acct(5, "Bo", 0)], ["theowner", ""])
check("players are players: staff by role and the owner by name are left out, and said",
      [a["id"] for a in kept] == [1, 5] and left == 3, (kept, left))

check("a day starts at the owner's midnight, not the server's",
      services.day_start(NOW, 0) == NOW // DAY * DAY
      and services.day_start(NOW, -600) == (NOW - 600 * 60) // DAY * DAY + 600 * 60
      and services.day_label(services.day_start(NOW, -600), -600) == "2027-01-14"
      and services.day_label(services.day_start(NOW, 0), 0) == "2027-01-15",
      (services.day_label(services.day_start(NOW, -600), -600), services.day_label(services.day_start(NOW, 0), 0)))
check("  an offset is held to the world's: -14 h to +14 h, and junk is UTC",
      services.clamp_offset(-300) == -300 and services.clamp_offset(9999) == 840
      and services.clamp_offset(-9999) == -840 and services.clamp_offset("x") == 0 and services.clamp_offset(None) == 0)

# A small history, every number of it known.
#   1 Ann     joined 20 days ago, played a fortnight, level 12, still playing
#   2 Bo      joined 10 days ago, played 5 minutes, one character, no kill
#   3 Cy      joined 5 days ago, played 40 minutes, level 3, first kill 6 min in
#   4 Di      joined 2 days ago, came back the next day, level 6, first kill 2 min in
#   5 Ed      joined 2 hours ago, no character yet
#   6 Fay     joined 60 days ago, left 50 days ago at level 22 in the field
#   7 Mod     a moderator - never counted
accounts = [acct(1, "Ann", NOW - 20 * DAY), acct(2, "Bo", NOW - 10 * DAY), acct(3, "Cy", NOW - 5 * DAY),
            acct(4, "Di", NOW - 2 * DAY), acct(5, "Ed", NOW - 7200), acct(6, "Fay", NOW - 60 * DAY),
            acct(7, "Mod", NOW - 30 * DAY, "mod")]
characters = {
    1: [char(12, "field", NOW - 3600), char(4, "elusion", NOW - 15 * DAY, "mage", 1)],
    2: [char(1, "elusion", NOW - 10 * DAY + 300)],
    3: [char(3, "elusion", NOW - 5 * DAY + 2400)],
    4: [char(6, "field", NOW - DAY + 600)],
    6: [char(22, "field", NOW - 50 * DAY, "tank")],
    7: [char(40, "field", NOW)],
}
last_seen = {1: NOW - 3600, 2: NOW - 10 * DAY + 300, 3: NOW - 5 * DAY + 2400, 4: NOW - DAY + 600,
             6: NOW - 50 * DAY, 7: NOW}
kill_totals = {1: (900, NOW - 20 * DAY + 600), 3: (14, NOW - 5 * DAY + 360), 4: (30, NOW - 2 * DAY + 120),
               6: (5000, NOW - 59 * DAY), 7: (99, NOW - DAY)}
enemies = {"slime": {"display_name": "Slime", "grants_rewards": True, "placed_count": 9},
           "sprite": {"display_name": "Sprite", "grants_rewards": True, "placed_count": 4},
           "boss": {"display_name": "The Crowned", "grants_rewards": True, "placed_count": 1},
           "ghost": {"display_name": "Ghost", "grants_rewards": True, "placed_count": 0},
           "target": {"display_name": "Target", "grants_rewards": False, "placed_count": 3}}
kills_recent = [(1, "slime", NOW - 3600)] * 6 + [(4, "slime", NOW - DAY + 300)] * 3 + [(4, "sprite", NOW - 2 * DAY + 120)] \
    + [(7, "boss", NOW - 600)] * 5 + [(3, "slime", NOW - 20 * DAY)]
logins_recent = [("ann", NOW - 3600), ("DI", NOW - DAY + 30), ("Mod", NOW - 60), ("nobody", NOW - 60)]
report = services.build_report(accounts, characters, last_seen, kill_totals, kills_recent, logins_recent, enemies,
                               NOW, 0, ["theowner"])
p = report["players"]
check("the players: 6, the moderator left out; 3 new this week, 5 this month, 4 seen this week",
      p == {"total": 6, "staff_left_out": 1, "new_today": 1, "new_7d": 3, "new_30d": 5,
            "seen_today": 2, "seen_7d": 4, "seen_30d": 5}, p)
road = report["road"]
check("the new players' road: 5 joined, 4 made a character, 3 killed, 2 reached level 5, 1 level 10",
      road["signed_up"] == 5 and road["made_character"] == 4 and road["first_kill"] == 3
      and road["level_5"] == 2 and road["level_10"] == 1, road)
check("  came back a day or more later: 2 of the 4 who joined over a day ago; still there after a week: 1 of 2",
      road["came_back"] == 2 and road["came_back_of"] == 4 and road["stayed_week"] == 1 and road["stayed_week_of"] == 2,
      road)
check("  the middle new player's first kill came 6 minutes after signing up (2, 6 and 10)",
      road["minutes_to_first_kill"] == 6.0, road["minutes_to_first_kill"])
quit_ = report["quit"]
stayed = {row["label"]: row["players"] for row in quit_["stayed"]}
levels = {row["label"]: row["players"] for row in quit_["levels"]}
check("the ones who stopped (not seen for 3 days): Bo, Cy and Fay",
      quit_["players"] == 3 and quit_["never_killed"] == 1, quit_)
check("  how long they played: one under 10 minutes, one 10 to 60, one over a week",
      stayed == {"under 10 minutes": 1, "10 to 60 minutes": 1, "1 to 24 hours": 0, "1 to 7 days": 0,
                 "over a week": 1}, stayed)
check("  how far they got, and where they were last: town twice, the field once",
      levels["level 1"] == 1 and levels["levels 2-4"] == 1 and levels["level 20+"] == 1 and levels["no character"] == 0
      and quit_["areas"] == [{"area": "elusion", "players": 2}, {"area": "field", "players": 1}], (levels, quit_["areas"]))
daily = report["daily"]
check("day by day, the last two weeks, today first, each day new players and who played",
      len(daily) == services.DAILY_DAYS and daily[0]["day"] == "2027-01-15"
      and daily[0] == {"day": "2027-01-15", "new": 1, "played": 2} and daily[1]["played"] == 1
      and daily[2] == {"day": "2027-01-13", "new": 1, "played": 1}, daily[:3])
monsters = report["monsters"]
check("the monsters played these two weeks: slime most, by 2 players; the moderator's boss kills not counted",
      monsters["most"][0] == {"enemy": "slime", "name": "Slime", "kills": 9, "players": 2}
      and monsters["most"][1]["enemy"] == "sprite" and monsters["kills"] == 10, monsters)
check("  and the least: the boss nobody fought first; a monster nowhere on the map, or one that pays nothing, left out",
      [m["enemy"] for m in monsters["least"]] == ["boss", "sprite", "slime"] and monsters["never"] == 1
      and monsters["kinds"] == 3, monsters)
check("the classes: characters of each, and the highest level",
      report["classes"] == [{"class": "warrior", "characters": 4, "top_level": 12},
                            {"class": "mage", "characters": 1, "top_level": 4},
                            {"class": "tank", "characters": 1, "top_level": 22}], report["classes"])
empty = services.build_report([], {}, {}, {}, [], [], {}, NOW)
check("an empty server: zeros, no first kill, every day there", empty["players"]["total"] == 0
      and empty["road"]["minutes_to_first_kill"] is None and len(empty["daily"]) == services.DAILY_DAYS)

# =============================================================================
section("S-2  data.py - the SQL")
owner = register("statsowner")
devtok = register("statsdev")
ann = register("statsann")
bo = register("statsbo")
sql("UPDATE users SET role = 'dev' WHERE username = 'statsdev'")
now = int(time.time())
ids = {r["username"]: int(r["id"]) for r in sql("SELECT id, username FROM users")}
client.put("/api/save", headers=auth(ann), json={"slot": 0, "class_id": "warrior", "name": "Ann"})
sql("UPDATE saves SET level = 7, area = 'field', updated_at = ? WHERE user_id = ?", (now - 120, ids["statsann"]))
sql("INSERT INTO kill_tally (user_id, slot, enemy_id, kills, first_at, last_at) VALUES (?, 0, 'slime', 12, ?, ?)",
    (ids["statsann"], now - 3000, now - 60))
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, at) VALUES (?, 0, 'slime', ?)", (ids["statsann"], now - 60))
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, at) VALUES (?, 0, 'slime', ?)", (ids["statsann"], now - 40 * DAY))
sql("INSERT INTO login_attempts (username, ip, ok, reason, at) VALUES ('statsann', '198.51.100.9', 1, '', ?)", (now - 30,))
sql("INSERT INTO login_attempts (username, ip, ok, reason, at) VALUES ('statsbo', '198.51.100.9', 0, 'bad', ?)", (now - 30,))
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
check("accounts: every account, with its role and when it joined",
      {a["username"]: a["role"] for a in data.accounts(db)}.get("statsdev") == "dev"
      and all(a["created_at"] > 0 for a in data.accounts(db)))
check("characters: level and area as saved",
      data.characters(db)[ids["statsann"]][0]["level"] == 7 and data.characters(db)[ids["statsann"]][0]["area"] == "field")
check("last seen: the latest of a save, a login, a heartbeat and a kill",
      data.last_seen(db)[ids["statsann"]] >= now - 60)
check("kill totals: kills ever and the first",
      data.kill_totals(db)[ids["statsann"]] == (12, now - 3000), data.kill_totals(db).get(ids["statsann"]))
check("kills since, and logins that worked since - the failed one is not a login",
      data.kills_since(db, now - DAY) == [(ids["statsann"], "slime", now - 60)]
      and data.logins_since(db, now - DAY) == [("statsann", now - 30)], (data.kills_since(db, now - DAY),
                                                                         data.logins_since(db, now - DAY)))
# EACH SOURCE OF "SEEN" ON ITS OWN: an account known only by a save, one only
# by a login address, one only by a heartbeat, one only by a kill.
seen_db = sqlite3.connect(":memory:")
seen_db.row_factory = sqlite3.Row
seen_db.executescript(
    "CREATE TABLE saves (user_id INTEGER, slot INTEGER, updated_at INTEGER);"
    "CREATE TABLE account_ips (user_id INTEGER, ip TEXT, last_seen INTEGER);"
    "CREATE TABLE sessions (token_hash TEXT, user_id INTEGER, last_seen_at INTEGER);"
    "CREATE TABLE kill_tally (user_id INTEGER, enemy_id TEXT, last_at INTEGER);"
    "INSERT INTO saves VALUES (1, 0, 100), (1, 1, 150);"
    "INSERT INTO account_ips VALUES (2, 'a', 200), (2, 'b', 250);"
    "INSERT INTO sessions VALUES ('t', 3, 300);"
    "INSERT INTO kill_tally VALUES (4, 'slime', 400), (1, 'slime', 120);")
check("  each of them counts on its own - a save, a login, a heartbeat, a kill - and the latest wins",
      data.last_seen(seen_db) == {1: 150, 2: 250, 3: 300, 4: 400}, data.last_seen(seen_db))
bare = sqlite3.connect(":memory:")
bare.row_factory = sqlite3.Row
check("a database without the tables answers with nothing, not an error",
      data.accounts(bare) == [] and data.last_seen(bare) == {} and data.characters(bare) == {}
      and data.kill_totals(bare) == {} and data.kills_since(bare, 0) == [] and data.logins_since(bare, 0) == [])
data_src = open(os.path.join(HERE, "playerstats", "data.py"), encoding="utf-8").read()
check("data.py only reads: not one INSERT, UPDATE, DELETE, REPLACE, DROP, CREATE, ALTER or commit",
      not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|DROP|CREATE|ALTER)\b|\.commit\(", data_src))
db.close()

# =============================================================================
section("S-3  routes.py - the edge")
res = client.get("/api/server/stats?offset=-300", headers=auth(owner))
body = res.get_json() or {}
check("the owner gets the report: players, the road, who stopped, day by day, monsters, classes",
      res.status_code == 200 and set(body) >= {"players", "road", "quit", "daily", "monsters", "classes", "now", "offset"}
      and body["offset"] == -300, (res.status_code, sorted(body)))
check("  the owner and the dev are not players: Ann and Bo are",
      body["players"]["total"] == 2 and body["players"]["staff_left_out"] == 2, body.get("players"))
check("  and today Ann played, by her kill and her login",
      body["daily"][0]["played"] >= 1, body.get("daily", [None])[0])
check("the report names nobody: no username in it, so it can be shown or shared",
      not any(name in res.get_data(as_text=True).lower() for name in ("statsann", "statsbo", "statsdev", "statsowner")))
res = client.get("/api/server/stats", headers=auth(bo))
check("a player gets the same bare 404 as a route that does not exist",
      res.status_code == 404 and "owner" not in str(res.get_json()).lower(), (res.status_code, res.get_json()))
res = client.get("/api/server/stats", headers=auth(devtok))
check("a dev gets the same bare 404 as a route that does not exist",
      res.status_code == 404 and "owner" not in str(res.get_json()).lower(), (res.status_code, res.get_json()))
check("no login: 401", client.get("/api/server/stats").status_code == 401)
res = client.get("/api/server/stats?days=nonsense&offset=99999", headers=auth(owner))
check("a query it cannot read is held to the defaults: 30 days, offset +14 h",
      res.status_code == 200 and res.get_json()["road"]["days"] == 30 and res.get_json()["offset"] == 840,
      res.get_json() and (res.get_json()["road"]["days"], res.get_json()["offset"]))
res = client.get("/api/server/stats?days=7", headers=auth(owner))
check("  and days=7 asks about the last week's new players", res.get_json()["road"]["days"] == 7)
counts_before = [sql("SELECT COUNT(*) AS n FROM %s" % t)[0]["n"] for t in ("users", "saves", "kill_tally", "kill_reports")]
client.get("/api/server/stats", headers=auth(owner))
counts_after = [sql("SELECT COUNT(*) AS n FROM %s" % t)[0]["n"] for t in ("users", "saves", "kill_tally", "kill_reports")]
check("asking changes nothing", counts_before == counts_after, (counts_before, counts_after))
view = app_module.app.view_functions["read_player_stats"]
check("owner only by its decorator (listed on /api/staff/powers under the co-owner), and no password asked again",
      getattr(view, "_elusion_min_role", None) == "coowner" and not getattr(view, "_elusion_fresh_password", False))
package_src = "".join(open(os.path.join(HERE, "playerstats", name), encoding="utf-8").read()
                      for name in ("__init__.py", "routes.py", "services.py", "data.py"))
check("the package never imports app.py - it is handed what it needs (register())",
      not re.search(r"^\s*(import app\b|from app\b|import elusion_app)", package_src, re.M))
services_src = open(os.path.join(HERE, "playerstats", "services.py"), encoding="utf-8").read()
check("services.py is arithmetic: no database, no request, no clock of its own",
      "sqlite3" not in services_src and "flask" not in services_src and not re.search(r"\btime\.time\(", services_src))

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
