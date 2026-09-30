"""Which character is this account playing? One answer, everywhere it is asked.

An account has up to four characters and plays one at a time. The server used
to guess which from whichever save was written LAST - and a save is written
only when something changes, so for the minute after somebody switched
character every list in the game showed yesterday's. The client now names its
slot on the broadcast poll (sessions.playing_slot) and PLAYING_SLOT_SQL is the
one rule: a fresh session's said slot with a save behind it, else the latest
save. test_trades.py holds the rule itself (T-1); this holds that every place
which answers the question asks it.

  P-1  the online list shows the character each player is playing
  P-2  and groups "here with you" by the area of the character YOU are playing
  P-3  the guild roster shows each member's playing character
  P-4  the staff account view marks which character is being played
  P-5  a staff teleport with no area goes to the area of the character the
       staff member is playing - it used to be whichever save SQLite returned
  P-6  nothing in app.py picks a character by "saved last" on its own any more
  P-7  the online list's per-row lookups are index reads, not scans

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_playing.py
"""

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_playing_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "boss"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

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


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def register(name):
    return client.post("/api/auth/register",
                       json={"username": name, "password": "password123"}).get_json()["token"]


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def uid(name):
    return int(sql("SELECT id FROM users WHERE username = ?", (name,))[0]["id"])


def character(token, slot, class_id, name, area, saved_at, level=1):
    client.put("/api/save", headers=auth(token),
               json={"slot": slot, "class_id": class_id, "name": name})
    who = sql("SELECT user_id FROM sessions WHERE token = ?", (token,))[0]["user_id"]
    sql("UPDATE saves SET area = ?, updated_at = ?, level = ? WHERE user_id = ? AND slot = ?",
        (area, saved_at, level, who, slot))


def beat(token, slot=None):
    path = "/api/server/broadcasts" if slot is None else "/api/server/broadcasts?slot=%d" % slot
    return client.get(path, headers=auth(token))


def online(token):
    return client.get("/api/players/online", headers=auth(token)).get_json() or {}


def listed(token, who):
    return next((p for p in online(token).get("players", []) if p["username"] == who), None)


BOSS = register("boss")
ALICE = register("alice")
BOB = register("bob")
character(BOSS, 0, "warrior", "Boss Town", "town", 3000)
character(BOSS, 1, "mage", "Boss Field", "field", 1000)
character(ALICE, 0, "healer", "Aldra", "town", 2000)
# Bob saved his FIRST character last - the old guess - but plays his second.
character(BOB, 0, "mage", "Bram First", "town", 2000, level=30)
character(BOB, 1, "tank", "Bram Second", "field", 1000, level=4)


# =============================================================================
print("\n--- P-1 the online list shows the character being played ---")
# =============================================================================
beat(ALICE, 0)
beat(BOB)
row = listed(ALICE, "bob")
check("with nothing said, the online list falls back to the latest save",
      row is not None and row["name"] == "Bram First", row)
beat(BOB, 1)
row = listed(ALICE, "bob")
check("once his client says slot 1, the list shows that character",
      row is not None and row["name"] == "Bram Second" and row["level"] == 4
      and row["area"] == "field", row)
check("once, not once per character", [p["username"] for p in online(ALICE)["players"]].count("bob") == 1)
beat(BOB, 0)
row = listed(ALICE, "bob")
check("and follows him back when he switches", row is not None and row["name"] == "Bram First", row)
# An account online with no character at all is still listed, just nameless.
NOONE = register("noone")
beat(NOONE)
row = listed(ALICE, "noone")
check("an account with no character is listed without one", row is not None and row["name"] == "", row)


# =============================================================================
print("\n--- P-2 'here with you' is where YOU are playing ---")
# =============================================================================
beat(BOB, 1)
beat(BOSS, 1)
mine = online(BOSS)
check("the caller's own area is their playing character's, not their latest save's",
      mine.get("area") == "field", mine.get("area"))
row = next((p for p in mine["players"] if p["username"] == "bob"), None)
check("so bob, in the field, is with them", row is not None and row["with_you"] is True, row)
row = next((p for p in mine["players"] if p["username"] == "alice"), None)
check("and alice, in town, is not", row is not None and row["with_you"] is False, row)
beat(BOSS, 0)
check("switching moves where 'here' is", online(BOSS).get("area") == "town")


# =============================================================================
print("\n--- P-3 the guild roster shows each member's playing character ---")
# =============================================================================
sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = 0",
    (app_module.GUILD_FOUND_COST, uid("alice")))
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
    " VALUES (0, ?, 0, ?, 'test_seed', 'guild fee')", (uid("alice"), app_module.GUILD_FOUND_COST))
made = client.post("/api/guild/create", headers=auth(ALICE), json={"name": "Lodge", "slot": 0})
check("alice founds a guild", made.status_code == 200, made.get_json())
client.post("/api/guild/invite", headers=auth(ALICE), json={"username": "bob"})
client.post("/api/guild/respond", headers=auth(BOB), json={"guild": "Lodge", "accept": True})


