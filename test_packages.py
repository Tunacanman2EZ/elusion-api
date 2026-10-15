"""The feature packages stand on their own (game 0.50.0).

playerstats/, events/ and insights/ are built the BE105 way: each has a
register(app, ...) that is HANDED the app, its login checks and its database,
and never imports app.py. The source of each is checked for that elsewhere
(test_stats.py); this RUNS them without it - the "application factory"
lesson's "two app instances from the same code", where it pays off here:

  P-1  nothing of app.py comes along: each package's source never imports
       it, and this file never loads it
  P-2  all three registered on a bare Flask app - stand-in login checks and a
       throwaway database - answer as they do on the real server: the owner
       gets in, a player gets the same bare 404 as no route, nobody without a
       login gets a 401
  P-3  two bare apps from the same code, each with its own database: an event
       set on one is on that one alone, and only that one's announcer and
       staff log hear of it

Run: python test_packages.py
"""
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from flask import Flask, g, request  # noqa: E402

import events  # noqa: E402
import insights  # noqa: E402
import playerstats  # noqa: E402

PACKAGES = ("playerstats", "events", "insights")
LANDS = {1: "dark", 2: "light", 3: "ice", 4: "wind", 5: "earth", 6: "fire", 7: "water"}

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


# =============================================================================
# A BARE APP: what app.py hands a package, stood in for.
# =============================================================================
USERS = {"boss": {"id": 1, "username": "boss", "role": "player"},
         "ann": {"id": 2, "username": "ann", "role": "player"},
         "bo": {"id": 3, "username": "bo", "role": "player"}}
OWNER = "boss"


def require_auth(view):
    """Who you are, from a header - the stand-in for a bearer token."""
    def wrapped(*args, **kwargs):
        user = USERS.get(request.headers.get("X-Test-User", ""))
        if user is None:
            return {"error": "Unauthorized", "message": "Missing, invalid or expired token."}, 401
        g.user = user
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def require_owner(view):
    """The owner only - the same bare 404 as no route to anybody else."""
    def wrapped(*args, **kwargs):
        if g.user["username"] != OWNER:
            return {"error": "Not Found", "message": "The requested URL was not found on the server."}, 404
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def require_fresh_password(view):
    return view


def bare_app(name):
    """A Flask app with nothing on it, a database of its own, and all three
    packages registered. Hands back (app, what was announced, what was logged,
    the database's path)."""
    path = os.path.join(tempfile.gettempdir(), "elusion_packages_%s.db" % name)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT, created_at INTEGER);"
        "CREATE TABLE saves (user_id INTEGER, slot INTEGER, class_id TEXT, level INTEGER, area TEXT, updated_at INTEGER);")
    now = int(time.time())
    for user in USERS.values():
        conn.execute("INSERT INTO users VALUES (?, ?, ?, ?)", (user["id"], user["username"], user["role"], now - 3600))
    conn.execute("INSERT INTO saves VALUES (2, 0, 'warrior', 4, 'field', ?)", (now,))
    conn.commit()
    conn.close()

    app = Flask("bare_" + name)
    said = []
    logged = []

    def get_db():
        if "db" not in g:
            g.db = sqlite3.connect(path)
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    playerstats.register(app, require_auth=require_auth, require_owner=require_owner, get_db=get_db,
                         left_out_names=lambda: (OWNER,))
    events.register(app, require_auth=require_auth, require_owner=require_owner,
                    require_fresh_password=require_fresh_password, get_db=get_db,
                    announce=lambda body, by="": said.append(body), log_action=logged.append,
                    clean_text=lambda raw, limit: str(raw).strip()[:limit], lands=LANDS,
                    left_out_names=lambda: (OWNER,))
    insights.register(app, require_auth=require_auth, require_owner=require_owner, get_db=get_db, lands=LANDS,
                      element_of=lambda _enemy: 0, left_out_names=lambda: (OWNER,),
                      next_stir=lambda _db: {"element": 3, "name": "ice", "at": 0, "reason": "Ice: handed in."})
    return app, said, logged, path


def who(name):
    return {"X-Test-User": name}


