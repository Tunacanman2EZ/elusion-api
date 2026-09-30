"""Guild life tests: who members are playing, and what has happened in the guild.

test_guilds.py is the rules - who may found, invite, rank, remove, disband.
This is what the guild panel shows on top of them: each member's character,
class, level and area, their chosen name colour, and a short history of the
guild in its members' own words.

  L-1  the roster says who each member is playing: their most recently saved
       character, and nothing invented for a member who has none
  L-2  every change to a guild writes one line of history, in the same
       transaction - founded, invited, joined, promoted, demoted, handed on,
       removed, left
  L-3  and only real changes: a second invitation, a declined one and a
       "promotion" to the rank already held write nothing
  L-4  staff renaming a guild appears in its history WITHOUT the staff
       member's name; the moderation log keeps who
  L-5  history is the guild's own: bounded per guild, never shown to anybody
       else, and gone with the guild
  L-6  every kind the server writes is a kind the panel has words for

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_guildlife.py
"""

import ast
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_guildlife_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "lifeowner"

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


def save(token, slot, class_id, name):
    return client.put("/api/save", headers=auth(token),
                      json={"slot": slot, "class_id": class_id, "name": name})


def give_gold(username, amount):
    # Into the table AND the ledger, so the supply invariant still holds -
    # the same helper test_guilds.py uses, for the same reason.
    with app_module.app.app_context():
        db = app_module.get_db()
        row = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        db.execute("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = 0",
                   (amount, row["id"]))
        db.execute(
            "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
            " VALUES (strftime('%s','now'), ?, 0, ?, 'test_grant', 'suite setup')",
            (row["id"], amount))
        db.commit()


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def mine(token):
    return client.get("/api/guild", headers=auth(token)).get_json() or {}


def post(token, path, body):
    return client.post(path, headers=auth(token), json=body)


def activity(token):
    return (mine(token).get("guild") or {}).get("activity", [])


def kinds(token):
    return [e["kind"] for e in activity(token)]


def member(token, who):
    for person in (mine(token).get("guild") or {}).get("members", []):
        if person["username"] == who:
            return person
    return {}


LEAD = register("lead")
OFFI = register("offi")
MEMB = register("memb")
NOCHAR = register("nochar")
OUT = register("outsider")
OWNER = register("lifeowner")
save(LEAD, 0, "warrior", "Kaelen")
save(OFFI, 0, "mage", "Ysolde")
save(MEMB, 0, "healer", "Brin")
save(OUT, 0, "tank", "Gorm")
give_gold("lead", app_module.GUILD_FOUND_COST * 2)
give_gold("outsider", app_module.GUILD_FOUND_COST * 2)


# =============================================================================
print("\n--- L-2 every change writes one line ---")
# =============================================================================
check("a guild is founded", post(LEAD, "/api/guild/create", {"name": "Lifers", "slot": 0}).status_code == 200)
check("and its history starts there", kinds(LEAD) == ["founded"], kinds(LEAD))
first = activity(LEAD)[0] if activity(LEAD) else {}
check("naming who founded it", first.get("actor") == "lead" and first.get("at", 0) > 0, first)

post(LEAD, "/api/guild/invite", {"username": "offi"})
post(LEAD, "/api/guild/invite", {"username": "memb"})
post(LEAD, "/api/guild/invite", {"username": "nochar"})
check("an invitation is a line, naming who asked whom",
      activity(LEAD)[0].get("kind") == "invited" and activity(LEAD)[0].get("target") == "nochar",
      activity(LEAD)[:1])
for token in (OFFI, MEMB, NOCHAR):
    post(token, "/api/guild/respond", {"guild": "Lifers", "accept": True})
check("each acceptance is a line in the joiner's name",
      kinds(LEAD)[:3] == ["joined", "joined", "joined"]
      and {e["actor"] for e in activity(LEAD)[:3]} == {"offi", "memb", "nochar"}, activity(LEAD)[:3])

post(LEAD, "/api/guild/rank", {"username": "offi", "rank": "officer"})
line = activity(LEAD)[0]
check("a promotion says who, whom, and to what",
      line["kind"] == "promoted" and line["actor"] == "lead" and line["target"] == "offi"
      and line["detail"] == "officer", line)
