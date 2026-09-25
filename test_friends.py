"""
test_friends.py - asking, answering, listing and ending a friendship.

    venv\\Scripts\\python.exe test_friends.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db.

WHAT THIS IS GUARDING. A friends list is a standing permission to watch when
somebody is online, so the checks that matter are about consent and about the
row never being able to disagree with itself:

  - nobody joins your list without answering a request
  - a decline leaves nothing behind, and tells the asker nothing
  - two people who add each other at the same moment end up friends, not
    deadlocked
  - removing clears BOTH directions, whichever way round it was made
  - the caps hold, and say whose list is full

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_friends_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "FRIENDOWNER"

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
                       json={"username": name, "password": "password123"}
                       ).get_json()["token"]


def ask(token, name):
    return client.post("/api/friends/request", json={"username": name},
                       headers=auth(token))


def answer(token, name, accept):
    return client.post("/api/friends/respond",
                       json={"username": name, "accept": accept},
                       headers=auth(token))


def drop(token, name):
    return client.post("/api/friends/remove", json={"username": name},
                       headers=auth(token))


def listing(token):
    return client.get("/api/friends", headers=auth(token)).get_json()


def names(entries):
    return sorted(e["username"] for e in entries)


def wipe():
    """Clear every link, so each section starts from a known board."""
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("DELETE FROM friends")
        db.commit()


print("\n--- setup ---")
ann_token = register("ann")
ben_token = register("ben")
cal_token = register("cal")
check("accounts created", all([ann_token, ben_token, cal_token]))

empty = listing(ann_token)
check("a new account has no friends", empty["friends"] == [], empty["friends"])
check("no requests waiting", empty["incoming"] == [] and empty["outgoing"] == [])
check("the server clock comes with it", int(empty.get("now", 0)) > 0)
check("and the caps are stated",
      empty["limits"]["friends"] == app_module.MAX_FRIENDS, empty["limits"])


print("\n--- asking ---")
res = ask(ann_token, "ben")
check("a request is accepted", res.status_code == 200, res.status_code)
check("and comes back pending", res.get_json()["state"] == "pending", res.get_json())

mine = listing(ann_token)
theirs = listing(ben_token)
check("the asker sees it as outgoing", names(mine["outgoing"]) == ["ben"], mine)
check("and not as a friend yet", mine["friends"] == [], mine)
check("the other side sees it as incoming", names(theirs["incoming"]) == ["ann"], theirs)
check("and not as a friend yet", theirs["friends"] == [], theirs)
check("nobody else sees anything", listing(cal_token)["incoming"] == [])

check("asking twice is a conflict", ask(ann_token, "ben").status_code == 409)
check("asking yourself is refused", ask(ann_token, "ann").status_code == 400)
check("asking a name that does not exist is a 404",
      ask(ann_token, "nobodyhere").status_code == 404)
check("asking with no name at all is a 400",
      client.post("/api/friends/request", json={}, headers=auth(ann_token)).status_code == 400)
check("the name is matched however it is typed",
      ask(cal_token, "ANN").status_code == 200)
wipe()


print("\n--- answering ---")
ask(ann_token, "ben")
check("the asker cannot answer their own request",
      answer(ann_token, "ben", True).status_code == 404)

res = answer(ben_token, "ann", True)
check("the person asked can accept", res.status_code == 200, res.status_code)
check("and it comes back accepted", res.get_json()["state"] == "accepted")

mine = listing(ann_token)
theirs = listing(ben_token)
check("both sides now list each other",
      names(mine["friends"]) == ["ben"] and names(theirs["friends"]) == ["ann"],
      (mine["friends"], theirs["friends"]))
check("the request is no longer outgoing", mine["outgoing"] == [])
check("nor incoming", theirs["incoming"] == [])
check("a friend entry carries their rank", mine["friends"][0]["role"] == "player")
check("and when they were last seen", "last_seen_at" in mine["friends"][0])
check("and when the friendship started", int(mine["friends"][0]["since"]) > 0)

check("asking an existing friend is a conflict", ask(ann_token, "ben").status_code == 409)
check("accepting twice is a 404", answer(ben_token, "ann", True).status_code == 404)
wipe()

ask(ann_token, "ben")
res = answer(ben_token, "ann", False)
check("declining is accepted", res.status_code == 200, res.status_code)
check("and says so", res.get_json()["state"] == "declined")
mine = listing(ann_token)
theirs = listing(ben_token)
check("a decline leaves the asker nothing",
      mine["outgoing"] == [] and mine["friends"] == [], mine)
check("and leaves no trace on the other side",
      theirs["incoming"] == [] and theirs["friends"] == [], theirs)
with app_module.app.app_context():
    rows = app_module.get_db().execute("SELECT COUNT(*) AS n FROM friends").fetchone()["n"]
check("no row is kept recording the refusal", rows == 0, rows)
check("and they can ask again later", ask(ann_token, "ben").status_code == 200)
wipe()


print("\n--- two people asking at once ---")
ask(ann_token, "ben")
res = ask(ben_token, "ann")
check("the second request is taken as the answer",
      res.status_code == 200 and res.get_json()["state"] == "accepted", res.get_json())
check("and they are friends, not deadlocked",
      names(listing(ann_token)["friends"]) == ["ben"], listing(ann_token))
check("with nothing left pending either way",
      listing(ann_token)["outgoing"] == [] and listing(ben_token)["incoming"] == [])
wipe()


print("\n--- ending it ---")
ask(ann_token, "ben")
answer(ben_token, "ann", True)
res = drop(ann_token, "ben")
check("a friend can be removed", res.status_code == 200, res.status_code)
check("the remover's list is empty", listing(ann_token)["friends"] == [])
check("AND SO IS THE OTHER SIDE'S", listing(ben_token)["friends"] == [],
      listing(ben_token)["friends"])
check("removing again is a 404", drop(ann_token, "ben").status_code == 404)

# THE OTHER DIRECTION. The row is stored the way it was asked, so removing has
# to find it from whichever end calls - this is the case that a naive
# single-direction DELETE passes the test above and still gets wrong.
ask(ann_token, "ben")
answer(ben_token, "ann", True)
check("and it works from the other end too", drop(ben_token, "ann").status_code == 200)
check("leaving nothing behind",
      listing(ann_token)["friends"] == [] and listing(ben_token)["friends"] == [])

ask(ann_token, "ben")
check("removing while still pending withdraws the request",
      drop(ann_token, "ben").status_code == 200)
check("the other side stops seeing it", listing(ben_token)["incoming"] == [])
check("removing someone you have no link to is a 404",
      drop(ann_token, "cal").status_code == 404)
check("removing yourself is refused", drop(ann_token, "ann").status_code == 400)
wipe()


print("\n--- online status ---")
ask(ann_token, "ben")
answer(ben_token, "ann", True)
seen = listing(ann_token)["friends"][0]
# LOGGING IN IS PRESENCE. new_session() stamps last_seen_at at creation, so a
# friend who signed in and has not beaten yet is genuinely there - this is not
# the heartbeat leaking, it is the login being the first beat.
check("somebody who just signed in reads as online", seen["online"] is True, seen)

# A session that has never been stamped at all, which is what a row created
# before the presence migration looks like.
with app_module.app.app_context():
    db = app_module.get_db()
    db.execute("UPDATE sessions SET last_seen_at = 0 WHERE user_id ="
               " (SELECT id FROM users WHERE username = 'ben')")
    db.commit()
seen = listing(ann_token)["friends"][0]
check("a session nobody has vouched for reads as offline", seen["online"] is False, seen)
check("and reports no last-seen time at all", int(seen["last_seen_at"]) == 0, seen)

# The heartbeat is what stamps presence from then on, so call it as the client
# would.
client.get("/api/auth/session", headers=auth(ben_token))
seen = listing(ann_token)["friends"][0]
check("after a heartbeat they read as online again", seen["online"] is True, seen)
check("and the timestamp comes with it", int(seen["last_seen_at"]) > 0, seen)

with app_module.app.app_context():
    db = app_module.get_db()
    stale = int(time.time()) - app_module.ONLINE_WINDOW_SECONDS - 10
    db.execute("UPDATE sessions SET last_seen_at = ? WHERE user_id ="
               " (SELECT id FROM users WHERE username = 'ben')", (stale,))
    db.commit()
seen = listing(ann_token)["friends"][0]
check("a stale heartbeat reads as offline again", seen["online"] is False, seen)
check("but still remembers when", int(seen["last_seen_at"]) == stale, seen)


print("\n--- the caps ---")
wipe()
real_pending = app_module.MAX_PENDING_SENT
real_friends = app_module.MAX_FRIENDS
app_module.MAX_PENDING_SENT = 2
extras = [register("spare%d" % n) for n in range(4)]
check("four spare accounts exist", all(extras))

check("first request fits", ask(ann_token, "spare0").status_code == 200)
check("second request fits", ask(ann_token, "spare1").status_code == 200)
res = ask(ann_token, "spare2")
check("the third is refused at the cap", res.status_code == 400, res.status_code)
check("and the refusal says why",
      "limit" in res.get_json()["message"], res.get_json())
# THE CAP IS ON WHAT IS OUTSTANDING, not on how many you may ever send. One of
# the two being answered - even with a no - has to free the slot, or a player
# who asked two people who never log in again can never ask anybody else.
declined = answer(extras[0], "ann", False)
check("answering one frees a slot", declined.status_code == 200, declined.status_code)
check("and then another can be sent", ask(ann_token, "spare2").status_code == 200)

app_module.MAX_PENDING_SENT = real_pending
wipe()

app_module.MAX_FRIENDS = 1
ask(ann_token, "ben")
answer(ben_token, "ann", True)
res = ask(ann_token, "cal")
check("a full list refuses a new request", res.status_code == 400, res.status_code)
check("and says it is yours that is full",
      "your friends list is full" in res.get_json()["message"], res.get_json())

res = ask(cal_token, "ann")
check("and somebody asking a full account is told so too",
      res.status_code == 400, res.status_code)
check("naming whose list it is",
      "their friends list is full" in res.get_json()["message"], res.get_json())
app_module.MAX_FRIENDS = real_friends
wipe()


print("\n--- nothing without a token ---")
check("listing needs one", client.get("/api/friends").status_code == 401)
check("asking needs one",
      client.post("/api/friends/request", json={"username": "ben"}).status_code == 401)
check("answering needs one",
      client.post("/api/friends/respond",
                  json={"username": "ann", "accept": True}).status_code == 401)
check("removing needs one",
      client.post("/api/friends/remove", json={"username": "ben"}).status_code == 401)
check("a made-up token is refused",
      client.get("/api/friends", headers=auth("nope")).status_code == 401)


print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
