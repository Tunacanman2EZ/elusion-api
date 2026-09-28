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
print("\n--- every message carries the moment it was sent ---")
#
# THE CLIENT WAS THROWING THIS AWAY, which is the bug this section exists for.
# The poll has always returned `at` per message and characterhud.gd read only
# `body` and `kind` - so a notice put on screen was stamped, if at all, with
# the moment this client happened to RECEIVE it. That is correct exactly once:
# for a player who was already logged in. Everybody who arrives afterwards is
# handed the tail of the table and shown a week of history as though all of it
# had just happened.
#
# A TIMESTAMP GENERATED ON RECEIPT IS NOT A TIMESTAMP. It is a measure of when
# the reader turned up.

before = int(time.time())
res = say(owner_token, "The east gate is open.")
sent_id = res.get_json().get("id")
after = int(time.time())

# A REAL GAP BETWEEN SENDING AND READING, and without it this section does not
# test anything. The first version read the message back in the same second it
# was sent, so a server stamping `at` with the clock at READ time passed every
# check here - the two moments were equal. The sabotage is the point: replace
# created_at with time.time() in the route and this suite must go red.
time.sleep(1.1)

body = listen(owner_token, since=sent_id - 1).get_json()
mine = [m for m in body["messages"] if m["id"] == sent_id]
check("the message came back", len(mine) == 1, str(body["messages"]))
stamped = mine[0] if mine else {}
check("it carries an at", "at" in stamped, str(sorted(stamped.keys())))
check("the at is a whole number of seconds",
      isinstance(stamped.get("at"), int), repr(stamped.get("at")))
check("and it is the moment it was SENT, not the moment it was read",
      before <= int(stamped.get("at", 0)) <= after,
      "%r not in [%d, %d]" % (stamped.get("at"), before, after))

check("every message on the page carries one",
      all(isinstance(m.get("at"), int) and m["at"] > 0 for m in body["messages"]),
      str([(m["id"], m.get("at")) for m in body["messages"] if not m.get("at")]))


# =============================================================================
print("\n--- the poll's other job: saying you are still here ---")
#
# "ONLINE" EVERYWHERE IN THIS SERVER means sessions.last_seen_at inside
# ONLINE_WINDOW_SECONDS - the friends list, the guild roster,
# /api/players/online, /api/players/nearby and the staff panel all read it.
#
# THE STAMP USED TO LIVE ONLY IN GET /api/auth/session, AND NOTHING CALLED THAT
# ON A TIMER. api.gd declares HEARTBEAT_SECONDS = 15 and characterhud.gd's
# comment says the client beats on it; the only caller of Api.heartbeat() in
# the project is the 401 handler. So the stamp happened once, at login, and
# forty-five seconds later every presence surface said the player was offline.
#
# It was visible in-game: a guild panel telling the only member of a guild
# "0 online of 1" while they were sitting there reading it.

presence = client.post("/api/auth/register",
                       json={"username": "stillhere",
                             "password": "password123"}).get_json()["token"]


def seen_age():
    """How old this account's newest stamp is, in seconds."""
    conn = __import__("sqlite3").connect(DB_PATH)
    row = conn.execute(
        "SELECT MAX(s.last_seen_at) FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE u.username = ?", ("stillhere",)).fetchone()
    conn.close()
    return int(time.time()) - int(row[0] or 0)


check("registering leaves a fresh stamp", seen_age() <= 2, seen_age())

# Age it, exactly as forty-five quiet seconds would.
conn = __import__("sqlite3").connect(DB_PATH)
conn.execute("UPDATE sessions SET last_seen_at = ?", (int(time.time()) - 300,))
conn.commit()
conn.close()
check("and it can go stale", seen_age() >= 300, seen_age())

listen(presence)
check("ONE POLL MAKES IT FRESH AGAIN", seen_age() <= 2, seen_age())

# THE CLIENT POLLS FASTER THAN THE WINDOW CLOSES, and that relationship is the
# whole mechanism. If BROADCAST_POLL_SECONDS ever exceeds ONLINE_WINDOW_SECONDS
# a player goes dark between their own beats - which is what "0 online of 1"
# looked like. The client half of this is checked in the Godot suite, which can
# read the real constant; here the window is asserted to be roomy enough for a
# poll every ten seconds to keep anybody lit.
check("the online window fits several polls",
      app_module.ONLINE_WINDOW_SECONDS >= 30, app_module.ONLINE_WINDOW_SECONDS)