post(LEAD, "/api/guild/rank", {"username": "memb", "rank": "officer"})
post(LEAD, "/api/guild/rank", {"username": "memb", "rank": "member"})
check("a demotion is read off the ladder, not the word", activity(LEAD)[0]["kind"] == "demoted",
      activity(LEAD)[0])

newest_first = [e["at"] for e in activity(LEAD)]
check("newest first", newest_first == sorted(newest_first, reverse=True), newest_first)


# =============================================================================
print("\n--- L-3 and only real changes ---")
# =============================================================================
before = len(sql("SELECT id FROM guild_events"))
post(OUT, "/api/guild/create", {"name": "Others", "slot": 0})
post(LEAD, "/api/guild/invite", {"username": "outsider"})   # already in a guild - refused
twice_before = len(sql("SELECT id FROM guild_events WHERE kind = 'invited'"))
fresh = register("fresh")
post(LEAD, "/api/guild/invite", {"username": "fresh"})
post(LEAD, "/api/guild/invite", {"username": "fresh"})
check("inviting somebody twice is one line, not two",
      len(sql("SELECT id FROM guild_events WHERE kind = 'invited'")) == twice_before + 1)
declined_before = len(sql("SELECT id FROM guild_events"))
post(fresh, "/api/guild/respond", {"guild": "Lifers", "accept": False})
check("turning an invitation down writes nothing in the guild",
      len(sql("SELECT id FROM guild_events")) == declined_before)
count = len(sql("SELECT id FROM guild_events"))
post(LEAD, "/api/guild/rank", {"username": "offi", "rank": "officer"})
check("promoting somebody to the rank they hold writes nothing",
      len(sql("SELECT id FROM guild_events")) == count)


# =============================================================================
print("\n--- L-1 who they are playing ---")
# =============================================================================
offi = member(LEAD, "offi")
check("a member's character comes with them",
      offi.get("character") == "Ysolde" and offi.get("class_id") == "mage" and offi.get("level", 0) >= 1, offi)
check("with the area they are in", offi.get("area", "") != "", offi)
nochar = member(LEAD, "nochar")
check("a member with no character gets nothing invented",
      nochar.get("character") == "" and nochar.get("class_id") == "" and nochar.get("level") == 0, nochar)

save(LEAD, 1, "tank", "Second")
sql("UPDATE saves SET updated_at = ? WHERE slot = 0 AND user_id = (SELECT id FROM users WHERE username = 'lead')",
    (int(time.time()) - 3600,))
sql("UPDATE saves SET updated_at = ? WHERE slot = 1 AND user_id = (SELECT id FROM users WHERE username = 'lead')",
    (int(time.time()),))
lead = member(OFFI, "lead")
check("the character shown is the one saved most recently", lead.get("character") == "Second"
      and lead.get("class_id") == "tank", lead)
sql("UPDATE saves SET updated_at = ? WHERE slot = 0 AND user_id = (SELECT id FROM users WHERE username = 'lead')",
    (int(time.time()) + 60,))
check("and follows the player back to the other one", member(OFFI, "lead").get("character") == "Kaelen",
      member(OFFI, "lead"))

client.put("/api/account/name-colour", headers=auth(MEMB), json={"hue": 275})
check("each member's chosen colour rides on the roster", member(LEAD, "memb").get("name_hue") == 275,
      member(LEAD, "memb"))
check("and 'never chose' is null", "name_hue" in nochar and nochar["name_hue"] is None, nochar)


# =============================================================================
print("\n--- L-2 continued: hand on, remove, leave ---")
# =============================================================================
post(LEAD, "/api/guild/rank", {"username": "offi", "rank": "leader"})
line = activity(LEAD)[0]
check("handing the guild on is its own line", line["kind"] == "leader" and line["actor"] == "lead"
      and line["target"] == "offi", line)
post(OFFI, "/api/guild/kick", {"username": "nochar"})
line = activity(OFFI)[0]
check("a removal names who did it and to whom", line["kind"] == "removed" and line["actor"] == "offi"
      and line["target"] == "nochar", line)