# =============================================================================
section("P-1  nothing of app.py comes along")
for name in PACKAGES:
    source = ""
    for root, _dirs, files in os.walk(os.path.join(HERE, name)):
        for file in files:
            if file.endswith(".py"):
                source += open(os.path.join(root, file), encoding="utf-8").read()
    check("%s/ never imports app.py" % name,
          source != "" and not re.search(r"^\s*(import app\b|from app\b|import elusion_app|from elusion_app)", source, re.M))
app_a, said_a, logged_a, path_a = bare_app("a")
app_b, said_b, logged_b, path_b = bare_app("b")
check("and this file never loaded it: no app.py module in the process",
      not any(m in sys.modules for m in ("app", "elusion_app", "wsgi")), [m for m in ("app", "elusion_app", "wsgi") if m in sys.modules])
rules = sorted(r.rule for r in app_a.url_map.iter_rules() if r.rule.startswith("/api/"))
check("a bare app has the packages' routes and nothing else",
      rules == sorted(["/api/server/stats", "/api/world/events/active", "/api/world/events/schedule", "/api/world/events",
                       "/api/world/events/cancel", "/api/world/events/preview", "/api/world/events/report",
                       "/api/server/insights"]), rules)

# =============================================================================
section("P-2  on a bare app, each answers as on the real server")
client_a = app_a.test_client()
res = client_a.get("/api/server/stats", headers=who("boss"))
body = res.get_json() or {}
check("playerstats: the owner gets the report - two players, the owner left out",
      res.status_code == 200 and body["players"]["total"] == 2 and body["players"]["staff_left_out"] == 1, body.get("players"))
res = client_a.get("/api/server/insights", headers=who("boss"))
body = res.get_json() or {}
check("insights: every land handed in, and the next stir from the callable handed in",
      res.status_code == 200 and [r["element"] for r in body["lands"]] == sorted(LANDS)
      and body["next"] == {"element": 3, "name": "ice", "at": 0, "reason": "Ice: handed in."}, body.get("next"))
res = client_a.get("/api/world/events/active", headers=who("ann"))
check("events: any player reads what is on", res.status_code == 200 and res.get_json()["events"] == [], res.get_json())
hidden = [client_a.get(path, headers=who("ann")).status_code
          for path in ("/api/server/stats", "/api/server/insights", "/api/world/events/schedule", "/api/world/events/report")]
check("a player gets the same bare 404 from every owner's route", hidden == [404, 404, 404, 404], hidden)
check("  and nobody without a login gets in anywhere",
      [client_a.get(path).status_code for path in ("/api/server/stats", "/api/world/events/active", "/api/server/insights")]
      == [401, 401, 401])

# =============================================================================
section("P-3  two apps from the same code, each its own")
now = int(time.time())
res = client_a.post("/api/world/events", headers=who("boss"), json={"kind": "xp", "amount": 2, "starts_at": now, "hours": 1})
check("an event set on the first app", res.status_code == 201, res.get_json())
on_a = client_a.get("/api/world/events/active", headers=who("ann")).get_json()["events"]
on_b = app_b.test_client().get("/api/world/events/active", headers=who("ann")).get_json()["events"]
check("  is on there, and not on the second", [e["name"] for e in on_a] == ["Double XP"] and on_b == [], (on_a, on_b))
check("  its staff log heard of it, the second's did not", len(logged_a) == 1 and "Double XP" in logged_a[0] and logged_b == [],
      (logged_a, logged_b))
client_a.post("/api/world/events", headers=who("boss"), json={"kind": "loot", "amount": 1.5, "starts_at": now + 86400 + 30,
                                                                 "hours": 2})
check("  and what it says - begun, when first asked; one set ahead, coming - is said on its own app only",
      len(said_a) == 2 and said_a[0].startswith("Double XP has begun, for ")
      and said_a[0].endswith(": 2x XP from every monster.")
      and said_a[1] == "Coming up: 1.5x Loot, in 1 day, for 2 hours - Loot bags drop 1.5x as often from every monster."
      and said_b == [], (said_a, said_b))
for path in (path_a, path_b):
    if os.path.exists(path):
        os.remove(path)

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