# AND IT IS NOT EXTENDING THE LOGIN. A beat proves the client is running, not
# that the credentials are fresher - a thirty-day session that renewed itself
# every ten seconds would never expire at all.
conn = __import__("sqlite3").connect(DB_PATH)
expiry_before = conn.execute(
    "SELECT MAX(s.expires_at) FROM sessions s JOIN users u ON u.id = s.user_id"
    " WHERE u.username = ?", ("stillhere",)).fetchone()[0]
conn.close()
listen(presence)
conn = __import__("sqlite3").connect(DB_PATH)
expiry_after = conn.execute(
    "SELECT MAX(s.expires_at) FROM sessions s JOIN users u ON u.id = s.user_id"
    " WHERE u.username = ?", ("stillhere",)).fetchone()[0]
conn.close()
check("a beat does not extend expires_at", expiry_before == expiry_after,
      "%r -> %r" % (expiry_before, expiry_after))

# A MALFORMED POLL IS ANSWERED, NOT RECORDED. The stamp sits after the
# validation on purpose, so a 400 is not evidence that anybody is playing.
conn = __import__("sqlite3").connect(DB_PATH)
conn.execute("UPDATE sessions SET last_seen_at = ?", (int(time.time()) - 300,))
conn.commit()
conn.close()
res = client.get("/api/server/broadcasts?since=banana", headers=auth(presence))
check("a bad since is still a 400", res.status_code == 400, str(res.status_code))
check("and it left no beat behind", seen_age() >= 300, seen_age())


# =============================================================================
print("\n--- the pvp switch, and when it was thrown ---")

# A FRESH SESSION, NOT player_token. The heartbeat section above deliberately
# kills that one, so reusing it here would test the 401 and not the 404 - and
# the difference between those two is the whole refusal rule in CLAUDE.md.
bystander = client.post("/api/auth/register",
                        json={"username": "pvpbystander",
                              "password": "password123"}).get_json()["token"]
res = client.post("/api/server/pvp", json={"on": True}, headers=auth(bystander))
check("a player cannot switch pvp on, and is not told the route exists",
      res.status_code == 404, str(res.status_code))

poll = listen(owner_token).get_json()
check("pvp reads off before anyone touches it", poll.get("pvp") is False, str(poll.get("pvp")))
check("and pvp_at is 0, not missing",
      poll.get("pvp_at") == 0, repr(poll.get("pvp_at")))

thrown = int(time.time())
res = client.post("/api/server/pvp", json={"on": True}, headers=auth(owner_token))
check("the owner switches pvp on", res.status_code == 200, str(res.status_code))

poll = listen(owner_token).get_json()
check("the poll says pvp is on", poll.get("pvp") is True, str(poll.get("pvp")))
check("pvp_at is when the switch was thrown",
      thrown <= int(poll.get("pvp_at", 0)) <= int(time.time()),
      "%r vs %d" % (poll.get("pvp_at"), thrown))

# THE ASSERTION THE WHOLE THING TURNS ON. Polling again does not move it - it
# is the moment of a TRANSITION, like a death, not a fact about this request.
# Reading the clock here instead of the row is the same class of mistake as
# stamping a notice on receipt, and it would make "PvP is ON since 14:32" mean
# "you polled at 14:32".
was = int(poll.get("pvp_at", 0))
time.sleep(1.1)
poll = listen(owner_token).get_json()
check("polling again does not move pvp_at",
      int(poll.get("pvp_at", 0)) == was, "%r vs %d" % (poll.get("pvp_at"), was))

check("switching it on announced it",
      any("hostile" in m["body"] for m in poll["messages"] + listen(owner_token, since=0).get_json()["messages"]),
      str([m["body"] for m in listen(owner_token, since=0).get_json()["messages"][-3:]]))

