"""The feature packages stand on their own (game 0.50.0).

Every feature package (ARCHITECTURE.md) - playerstats/, events/, insights/,
and killrecord/, friends/, gathering/, loot/, guilds/ and parties/, taken out of app.py - is built the BE105 way: each has a
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
  P-4  EVERY PACKAGE IS HERE: the list is the code map's (a folder app.py
       imports, with a routes.py), not one kept by hand - a package added
       without a bare-app run of its own fails this file

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

import codemap  # noqa: E402
import events  # noqa: E402
import friends  # noqa: E402
import gathering  # noqa: E402
import guilds  # noqa: E402
import parties  # noqa: E402
import loot  # noqa: E402
import insights  # noqa: E402
import killrecord  # noqa: E402
import playerstats  # noqa: E402

# Found, not listed: what app.py imports that has a routes.py (codemap.py).
PACKAGES = tuple(name for name, _rows, _layers in codemap.feature_packages())
# The ones this file registers on its bare apps, below.
RUN_HERE = ("playerstats", "events", "insights", "killrecord", "friends", "gathering", "loot", "guilds", "parties")
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
        "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, role TEXT, created_at INTEGER, name_hue INTEGER);"
        "CREATE TABLE saves (user_id INTEGER, slot INTEGER, class_id TEXT, level INTEGER, area TEXT, updated_at INTEGER,"
        " last_cast_at INTEGER NOT NULL DEFAULT 0, cast_tokens REAL NOT NULL DEFAULT 6.0,"
        " gold INTEGER NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT '', hp INTEGER NOT NULL DEFAULT 0,"
        " max_hp INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE parties (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER);"
        "CREATE TABLE party_members (user_id INTEGER PRIMARY KEY, party_id INTEGER, is_leader INTEGER DEFAULT 0,"
        " joined_at INTEGER);"
        "CREATE TABLE party_invites (user_id INTEGER, from_id INTEGER, created_at INTEGER, PRIMARY KEY (user_id, from_id));"
        "CREATE TABLE sessions (user_id INTEGER, last_seen_at INTEGER);"
        "CREATE TABLE guilds (id INTEGER PRIMARY KEY, name TEXT, folded TEXT UNIQUE, founded_by TEXT, created_at INTEGER);"
        "CREATE TABLE guild_members (user_id INTEGER PRIMARY KEY, guild_id INTEGER, rank TEXT, joined_at INTEGER);"
        "CREATE TABLE guild_invites (guild_id INTEGER, user_id INTEGER, invited_by TEXT, created_at INTEGER,"
        " UNIQUE (guild_id, user_id));"
        "CREATE TABLE guild_events (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, at INTEGER, kind TEXT,"
        " actor TEXT, target TEXT, detail TEXT);"
        "CREATE TABLE carry_items (user_id INTEGER, slot INTEGER, position INTEGER, item_id TEXT, quantity INTEGER);"
        "CREATE TABLE skills (user_id INTEGER, slot INTEGER, skill_id TEXT, level INTEGER, xp INTEGER);"
        "CREATE TABLE loot_bags (bag_id TEXT PRIMARY KEY, user_id INTEGER, slot INTEGER, enemy_id TEXT, created_at INTEGER);"
        "CREATE TABLE loot_bag_items (bag_id TEXT, position INTEGER, item_id TEXT, quantity INTEGER);"
        "CREATE TABLE kill_tally (user_id INTEGER, slot INTEGER, enemy_id TEXT, kills INTEGER, first_at INTEGER,"
        " last_at INTEGER);"
        "CREATE TABLE friends (requester_id INTEGER, addressee_id INTEGER, state TEXT DEFAULT 'pending',"
        " created_at INTEGER, decided_at INTEGER);")
    now = int(time.time())
    for user in USERS.values():
        conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, NULL)", (user["id"], user["username"], user["role"], now - 3600))
    conn.execute("INSERT INTO saves (user_id, slot, class_id, level, area, updated_at) VALUES (2, 0, 'warrior', 4, 'field', ?)",
                 (now,))
    for user_id, slot, enemy, kills in ((2, 0, "slime", 5), (2, 1, "slime", 2), (3, 0, "slime", 6), (2, 0, "bat", 1)):
        conn.execute("INSERT INTO kill_tally VALUES (?, ?, ?, ?, ?, ?)", (user_id, slot, enemy, kills, now - 600, now))
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
    killrecord.register(app, require_auth=require_auth, get_db=get_db,
                        parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None,
                        max_slot=3, since=lambda: 1234)

    def find_user(name):
        return next((u for u in USERS.values() if u["username"].casefold() == name.casefold()), None)

    friends.register(app, require_auth=require_auth, get_db=get_db, find_user=find_user,
                     bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                     # bo ignores ann.
                     ignores=lambda _db, a, b: (a, b) == (3, 2),
                     presence=lambda _db, ids: {2: int(time.time())} if 2 in ids else {},
                     person=lambda row, seen, now: {"username": row["username"], "online": 2 in seen and row["id"] == 2},
                     limits=lambda: (100, 1))

    # gathering/: the grants are app.py's, so here they are four-cell stand-ins
    # over this database - writing without committing, as app.py's do, so a
    # refusal after a take leaves nothing behind.
    def carried(user_id, slot):
        return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in get_db().execute(
            "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))}

    def take(user_id, slot, item_id, quantity):
        cells = [(p, q) for p, (i, q) in sorted(carried(user_id, slot).items()) if i == item_id]
        if sum(q for _p, q in cells) < quantity:
            return False
        for position, have in cells:
            gone = min(have, quantity)
            quantity -= gone
            get_db().execute("UPDATE carry_items SET quantity = quantity - ? WHERE user_id = ? AND slot = ? AND position = ?",
                             (gone, user_id, slot, position))
        get_db().execute("DELETE FROM carry_items WHERE quantity <= 0")
        return True

    def add(user_id, slot, item_id, quantity):
        free = [p for p in range(GATHER_CELLS) if p not in carried(user_id, slot)]
        if not free:
            return None
        get_db().execute("INSERT INTO carry_items VALUES (?, ?, ?, ?, ?)", (user_id, slot, free[0], item_id, quantity))
        return [free[0]]

    def skills_of(user_id, slot):
        return {r["skill_id"]: {"level": int(r["level"]), "xp": int(r["xp"])} for r in get_db().execute(
            "SELECT skill_id, level, xp FROM skills WHERE user_id = ? AND slot = ?", (user_id, slot))}

    def grant(user_id, slot, skill_id, gained):
        had = skills_of(user_id, slot).get(skill_id, {"level": 1, "xp": 0})
        xp = had["xp"] + int(gained)
        level = 1 + xp // 100
        get_db().execute("DELETE FROM skills WHERE user_id = ? AND slot = ? AND skill_id = ?", (user_id, slot, skill_id))
        get_db().execute("INSERT INTO skills VALUES (?, ?, ?, ?, ?)", (user_id, slot, skill_id, level, xp))
        return level, xp, level - had["level"]

    def roll_catch(rod_tier, level):
        rolled.append((rod_tier, level))
        return {"item_id": "rawmudfish", "quantity": 1, "xp": 150}

    def roll_cook(item_id, level):
        if item_id == "rawshark":
            return {"ok": False, "reason": "level", "needs": 40}
        if item_id != "rawmudfish":
            return {"ok": False, "reason": "recipe"}
        return {"ok": True, "burnt": False, "output": "cookedmudfish", "xp": 30}

    rolled = app.config.setdefault("rolled", [])
    gathering.register(app, require_auth=require_auth, get_db=get_db,
                       parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None, max_slot=3,
                       bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                       slot_exists=lambda user_id, slot: get_db().execute(
                           "SELECT 1 FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone() is not None,
                       item_row={"bronzefishingrod": {"tier": 2}}.get, roll_catch=roll_catch, roll_cook=roll_cook,
                       skills_of=skills_of, take_from_backpack=take, add_to_backpack=add, grant_skill_xp=grant,
                       status_of=lambda user_id, slot: {"slot": slot},
                       inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                                           for p, (i, q) in sorted(carried(user_id, slot).items())],
                       bag_cells=GATHER_CELLS)

    # loot/: the money and the backpack are app.py's, so here the ledgers are
    # lists on the app (what was minted, and why), the lusions a total, and
    # the backpack the same four cells.
    minted = app.config.setdefault("minted", {"gold": [], "lusions": [], "holds": []})

    def gold_delta(_db, user_id, slot, delta, reason, _detail=""):
        minted["gold"].append((user_id, slot, delta, reason))

    def lusion_delta(_db, user_id, slot, delta, reason, _detail=""):
        minted["lusions"].append((user_id, slot, delta, reason))

    def parse_stat(raw):
        return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None

    loot.register(app, require_auth=require_auth, get_db=get_db,
                  bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                  parse_stat=parse_stat,
                  item_row={"smallhealthpotion": {"type_name": "CONSUMABLE"}, "silvercoin": {"type_name": "CURRENCY"},
                            "petmage": {"type_name": "PET"}}.get,
                  owns_item=lambda user_id, slot, item_id: any(i == item_id for i, _q in carried(user_id, slot).values()),
                  lusions_item_id="lusions", dupe_pet_lusions=lambda: 20,
                  gold_item_value=lambda item_id: {"silvercoin": 50}.get(item_id, 0),
                  gold_delta=gold_delta, lusion_delta=lusion_delta,
                  ensure_account=lambda user_id: {"lusions": sum(d for u, _s, d, _r in minted["lusions"] if u == user_id)},
                  add_to_backpack=add,
                  place_trade_hold=lambda _db, user_id, item_id, quantity: minted["holds"].append((user_id, item_id, quantity)),
                  status_of=lambda user_id, slot: {"slot": slot},
                  inventory_of=lambda user_id, slot: [{"position": p, "item_id": i, "quantity": q}
                                                      for p, (i, q) in sorted(carried(user_id, slot).items())],
                  bag_cells=GATHER_CELLS)

    # guilds/: the purse and the bank are a column and a dict here, through
    # ledgers that list what was burned and why; the moderation log is a list
    # of its own; staff are the owner alone.
    bank = app.config.setdefault("bank", {})
    staff_log = app.config.setdefault("staff_log", [])

    class Short(Exception):
        pass

    def purse_delta(db, user_id, slot, delta, reason, _detail=""):
        have = db.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)).fetchone()[0]
        if have + delta < 0:
            raise Short()
        db.execute("UPDATE saves SET gold = ? WHERE user_id = ? AND slot = ?", (have + delta, user_id, slot))
        minted["gold"].append((user_id, slot, delta, reason))
        return have + delta

    def bank_delta(_db, user_id, delta, reason, _detail=""):
        if bank.get(user_id, 0) + delta < 0:
            raise Short()
        bank[user_id] = bank.get(user_id, 0) + delta
        minted["gold"].append((user_id, "bank", delta, reason))
        return bank[user_id]

    def owner_only(_minimum):
        def decorator(view):
            def wrapped(*args, **kwargs):
                if g.user["username"] != OWNER:
                    return {"error": "Not Found", "message": "The requested URL was not found on the server."}, 404
                return view(*args, **kwargs)
            wrapped.__name__ = view.__name__
            return wrapped
        return decorator

    guilds.register(app, require_auth=require_auth, require_role=owner_only, get_db=get_db,
                    bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                    parse_slot=lambda raw: int(raw) if str(raw) in ("0", "1", "2", "3") else None,
                    find_user=find_user, ensure_account=lambda user_id: {"bank_gold": bank.get(user_id, 0)},
                    gold_delta=purse_delta, bank_gold_delta=bank_delta, insufficient_gold=Short,
                    log_staff_action=lambda user, action, target, detail="": staff_log.append(
                        "%s %s %s: %s" % (user["username"], action, target, detail)),
                    has_owner_powers=lambda name: name == OWNER,
                    role_for=lambda row: row["role"], name_hue_of=lambda row: row["name_hue"],
                    # The character an account plays: its lowest slot, for any
                    # account seen within the window - the two ? are app.py's.
                    playing_slot_sql="(SELECT MIN(slot) FROM saves s2 WHERE s2.user_id = {user} AND ? < ?)",
                    online_window=45)

    # parties/: the relay is a stand-in that counts how often it is asked; the
    # TURN key comes from app.config, so a check can take it away.
    relay = app.config.setdefault("relay", {"mode": "relay", "asked": 0})

    def turn_fetch(_key_id, _token, ttl):
        relay["asked"] += 1
        relay["ttl"] = ttl
        return [{"urls": ["stun:stun.example.com:3478", "stun:stun.example.com:53"]},
                {"urls": ["turn:turn.example.com:3478?transport=udp"], "username": "u", "credential": "c"}]

    import threading
    parties.register(app, require_auth=require_auth, get_db=get_db,
                     bad_request=lambda message: ({"error": "Bad Request", "message": message}, 400),
                     find_user=find_user,
                     # bo ignores ann, as for friends.
                     ignores=lambda _db, a, b: (a, b) == (3, 2),
                     playing_slot_sql="(SELECT MIN(slot) FROM saves s2 WHERE s2.user_id = {user} AND ? < ?)",
                     online_window=45,
                     voice_settings=lambda: (relay["mode"], "keyid", "token") if relay["mode"] else ("", "", ""),
                     turn_fetch=turn_fetch, voice_issued={}, voice_asks={}, voice_lock=threading.Lock())
    return app, said, logged, path


# A four-cell bag for gathering/'s stand-ins.
GATHER_CELLS = 4


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
                       "/api/server/insights", "/api/kills", "/api/kills/everyone", "/api/friends",
                       "/api/friends/request", "/api/friends/respond", "/api/friends/remove",
                       "/api/fishing/catch", "/api/cooking/cook", "/api/loot/take", "/api/loot/bag",
                       "/api/guild", "/api/guild/create", "/api/guild/invite", "/api/guild/respond", "/api/guild/leave",
                       "/api/guild/kick", "/api/guild/rank", "/api/guild/disband", "/api/staff/guild",
                       "/api/guild/list", "/api/party", "/api/party/invite", "/api/party/respond",
                       "/api/party/leave", "/api/party/kick", "/api/party/lead", "/api/party/voice"]), rules)

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
res = client_a.get("/api/kills?slot=0", headers=who("ann"))
body = res.get_json() or {}
check("killrecord: a character's record - its own slot's kills, most first, and the day counting began",
      res.status_code == 200 and [(k["enemy_id"], k["kills"]) for k in body["kills"]] == [("slime", 5), ("bat", 1)]
      and body["total"] == 6 and body["kinds"] == 2 and body["since"] == 1234 and body["slot"] == 0, body)
res = client_a.get("/api/kills/everyone", headers=who("ann"))
body = res.get_json() or {}
slime = next((k for k in body.get("kills", []) if k["enemy_id"] == "slime"), {})
check("  everyone's - an account's characters added together (ann's 7 beats bo's 6), and ann's own share beside it",
      res.status_code == 200 and slime.get("kills") == 13 and slime.get("top") == "ann" and slime.get("top_kills") == 7
      and slime.get("yours") == 7 and body["players"] == 2, body)
check("  and a bad slot is the 400 it was, in its words",
      client_a.get("/api/kills?slot=9", headers=who("ann")).get_json() == {"error": "Bad Request", "message": "slot must be 0-3"})
res = client_a.post("/api/friends/request", headers=who("boss"), json={"username": "ANN"})
check("friends: boss asks ann - by her name in any case - and it waits",
      res.status_code == 200 and res.get_json() == {"username": "ann", "state": "pending"}, res.get_json())
check("  a second ask is past the one-outstanding limit handed in",
      client_a.post("/api/friends/request", headers=who("boss"), json={"username": "bo"}).get_json()
      == {"error": "Bad Request", "message": "you have 1 requests waiting for an answer; that is the limit"})
res = client_a.post("/api/friends/respond", headers=who("ann"), json={"username": "boss", "accept": True})
listed = client_a.get("/api/friends", headers=who("boss")).get_json() or {}
check("  ann says yes: on boss's list, online by the presence handed in, the limits his",
      res.get_json() == {"username": "boss", "state": "accepted"}
      and [(f["username"], f["online"]) for f in listed.get("friends", [])] == [("ann", True)]
      and listed.get("limits") == {"friends": 100, "pending_sent": 1, "friends_used": 1, "pending_sent_used": 0}, listed)
check("  bo ignores ann, so she cannot ask him - the ignore list handed in",
      client_a.post("/api/friends/request", headers=who("ann"), json={"username": "bo"}).status_code == 403)
check("  and removed, the link is gone both ways",
      client_a.post("/api/friends/remove", headers=who("ann"), json={"username": "boss"}).get_json()
      == {"username": "boss", "state": "none"}
      and client_a.get("/api/friends", headers=who("boss")).get_json()["friends"] == [])


def ann_holds(item_id):
    conn = sqlite3.connect(path_a)
    held = conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM carry_items WHERE user_id = 2 AND slot = 0 AND item_id = ?",
                        (item_id,)).fetchone()[0]
    conn.close()
    return int(held)


def ann_gets(item_id, quantity, position):
    conn = sqlite3.connect(path_a)
    conn.execute("INSERT INTO carry_items VALUES (2, 0, ?, ?, ?)", (position, item_id, quantity))
    conn.commit()
    conn.close()


def fish(slot=0):
    return client_a.post("/api/fishing/catch", headers=who("ann"), json={"slot": slot})


def cook(item_id):
    return client_a.post("/api/cooking/cook", headers=who("ann"), json={"slot": 0, "item_id": item_id})


check("gathering: no rod is the 403 it was", fish().get_json() == {"error": "Forbidden", "message": "You need a fishing rod."})
ann_gets("bronzefishingrod", 1, 0)
check("  a rod and no worms, the other 403", fish().get_json() == {"error": "Forbidden", "message": "You need worms for bait."})
ann_gets("fishingworm", 3, 1)
res = fish()
body = res.get_json() or {}
check("  a rod and worms: the fish lands in a free cell, one worm is spent, the skill handed in pays",
      res.status_code == 200 and body.get("item_id") == "rawmudfish" and body.get("carry_positions") == [2]
      and body.get("fishing_level") == 2 and body.get("levelled_up") is True and ann_holds("fishingworm") == 2
      and body.get("skills", {}).get("fishing") == {"level": 2, "xp": 150}, body)
check("  rolled with the rod's tier from the catalogue handed in, and the fishing level before the cast",
      app_a.config["rolled"] == [(2, 1)], app_a.config["rolled"])
conn = sqlite3.connect(path_a)
tokens, cast_at = conn.execute("SELECT cast_tokens, last_cast_at FROM saves WHERE user_id = 2 AND slot = 0").fetchone()
conn.execute("UPDATE saves SET cast_tokens = 0.2, last_cast_at = ? WHERE user_id = 2 AND slot = 0", (int(time.time() * 1000),))
conn.commit()
conn.close()
check("  the cast bucket paid one token (%g of %g), and the cast's time" % (tokens, gathering.CAST_BUCKET_CAPACITY),
      abs(tokens - (gathering.CAST_BUCKET_CAPACITY - 1.0)) < 1e-6 and cast_at > 0, (tokens, cast_at))
check("  an empty bucket is the 429 it was, in its words",
      fish().get_json() == {"error": "Too Many Requests", "message": "Casts are arriving faster than 0.4 per second."})
conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET cast_tokens = 6.0 WHERE user_id = 2 AND slot = 0")
conn.commit()
conn.close()
ann_gets("pebble", 1, 3)
res = fish()
check("  a full bag is the 409 it was - and the worm is NOT spent: nothing was committed",
      res.get_json() == {"error": "Conflict", "message": "Your backpack is full (4 slots)."} and ann_holds("fishingworm") == 2,
      (res.get_json(), ann_holds("fishingworm")))
check("  an empty slot and a bad one, the 404 and 400 they were",
      fish(slot=1).get_json() == {"error": "Not Found", "message": "No character in slot 1."}
      and fish(slot=9).get_json() == {"error": "Bad Request", "message": "slot must be an integer 0-3"})
res = cook("rawmudfish")
body = res.get_json() or {}
check("cooking: the raw fish goes, the dish comes, and cooking XP is paid",
      res.status_code == 200 and body.get("burnt") is False and body.get("output") == "cookedmudfish"
      and ann_holds("rawmudfish") == 0 and ann_holds("cookedmudfish") == 1 and body.get("cooking_level") == 1
      and body.get("skills", {}).get("cooking") == {"level": 1, "xp": 30}, body)
conn = sqlite3.connect(path_a)
conn.execute("DELETE FROM carry_items WHERE item_id = 'pebble'")
conn.execute("INSERT INTO carry_items VALUES (2, 0, 3, 'rawshark', 1)")
conn.commit()
conn.close()
check("  a level too low, the 403 it was, and the fish kept",
      cook("rawshark").get_json() == {"error": "Forbidden", "message": "You need cooking level 40."} and ann_holds("rawshark") == 1)
check("  what is not carried, the 404; what has no recipe, the 409",
      cook("rawtuna").get_json() == {"error": "Not Found", "message": "You are not carrying that."}
      and cook("bronzefishingrod").get_json() == {"error": "Conflict", "message": "That cannot be cooked."})


BAGS_MADE = []


def bag_for_bo(entries, made_at=None):
    """A bag rolled for bo (account 3, slot 0) the way a kill makes one -
    stamped() and store_bag() - and its id."""
    conn = sqlite3.connect(path_a)
    now = int(time.time())
    BAGS_MADE.append(1)
    bag_id = "bag%d" % len(BAGS_MADE)
    stored = loot.stamped([{"item_id": i, "quantity": q} for i, q in entries])
    loot.store_bag(conn, bag_id, 3, 0, "slime", stored, now if made_at is None else made_at,
                   prune_before=now - loot.LOOT_BAG_TTL_SECONDS - 3600)
    conn.commit()
    conn.close()
    return bag_id, stored


def take(bag_id, position, as_who="bo"):
    return client_a.post("/api/loot/take", headers=who(as_who), json={"bag_id": bag_id, "position": position})


def bo_gets(item_id, position):
    conn = sqlite3.connect(path_a)
    conn.execute("INSERT INTO carry_items VALUES (3, 0, ?, ?, 1)", (position, item_id))
    conn.commit()
    conn.close()


minted = app_a.config["minted"]
bo_gets("petmage", 0)
bag, stored = bag_for_bo([("smallhealthpotion", 2), ("silvercoin", 3), ("petmage", 1), ("smallhealthpotion", 1)]
                         + [("pebble", 1)] * 7)
check("loot: a roll past %d cells is stored cut to them, each entry with its cell" % loot.LOOT_BAG_CAPACITY,
      [e["position"] for e in stored] == list(range(loot.LOOT_BAG_CAPACITY)), stored)
res = take(bag, 0)
body = res.get_json() or {}
check("  a potion goes into the backpack handed in, and is held from trade there",
      res.status_code == 200 and body.get("credited") == "inventory" and body.get("carry_positions") == [1]
      and body.get("granted_quantity") == 2 and minted["holds"] == [(3, "smallhealthpotion", 2)], body)
check("  the same cell again is the 404 it was - a second take changes nothing",
      take(bag, 0).get_json() == {"error": "Not Found", "message": "Nothing in that slot of the bag."})
body = take(bag, 1).get_json() or {}
check("  three silver coins are 150 gold, through the gold ledger handed in, as loot",
      body.get("credited") == "gold" and body.get("gold_amount") == 150 and minted["gold"] == [(3, 0, 150, "loot")], body)
body = take(bag, 2).get_json() or {}
check("  a pet bo already holds is 20 lusions instead, through the lusion ledger",
      body.get("duplicate_pet") is True and body.get("credited") == "lusions" and body.get("granted_item_id") == "lusions"
      and body.get("granted_quantity") == 20 and body.get("lusions") == 20
      and minted["lusions"] == [(3, 0, 20, "duplicate_pet")], body)
check("  a cell past the bag's is the 400 it was, in its words",
      take(bag, loot.LOOT_BAG_CAPACITY).get_json()
      == {"error": "Bad Request", "message": "position must be an integer 0-%d" % (loot.LOOT_BAG_CAPACITY - 1)})
check("  and somebody else's bag is the same 404 as no bag at all",
      take(bag, 3, as_who="ann").get_json() == {"error": "Not Found", "message": "No such loot bag."}
      and take("nosuchbag", 3).get_json() == {"error": "Not Found", "message": "No such loot bag."})
res = client_a.get("/api/loot/bag?bag_id=%s" % bag, headers=who("bo"))
body = res.get_json() or {}
check("  what is left: cells 3 to 8, the monster, the slot",
      res.status_code == 200 and [c["position"] for c in body.get("contents", [])] == list(range(3, loot.LOOT_BAG_CAPACITY))
      and body.get("enemy_id") == "slime" and body.get("slot") == 0, body)
check("  and asked with no bag, the 400 it was",
      client_a.get("/api/loot/bag", headers=who("bo")).get_json() == {"error": "Bad Request", "message": "bag_id is required"})
bo_gets("pebble", 2)
bo_gets("pebble", 3)
res = take(bag, 3)
check("  a full backpack is the 409 it was, and the potion stays in the bag",
      res.get_json() == {"error": "Conflict", "message": "Your backpack is full (4 slots)."}
      and 3 in [c["position"] for c in client_a.get("/api/loot/bag?bag_id=%s" % bag, headers=who("bo")).get_json()["contents"]])
old_bag, _stored = bag_for_bo([("silvercoin", 1)], made_at=int(time.time()) - loot.LOOT_BAG_TTL_SECONDS - 5)
check("  a bag past its %d seconds is the 410 it was, and gone after" % loot.LOOT_BAG_TTL_SECONDS,
      take(old_bag, 0).get_json() == {"error": "Gone", "message": "That loot bag has expired."}
      and client_a.get("/api/loot/bag?bag_id=%s" % old_bag, headers=who("bo")).status_code == 404)
last_bag, _stored = bag_for_bo([("silvercoin", 2)])
body = take(last_bag, 0).get_json() or {}
check("  the last thing out empties the bag, and the bag goes with it",
      body.get("bag_empty") is True and body.get("gold_amount") == 100
      and client_a.get("/api/loot/bag?bag_id=%s" % last_bag, headers=who("bo")).status_code == 404, body)


def guild_post(as_who, path, body):
    res = client_a.post(path, headers=who(as_who), json=body)
    return res.status_code, res.get_json() or {}


def guild_of(as_who):
    return client_a.get("/api/guild", headers=who(as_who)).get_json() or {}


conn = sqlite3.connect(path_a)
conn.execute("UPDATE saves SET gold = 3000, name = 'Ann' WHERE user_id = 2 AND slot = 0")
conn.execute("INSERT INTO sessions VALUES (2, ?)", (int(time.time()),))
conn.commit()
conn.close()
app_a.config["bank"][2] = 4000
gold_before = len(minted["gold"])
code, body = guild_post("ann", "/api/guild/create", {"name": "The Crowned", "slot": 0})
check("guilds: ann founds one - %d gold, the carried 3000 first, then 2000 from the bank, both burned"
      % guilds.GUILD_FOUND_COST,
      code == 200 and body.get("rank") == "leader" and body.get("from_carried") == 3000 and body.get("from_bank") == 2000
      and body.get("carried_gold") == 0 and body.get("bank_gold") == 2000
      and minted["gold"][gold_before:] == [(2, 0, -3000, "guild_found"), (2, "bank", -2000, "guild_found")], body)
crowned = body.get("guild", {})
check("  the guild as every route returns it: tagged, on the roster as she plays, online by the window handed in",
      crowned.get("tag") == "THE CROWNED" and crowned.get("capacity") == 50
      and [(m["username"], m["rank"], m["online"], m["character"]) for m in crowned.get("members", [])]
      == [("ann", "leader", True, "Ann")], crowned)
check("  a second founding is the 409 it was, a bad name the 400",
      guild_post("ann", "/api/guild/create", {"name": "Another", "slot": 0})[0] == 409
      and guild_post("bo", "/api/guild/create", {"name": "x", "slot": 0})[1].get("message", "").startswith(
          "a guild name is 3 to 12 characters"))
check("  and an empty slot pays nothing: the 404 it was",
      guild_post("bo", "/api/guild/create", {"name": "Bo's Lot", "slot": 0})
      == (404, {"error": "Not Found", "message": "That slot is empty."}))
guild_post("ann", "/api/guild/invite", {"username": "bo"})
check("  invited, bo sees it waiting",
      [i["guild"] for i in guild_of("bo").get("invites", [])] == ["The Crowned"], guild_of("bo"))
code, body = guild_post("bo", "/api/guild/respond", {"guild": "the crowned", "accept": True})
check("  accepted - by the name in any case - he is a member",
      code == 200 and body.get("state") == "joined" and body.get("guild", {}).get("size") == 2, body)
code, body = guild_post("ann", "/api/guild/rank", {"username": "bo", "rank": "officer"})
check("  the leader makes him an officer", code == 200 and body.get("rank") == "officer", body)
check("  an officer cannot remove the leader: the same 404 as no such member",
      guild_post("bo", "/api/guild/kick", {"username": "ann"}) == (404, {"error": "Not Found", "message": "Not found."}))
check("  and the leader cannot walk out on him",
      guild_post("ann", "/api/guild/leave", {})[1].get("message")
      == "make somebody else the leader first, or disband the guild")
check("  its history, newest first",
      [e["kind"] for e in guild_of("ann").get("guild", {}).get("activity", [])]
      == ["promoted", "joined", "invited", "founded"], guild_of("ann"))
code, body = guild_post("boss", "/api/staff/guild", {"guild": "The Crowned", "action": "rename", "name": "The First",
                                                     "reason": "a test"})
check("staff: renamed, on the moderation log handed in, and in the guild's history without who did it",
      code == 200 and body == {"guild": "The First", "was": "The Crowned", "state": "renamed"}
      and app_a.config["staff_log"] == ["boss guild_rename The Crowned: -> The First: a test"]
      and guild_of("bo").get("guild", {}).get("activity", [{}])[0] == {
          "at": guild_of("bo")["guild"]["activity"][0]["at"], "kind": "renamed", "actor": "", "target": "The First",
          "detail": "The Crowned"}, (body, app_a.config["staff_log"]))
check("  and the old name no longer finds it",
      guild_post("boss", "/api/staff/guild", {"guild": "The Crowned", "action": "disband", "reason": "x"})[0] == 404)
listed = client_a.get("/api/guild/list", headers=who("boss")).get_json() or {}
check("  the staff list: one guild, its size and leader, and the owner may take it down",
      [(r["name"], r["size"], r["leader"]) for r in listed.get("guilds", [])] == [("The First", 2, "ann")]
      and listed.get("may_disband") is True, listed)
check("  and nobody else is shown the list",
      client_a.get("/api/guild/list", headers=who("ann")).status_code == 404)
code, body = guild_post("ann", "/api/guild/disband", {})
check("the leader takes it down: both out, and the guild gone",
      code == 200 and body.get("members_removed") == 2 and guild_of("bo").get("in_guild") is False
      and client_a.get("/api/guild/list", headers=who("boss")).get_json()["guilds"] == [], body)


def party_post(as_who, path, body):
    res = client_a.post(path, headers=who(as_who), json=body)
    return res.status_code, res.get_json() or {}


def party_of(as_who):
    return client_a.get("/api/party", headers=who(as_who)).get_json() or {}


check("parties: bo ignores ann, so her invitation is the 403 it was - the ignore list handed in",
      party_post("ann", "/api/party/invite", {"username": "bo"})
      == (403, {"error": "Forbidden", "message": "bo is not taking party invites from you."}))
code, body = party_post("boss", "/api/party/invite", {"username": "ann"})
check("  boss asks ann, and she sees it waiting",
      code == 200 and body.get("expires_in") == parties.services.PARTY_INVITE_SECONDS
      and [i["from"] for i in party_of("ann").get("invites", [])] == ["boss"], body)
code, body = party_post("ann", "/api/party/respond", {"from": "boss", "accept": True})
members = (body.get("party") or {}).get("members", [])
check("  she says yes: the party is made with boss leading, and her on it as she plays, online",
      code == 200 and body.get("state") == "joined"
      and [(m["username"], m["leader"], m["online"], m["character"]) for m in members]
      == [("boss", True, False, ""), ("ann", False, True, "Ann")], body)
check("  only the leader invites",
      party_post("ann", "/api/party/invite", {"username": "bo"})
      == (400, {"error": "Bad Request", "message": "only the party's leader can invite"}))
code, body = party_post("boss", "/api/party/lead", {"username": "ann"})
check("  boss hands it on to ann", code == 200 and body.get("party", {}).get("leader") == "ann", body)
check("  and no longer leading, he cannot remove anybody",
      party_post("boss", "/api/party/kick", {"username": "ann"})
      == (404, {"error": "Not Found", "message": "You are not leading a party."}))
code, body = party_post("ann", "/api/party/kick", {"username": "boss"})
check("  ann removes him: down to one, the party is gone",
      code == 200 and body.get("disbanded") is True and body.get("party") is None
      and party_of("boss").get("party") is None, body)
check("  and an answer to nobody's invitation is the 404 it was",
      party_post("ann", "/api/party/respond", {"from": "bo", "accept": True})
      == (404, {"error": "Not Found", "message": "No invitation from them - it may have run out."}))
check("party voice: in no party, the 409 it was",
      client_a.get("/api/party/voice", headers=who("ann")).get_json()
      == {"error": "Conflict", "message": "Voice is for parties: invite somebody first."})
party_post("boss", "/api/party/invite", {"username": "ann"})
party_post("ann", "/api/party/respond", {"from": "boss", "accept": True})
relay = app_a.config["relay"]
body = client_a.get("/api/party/voice", headers=who("ann")).get_json() or {}
urls = [u for server in body.get("ice_servers", []) for u in server["urls"]]
check("  in one: the relay handed in is asked once, for a day, and port 53 is dropped from its answer",
      body.get("policy") == "relay" and relay["asked"] == 1 and relay["ttl"] == parties.VOICE_TURN_TTL
      and "stun:stun.example.com:53" not in urls and "turn:turn.example.com:3478?transport=udp" in urls
      and body.get("expires_at", 0) - time.time() > 23 * 3600, (body, relay))
again = client_a.get("/api/party/voice", headers=who("ann")).get_json() or {}
check("  asked again, the same credentials, and the relay is not asked twice",
      again.get("ice_servers") == body.get("ice_servers") and relay["asked"] == 1, relay)
relay["mode"] = ""
check("  and with no TURN key in the settings handed in, the 503 it was",
      client_a.get("/api/party/voice", headers=who("boss")).get_json()
      == {"error": "Service Unavailable", "message": "Voice is not set up on this server yet."})
check("  and nobody without a login gets in anywhere",
      [client_a.get(path).status_code for path in ("/api/server/stats", "/api/world/events/active", "/api/server/insights",
                                                  "/api/kills?slot=0", "/api/kills/everyone", "/api/friends",
                                                  "/api/loot/bag?bag_id=%s" % bag, "/api/guild", "/api/guild/list",
                                                  "/api/party", "/api/party/voice")]
      + [client_a.post(path, json={"slot": 0}).status_code for path in ("/api/fishing/catch", "/api/cooking/cook",
                                                                         "/api/loot/take", "/api/guild/create",
                                                                         "/api/staff/guild", "/api/party/invite")]
      == [401] * 17)

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

# =============================================================================
section("P-4  every package is here")
check("the code map finds the packages app.py imports: %s" % ", ".join(PACKAGES),
      set(PACKAGES) >= {"playerstats", "events", "insights", "killrecord", "friends", "gathering", "loot", "guilds",
                        "parties"},
      PACKAGES)
check("  and every one of them runs on a bare app in this file", set(PACKAGES) == set(RUN_HERE),
      sorted(set(PACKAGES) ^ set(RUN_HERE)))

print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