check("and the removed member no longer sees the guild's history", mine(NOCHAR).get("in_guild") is False
      and "guild" not in mine(NOCHAR))
post(MEMB, "/api/guild/leave", {})
check("leaving is a line in the leaver's name", activity(OFFI)[0]["kind"] == "left"
      and activity(OFFI)[0]["actor"] == "memb", activity(OFFI)[:1])


# =============================================================================
print("\n--- L-4 a staff rename, without the staff member's name ---")
# =============================================================================
staff_rename = client.post("/api/staff/guild", headers=auth(OWNER),
                           json={"guild": "Lifers", "action": "rename", "name": "Keepers",
                                 "reason": "test"})
check("staff can rename a guild", staff_rename.status_code == 200, staff_rename.get_json())
line = activity(OFFI)[0] if activity(OFFI) else {}
check("the guild's history says it was renamed, from what, to what",
      line.get("kind") == "renamed" and line.get("detail") == "Lifers" and line.get("target") == "Keepers", line)
check("and does not say which member of staff did it", line.get("actor") == "", line)
check("the moderation log still does",
      len(sql("SELECT id FROM staff_actions WHERE action = 'guild_rename' AND actor_name = 'lifeowner'")) == 1)


# =============================================================================
print("\n--- L-5 the guild's own, bounded, and gone with it ---")
# =============================================================================
theirs = [e["kind"] for e in activity(OUT)]
check("another guild's history is its own", theirs == ["founded"], theirs)
check("somebody in no guild is sent no history", "guild" not in mine(fresh))
check("the panel is sent a dozen, not the archive",
      len(activity(OFFI)) <= app_module.GUILD_ACTIVITY_SHOWN, len(activity(OFFI)))

keepers = sql("SELECT id FROM guilds WHERE name = 'Keepers'")[0][0]
with app_module.app.app_context():
    db = app_module.get_db()
    for i in range(app_module.GUILD_EVENTS_KEPT + 30):
        app_module.log_guild_event(db, keepers, "invited", "offi", "someone%d" % i)
    db.commit()
kept = sql("SELECT COUNT(*) FROM guild_events WHERE guild_id = ?", (keepers,))[0][0]
check("a guild keeps GUILD_EVENTS_KEPT lines and no more", kept == app_module.GUILD_EVENTS_KEPT, kept)
newest = sql("SELECT target FROM guild_events WHERE guild_id = ? ORDER BY id DESC LIMIT 1", (keepers,))[0][0]
check("and what it drops is the oldest", newest == "someone%d" % (app_module.GUILD_EVENTS_KEPT + 29), newest)
others_id = sql("SELECT id FROM guilds WHERE name = 'Others'")[0][0]
check("pruning one guild leaves another's history alone",
      sql("SELECT COUNT(*) FROM guild_events WHERE guild_id = ?", (others_id,))[0][0] == 1)

post(OFFI, "/api/guild/disband", {})
check("a disbanded guild's history goes with it",
      sql("SELECT COUNT(*) FROM guild_events WHERE guild_id = ?", (keepers,))[0][0] == 0)
post(OUT, "/api/guild/leave", {})
check("so does the history of a guild whose last member walked out",
      sql("SELECT COUNT(*) FROM guild_events WHERE guild_id = ?", (others_id,))[0][0] == 0)


# =============================================================================
print("\n--- L-6 every kind written is a kind the panel knows ---")
# =============================================================================
with open(os.path.join(HERE, "app.py"), encoding="utf-8") as fh:
    tree = ast.parse(fh.read())
written = set()
calls = 0
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "log_guild_event" and len(node.args) >= 3):
        calls += 1
        # A kind can be a conditional - "promoted" if up else "demoted" - so
        # every string in the argument counts.
        for part in ast.walk(node.args[2]):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                written.add(part.value)
declared = set(app_module.GUILD_EVENT_KINDS)
check("found the calls", calls >= 8, calls)
check("every kind written is in GUILD_EVENT_KINDS", written <= declared, sorted(written - declared))
check("and every kind listed is written somewhere", declared <= written, sorted(declared - written))


print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