def roster_row(token, who):
    guild = (client.get("/api/guild", headers=auth(token)).get_json() or {}).get("guild") or {}
    return next((m for m in guild.get("members", []) if m["username"] == who), None)


beat(BOB, 1)
row = roster_row(ALICE, "bob")
check("the roster shows bob as the character he is playing",
      row is not None and row["character"] == "Bram Second" and row["class_id"] == "tank"
      and row["level"] == 4 and row["area"] == "field", row)
beat(BOB, 0)
row = roster_row(ALICE, "bob")
check("and follows him when he switches", row is not None and row["character"] == "Bram First", row)
check("a member's own row follows the same rule",
      (roster_row(ALICE, "alice") or {}).get("character") == "Aldra")


# =============================================================================
print("\n--- P-4 the staff view marks which character is being played ---")
# =============================================================================
beat(BOB, 1)
view = client.get("/api/staff/user/bob", headers=auth(BOSS)).get_json() or {}
chars = view.get("characters", [])
check("the staff view lists both of bob's characters", len(chars) == 2, chars)
check("and marks exactly the one he is playing",
      [c["slot"] for c in chars if c.get("playing")] == [1], chars)
beat(BOB, 0)
chars = (client.get("/api/staff/user/bob", headers=auth(BOSS)).get_json() or {}).get("characters", [])
check("and moves the mark when he switches", [c["slot"] for c in chars if c.get("playing")] == [0], chars)


# =============================================================================
print("\n--- P-5 a staff teleport with no area goes to where the staff member is playing ---")
# =============================================================================
sql("DELETE FROM pending_teleports")
beat(BOSS, 1)
moved = client.post("/api/staff/teleport", headers=auth(BOSS), json={"username": "bob"})
check("the owner brings bob to them", moved.status_code == 200, moved.get_json())
where = sql("SELECT area FROM pending_teleports WHERE user_id = ?", (uid("bob"),))
check("to the field, where the owner is playing - not town, where an alt is parked",
      [r["area"] for r in where] == ["field"], [dict(r) for r in where])
beat(BOSS, 0)
client.post("/api/staff/teleport", headers=auth(BOSS), json={"username": "bob"})
where = sql("SELECT area FROM pending_teleports WHERE user_id = ?", (uid("bob"),))
check("and to town once they switch", [r["area"] for r in where] == ["town"], [dict(r) for r in where])
client.post("/api/staff/teleport", headers=auth(BOSS), json={"username": "bob", "area": "crypt"})
where = sql("SELECT area FROM pending_teleports WHERE user_id = ?", (uid("bob"),))
check("an area that is named still wins", [r["area"] for r in where] == ["crypt"])


# =============================================================================
print("\n--- P-6 nothing picks a character by 'saved last' on its own ---")
# =============================================================================
# READ OFF THE SOURCE, because the next list somebody adds is the one that will
# reach for MAX(updated_at) again. Each statement touching `saves` that orders
# or aggregates by updated_at is found, and its function has to be on this
# list with a reason.
source = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
ALLOWED = {
    "PLAYING_SLOT_SQL": "the rule itself - its fallback IS the latest save",
    "_take_resync": "which flagged save to deliver first, not which character is played",
}
found = []
for m in re.finditer(r'FROM saves[^"\n]*(?:"\s*\n\s*"[^"\n]*)*', source):
    text = m.group(0)
    if not re.search(r"updated_at DESC|MAX\(updated_at\)|LIMIT 1", text):
        continue
    before = source[:m.start()]
    owner = re.findall(r"\n(?:def (\w+)|(PLAYING_SLOT_SQL) = )", before)
    name = [a or b for a, b in owner][-1] if owner else "?"
    found.append(name)
check("the scan finds the two sanctioned reads", {"PLAYING_SLOT_SQL", "_take_resync"} <= set(found), found)
check("and nothing else picks a save by recency or at random",
      [f for f in found if f not in ALLOWED] == [], [f for f in found if f not in ALLOWED])


# =============================================================================
print("\n--- P-7 the per-row lookups are index reads ---")
# =============================================================================
now = int(time.time())
with app_module.app.app_context():
    db = app_module.get_db()
    shown = " | ".join(str(tuple(r)) for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT %s" % app_module.PLAYING_SLOT_SQL.format(user="?"),
        (1, now - 45, now, 1)).fetchall())
check("the said-slot lookup reads sessions by account", "idx_sessions_user" in shown, shown)
# "SCAN CONSTANT ROW" is the bare SELECT around the expression, not a table.
check("and never scans a table", re.search(r"SCAN (?!CONSTANT ROW)", shown) is None, shown)
check("the fallback reads saves by account too", "SEARCH pl USING INDEX" in shown, shown)


# =============================================================================
print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
