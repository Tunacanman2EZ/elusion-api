"""Revocation tests: what it costs to change your mind about somebody.

The school exercise asks for a JWT login, then a server-side session version,
then a comparison of "how instant revocation feels". This suite is the second
half done against the real API, plus the first half modelled locally so the
comparison produces numbers instead of an impression.

What it proves about this server:

  R-1  a ban ends every session at once, on every device
  R-2  a live token for a banned account is refused anyway, because
       "the sessions were deleted" is not a security control
  R-3  a DEMOTION needs no revocation at all - the rank is a JOIN, not a
       claim, so one UPDATE is effective on the very next request
  R-4  owner is not a row, so it can be neither forged nor demoted in band
  R-5  a kick ends sessions without banning, which is the sanction that had
       to exist once ban started deleting sessions

R-3 is the one the exercise does not ask for and the one that matters most. A
ban is loud: the player keeps playing and notices. A demotion is quiet - a
demoted dev goes on banning people, and the only trace is the audit log, read
afterwards.

Runs against a THROWAWAY database in the temp folder, exactly like test_api.py
and test_security.py, so it never touches elusion.db. Run: python test_revocation.py
"""

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_revocation_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
# The owner is the rank that comes from the environment rather than a row, and
# R-4 is about exactly that, so the suite needs one.
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


def register(username, password="password123"):
    """Create the account only. Deliberately does NOT log in.

    EVERY ACCOUNT THIS SUITE NEEDS IS REGISTERED BEFORE THE FIRST BAN, and that
    ordering is load-bearing. _ban_evasion_state() refuses REGISTRATION from an
    address a currently-banned account logged in from - the common pattern being
    banned, new account, same connection, same minute. The test client is always
    127.0.0.1, so banning anybody makes every later register() on this suite look
    exactly like evasion. The server is right and the test has to be arranged
    around it.
    """
    return client.post("/api/auth/register",
                       json={"username": username, "password": password}).status_code


def auth(username, password="password123"):
    """A fresh login, which is a fresh session row. Called repeatedly on purpose:
    one token is not a fair test of 'signed out everywhere'."""
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    token = (res.get_json() or {}).get("token")
    return {"Authorization": "Bearer " + token} if token else {}


def stray_session(username):
    """A second live session on one account, put in the table by hand.

    A second LOGIN can no longer make one: ONE LOGIN AT A TIME in app.py ends
    the account's other sessions whenever it signs in (test_accounts.py holds
    that). But "a ban ends every session" is a promise about the TABLE, not
    about how rows got there - a session from before that rule, a race, or a
    restored backup - so the rows are made directly and the sanctions still
    have to clear them all."""
    db = sqlite3.connect(DB_PATH)
    try:
        uid = db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        token = "stray-%s-%d" % (username, db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        db.execute("INSERT INTO sessions (token, user_id, expires_at, last_seen_at)"
                   " VALUES (?, ?, ?, ?)", (token, uid, int(time.time()) + 3600, int(time.time())))
        db.commit()
    finally:
        db.close()
    return {"Authorization": "Bearer " + token}


def live(headers):
    """Does this token still get served? /api/account is the cheapest authed route."""
    return client.get("/api/account", headers=headers).status_code


def sessions_for(username):
    db = sqlite3.connect(DB_PATH)
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE u.username = ?", (username,)
        ).fetchone()
        return row[0]
    finally:
        db.close()


# Everything registered up front - see the note in register().
for who in ("boss", "griefer", "helper", "noisy"):
    code = register(who)
    check("registered %s" % who, code == 201, code)

owner = auth("boss")

# =============================================================================
print("\n=== R-1  A BAN ENDS EVERY SESSION AT ONCE ===\n")
# =============================================================================
# Three logins, because the interesting property is not "the token I banned
# stopped working" - it is that the other two did as well, without anybody
# having to know they existed.

dev_a = auth("griefer")
dev_b = stray_session("griefer")
dev_c = stray_session("griefer")

# THREE: the login ended the session register handed back (one login at a
# time), and the other two are rows no login made - see stray_session().
check("a login plus two stray sessions is three live sessions",
      sessions_for("griefer") == 3, sessions_for("griefer"))
check("and all three are served", all(live(h) == 200 for h in (dev_a, dev_b, dev_c)))

res = client.post("/api/staff/ban", headers=owner,
                  json={"username": "griefer", "days": 7, "reason": "testing"})
check("the ban returns 200", res.status_code == 200, res.get_json())

check("every device is refused on its next request",
      all(live(h) == 401 for h in (dev_a, dev_b, dev_c)),
      "a session that survives a ban makes the ban a suggestion")
check("and no session row is left behind", sessions_for("griefer") == 0,
      sessions_for("griefer"))

# =============================================================================
print("\n=== R-2  A LIVE TOKEN FOR A BANNED ACCOUNT IS REFUSED ANYWAY ===\n")
# =============================================================================
# app.py says it plainly at user_for_token(): "Banning deletes the user's
# sessions in the same transaction, so a live token should not exist - but
# 'should not exist' is not a security control."
#
# So put one there. A row inserted straight into the table is what a bug, a race
# or a restored backup would leave, and the ban check has to hold on its own.

