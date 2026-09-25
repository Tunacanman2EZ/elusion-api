"""
test_chat.py - world chat, end to end.

    venv\\Scripts\\python.exe test_chat.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db.

WHAT THIS IS GUARDING. A public channel is the first thing in this game that
one player can use to reach every other player, so the checks that matter are
not "can somebody type a line". They are:

  - the cursor never repeats a message and never drops one
  - the flood bucket actually stops a flood, and never stops a conversation
  - a message records who said it AT THE TIME, so a promotion cannot rewrite
    history and a rename cannot orphan it
  - only staff can take a line down, and not one from above them
  - nothing about any of it leaks to someone without a token

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_chat_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "CHATOWNER"

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
    res = client.post("/api/auth/register",
                      json={"username": name, "password": "password123"})
    return res.get_json()["token"]


def say(token, body):
    return client.post("/api/chat/send", json={"body": body}, headers=auth(token))


def read(token, since=None):
    url = "/api/chat"
    if since is not None:
        url += "?since=%s" % since
    return client.get(url, headers=auth(token))


def refill(token_owner_name):
    """
    Hand an account a full bucket again.

    The suite sends far more messages than a person would, and the throttle is
    the point of several of these checks - so it has to be resettable without
    the tests sleeping through it.
    """
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("UPDATE users SET chat_tokens = ?, last_chat_at = 0"
                   " WHERE username = ?",
                   (app_module.CHAT_BUCKET_CAPACITY, token_owner_name))
        db.commit()


print("\n--- setup ---")
owner_token = register("chatowner")
alice_token = register("alice")
bob_token = register("bob")
check("accounts created", all([owner_token, alice_token, bob_token]))


print("\n--- saying something ---")
res = say(alice_token, "hello world")
check("a message posts", res.status_code == 200, res.status_code)
body = res.get_json() if res.status_code == 200 else {}
check("the reply names the author", body.get("by") == "alice", body)
check("and their rank", body.get("role") == "player", body)
check("and gives it an id", int(body.get("id", 0)) > 0, body)

res = read(bob_token)
feed = res.get_json()
check("somebody else can read it", res.status_code == 200, res.status_code)
check("the message is in the feed",
      any(m["body"] == "hello world" for m in feed["messages"]), feed)
check("carrying the author", feed["messages"][-1]["by"] == "alice")
check("and a server clock", int(feed.get("now", 0)) > 0, feed)
check("and the newest id", int(feed.get("latest_id", 0)) > 0, feed)

check("an empty message is refused", say(alice_token, "   ").status_code == 400)
check("and so is a missing one",
      client.post("/api/chat/send", json={}, headers=auth(alice_token)).status_code == 400)

long_line = "x" * 400
say(alice_token, long_line)
newest = read(bob_token).get_json()["messages"][-1]
check("an over-long message is cut to the cap",
      len(newest["body"]) == app_module.MAX_CHAT_LENGTH, len(newest["body"]))


print("\n--- the cursor never repeats and never drops ---")
refill("alice")
start = read(bob_token).get_json()["latest_id"]
for n in range(5):
    say(alice_token, "line %d" % n)

page = read(bob_token, since=start).get_json()
bodies = [m["body"] for m in page["messages"]]
check("everything after the cursor arrives", bodies == ["line 0", "line 1", "line 2",
                                                        "line 3", "line 4"], bodies)
check("and nothing before it does", all(m["id"] > start for m in page["messages"]))

again = read(bob_token, since=page["latest_id"]).get_json()
check("polling again returns nothing new", again["messages"] == [], again["messages"])
check("but still reports the newest id", again["latest_id"] == page["latest_id"])

ids = [m["id"] for m in page["messages"]]
check("ids are strictly increasing", ids == sorted(ids) and len(set(ids)) == len(ids), ids)

check("a first poll gets the tail, not the whole table",
      len(read(bob_token).get_json()["messages"]) <= app_module.CHAT_PAGE_LIMIT)
check("a junk cursor is a 400, not a crash",
      read(bob_token, since="soon").status_code == 400)
check("a negative cursor is treated as the start",
      read(bob_token, since=-5).status_code == 200)


print("\n--- the flood bucket ---")
refill("bob")
sent = 0
for n in range(int(app_module.CHAT_BUCKET_CAPACITY)):
    if say(bob_token, "burst %d" % n).status_code == 200:
        sent += 1
check("a full bucket sends a whole burst",
      sent == int(app_module.CHAT_BUCKET_CAPACITY), sent)

blocked = say(bob_token, "one too many")
check("and the next one is refused", blocked.status_code == 429, blocked.status_code)
check("with a Retry-After a client can obey",
      int(blocked.headers.get("Retry-After", 0)) >= 1, dict(blocked.headers))
check("as a 429, not a 400 - nothing was wrong with the message",
      blocked.get_json()["error"] == "Too Many Requests")

before = read(alice_token).get_json()["latest_id"]
say(bob_token, "this must not be stored")
after = read(alice_token).get_json()
check("a refused message is not written anyway", after["latest_id"] == before,
      (before, after["latest_id"]))
check("and does not appear in the feed",
      not any(m["body"] == "this must not be stored" for m in after["messages"]))

# THE BUCKET REFILLS. Proved by handing the row a stale timestamp rather than
# by sleeping: the refill is arithmetic on last_chat_at, so moving that back is
# indistinguishable from waiting, and the suite stays fast.
with app_module.app.app_context():
    db = app_module.get_db()
    db.execute("UPDATE users SET last_chat_at = ? WHERE username = 'bob'",
               (int((time.time() - 30) * 1000),))
    db.commit()
check("waiting refills it", say(bob_token, "back again").status_code == 200)

check("one account's flood does not throttle another",
      say(alice_token, "alice is fine").status_code == 200)


print("\n--- who said it is recorded, not looked up ---")
refill("alice")
say(alice_token, "said as a player")
with app_module.app.app_context():
    db = app_module.get_db()
    db.execute("UPDATE users SET role = 'dev' WHERE username = 'alice'")
    db.commit()
feed = read(bob_token).get_json()["messages"]
old_line = [m for m in feed if m["body"] == "said as a player"][-1]
check("promoting somebody does not re-badge what they already said",
      old_line["role"] == "player", old_line)
refill("alice")
say(alice_token, "said as a dev")
fresh = read(bob_token).get_json()["messages"][-1]
check("but the next thing they say carries the new rank",
      fresh["role"] == "dev", fresh)


print("\n--- taking a line back down ---")
refill("bob")
target = say(bob_token, "delete me").get_json()["id"]
res = client.post("/api/chat/delete", json={"id": target}, headers=auth(alice_token))
check("a dev can delete a player's message", res.status_code == 200, res.status_code)
check("and it leaves the feed",
      not any(m["id"] == target for m in read(bob_token).get_json()["messages"]))

refill("bob")
mine = say(bob_token, "player trying to moderate").get_json()["id"]
res = client.post("/api/chat/delete", json={"id": mine}, headers=auth(bob_token))
check("a player cannot delete anything", res.status_code == 404, res.status_code)
check("not even their own line", res.status_code == 404)
check("and the message survives",
      any(m["id"] == mine for m in read(bob_token).get_json()["messages"]))
check("the refusal is a 404, so the route does not confirm it exists",
      res.get_json()["error"] == "Not Found")

refill("chatowner")
owners_line = say(owner_token, "the owner speaks").get_json()["id"]
res = client.post("/api/chat/delete", json={"id": owners_line}, headers=auth(alice_token))
check("a dev cannot delete the owner's message", res.status_code == 404, res.status_code)
check("and it survives",
      any(m["id"] == owners_line for m in read(bob_token).get_json()["messages"]))
check("the owner can delete it themselves",
      client.post("/api/chat/delete", json={"id": owners_line},
                  headers=auth(owner_token)).status_code == 200)

# THE OWNER CAN TAKE DOWN ANYTHING, which is the whole point of the x the
# client now draws beside every line. can_act_on() is strictly-greater, so
# "anyone below me" covers every rank there is, and their own line is allowed
# by the explicit exception beside it.
for _who in ("bob", "alice", "chatowner"):
    refill(_who)
for whose_token, whose_name in [(bob_token, "a player"), (alice_token, "a dev"),
                                (owner_token, "their own")]:
    for _who in ("bob", "alice", "chatowner"):
        refill(_who)
    line_id = say(whose_token, "something to remove").get_json()["id"]
    check("the owner can delete %s message" % whose_name,
          client.post("/api/chat/delete", json={"id": line_id},
                      headers=auth(owner_token)).status_code == 200)
    check("and %s line really goes" % whose_name,
          not any(m["id"] == line_id
                  for m in read(bob_token).get_json()["messages"]))

check("deleting nothing is a 404",
      client.post("/api/chat/delete", json={"id": 999999},
                  headers=auth(owner_token)).status_code == 404)
check("a junk id is a 400",
      client.post("/api/chat/delete", json={"id": "soon"},
                  headers=auth(owner_token)).status_code == 400)
check("and so is a missing one",
      client.post("/api/chat/delete", json={}, headers=auth(owner_token)).status_code == 400)


print("\n--- old talk is pruned, not kept ---")
with app_module.app.app_context():
    db = app_module.get_db()
    stale = int(time.time()) - app_module.CHAT_RETENTION_SECONDS - 60
    db.execute("INSERT INTO chat_messages (user_id, username, role, body, created_at)"
               " VALUES (1, 'ghost', 'player', 'from last week', ?)", (stale,))
    db.commit()
    kept = db.execute("SELECT COUNT(*) AS n FROM chat_messages"
                      " WHERE body = 'from last week'").fetchone()["n"]
check("a stale line can be planted", kept == 1, kept)
refill("alice")
say(alice_token, "a fresh line")
with app_module.app.app_context():
    db = app_module.get_db()
    left = db.execute("SELECT COUNT(*) AS n FROM chat_messages"
                      " WHERE body = 'from last week'").fetchone()["n"]
check("and writing prunes it away", left == 0, left)


print("\n--- nothing without a token ---")
check("sending needs one", client.post("/api/chat/send",
                                       json={"body": "hi"}).status_code == 401)
check("reading needs one", client.get("/api/chat").status_code == 401)
check("deleting needs one", client.post("/api/chat/delete",
                                        json={"id": 1}).status_code == 401)
check("a made-up token is refused",
      client.get("/api/chat", headers=auth("not-a-real-token")).status_code == 401)


print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
