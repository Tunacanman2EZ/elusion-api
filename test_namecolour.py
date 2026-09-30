"""Name colour tests: the colour a player chose, on every name the server sends.

The colour used to be a local setting. It was drawn over the chooser's own
head and nowhere else - chat, the friends list, the players menu and the guild
roster all painted names by rank - and for staff the Options slider did
nothing at all, because staff names were locked to their rank colour. Rank is
a badge now (the owner's crown, MOD, DEV), so the colour is everybody's to
choose and the server carries it.

  N-1  the route stores a hue 0-359 and refuses anything else, including the
       JSON shapes a client can send by accident (a bool, a string)
  N-2  it is YOUR colour: the route can only ever change the caller's row
  N-3  login and the session heartbeat hand it back, so a second machine
       draws the name the first one chose
  N-4  staff choose too - the old rule is gone
  N-5  a chat line keeps the colour it was said in (a snapshot, like its rank
       and guild), and a new line takes the new one
  N-6  the friends list, the players menu and the guild roster carry it
  N-7  "never chose" is null, not a number - the client owns the default

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_namecolour.py
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_namecolour_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "hueowner"

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


def register(name):
    res = client.post("/api/auth/register", json={"username": name, "password": "password123"})
    body = res.get_json() or {}
    return {"Authorization": "Bearer " + body.get("token", "")}, body


def set_hue(headers, hue):
    res = client.put("/api/account/name-colour", headers=headers, json={"hue": hue})
    return res.status_code, (res.get_json() or {})


def refill(*names):
    # Chat is rate-limited per account; the suite talks more than a person would.
    conn = sqlite3.connect(DB_PATH)
    for name in names:
        conn.execute("UPDATE users SET chat_tokens = ?, last_chat_at = 0 WHERE username = ?",
                     (app_module.CHAT_BUCKET_CAPACITY, name))
    conn.commit()
    conn.close()


def stored(name):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute("SELECT name_hue FROM users WHERE username = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


ANN, ann_body = register("ann")
BEN, _ = register("ben")
OWNER, _ = register("hueowner")
MOD, _ = register("huemod")
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE users SET role = 'mod' WHERE username = 'huemod'")
conn.commit()
conn.close()


# =============================================================================
print("\n--- N-7 never chose is null ---")
# =============================================================================
check("a new account comes back with no colour chosen",
      "name_hue" in ann_body and ann_body["name_hue"] is None, ann_body.get("name_hue", "missing"))
check("and the column is NULL, not a copy of the client's default", stored("ann") is None)
session = client.get("/api/auth/session", headers=ANN).get_json() or {}
check("the heartbeat says the same", "name_hue" in session and session["name_hue"] is None, session)


# =============================================================================
print("\n--- N-1 the route ---")
# =============================================================================
code, body = set_hue(ANN, 200)
check("a hue is stored", code == 200 and body.get("name_hue") == 200 and stored("ann") == 200, body)
for label, value in [
    ("one past the wheel", 360),
    ("a negative hue", -1),
    ("a word", "blue"),
    ("a boolean dressed as a number", True),
    ("nothing at all", None),
]:
    code, _ = set_hue(ANN, value)
    check("%s is a 400" % label, code == 400, code)
check("and a refused one changes nothing", stored("ann") == 200, stored("ann"))
res = client.put("/api/account/name-colour", headers=ANN, json={})
check("a body with no hue is a 400", res.status_code == 400, res.status_code)
code, body = set_hue(ANN, 45.0)
check("a whole number sent as a float is accepted - Godot sends every number as one",
      code == 200 and body.get("name_hue") == 45 and stored("ann") == 45, body)
check("both ends of the wheel", set_hue(ANN, 0)[0] == 200 and set_hue(ANN, 359)[0] == 200)
res = client.put("/api/account/name-colour", json={"hue": 10})
check("it needs a login", res.status_code == 401, res.status_code)


# =============================================================================
print("\n--- N-2 it is your colour ---")
# =============================================================================
set_hue(BEN, 90)
set_hue(ANN, 300)
check("setting yours leaves everybody else's alone", stored("ben") == 90, stored("ben"))
res = client.put("/api/account/name-colour", headers=ANN, json={"hue": 10, "username": "ben"})
check("and naming somebody else in the body does not reach them",
      res.status_code == 200 and stored("ben") == 90 and stored("ann") == 10, (stored("ann"), stored("ben")))


# =============================================================================
print("\n--- N-3 it comes back with the login ---")
# =============================================================================
login = client.post("/api/auth/login", json={"username": "ann", "password": "password123"}).get_json() or {}
check("a login hands the colour back, for a second machine", login.get("name_hue") == 10, login.get("name_hue"))
session = client.get("/api/auth/session", headers=ANN).get_json() or {}
check("and so does the heartbeat", session.get("name_hue") == 10, session.get("name_hue"))


# =============================================================================
print("\n--- N-4 staff choose too ---")
# =============================================================================
check("the owner can set theirs", set_hue(OWNER, 210)[0] == 200 and stored("hueowner") == 210)
check("and so can a mod", set_hue(MOD, 140)[0] == 200 and stored("huemod") == 140)
owner_session = client.get("/api/auth/session", headers=OWNER).get_json() or {}
check("the owner is still the owner - rank did not ride on the colour",
      owner_session.get("role") == "owner" and owner_session.get("name_hue") == 210, owner_session)


# =============================================================================
print("\n--- N-5 a chat line keeps the colour it was said in ---")
# =============================================================================
refill("ann")
set_hue(ANN, 120)
sent = client.post("/api/chat/send", headers=ANN, json={"body": "green words", "channel": "world"})
check("the send answer carries the colour the line was said in",
      sent.status_code == 200 and (sent.get_json() or {}).get("name_hue") == 120, sent.get_json())
set_hue(ANN, 300)
refill("ann")
client.post("/api/chat/send", headers=ANN, json={"body": "violet words", "channel": "world"})
lines = (client.get("/api/chat?channel=world", headers=BEN).get_json() or {}).get("messages", [])
by_body = {line["body"]: line for line in lines}
check("an old line keeps the colour it was said in",
      by_body.get("green words", {}).get("name_hue") == 120, by_body.get("green words"))
check("and a new one takes the new colour",
      by_body.get("violet words", {}).get("name_hue") == 300, by_body.get("violet words"))
refill("ben")
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE users SET name_hue = NULL WHERE username = 'ben'")
conn.commit()
conn.close()
client.post("/api/chat/send", headers=BEN, json={"body": "default words", "channel": "world"})
lines = (client.get("/api/chat?channel=world", headers=ANN).get_json() or {}).get("messages", [])
plain = [line for line in lines if line["body"] == "default words"]
check("a line from somebody who never chose says so with null",
      plain and "name_hue" in plain[0] and plain[0]["name_hue"] is None, plain)


# =============================================================================
print("\n--- N-6 the lists carry it ---")
# =============================================================================
client.post("/api/friends/request", headers=ANN, json={"username": "hueowner"})
client.post("/api/friends/respond", headers=OWNER, json={"username": "ann", "accept": True})
friends = (client.get("/api/friends", headers=OWNER).get_json() or {}).get("friends", [])
ann_row = next((f for f in friends if f["username"] == "ann"), {})
check("the friends list draws a friend in their colour", ann_row.get("name_hue") == 300, ann_row)

# The players menu lists people with a character; give ann one and a heartbeat.
client.put("/api/save", headers=ANN, json={"slot": 0, "class_id": "warrior", "name": "Annie"})
client.get("/api/server/broadcasts", headers=ANN)
online = (client.get("/api/players/online", headers=BEN).get_json() or {}).get("players", [])
ann_row = next((p for p in online if p["username"] == "ann"), {})
check("the players menu too", ann_row.get("name_hue") == 300, ann_row)


print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