db = sqlite3.connect(DB_PATH)
uid = db.execute("SELECT id FROM users WHERE username = ?", ("griefer",)).fetchone()[0]
smuggled = "smuggled-token-r2"
db.execute(
    "INSERT INTO sessions (token, user_id, expires_at, last_seen_at) VALUES (?, ?, ?, ?)",
    (smuggled, uid, int(time.time()) + 3600, int(time.time())),
)
db.commit()
db.close()

check("a session row now exists that the ban did not delete",
      sessions_for("griefer") == 1, sessions_for("griefer"))
check("and it is STILL refused, on the ban check rather than the session check",
      live({"Authorization": "Bearer " + smuggled}) == 401,
      "the second gate is what makes the first one's failure survivable")

# =============================================================================
print("\n=== R-3  A DEMOTION NEEDS NO REVOCATION ===\n")
# =============================================================================
# The rank is not stored on the session and not signed into the token. It is
# read from users on every request, by the JOIN in user_for_token(). So the
# whole of "revoking dev" is one UPDATE, and the token the dev is already
# holding changes meaning under them between one request and the next.
#
# This is the case a JWT with a role claim cannot do: see the model at the end.

helper = auth("helper")

res = client.put("/api/staff/role", headers=owner, json={"username": "helper", "role": "dev"})
check("the owner can make a dev", res.status_code == 200, res.get_json())

# /api/staff/powers is the right oracle here, and it is the server's own idea:
# it reports what the CALLER may do, derived from the @require_role decorators
# rather than from a hand-kept list. So asking it is asking the live token who it
# currently is.
def whoami(headers):
    body = client.get("/api/staff/powers", headers=headers).get_json() or {}
    return body.get("you_are"), body.get("you_may_grant") or []

rank, grants = whoami(helper)
check("the dev's EXISTING token already reports dev, with no re-login",
      rank == "dev", "%s / %s" % (rank, grants))
check("and dev may grant mod", "mod" in grants, grants)

res = client.put("/api/staff/role", headers=owner, json={"username": "helper", "role": "mod"})
check("the owner can demote them again", res.status_code == 200, res.get_json())

rank, grants = whoami(helper)
check("the SAME token now reports mod, on the very next request",
      rank == "mod", "%s - a demotion that waits for a token to expire is not a demotion" % rank)
check("and it may no longer grant mod", "mod" not in grants, grants)

# A dev-only route refuses them now - and refuses with 404, not 403. app.py:
# "A 403 confirms the route exists and that you are not allowed to use it, which
# tells someone exactly where to push." Worth pinning, because a later refactor
# that "fixes" it to 403 would be a regression dressed as a correction.
gone = client.post("/api/staff/teleport", headers=helper,
                   json={"username": "helper", "area": "town"})
check("a dev-only route refuses the demoted token with 404, not 403",
      gone.status_code == 404 and gone.get_json().get("message") == "Not found.",
      "%d %s" % (gone.status_code, gone.get_json()))
check("no session was destroyed to achieve that", sessions_for("helper") >= 1,
      "the point of R-3 is that revocation was not needed at all")
check("and they are still signed in, because demotion is not removal",
      live(helper) == 200,
      "demoting somebody should not log them out of the game")

# =============================================================================
print("\n=== R-4  OWNER IS NOT A ROW ===\n")
# =============================================================================
# It comes from ELUSION_OWNER in the environment and is re-derived per request.
# That is what makes it unforgeable by tampering and unstaleable by caching -
# and also what means the API cannot change it. Both directions are asserted,
# because the second one is a real limitation and not an oversight.

res = client.put("/api/staff/role", headers=owner, json={"username": "helper", "role": "owner"})
check("owner cannot be granted through the API", res.status_code != 200,
      "%d - owner is environment state, so granting it in band would be a lie" % res.status_code)

res = client.put("/api/staff/role", headers=owner, json={"username": "boss", "role": "player"})
check("and the owner cannot demote themselves either", res.status_code != 200,
      "%d" % res.status_code)

mod_h = auth("helper")
res = client.post("/api/staff/ban", headers=mod_h, json={"username": "boss", "days": 1})
check("a mod cannot ban the owner", res.status_code in (403, 404),
      "%d" % res.status_code)
check("the owner is still served", live(owner) == 200)

# =============================================================================
print("\n=== R-5  A KICK IS THE SANCTION BAN MADE NECESSARY ===\n")
# =============================================================================
# Once ban deletes sessions, ban is the only way to get somebody out of the
# game - which makes the smallest response to "this account is behaving oddly"
# the largest one. Asserted mostly on what it does NOT do.

n_a, n_b = auth("noisy"), stray_session("noisy")
check("a login plus a stray session is two live sessions",
      sessions_for("noisy") == 2, sessions_for("noisy"))

