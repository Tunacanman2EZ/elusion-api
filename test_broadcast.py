"""
test_broadcast.py - the server's voice, end to end.

    venv\\Scripts\\python.exe test_broadcast.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db.

WHAT THIS IS GUARDING. Broadcasts exist so the kill switch can reach people who
are already playing - the login screen only reaches the ones who are not. So
the checks that matter most are not "can the owner type a message", they are:

  - only the owner can speak, and everyone logged in can listen
  - the cursor never repeats or drops a message
  - closing the server SAYS SO, with the countdown, without being asked
  - the poll is also the heartbeat: it 401s the moment a session dies

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_broadcast_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "CHECKER"

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


def say(token, body, kind=None):
    payload = {"body": body}
    if kind is not None:
        payload["kind"] = kind
    return client.post("/api/server/broadcast", json=payload, headers=auth(token))


def listen(token, since=None):
    url = "/api/server/broadcasts"
    if since is not None:
        url += "?since=%s" % since
    return client.get(url, headers=auth(token))


print("\n--- setup ---")
owner_token = client.post("/api/auth/register",
                          json={"username": "checker", "password": "password123"}).get_json()["token"]
player_token = client.post("/api/auth/register",
                           json={"username": "someplayer", "password": "password123"}).get_json()["token"]
check("accounts created", bool(owner_token) and bool(player_token))


# =============================================================================
print("\n--- only the owner may speak ---")

res = say(player_token, "hello everyone")
check("a player cannot broadcast", res.status_code == 404, str(res.status_code))

res = client.post("/api/server/broadcast", json={"body": "hi"})
check("an anonymous caller cannot broadcast", res.status_code == 401, str(res.status_code))

res = say(owner_token, "   ")
check("an empty message is refused", res.status_code == 400, str(res.status_code))

res = say(owner_token, "hi", kind="banner")
check("an unknown kind is refused", res.status_code == 400, str(res.status_code))


# =============================================================================
print("\n--- speaking and listening ---")

res = say(owner_token, "Server maintenance at 9pm tonight.")
check("owner broadcasts", res.status_code == 200, str(res.status_code))
first_id = res.get_json().get("id")
check("it comes back with an id", isinstance(first_id, int) and first_id > 0, str(first_id))

res = listen(player_token)
body = res.get_json()
check("a player can listen", res.status_code == 200, str(res.status_code))
check("the message is there",
      any(m["body"] == "Server maintenance at 9pm tonight." for m in body["messages"]),
      str(body["messages"]))
check("latest_id matches", body.get("latest_id") == first_id, str(body.get("latest_id")))
check("it is marked as system",
      body["messages"][-1]["kind"] == "system", str(body["messages"][-1]))


# =============================================================================
print("\n--- the cursor never repeats or drops ---")

res = listen(player_token, since=first_id)
check("nothing new yet", res.get_json()["messages"] == [], str(res.get_json()["messages"]))

say(owner_token, "one")
say(owner_token, "two")
say(owner_token, "three")

res = listen(player_token, since=first_id)
bodies = [m["body"] for m in res.get_json()["messages"]]
check("exactly the three new ones, in order", bodies == ["one", "two", "three"], str(bodies))

cursor = res.get_json()["latest_id"]
res = listen(player_token, since=cursor)
check("cursor is now caught up", res.get_json()["messages"] == [], str(res.get_json()["messages"]))

res = listen(player_token, since="soon")
check("a nonsense cursor is refused", res.status_code == 400, str(res.status_code))

# Two messages inside the same second must both survive - this is the reason
# the cursor is an id and not a timestamp.
say(owner_token, "same-second A")
say(owner_token, "same-second B")
res = listen(player_token, since=cursor)
bodies = [m["body"] for m in res.get_json()["messages"]]
check("two messages in one second both survive",
      bodies == ["same-second A", "same-second B"], str(bodies))
cursor = res.get_json()["latest_id"]


# =============================================================================
print("\n--- length is capped ---")

res = say(owner_token, "x" * 500)
check("an overlong message is accepted but trimmed",
      res.status_code == 200 and len(res.get_json()["body"]) == app_module.MAX_BROADCAST_LENGTH,
      str(len(res.get_json().get("body", ""))))
cursor = listen(player_token, since=cursor).get_json()["latest_id"]


# =============================================================================
print("\n--- the kill switch announces itself ---")

res = client.post("/api/server/maintenance",
                  json={"on": True, "message": "Patching the boss fight.", "grace_seconds": 60},
                  headers=auth(owner_token))
check("server closed", res.status_code == 200, str(res.status_code))

res = listen(player_token, since=cursor)
said = [m["body"] for m in res.get_json()["messages"]]
check("closing posted a notice without being asked", len(said) == 1, str(said))
check("the notice carries the owner's message",
      "Patching the boss fight." in said[0], str(said))
check("the notice carries the countdown", "60s" in said[0], str(said))
check("the notice promises the save", "progress is being saved" in said[0], str(said))
cursor = res.get_json()["latest_id"]

check("the poll also carries the maintenance state",
      isinstance(res.get_json().get("maintenance"), dict), str(res.get_json().get("maintenance")))

res = client.post("/api/server/maintenance", json={"on": False}, headers=auth(owner_token))
check("server reopened", res.status_code == 200, str(res.status_code))

res = listen(player_token, since=cursor)
said = [m["body"] for m in res.get_json()["messages"]]
check("reopening says so too", said == ["The server is open again."], str(said))
cursor = res.get_json()["latest_id"]


# =============================================================================
print("\n--- the poll is also the heartbeat ---")

client.post("/api/server/maintenance",
            json={"on": True, "grace_seconds": 1}, headers=auth(owner_token))

res = listen(player_token, since=cursor)
check("still listening during the save window", res.status_code == 200, str(res.status_code))

time.sleep(1.4)

res = listen(player_token, since=cursor)
check("the poll 401s once the window is spent", res.status_code == 401, str(res.status_code))
check("and explains why", res.get_json().get("maintenance") is True, str(res.get_json()))

res = listen(owner_token, since=cursor)
check("the owner keeps listening", res.status_code == 200, str(res.status_code))

client.post("/api/server/maintenance", json={"on": False}, headers=auth(owner_token))


# =============================================================================
print("\n--- a newcomer gets the tail, not the archive ---")

res = listen(player_token if False else owner_token)
body = res.get_json()
check("since=0 returns at most one page",
      len(body["messages"]) <= app_module.BROADCAST_PAGE_LIMIT, str(len(body["messages"])))
check("and they are in oldest-first order",
      [m["id"] for m in body["messages"]] == sorted(m["id"] for m in body["messages"]),
      str([m["id"] for m in body["messages"]]))


# =============================================================================
print("\n=================================")
print("passed: %d   failed: %d" % (passed, failed))
if failures:
    print("failed checks:")
    for name in failures:
        print("  - %s" % name)
print("=================================")
sys.exit(1 if failed else 0)
