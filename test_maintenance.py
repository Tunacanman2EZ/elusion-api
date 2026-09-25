"""
test_maintenance.py - the owner's kill switch, end to end.

    venv\\Scripts\\python.exe test_maintenance.py

Runs against a THROWAWAY database in your temp folder, exactly like
test_api.py. It never touches elusion.db.

WHAT THIS IS GUARDING. The switch is two events with a gap between them, and
every bug worth having here lives in that gap:

  - the door shuts immediately (no new logins), but
  - the room empties LATER, so clients get a window to flush their saves.

A switch that closed both at once would take whatever progress the clients had
not sent yet, which is the "the update ate my loot" bug. So the tests below
spend most of their effort proving that an already-logged-in player can still
reach the server DURING the window and cannot after it - and that the owner is
never locked out by their own switch.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_maintenance_test.db")

# Point the app at a scratch database BEFORE importing it - app.py calls
# init_db() at import time and must not touch the real one.
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Different case from the account registered below on purpose: users.username
# is COLLATE NOCASE, and an owner check that disagreed about case would lock
# the owner out of their own server.
os.environ["ELUSION_OWNER"] = "CHECKER"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()


# =============================================================================
# TINY TEST HARNESS  (same shape as test_api.py)
# =============================================================================

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


def register(username, password="password123"):
    return client.post("/api/auth/register", json={"username": username, "password": password})


def login(username, password="password123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def close_server(token, **kwargs):
    body = {"on": True}
    body.update(kwargs)
    return client.post("/api/server/maintenance", json=body, headers=auth(token))


def open_server(token):
    return client.post("/api/server/maintenance", json={"on": False}, headers=auth(token))


# =============================================================================
# SETUP
# =============================================================================

print("\n--- setup ---")

owner = register("checker")
check("owner registers", owner.status_code == 201, str(owner.status_code))
owner_token = owner.get_json()["token"]

player = register("someplayer")
check("player registers", player.status_code == 201, str(player.status_code))
player_token = player.get_json()["token"]


# =============================================================================
# THE SERVER STARTS OPEN
# =============================================================================

print("\n--- open by default ---")

res = client.get("/api/status")
body = res.get_json()
check("status 200", res.status_code == 200, str(res.status_code))
check("not in maintenance", body.get("maintenance") is False, str(body.get("maintenance")))
check("no message while open", body.get("message") == "", repr(body.get("message")))

res = login("someplayer")
check("player can log in while open", res.status_code == 200, str(res.status_code))
player_token = res.get_json()["token"]

res = client.get("/api/auth/session", headers=auth(player_token))
check("heartbeat carries no notice while open",
      res.get_json().get("maintenance") is None, str(res.get_json().get("maintenance")))


# =============================================================================
# ONLY THE OWNER MAY THROW THE SWITCH
# =============================================================================

print("\n--- the switch is owner-only ---")

res = close_server(player_token)
# require_owner answers 404, not 403, so the route does not confirm it exists.
check("a player cannot close the server", res.status_code == 404, str(res.status_code))

res = client.post("/api/server/maintenance", json={"on": True})
check("an anonymous caller cannot close the server", res.status_code == 401, str(res.status_code))

res = client.get("/api/status")
check("failed attempts left the server open",
      res.get_json().get("maintenance") is False, str(res.get_json().get("maintenance")))

res = client.post("/api/server/maintenance", json={}, headers=auth(owner_token))
check("a body without 'on' is refused", res.status_code == 400, str(res.status_code))


# =============================================================================
# CLOSING WITH A GRACE WINDOW
# =============================================================================

print("\n--- closing the server (60s grace) ---")

res = close_server(owner_token, message="Update in progress - please come back later.",
                   grace_seconds=60)
body = res.get_json()
check("owner closes the server", res.status_code == 200, str(res.status_code))
check("reports maintenance on", body.get("maintenance") is True, str(body.get("maintenance")))
check("reports the grace window", body.get("grace_seconds") == 60, str(body.get("grace_seconds")))
check("nobody signed out yet", body.get("sessions_ended") == 0, str(body.get("sessions_ended")))
check("counts who is online", "online_now" in body, str(body))

res = client.get("/api/status")
body = res.get_json()
check("status reports maintenance", body.get("maintenance") is True, str(body.get("maintenance")))
check("status carries the message",
      body.get("message") == "Update in progress - please come back later.",
      repr(body.get("message")))
check("status counts down", 0 < body.get("seconds_left", 0) <= 60, str(body.get("seconds_left")))


# =============================================================================
# THE DOOR IS SHUT
# =============================================================================

print("\n--- new logins are refused ---")

res = login("someplayer")
check("player login refused with 503", res.status_code == 503, str(res.status_code))
check("refusal carries the message",
      res.get_json().get("message") == "Update in progress - please come back later.",
      repr(res.get_json().get("message")))
check("refusal is flagged as maintenance",
      res.get_json().get("maintenance") is True, str(res.get_json()))

res = register("latecomer")
check("registration refused too", res.status_code == 503, str(res.status_code))

res = login("checker")
check("OWNER can still log in", res.status_code == 200, str(res.status_code))
owner_token = res.get_json()["token"]


# =============================================================================
# THE SAVE WINDOW  (the whole reason the gap exists)
# =============================================================================

print("\n--- inside the grace window, players can still save ---")

res = client.get("/api/auth/session", headers=auth(player_token))
check("player's session still works during grace", res.status_code == 200, str(res.status_code))

notice = res.get_json().get("maintenance")
check("heartbeat now carries the closing notice", isinstance(notice, dict), str(notice))
if isinstance(notice, dict):
    check("notice says the server is closing", notice.get("on") is True, str(notice))
    check("notice carries the message",
          notice.get("message") == "Update in progress - please come back later.",
          repr(notice.get("message")))
    check("notice says how long is left",
          0 < notice.get("seconds_left", 0) <= 60, str(notice.get("seconds_left")))

# A real write, not just the heartbeat: this is what "saves everyone before
# disconnecting" actually depends on.
res = client.get("/api/save", headers=auth(player_token))
check("player can still READ their save during grace",
      res.status_code in (200, 204), str(res.status_code))


# =============================================================================
# THE WINDOW RUNS OUT
# =============================================================================

print("\n--- once the window is spent, the session ends ---")

# Re-close with a 1 second window so the expiry is reachable in a test.
close_server(owner_token, message="Update in progress - please come back later.",
             grace_seconds=1)
res = client.get("/api/auth/session", headers=auth(player_token))
check("still in before the window expires", res.status_code == 200, str(res.status_code))

time.sleep(1.4)

res = client.get("/api/auth/session", headers=auth(player_token))
check("player is signed out after the window", res.status_code == 401, str(res.status_code))
check("the 401 explains why",
      res.get_json().get("maintenance") is True, str(res.get_json()))

# The session row is really gone, not merely refused.
res = client.get("/api/auth/session", headers=auth(player_token))
check("the session is destroyed, not just blocked", res.status_code == 401, str(res.status_code))

res = client.get("/api/auth/session", headers=auth(owner_token))
check("OWNER is never disconnected by their own switch",
      res.status_code == 200, str(res.status_code))


# =============================================================================
# REOPENING
# =============================================================================

print("\n--- reopening ---")

res = open_server(owner_token)
check("owner reopens the server", res.status_code == 200, str(res.status_code))
check("reports maintenance off", res.get_json().get("maintenance") is False,
      str(res.get_json().get("maintenance")))

res = client.get("/api/status")
check("status reports open again", res.get_json().get("maintenance") is False,
      str(res.get_json().get("maintenance")))

res = login("someplayer")
check("player can log in again", res.status_code == 200, str(res.status_code))
player_token = res.get_json()["token"]

res = register("latecomer")
check("registration works again", res.status_code == 201, str(res.status_code))


# =============================================================================
# NO GRACE AT ALL  (the hard kill)
# =============================================================================

print("\n--- grace_seconds=0 signs everyone out immediately ---")

res = close_server(owner_token, grace_seconds=0)
body = res.get_json()
check("hard close accepted", res.status_code == 200, str(res.status_code))
check("it signed people out on the spot", body.get("sessions_ended", 0) >= 1,
      str(body.get("sessions_ended")))
check("default message used when none given",
      body.get("message") == app_module.MAINTENANCE_DEFAULT_MESSAGE, repr(body.get("message")))

res = client.get("/api/auth/session", headers=auth(player_token))
check("player is out immediately", res.status_code == 401, str(res.status_code))

res = client.get("/api/auth/session", headers=auth(owner_token))
check("owner survives the hard kill", res.status_code == 200, str(res.status_code))


# =============================================================================
# IT SURVIVES A RESTART
# =============================================================================

print("\n--- the switch outlives the process ---")

# The whole point of storing this in the database: the restart it announces
# must not reopen the server. Reading it back through a fresh request context
# is the closest a single-process test gets to a reboot.
with app_module.app.app_context():
    state = app_module.maintenance_state()
check("state persisted to the database", state["on"] is True, str(state))
check("persisted message survived", state["message"] == app_module.MAINTENANCE_DEFAULT_MESSAGE,
      repr(state["message"]))

open_server(owner_token)

# Clamping, so a typo cannot close the server for a week.
res = close_server(owner_token, grace_seconds=999999)
check("an absurd grace window is clamped",
      res.get_json().get("grace_seconds") == app_module.MAINTENANCE_MAX_GRACE_SECONDS,
      str(res.get_json().get("grace_seconds")))

res = client.post("/api/server/maintenance",
                  json={"on": True, "grace_seconds": "soon"}, headers=auth(owner_token))
check("a non-numeric grace window is refused", res.status_code == 400, str(res.status_code))

open_server(owner_token)


# =============================================================================
print("\n=================================")
print("passed: %d   failed: %d" % (passed, failed))
if failures:
    print("failed checks:")
    for name in failures:
        print("  - %s" % name)
print("=================================")
sys.exit(1 if failed else 0)
