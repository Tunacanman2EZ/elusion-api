"""
test_clientbuild.py - the client build gate, and the ways it could lock you out.

    venv\\Scripts\\python.exe test_clientbuild.py

Runs against a THROWAWAY database in your temp folder, exactly like
test_maintenance.py. It never touches elusion.db.

WHAT THIS IS GUARDING, and it is not what the shape suggests. The gate is not a
security control - the build number is a header and headers are
client-controlled, so anyone who can type a curl command can claim any build.
What it protects is the PROTOCOL: a build that predates a wire change must be
told so, instead of half-working against a server that moved on.

So the failures worth testing are not "can it be bypassed" (it can, by design,
and the docstring on _refuse_outdated_client says so). They are:

  - does it stay OFF until somebody turns it on, so shipping it cannot hurt
  - can a refused client still find out WHY, or is it refused into silence
  - can the owner lock themselves out with one wrong number

The last one is the dangerous one, because the gate runs BEFORE authentication
- it has to, since refusing a protocol mismatch cannot depend on parsing a token
that build may not know how to send. That means no owner exemption is possible,
which means the guard has to be on the way in.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_clientbuild_test.db")

# Point the app at a scratch database BEFORE importing it - app.py calls
# init_db() at import time and must not touch the real one.
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Different case from the account registered below, on purpose: users.username
# is COLLATE NOCASE, and an owner check that disagreed about case would lock the
# owner out of their own server.
os.environ["ELUSION_OWNER"] = "BUILDOWNER"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

HEADER = app_module.CLIENT_BUILD_HEADER
CURRENT = app_module.CURRENT_CLIENT_BUILD


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


def auth(token, build=None):
    h = {"Authorization": "Bearer %s" % token}
    if build is not None:
        h[HEADER] = str(build)
    return h


def register(username, password="password123"):
    return client.post("/api/auth/register",
                       json={"username": username, "password": password})


def set_min(token, build, my_build=CURRENT):
    return client.post("/api/server/minbuild", json={"build": build},
                       headers=auth(token, my_build))


print("\n--- setup ---")


def token_from(res, who):
    """The token, or a named failure and a clean stop.

    SETUP IS WHERE THIS SUITE DIES MOST INTERESTINGLY, and it used to die
    badly. Sabotage the gate so it defaults to ARMED and registration answers
    426 - correct, and exactly the catastrophe the default exists to prevent -
    but the suite then did `res.get_json()["token"]` on a refusal body, raised
    a KeyError, and ended in a traceback with no verdict. Right exit code, and
    an output that reads as "the suite is broken" rather than "the server
    refuses everybody".

    The same defect restore_drill.py had, found the same way. A test suite gets
    to fail; it does not get to be unreadable while doing it."""
    body = res.get_json() or {}
    tok = body.get("token", "")
    if not tok:
        check("%s registers and gets a token" % who, False,
              "HTTP %d %s" % (res.status_code, body.get("message", "")))
        print("\n" + "=" * 70)
        print("  %d passed, %d failed" % (passed, failed))
        print("\n  Setup could not complete - nothing below this ran.")
        print("  A 426 here means the build gate is refusing every client,")
        print("  which is what min_client_build() defaulting to 0 prevents.")
        print("=" * 70)
        sys.exit(1)
    check("%s registers and gets a token" % who, res.status_code == 201,
          str(res.status_code))
    return tok


owner_token = token_from(register("buildowner"), "owner")
player_token = token_from(register("someplayer"), "player")


# =============================================================================
# IT SHIPS DISARMED, AND THAT IS THE WHOLE REASON IT CAN SHIP AT ALL
# =============================================================================
# The mechanism is what cannot be added later - every build released before the
# header exists is permanently unidentifiable. The ENFORCEMENT can be switched
# on at any moment. So the default has to be "refuses nobody", or shipping the
# door risks the thing the door exists to prevent.

print("\n--- disarmed by default ---")

res = client.get("/api/status")
body = res.get_json()
check("status reports the gate", "min_client_build" in body, sorted(body)[:8])
check("and it starts at zero", body.get("min_client_build") == 0,
      str(body.get("min_client_build")))
check("status also names the current build", body.get("current_client_build") == CURRENT,
      str(body.get("current_client_build")))

res = client.get("/api/auth/session", headers=auth(player_token))
check("a client sending NO build header is let in", res.status_code == 200,
      str(res.status_code))
res = client.get("/api/auth/session", headers=auth(player_token, 1))
check("and so is one that sends a build", res.status_code == 200, str(res.status_code))


# =============================================================================
# ARMED
# =============================================================================

print("\n--- armed at %d ---" % CURRENT)

res = set_min(owner_token, CURRENT)
check("the owner can arm it", res.status_code == 200, str(res.status_code))
check("and the answer says it is armed", res.get_json().get("armed") is True,
      str(res.get_json()))

res = client.get("/api/status")
check("status reflects the new minimum",
      res.get_json().get("min_client_build") == CURRENT,
      str(res.get_json().get("min_client_build")))

# NO HEADER SORTS BELOW EVERY REAL BUILD. Every build shipped before the header
# existed will never send one, so the absent case has to mean "ancient" and has
# to keep meaning it.
res = client.get("/api/auth/session", headers=auth(player_token))
check("a client with no build header is refused", res.status_code == 426,
      str(res.status_code))
check("with 426, not 403 - it is the protocol that is wrong, not the account",
      res.status_code == 426, str(res.status_code))

body = res.get_json()
check("the refusal names the minimum", body.get("min_build") == CURRENT,
      str(body.get("min_build")))
check("and names what the client said it was", body.get("your_build") == 0,
      str(body.get("your_build")))
check("and says what to do about it", "update" in body.get("message", "").lower(),
      body.get("message", ""))

res = client.get("/api/auth/session", headers=auth(player_token, CURRENT))
check("a current client still gets through", res.status_code == 200,
      str(res.status_code))


# =============================================================================
# REFUSED, BUT NOT INTO SILENCE
# =============================================================================
# THE ONE EXEMPTION THAT MATTERS. /api/status is how a client discovers it is
# too old. Gate that too and an outdated build is refused everywhere with no way
# to learn why - the player sees "something is broken" rather than "update the
# game", which is the difference between a support conversation and a patch.

print("\n--- a refused client can still find out why ---")

res = client.get("/api/status")
check("/api/status answers even while the gate is armed", res.status_code == 200,
      str(res.status_code))
check("and it carries the number that refused them",
      res.get_json().get("min_client_build") == CURRENT,
      str(res.get_json().get("min_client_build")))

# LOGIN IS NOT EXEMPT, deliberately. Being let in and then refused on every
# subsequent request is worse than being told at the door, and the login screen
# is the one place the client already knows how to display a refusal.
res = client.post("/api/auth/login",
                  json={"username": "someplayer", "password": "password123"})
check("login itself is refused, rather than failing later", res.status_code == 426,
      str(res.status_code))


# =============================================================================
# A MALFORMED HEADER IS AN OLD CLIENT, NOT A BAD REQUEST
# =============================================================================

print("\n--- headers that are not numbers ---")

for junk in ("banana", "1.0", "", "-4", "9999999999999999999999"):
    res = client.get("/api/auth/session",
                     headers={"Authorization": "Bearer %s" % player_token,
                              HEADER: junk})
    # "-4" and the huge one parse as ints; max(0, ...) floors the negative at 0
    # and the huge one is genuinely newer than the minimum, so it passes. Both
    # are correct: the gate asks "older than", not "plausible".
    expected = 200 if junk == "9999999999999999999999" else 426
    check("header %-24r -> %d" % (junk, expected), res.status_code == expected,
          str(res.status_code))


# =============================================================================
# THE LOCKOUT, WHICH IS THE ONLY WAY THIS FEATURE CAN HURT ANYBODY
# =============================================================================
# The gate runs before authentication - it must, because refusing a protocol
# mismatch cannot depend on parsing a token the refused build may not know how
# to send. So there is no owner exemption to build, and a minimum above every
# build that exists would refuse everyone including the person who set it.

print("\n--- the lockout guard ---")

res = set_min(owner_token, CURRENT + 1)
check("a minimum newer than any client that exists is refused",
      res.status_code == 400, str(res.status_code))
check("and the refusal explains the consequence",
      "including you" in res.get_json().get("message", ""),
      res.get_json().get("message", "")[:90])

res = set_min(owner_token, -1)
check("a negative minimum is refused", res.status_code == 400, str(res.status_code))

res = client.post("/api/server/minbuild", json={}, headers=auth(owner_token, CURRENT))
check("an empty body is refused rather than read as zero", res.status_code == 400,
      str(res.status_code))

# THE WAY BACK IN, tested rather than promised. The gate is bypassable by
# forging the header - that is stated plainly in its docstring - and that
# limitation IS the recovery: an owner shut out by their own switch can send any
# build number by hand and disarm it, with no shell on the box.
res = client.post("/api/server/minbuild", json={"build": 0},
                  headers={"Authorization": "Bearer %s" % owner_token,
                           HEADER: "999999"})
check("an owner can disarm it by forging the header - the documented way back",
      res.status_code == 200, str(res.status_code))
check("and the gate is off afterwards",
      client.get("/api/status").get_json().get("min_client_build") == 0,
      str(client.get("/api/status").get_json().get("min_client_build")))

res = client.get("/api/auth/session", headers=auth(player_token))
check("so the old client is let back in", res.status_code == 200, str(res.status_code))


# =============================================================================
# ONLY THE OWNER, AND THE REFUSAL DOES NOT CONFIRM THE ROUTE
# =============================================================================

print("\n--- the owner gate ---")

res = set_min(player_token, CURRENT)
check("a player setting the minimum gets 404, not 403",
      res.status_code == 404, str(res.status_code))
check("the gate stayed off",
      client.get("/api/status").get_json().get("min_client_build") == 0,
      str(client.get("/api/status").get_json().get("min_client_build")))


# =============================================================================
# IT IS READ, NOT REMEMBERED
# =============================================================================
# The same property the PvP switch needed: an owner may have set this from
# another machine, and a process that cached the number at boot would enforce
# yesterday's answer.

print("\n--- read per request ---")

def write_setting_behind_the_process(value):
    """Change the row the way another worker (or another machine) would.

    INSIDE AN app_context BECAUSE get_db() LIVES ON `g`, which is per-request.
    Calling it from module scope raises "Working outside of application
    context" - which is what this block did on its first run, and is a test bug
    rather than a finding. Recorded because the traceback points at app.py and
    reads like one."""
    with app_module.app.app_context():
        app_module.set_server_setting(app_module.MIN_BUILD_KEY, value, "test")
        app_module.get_db().commit()


set_min(owner_token, CURRENT)
check("armed again", client.get("/api/status").get_json().get("min_client_build") == CURRENT)

write_setting_behind_the_process('{"build": 0}')
res = client.get("/api/auth/session", headers=auth(player_token))
check("a change written underneath the process takes effect on the next request",
      res.status_code == 200, str(res.status_code))

# A CORRUPT ROW MUST NOT LOCK EVERYONE OUT. This fails toward DISARMED, the
# opposite direction from the maintenance switch, because the failure it could
# cause is the exact failure the gate exists to make survivable.
write_setting_behind_the_process("not json at all")
res = client.get("/api/auth/session", headers=auth(player_token))
check("an unreadable setting disarms rather than locks out",
      res.status_code == 200, str(res.status_code))

write_setting_behind_the_process('{"build": "banana"}')
res = client.get("/api/auth/session", headers=auth(player_token))
check("and so does a setting with a non-number in it",
      res.status_code == 200, str(res.status_code))


print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