client.post("/api/server/pvp", json={"on": False}, headers=auth(owner_token))
poll = listen(owner_token).get_json()
check("switching it off turns pvp off", poll.get("pvp") is False, str(poll.get("pvp")))
check("and moves pvp_at forward, because that is a transition too",
      int(poll.get("pvp_at", 0)) >= was, "%r vs %d" % (poll.get("pvp_at"), was))


# =============================================================================
print("\n--- your own guild rides the poll ---")
# =============================================================================
# THE NAMEPLATE NEEDS A FACT THAT IS TRUE RIGHT NOW. GET /api/guild is the only
# other route that says which guild you are in, and nothing calls it on a timer
# - so a tag fed from there would appear when a panel happened to be opened and
# then go on being whatever it was. That is the shape this project keeps finding
# and keeps having to fix: a display fed by something nobody schedules.

poll = listen(owner_token).get_json()
check("somebody in no guild gets an empty string, not a null",
      poll.get("guild") == "" and poll.get("guild_tag") == "",
      "%r / %r" % (poll.get("guild"), poll.get("guild_tag")))

# THE GUILD IS INSERTED DIRECTLY, and that is the right call for THIS suite.
# Founding one through the route needs a character, a purse and the whole
# payment path, all of which test_guilds.py already exercises in 161 checks.
# What is under test here is whether the POLL reports a membership, so the
# membership is arranged in the cheapest honest way and the poll is asked.
with app_module.app.app_context():
    db = app_module.get_db()
    uid = db.execute("SELECT id FROM users WHERE username = 'checker'").fetchone()["id"]
    db.execute("INSERT INTO guilds (name, folded, founded_by, created_at)"
               " VALUES (?, ?, ?, ?)",
               ("The Crowned", "the crowned", "checker", int(time.time())))
    gid = db.execute("SELECT id FROM guilds WHERE folded = 'the crowned'").fetchone()["id"]
    db.execute("INSERT INTO guild_members (user_id, guild_id, rank, joined_at)"
               " VALUES (?, ?, 'leader', ?)", (uid, gid, int(time.time())))
    db.commit()

poll = listen(owner_token).get_json()
check("the poll now names the guild", poll.get("guild") == "The Crowned",
      repr(poll.get("guild")))
check("and carries the tag the client draws",
      poll.get("guild_tag") == "THE CROWNED", repr(poll.get("guild_tag")))

# THE TAG IS THE SERVER'S, NOT THE CLIENT'S. guild_tag() uppercases and bounds
# at MAX_GUILD_NAME, and asserting that here is what stops the bound quietly
# moving to the side that can be edited by whoever holds the client.
check("the tag is never longer than the cap",
      len(poll.get("guild_tag", "")) <= app_module.MAX_GUILD_NAME,
      poll.get("guild_tag"))

# SOMEBODY ELSE'S GUILD IS NOT YOURS. One row per caller, and a poll that
# answered with any guild at all would put a stranger's tag over your head.
# A FRESH ACCOUNT, because player_token was revoked by the revocation section
# above and a 401 has no "guild" key at all - so reusing it would have made this
# check pass for the wrong reason the moment the field was spelled differently.
# It failed loudly instead, which is the only reason that was noticed.
bystander = client.post("/api/auth/register",
                        json={"username": "bystander", "password": "password123"}
                        ).get_json()["token"]
other = listen(bystander).get_json()
check("another player's poll still says no guild",
      other.get("guild") == "" and other.get("guild_tag") == "",
      "%r / %r" % (other.get("guild"), other.get("guild_tag")))

# AND LEAVING REACHES THE PLAYER. Being kicked, or a mod disbanding the guild,
# has to clear the plate - otherwise the tag outlives the membership and the
# one person who cannot see that is the one wearing it.
client.post("/api/guild/disband", headers=auth(owner_token), json={})
poll = listen(owner_token).get_json()
check("disbanding clears it on the very next poll",
      poll.get("guild") == "" and poll.get("guild_tag") == "",
      "%r / %r" % (poll.get("guild"), poll.get("guild_tag")))


# =============================================================================
print("\n=================================")
print("passed: %d   failed: %d" % (passed, failed))
if failures:
    print("failed checks:")
    for name in failures:
        print("  - %s" % name)
print("=================================")
sys.exit(1 if failed else 0)