res = client.post("/api/staff/kick", headers=owner, json={"username": "noisy", "reason": "quiet"})
check("the kick reports how many it ended",
      res.status_code == 200 and res.get_json().get("sessions_ended") == 2, res.get_json())
check("both devices are signed out", all(live(h) == 401 for h in (n_a, n_b)))
check("the account is NOT banned", sessions_for("noisy") == 0 and
      client.post("/api/auth/login",
                  json={"username": "noisy", "password": "password123"}).status_code == 200,
      "a kick that cannot be undone by logging in is a ban wearing a smaller word")

# =============================================================================
print("\n=== WHAT A JWT WOULD HAVE COST, MEASURED ===\n")
# =============================================================================
# The exercise's first half, modelled locally rather than bolted onto a server
# that does not need it. A real HS256 JWT - same header.payload.signature shape,
# genuine HMAC-SHA256, constant-time compare - driven by a fake clock, because
# waiting out a real expiry to prove a point is time nobody needs to spend.

JWT_SECRET = b"lesson-secret"
ROLES = ["player", "mod", "dev", "owner"]


def b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_dec(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwt_mint(claims):
    head = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, sort_keys=True,
                           separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(JWT_SECRET, (head + "." + body).encode(), hashlib.sha256).digest()
    return head + "." + body + "." + b64u(sig)


def jwt_verify(token, now):
    head, body, sig = token.split(".")
    expected = hmac.new(JWT_SECRET, (head + "." + body).encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, b64u_dec(sig)):
        return None
    claims = json.loads(b64u_dec(body))
    return None if claims.get("exp", 0) <= now else claims


check("the locally minted JWT verifies before expiry",
      jwt_verify(jwt_mint({"sub": "x", "exp": 100}), 50) is not None)
check("and is refused after it", jwt_verify(jwt_mint({"sub": "x", "exp": 100}), 150) is None)
forged = jwt_mint({"sub": "rogue", "role": "mod", "exp": 999})
head, body, sig = forged.split(".")
lifted = b64u(json.dumps({"sub": "rogue", "role": "dev", "exp": 999},
                         sort_keys=True, separators=(",", ":")).encode())
check("a self-promoted role claim fails the signature",
      jwt_verify(head + "." + lifted + "." + sig, 50) is None,
      "if this passes, the MAC is not doing anything")


def seconds_still_served(ttl, role_in_token):
    """Ban at t0, then count the seconds a JWT holder keeps acting."""
    now, banned, users = 1000, {"rogue"}, {"rogue": "mod"}
    tokens = [jwt_mint({"sub": "rogue", "role": "dev", "exp": now + ttl}) for _ in range(2)]
    served = 0
    for elapsed in range(ttl + 2):
        ok = False
        for t in tokens:
            claims = jwt_verify(t, now + elapsed)
            if claims is None or claims["sub"] in banned:
                continue
            role = claims["role"] if role_in_token else users.get(claims["sub"], "player")
            if ROLES.index(role) >= ROLES.index("dev"):
                ok = True
        if ok:
            served += 1
    return served


def demoted_still_dev(ttl, role_in_token):
    """Demote dev -> mod at t0, then count the seconds of dev powers that remain."""
    now, users = 1000, {"rogue": "dev"}
    tokens = [jwt_mint({"sub": "rogue", "role": "dev", "exp": now + ttl}) for _ in range(2)]
    users["rogue"] = "mod"
    held = 0
    for elapsed in range(ttl + 2):
        for t in tokens:
            claims = jwt_verify(t, now + elapsed)
            if claims is None:
                continue
            role = claims["role"] if role_in_token else users.get(claims["sub"], "player")
            if ROLES.index(role) >= ROLES.index("dev"):
                held += 1
                break
    return held


claim_60 = demoted_still_dev(60, True)
claim_900 = demoted_still_dev(900, True)
lookup_120 = demoted_still_dev(120, False)

check("a demoted dev keeps dev powers for the whole life of a role-claim JWT",
      claim_60 == 60 and claim_900 == 900,
      "60s token: %ds, 900s token: %ds" % (claim_60, claim_900))
check("only looking the role up per request closes it", lookup_120 == 0,
      "%ds" % lookup_120)
check("which is what this server already does, so R-3 measured 0 either way",
      lookup_120 == 0)

print("""
  seconds of dev power surviving a demotion
    role signed into a 60s JWT ............ %d s
    role signed into a 900s JWT ........... %d s
    role looked up per request ............. %d s
    Elusion sessions (measured in R-3) ..... 0 s

  Shortening the token shrinks the window; it never closes it. Closing it means
  reading the user's row on every request - which is the cost the JWT was chosen
  to avoid, and at that point it is a session token with a signature attached.
  Where a JWT earns its place is between services you own, on a 60-second pass:
  nobody revokes anything inside a minute, and the alternative is two services
  sharing an accounts database. That is the case TODO.md already names.
""" % (claim_60, claim_900, lookup_120))

# =============================================================================
print("=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failed:
    print("\n  failing checks:")
    for f in failures:
        print("    - " + f)
print("=" * 60)
sys.exit(1 if failed else 0)
