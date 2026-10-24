"""The server's health (game 0.78.0): the GM panel's Health tab.

The owner, of four next steps after the 128-player swarm - send each game
only the players near it, watch the live server, test on the real machine,
let the server referee every kill: "work on this or if you have a even better
idea lets hear it". Two of them were numbers only SSH could read, and the
first needed one nobody had. GET /api/server/health puts them on the panel.
Built like economywatch/, so each layer is held on its own:

  H-1  services.py, the rules: the last minute, a day and a week; the month
       the week's data adds up to against the allowance; when one area needs
       sending only what is near; the tick; whether the kill receipts are
       ready; the slowest routes; the machine
  H-2  data.py, the SQL: reads only, nothing from a database without the
       table, the disk
  H-3  routes.py, the edge: the owner's and a co-owner's, nobody else's; what
       presence.py wrote, read back; the receipts put to the real books,
       killwatch.py's own count, kept five minutes unless asked fresh

presence.py's half - the row a minute - is test_presence.py P-13.

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_serverhealth.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_serverhealth_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "healthowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

import killwatch  # noqa: E402
from serverhealth import data, services  # noqa: E402

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


def sql(statement, args=(), many=None):
    conn = sqlite3.connect(DB_PATH)
    try:
        if many is not None:
            conn.executemany(statement, many)
            conn.commit()
            return []
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


DAY = services.DAY
GIB = services.GIB


def row(at, seconds=60.0, players=0, crowd=(), ticks=600, late=0, frames=0, sent=0, net=None, cpu=1.0,
        load1=0.2, mem=400, connections=None):
    return {"at": at, "seconds": seconds, "connections": players if connections is None else connections,
            "players": players, "areas": len(crowd), "crowd": json.dumps([list(pair) for pair in crowd]),
            "ticks": ticks, "late_ticks": late, "frames_out": frames, "bytes_out": sent, "net_out": net,
            "cpu": cpu, "load1": load1, "mem_free_mb": mem}


# =============================================================================
section("H-1  services.py - the rules")
NOW = 2_000_000_000
last = services.last_minute(row(NOW - 30, players=5, crowd=[("field", 3), ("town", 2)], ticks=600, sent=60000,
                                net=120000), NOW)
check("the last minute: how old, whether presence still reports, who is where",
      last["age_seconds"] == 30 and last["reporting"] and last["players"] == 5
      and last["crowd"] == [["field", 3], ["town", 2]], last)
check("  and its rates a second: ticks, presence's bytes, the machine's network out",
      (last["ticks_per_second"], last["bytes_per_second"], last["net_per_second"]) == (10.0, 1000.0, 2000.0), last)
check("  a row older than REPORTING_SECONDS (three minutes) is presence gone quiet",
      not services.last_minute(row(NOW - services.REPORTING_SECONDS - 1), NOW)["reporting"]
      and services.last_minute(row(NOW - services.REPORTING_SECONDS), NOW)["reporting"])
check("  none before the first row; a machine whose network was not read has no network rate",
      services.last_minute(None, NOW) is None
      and services.last_minute(row(NOW, net=None), NOW)["net_per_second"] is None)
check("  a crowd that cannot be read is no crowd, not an error",
      services.crowd_of({"crowd": "{not json"}) == [] and services.crowd_of({"crowd": None}) == []
      and services.crowd_of({"crowd": '[["a", 2], "x", ["b"], ["c", "3"]]'}) == [["a", 2]])

rows = [row(NOW - 180, players=4, crowd=[("field", 4)], sent=6000, net=12000, late=0, load1=0.5, mem=500, cpu=3.0),
        row(NOW - 120, players=9, crowd=[("town", 6), ("field", 3)], sent=12000, net=None, late=1, load1=1.5,
            mem=300, cpu=9.0),
        row(NOW - 60, players=7, crowd=[("field", 7)], sent=0, net=36000, late=2, load1=0.7, mem=450, cpu=4.0)]
week = services.window(rows)
check("a window: the minutes it covers, the most players at any minute, the biggest crowd and where",
      week["samples"] == 3 and week["seconds"] == 180.0 and week["peak_players"] == 9
      and (week["peak_area"], week["peak_area_players"]) == ("field", 7), week)
check("  presence's bytes over every minute; the machine's network over the minutes it was read",
      week["bytes_out"] == 18000 and week["bytes_per_second"] == 100.0
      and week["net_out"] == 48000 and week["net_per_second"] == 400.0, week)
check("  the late ticks summed; the busiest core and load, the least memory free",
      week["late_ticks"] == 3 and week["peak_cpu"] == 9.0 and week["peak_load"] == 1.5
      and week["least_mem_free_mb"] == 300 and week["ticks_per_second"] == 10.0, week)
empty = services.window([])
check("  an empty window is zeros and Nones, never a division by nothing",
      empty["samples"] == 0 and empty["bytes_per_second"] == 0.0 and empty["net_per_second"] is None
      and empty["peak_load"] is None, empty)

held = 1000 * GIB / services.MONTH_SECONDS          # exactly the allowance, a second
month = services.month({"samples": 10, "net_per_second": held * 1.5, "bytes_per_second": 1.0})
check("the month: the week's rate held thirty days, from the machine's own network where it was read",
      month["source"] == "machine" and month["gib"] == 1500.0 and month["share"] == 1.5
      and month["allowance_gib"] == services.TRANSFER_ALLOWANCE_GIB == 1000, month)
check("  and what the GiB past the allowance would cost, at $0.01 a GiB",
      month["over_cost"] == 5.0 and services.OVERAGE_PER_GIB == 0.01, month)
by_presence = services.month({"samples": 10, "net_per_second": None, "bytes_per_second": held / 4})
check("  presence's own frames where the machine could not be read; nothing over, nothing owed",
      by_presence["source"] == "presence" and by_presence["gib"] == 250.0 and by_presence["over_cost"] == 0.0,
      by_presence)
check("  and no month before a minute to go by", services.month(empty) is None and services.month(None) is None)

def crowd(peak, share):
    return services.crowd_verdict({"samples": 1, "peak_area_players": peak}, {"share": share})["verdict"]

check("sending only what is near: fine below %d in one area and half the allowance" % services.CROWD_WATCH,
      crowd(services.CROWD_WATCH - 1, 0.49) == "fine")
check("  watch from %d in one area, or half the allowance" % services.CROWD_WATCH,
      crowd(services.CROWD_WATCH, 0.0) == "watch" and crowd(0, 0.5) == "watch")
check("  now from %d in one area, or the whole allowance" % services.CROWD_NOW,
      crowd(services.CROWD_NOW, 0.0) == "now" and crowd(0, 1.0) == "now"
      and services.crowd_verdict({"samples": 1, "peak_area_players": 0}, {"share": 1.2})["by"] == "data"
      and services.crowd_verdict({"samples": 1, "peak_area_players": 200}, {"share": 0.1})["by"] == "crowd")
check("  unknown with no minute to go by", services.crowd_verdict(empty, None)["verdict"] == "unknown")

tick = services.tick_verdict(last, {"late_ticks": 4})
check("the tick: steady at ten a second with no late beat; the day's late ones counted",
      tick == {"verdict": "steady", "per_second": 10.0, "late_today": 4}, tick)
check("  late below %.1f a second, or with a late beat in the minute; unknown with none" % services.TICK_STEADY,
      services.tick_verdict(dict(last, ticks_per_second=9.2), {})["verdict"] == "late"
      and services.tick_verdict(dict(last, late_ticks=1), {})["verdict"] == "late"
      and services.tick_verdict(None, {})["verdict"] == "unknown")


def backing(paid, backed, never=None, books_days=8.0, short=0, not_due=0):
    never = never or {}
    return {"paid": paid, "backed": backed, "short": short, "not_due": not_due,
            "never": sum(never.values()), "since": int(NOW - books_days * DAY), "never_by_enemy": never}


def verdict(b, on=False, min_build=7):
    return services.receipts(b, on, min_build, 7, 7, NOW)


ready = verdict(backing(500, 495, {"slime": 3}, short=2))
check("the receipts: ready - a week of books, %d kills or more, %d%% backed, no monster unseen often"
      % (services.RECEIPTS_MIN_KILLS, round(services.RECEIPTS_READY_SHARE * 100)),
      ready["verdict"] == "ready" and ready["share"] == 0.99 and ready["never_by_enemy"] == [["slime", 3]]
      and not ready["min_build_low"], ready)
check("  no books yet; books younger than the week (an hour's slack); too few kills to judge by",
      verdict(None)["verdict"] == "no_books" and verdict(backing(500, 500, books_days=5))["verdict"] == "young"
      and verdict(backing(500, 500, books_days=6.97))["verdict"] == "ready"
      and verdict(backing(services.RECEIPTS_MIN_KILLS - 1, services.RECEIPTS_MIN_KILLS - 1))["verdict"] == "few")
check("  short below the share; a monster the books never saw too often, however good the rest",
      verdict(backing(500, 489))["verdict"] == "short"
      and verdict(backing(1000, 990, {"bat": 11, "slime": 4}))["verdict"] == "monster"
      and verdict(backing(1000, 990, {"bat": 10}))["verdict"] == "ready"
      and verdict(backing(200, 200, {"bat": 3}))["verdict"] == "ready"
      and verdict(backing(200, 198, {"bat": 4}))["verdict"] == "monster")
on = verdict(backing(500, 100), on=True, min_build=0)
check("  on is on, whatever the week; and a minimum build below the receipts' own is said",
      on["verdict"] == "on" and on["min_build_low"] and on["receipt_build"] == 7
      and verdict(None, on=True)["verdict"] == "on", on)
check("  the never-seen monsters named, most first, at most %d" % services.RECEIPTS_NAMED,
      verdict(backing(1000, 990, {"a": 1, "b": 5, "c": 2, "d": 5}))["never_by_enemy"]
      == [["b", 5], ["d", 5], ["c", 2]])

metrics = {"uptime_seconds": 3600, "total_requests": 1200, "endpoints": {
    "POST combat_kill": {"count": 500, "error_rate": 0.002, "p99_ms": 12.5},
    "PUT save": {"count": 400, "error_rate": 0.0, "p99_ms": 9.0},
    "POST login": {"count": 20, "error_rate": 0.05, "p99_ms": 110.0},
    "GET rare": {"count": 3, "error_rate": 0.0, "p99_ms": 900.0},
    "GET a": {"count": 50, "p99_ms": 1.0}, "GET b": {"count": 50, "p99_ms": 2.0},
    "GET c": {"count": 50, "p99_ms": 3.0}}}
routes = services.routes(metrics)
check("the routes: the slowest by p99, a route asked fewer than %d times left out, at most %d"
      % (services.API_MIN_COUNT, services.API_SLOWEST),
      [r["route"] for r in routes["slowest"]] == ["POST login", "POST combat_kill", "PUT save", "GET c", "GET b"]
      and routes["requests"] == 1200 and routes["uptime_seconds"] == 3600, routes)
check("  and the server errors counted from each route's rate", routes["errors"] == 2, routes)
check("  nothing to say is an empty list", services.routes(None)["slowest"] == [])
machine = services.machine({"load1": 0.4, "mem_free_mb": 512}, 1, 10 * GIB, 25 * GIB, 3 * 1024 * 1024)
check("the machine: cores, the last minute's load and memory, the disk's free and total, the database's size",
      machine == {"cores": 1, "load1": 0.4, "mem_free_mb": 512, "disk_free_gb": 10.0, "disk_total_gb": 25.0,
                  "db_mb": 3.0}, machine)

# =============================================================================
section("H-2  data.py - reads only")
plain = os.path.join(testdir.PATH, "no_health_table.db")
con = sqlite3.connect(plain)
check("a database from before the table: no rows and no newest, never an error",
      data.health_rows(con, 0) == [] and data.latest_health(con) is None)
con.close()
free, total, size = data.disk(DB_PATH)
check("the disk the database is on, and the database's size with its WAL",
      free is not None and total is not None and 0 < free <= total and size is not None and size > 0,
      (free, total, size))
check("  and Nones for the size of a database that is not there",
      data.disk(os.path.join(testdir.PATH, "nowhere", "x.db"))[2] is None)
source = "".join(open(os.path.join(HERE, "serverhealth", name), encoding="utf-8").read()
                 for name in ("data.py", "services.py", "routes.py", "__init__.py"))
check("  the package writes nothing, and imports neither app.py nor killwatch.py",
      "INSERT" not in source and "UPDATE " not in source and "DELETE" not in source
      and "import app" not in source and "import killwatch" not in source)

# =============================================================================
section("H-3  routes.py - the edge")
owner = register("healthowner")
player = register("healthplayer")
check("no login is a 401; a player gets the same bare 404 as no route",
      client.get("/api/server/health").status_code == 401
      and client.get("/api/server/health", headers=auth(player)).status_code == 404)
res = client.get("/api/server/health", headers=auth(owner))
body = res.get_json() or {}
check("the owner, before presence.py has written a minute: every part there, nothing reporting",
      res.status_code == 200 and body["presence"]["reporting"] is False and body["presence"]["last"] is None
      and body["month"] is None and body["crowd"]["verdict"] == "unknown" and body["tick"]["verdict"] == "unknown"
      and set(body) == {"now", "presence", "month", "crowd", "tick", "machine", "routes", "receipts"}, body)
check("  the routes are this worker's /api/metrics: the health ask itself among them soon enough",
      isinstance(body["routes"]["slowest"], list) and body["routes"]["requests"] >= 1, body.get("routes"))
check("  the machine's disk, read here", body["machine"]["disk_free_gb"] is not None
      and body["machine"]["cores"] == os.cpu_count(), body.get("machine"))

now = int(time.time())
written = []
for minute in range(60 * 24 * 7, 0, -30):        # a week, a row every half hour
    written.append(row(now - minute * 60, players=3, crowd=[("field", 3)], sent=6000, net=60000))
written.append(row(now - 3 * DAY, players=40, crowd=[("field", 34), ("town", 6)], sent=600000, net=900000, late=3))
written.append(row(now - 20, players=4, crowd=[("town", 4)], sent=12000, net=120000, load1=0.3, mem=444))
cols = ("at", "seconds", "connections", "players", "areas", "crowd", "ticks", "late_ticks", "frames_out",
        "bytes_out", "net_out", "cpu", "load1", "mem_free_mb")
sql("INSERT INTO presence_health (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
    many=[[r[c] for c in cols] for r in written])
body = client.get("/api/server/health", headers=auth(owner)).get_json()
pres = body["presence"]
check("what presence.py wrote, read back: the last minute, reporting",
      pres["reporting"] and pres["last"]["players"] == 4 and pres["last"]["crowd"] == [["town", 4]]
      and pres["last"]["net_per_second"] == 2000.0, pres["last"])
check("  the week: the biggest crowd three days ago, where it was; the day: not that one",
      pres["week"]["peak_area"] == "field" and pres["week"]["peak_area_players"] == 34
      and pres["day"]["peak_area_players"] == 4 and pres["day"]["samples"] < pres["week"]["samples"],
      (pres["week"]["peak_area_players"], pres["day"]["peak_area_players"]))
check("  so one area is worth watching (%d or more in it), by the crowd" % services.CROWD_WATCH,
      body["crowd"]["verdict"] == "watch" and body["crowd"]["by"] == "crowd", body["crowd"])
check("  the month from the machine's own network; the tick; the machine's load and memory from the last minute",
      body["month"]["source"] == "machine" and body["month"]["gib"] > 0 and body["tick"]["verdict"] == "steady"
      and body["machine"]["load1"] == 0.3 and body["machine"]["mem_free_mb"] == 444, (body["month"], body["tick"]))
sql("UPDATE presence_health SET at = at - 600")
check("  and presence gone quiet - its newest minute ten minutes old - is said",
      client.get("/api/server/health", headers=auth(owner)).get_json()["presence"]["reporting"] is False)

# THE RECEIPTS, PUT TO THE REAL BOOKS.
uid = sql("SELECT id FROM users WHERE username = 'healthplayer'")[0][0]
receipts = client.get("/api/server/health?fresh=1", headers=auth(owner)).get_json()["receipts"]
check("the receipts with no books yet: no_books, the switch off, the week killwatch measures",
      receipts["verdict"] == "no_books" and receipts["on"] is False and receipts["days"] == killwatch.RECEIPTS_DAYS
      and receipts["receipt_build"] == 7, receipts)
first = now - 8 * DAY
sql("INSERT INTO combat_kills (at, area, enemy_id, user_id, slot, verdict) VALUES (?, 'field', 'slime', ?, 0, 'agreed')",
    (first, uid))
kills, books = [], []
for i in range(210):
    at = now - 6 * DAY + i * 60
    books.append((at - 1, "field", "slime", uid, 0, "agreed"))
    kills.append((uid, 0, "slime", 5, at))
sql("INSERT INTO combat_kills (at, area, enemy_id, user_id, slot, verdict) VALUES (?, ?, ?, ?, ?, ?)", many=books)
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, level_at, at) VALUES (?, ?, ?, ?, ?)", many=kills)
receipts = client.get("/api/server/health", headers=auth(owner)).get_json()["receipts"]
check("  asked again within five minutes: the week as it was read, not read again",
      receipts["verdict"] == "no_books", receipts)
receipts = client.get("/api/server/health?fresh=1", headers=auth(owner)).get_json()["receipts"]
direct = killwatch.receipts_backing(sqlite3.connect(DB_PATH), days=killwatch.RECEIPTS_DAYS)
check("  asked fresh: 210 kills paid, every one backed - ready; killwatch.py's own count, to the kill",
      receipts["verdict"] == "ready" and receipts["paid"] == 210 == direct["paid"]
      and receipts["backed"] == 210 == direct["backed"] and receipts["share"] == 1.0, (receipts, direct))
check("  and the minimum build still below the receipts' is said, ready or not",
      receipts["min_build_low"] is True and receipts["min_client_build"] == 0, receipts)
sql("INSERT INTO kill_reports (user_id, slot, enemy_id, level_at, at) VALUES (?, ?, ?, ?, ?)",
    many=[(uid, 0, "bat", 5, now - 3600 + i) for i in range(10)])
app_module._server_health_cache_clear()
receipts = client.get("/api/server/health", headers=auth(owner)).get_json()["receipts"]
check("  ten kills of a monster the books never saw: short of %d%%, and that monster named"
      % round(services.RECEIPTS_READY_SHARE * 100),
      receipts["verdict"] == "short" and receipts["never"] == 10 and receipts["never_by_enemy"] == [["bat", 10]],
      receipts)
sql("INSERT OR REPLACE INTO server_settings (key, value, updated_by, updated_at) VALUES (?, ?, 'test', ?)",
    (app_module.RECEIPTS_KEY, json.dumps({"on": True}), now))
receipts = client.get("/api/server/health", headers=auth(owner)).get_json()["receipts"]
check("  the switch on: on, read from the switch itself",
      receipts["on"] is True and receipts["verdict"] == "on", receipts)

broken = []
real = app_module.killwatch.receipts_backing
app_module.killwatch.receipts_backing = lambda *a, **k: broken.append(1) or (_ for _ in ()).throw(RuntimeError("x"))
try:
    res = client.get("/api/server/health?fresh=1", headers=auth(owner))
finally:
    app_module.killwatch.receipts_backing = real
check("a week that cannot be read is no books, never a 500 - and the rest of the tab is still there",
      res.status_code == 200 and broken == [1] and res.get_json()["receipts"]["paid"] == 0
      and res.get_json()["presence"]["week"]["samples"] > 0, res.status_code)

routes_lock = open(os.path.join(HERE, "routes.lock"), encoding="utf-8").read()
check("the route in routes.lock: GET, an owner's and a co-owner's, no password asked again",
      "GET /api/server/health  read_server_health  auth  rank=coowner  fresh=-  lock=yes  doc=yes" in routes_lock)

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
