"""What broke in the players' games (game 0.88.0): clienterrors/.

The owner rated stability 8 of 10 - "rare live breakages, and fixes go out
fast" - and asked for the 9. A script error in a player's game used to reach
him only if the player said so. Now each game sends the errors it hits, and
the GM panel's Health tab lists them.

  CE-1  services.py: what a report must be; a bad line dropped, not the report
  CE-2  the write: counted per account and day, one row an error; the day's
        cap; a count held; pruned after DAYS_KEPT; anyone signed in, nobody
        else
  CE-3  the read: the owner's and a co-owner's alone; grouped, worst first,
        players counted and never named; the hours asked

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_clienterrors.py
"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_clienterrors_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "errowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

from clienterrors import services  # noqa: E402

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


def auth(token, build=None):
    headers = {"Authorization": "Bearer %s" % token}
    if build is not None:
        headers["X-Elusion-Build"] = str(build)
    return headers


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def report(token, errors, version="0.88.0", area="field", build=8):
    return client.post("/api/client/errors", headers=auth(token, build),
                       json={"version": version, "area": area, "errors": errors})


# =============================================================================
section("CE-1  services.py - what a report must be")
good = {"version": "0.88.0", "area": "bigfield",
        "errors": [{"where": "res://src/ui/party/partypanel.gd:312", "what": "Invalid call. Nonexistent function 'x'.",
                    "count": 3.0}]}
cleaned = services.clean_report(good)
check("a report: the version, the area, each error's where, what and count (a whole float is a count)",
      cleaned == ("0.88.0", "bigfield",
                  [("res://src/ui/party/partypanel.gd:312", "Invalid call. Nonexistent function 'x'.", 3)], 0),
      cleaned)
check("not a report: no version, a version that is not a version, errors not a list, not a dict at all",
      services.clean_report({"area": "x", "errors": []}) is None
      and services.clean_report(dict(good, version="latest")) is None
      and services.clean_report(dict(good, errors="boom")) is None
      and services.clean_report(["x"]) is None and services.clean_report(None) is None)
bad_lines = [{"where": "C:/Users/someone/x.gd:1", "what": "a", "count": 1},
             {"where": "res://src/a.gd:1", "what": "", "count": 1},
             {"where": "res://src/a.gd:1", "what": "a", "count": 0},
             {"where": "res://src/a.gd:1", "what": "a", "count": 1.5},
             {"where": "res://src/a.gd:1", "what": "a", "count": True},
             "not a dict",
             {"where": "res://src/a.gd:2", "what": "fine", "count": 1}]
cleaned = services.clean_report(dict(good, errors=bad_lines))
check("a bad line is dropped and counted, never the report: a path off the game, no message, a count "
      "of 0, a fraction, a bool, not a dict",
      cleaned is not None and cleaned[2] == [("res://src/a.gd:2", "fine", 1)] and cleaned[3] == 6, cleaned)
check("  an area that is not an area id is kept as none, not a refusal",
      services.clean_report(dict(good, area="../etc"))[1] == "")
many = [{"where": "res://src/a.gd:%d" % i, "what": "m", "count": 1} for i in range(services.ERRORS_PER_POST + 5)]
cleaned = services.clean_report(dict(good, errors=many))
check("at most ERRORS_PER_POST a report; the rest dropped and said",
      len(cleaned[2]) == services.ERRORS_PER_POST and cleaned[3] == 5, (len(cleaned[2]), cleaned[3]))
twice = [{"where": "res://src/a.gd:1", "what": "m", "count": 2}, {"where": "res://src/a.gd:1", "what": "m", "count": 5}]
check("the same error twice in one report is one, its counts added",
      services.clean_report(dict(good, errors=twice))[2] == [("res://src/a.gd:1", "m", 7)])
check("the message is one line: control characters gone, spaces folded, cut to WHAT_MOST",
      services.clean_what("a\n\tb\x00   c  ") == "a b c"
      and len(services.clean_what("x" * (services.WHAT_MOST + 50))) == services.WHAT_MOST)
check("  the engine's own word for no file is a where too",
      services.clean_report(dict(good, errors=[{"where": "<native>", "what": "m", "count": 1}]))[2][0][0] == "<native>")
check("the hours asked: 24 by default, held from 1 to DAYS_KEPT days",
      (services.hours_asked(None), services.hours_asked("x"), services.hours_asked("0"),
       services.hours_asked("100000"), services.hours_asked("6"))
      == (24, 24, 1, services.HOURS_MOST, 6))

# =============================================================================
section("CE-2  the write: per account and day, one row an error")
PLAYER = register("errplayer")
OTHER = register("errother")
THIRD = register("errthird")
OWNER = register("errowner")
WHERE = "res://src/ui/party/partypanel.gd:312"
WHAT = "Invalid get index 'detail' (on base: 'Nil')."
res = report(PLAYER, [{"where": WHERE, "what": WHAT, "count": 3}])
check("a game sends what it hit and is told it was kept", res.status_code == 200
      and res.get_json() == {"kept": 1, "dropped": 0}, res.get_json())
res = report(PLAYER, [{"where": WHERE, "what": WHAT, "count": 4}], area="town")
stored = sql("SELECT times, area, build, version FROM client_errors WHERE where_at = ?", (WHERE,))
check("  sent again, the same row counts on: one row, 7 times, the area it was last seen in, the build",
      stored == [(7, "town", 8, "0.88.0")], stored)
report(PLAYER, [{"where": WHERE, "what": WHAT, "count": 1}], version="0.88.1")
check("  another version of the game is another row (a fix that did not fix shows apart)",
      sql("SELECT COUNT(*) FROM client_errors WHERE where_at = ?", (WHERE,))[0][0] == 2)
check("not a report is a 400, and no login a 401",
      client.post("/api/client/errors", headers=auth(PLAYER), json={"errors": []}).status_code == 400
      and client.post("/api/client/errors", json={"version": "0.88.0", "errors": []}).status_code == 401)
flood = [{"where": "res://src/flood.gd:%d" % i, "what": "loop", "count": 1} for i in range(services.ERRORS_PER_POST)]
for start in range(0, services.ROWS_PER_DAY + 40, services.ERRORS_PER_POST):
    report(OTHER, [{"where": "res://src/flood.gd:%d" % (start + i), "what": "loop", "count": 1}
                   for i in range(services.ERRORS_PER_POST)])
rows_other = sql("SELECT COUNT(*) FROM client_errors WHERE user_id = (SELECT id FROM users WHERE username = 'errother')")[0][0]
check("one account keeps at most ROWS_PER_DAY distinct errors a day - a broken build cannot fill the disk",
      rows_other == services.ROWS_PER_DAY, rows_other)
res = report(OTHER, [{"where": "res://src/flood.gd:0", "what": "loop", "count": 5},
                     {"where": "res://src/flood.gd:99999", "what": "new", "count": 1}])
check("  past the cap an error already kept still counts on; a new one is dropped and said",
      res.get_json() == {"kept": 1, "dropped": 1}
      and sql("SELECT times FROM client_errors WHERE where_at = 'res://src/flood.gd:0'")[0][0] == 6, res.get_json())
report(THIRD, [{"where": WHERE, "what": WHAT, "count": services.REPORT_TIMES_MOST * 50}])
for _ in range(12):
    report(THIRD, [{"where": WHERE, "what": WHAT, "count": services.REPORT_TIMES_MOST}])
held = sql("SELECT times FROM client_errors WHERE user_id = (SELECT id FROM users WHERE username = 'errthird')")
check("a report adds at most REPORT_TIMES_MOST; a row's day at most TIMES_MOST",
      held == [(services.TIMES_MOST,)], held)
old_day = int(time.time()) // services.DAY - services.DAYS_KEPT - 1
sql("INSERT INTO client_errors (day, user_id, version, where_at, what, times, first_at, last_at) "
    "VALUES (?, 1, '0.80.0', 'res://old.gd:1', 'old', 1, 0, 0)", (old_day,))
report(PLAYER, [{"where": WHERE, "what": WHAT, "count": 1}])
check("rows older than DAYS_KEPT days go on the next write",
      sql("SELECT COUNT(*) FROM client_errors WHERE where_at = 'res://old.gd:1'")[0][0] == 0)

# =============================================================================
section("CE-3  the read: the owner's, grouped, worst first, no names")
check("a player gets the bare 404, no login the 401",
      client.get("/api/server/client-errors", headers=auth(PLAYER)).status_code == 404
      and client.get("/api/server/client-errors").status_code == 401)
res = client.get("/api/server/client-errors", headers=auth(OWNER))
body = res.get_json()
first = body["errors"][0] if body and body.get("errors") else {}
check("the owner reads it: the error two players hit first, with players, times, versions newest first, "
      "the areas each was last seen in",
      res.status_code == 200 and first.get("where") == WHERE and first.get("what") == WHAT
      and first.get("players") == 2 and first.get("versions") == ["0.88.1", "0.88.0"]
      and first.get("areas") == ["field"] and first.get("times") == 9 + services.TIMES_MOST, first)
check("  at most SHOWN errors, the rest counted in more; who hit any, and how many times in all",
      len(body["errors"]) == services.SHOWN and body["more"] > 0 and body["players"] == 3
      and body["times"] > services.TIMES_MOST, {k: body[k] for k in ("more", "players", "times")})
text = json.dumps(body)
check("  and no name in it anywhere - not a player's, not the owner's",
      not any(name in text for name in ("errplayer", "errother", "errthird", "errowner")))
sql("UPDATE client_errors SET last_at = last_at - 7200 WHERE where_at = ?", (WHERE,))
recent = client.get("/api/server/client-errors?hours=1", headers=auth(OWNER)).get_json()
check("the hours asked: an error last seen two hours ago is not in the last hour",
      recent["hours"] == 1 and all(e["where"] != WHERE for e in recent["errors"]), recent["hours"])

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
