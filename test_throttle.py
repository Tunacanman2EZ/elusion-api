# test_throttle.py - the login endpoint's defences.
#
# SEPARATE FROM test_api.py ON PURPOSE. That suite proves endpoints return the
# right data; this one proves the login endpoint REFUSES to. They fail for
# different reasons and get read at different times - "did I break the API" is
# a question you ask on every change, "is brute force still blocked" is one you
# ask after touching auth.
#
# Run it the same way:
#   .\venv\Scripts\python.exe test_throttle.py
#
# It builds its own database from scratch and never touches elusion.db.

import os
import sys
import sqlite3
import tempfile

DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_throttle_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "owneraccount"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as app_module                                    # noqa: E402

client = app_module.app.test_client()

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


def login(username, password, ip="203.0.113.7"):
    """A login attempt from a named address.

    FIXTURES MUST BE VALID CREDENTIALS, or validate_credentials() answers 400
    and the request never reaches the throttle at all - the test then passes or
    fails on the validator instead of the thing it names. Usernames are
    [A-Za-z0-9_]{3,20} (no hyphens) and passwords are 8+ characters.

    environ_base is how REMOTE_ADDR gets set on a test request - without it
    every call looks like it came from the same place, which is exactly the
    condition this file exists to test and therefore the one thing it must be
    able to vary.
    """
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        environ_base={"REMOTE_ADDR": ip},
    )


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


REAL_PASSWORD = "CorrectHorse9"

section("SETUP")
r = client.post(
    "/api/auth/register",
    json={"username": "realuser", "password": REAL_PASSWORD},
    environ_base={"REMOTE_ADDR": "192.0.2.10"},
)
check("a real account exists to attack", r.status_code == 201, r.get_json())


section("PER-ACCOUNT LOCKOUT - vertical brute force")
# Many passwords against ONE account. LOGIN_MAX_ATTEMPTS consecutive misses
# freeze it, and the freeze is checked BEFORE the password, so guessing right
# on the next attempt does not get you in.
for i in range(app_module.LOGIN_MAX_ATTEMPTS):
    r = login("realuser", "wrongpass%d" % i, ip="198.51.100.1")
check("misses below the ceiling stay 401", r.status_code == 401, r.status_code)

r = login("realuser", "wrongagain1", ip="198.51.100.1")
check("crossing LOGIN_MAX_ATTEMPTS -> 429", r.status_code == 429, r.get_json())

r = login("realuser", REAL_PASSWORD, ip="198.51.100.1")
check("the CORRECT password is refused while locked", r.status_code == 429,
      "a lockout you can guess your way out of is not a lockout")


section("PER-IP THROTTLE - horizontal spray")
# The attack the per-account counter cannot see: one password, many usernames.
# No single account ever reaches its own ceiling.
SPRAYER = "198.51.100.99"
# The gate reads the window BEFORE this attempt is recorded, so the ceiling
# is what you are ALLOWED - the attempt after it is the one refused. Same
# shape as LOGIN_MAX_ATTEMPTS above.
codes = [login("victim%d" % i, "password123", ip=SPRAYER).status_code
         for i in range(app_module.IP_MAX_USERNAMES)]
check("a spray below the ceiling still answers 401",
      codes == [401] * len(codes), codes)
check("and never reveals which usernames exist",
      len(set(codes)) == 1, "every answer must be identical")

r = login("victimfinal", "password123", ip=SPRAYER)
check("crossing IP_MAX_USERNAMES -> 429", r.status_code == 429, r.get_json())

r = login("realuser", REAL_PASSWORD, ip=SPRAYER)
check("a locked address cannot log in even correctly", r.status_code == 429,
      r.status_code)


section("THE THROTTLE MUST NOT BECOME THE OUTAGE")
# The failure mode that matters more than the attack: if one attacker could
# lock out everyone else, the defence would be the denial of service.
#
# A SECOND ACCOUNT, not realuser - realuser is still frozen by the per-account
# section above, so asking it here would re-test that lockout and call the
# answer an IP result. The two throttles are independent and the test has to
# keep them independent too.
r = client.post(
    "/api/auth/register",
    json={"username": "bystander", "password": REAL_PASSWORD},
    environ_base={"REMOTE_ADDR": "192.0.2.77"},
)
check("an uninvolved account exists", r.status_code == 201, r.get_json())

r = login("bystander", REAL_PASSWORD, ip="192.0.2.77")
check("an unrelated address is completely unaffected",
      r.status_code == 200, r.get_json())
token = r.get_json()["token"]


section("THE LOG")
rows = db().execute("SELECT username, ip, ok, reason FROM login_attempts").fetchall()
check("attempts are recorded", len(rows) > 0, len(rows))

reasons = {row["reason"] for row in rows}
check("a miss on a real account is distinguishable", "bad-password" in reasons, reasons)
check("a miss on no account at all is too", "no-such-user" in reasons, reasons)
check("and so is a throttled attempt", "ip-spray" in reasons, reasons)
check("successes are logged, not only failures",
      any(row["ok"] == 1 for row in rows),
      "a window of pure failure cannot be told from an outage")

# THE ONE THAT MUST NEVER REGRESS. A password field here would be worse than
# no log at all, and a mistyped password is usually a real one off by a letter.
columns = [c[1] for c in db().execute("PRAGMA table_info(login_attempts)")]
check("no password column exists",
      not any("pass" in c.lower() for c in columns), columns)
dump = " ".join(str(tuple(row)) for row in rows)
check("no password text reached the table",
      REAL_PASSWORD not in dump and "password123" not in dump)


section("THE QUESTIONS THE LOG IS FOR")
# Both of these are the reason the table is shaped the way it is. If either
# needs more than one query, the shape is wrong.
spray = db().execute(
    """
    SELECT ip, COUNT(DISTINCT username) AS names
    FROM login_attempts WHERE ok = 0
    GROUP BY ip ORDER BY names DESC LIMIT 1
    """
).fetchone()
check("'one address, many usernames' is one query",
      spray["names"] >= app_module.IP_MAX_USERNAMES, dict(spray))

grind = db().execute(
    """
    SELECT username, COUNT(*) AS misses
    FROM login_attempts WHERE ok = 0
    GROUP BY username ORDER BY misses DESC LIMIT 1
    """
).fetchone()
check("'one account, many failures' is one query",
      grind["misses"] >= app_module.LOGIN_MAX_ATTEMPTS, dict(grind))


section("REVOCATION - what makes a 30-day token defensible")
second = login("bystander", REAL_PASSWORD, ip="192.0.2.78").get_json()["token"]

# SCOPED TO THIS USER. Counting every row in sessions also counts realuser's,
# which logout-all must NOT touch - so a total here would either fail wrongly
# or, worse, pass while the endpoint was revoking the whole server.
mine = db().execute(
    "SELECT COUNT(*) AS c FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = 'bystander')"
).fetchone()["c"]
others = db().execute(
    "SELECT COUNT(*) AS c FROM sessions WHERE user_id <> (SELECT id FROM users WHERE username = 'bystander')"
).fetchone()["c"]
check("this account has several devices signed in", mine >= 2, mine)
check("and another account has a live session too", others >= 1, others)

r = client.post(
    "/api/auth/logout-all",
    headers={"Authorization": "Bearer " + token},
    environ_base={"REMOTE_ADDR": "192.0.2.77"},
)
check("logout-all succeeds", r.status_code == 200, r.get_json())
check("and reports how many it killed", r.get_json()["revoked"] == mine,
      (r.get_json(), mine))

# THE BLAST RADIUS. "Log out everywhere" means everywhere for ONE account.
still = db().execute(
    "SELECT COUNT(*) AS c FROM sessions WHERE user_id <> (SELECT id FROM users WHERE username = 'bystander')"
).fetchone()["c"]
check("nobody else was signed out", still == others, (still, others))

r = client.get("/api/character?slot=0", headers={"Authorization": "Bearer " + second})
check("the OTHER device's token is dead", r.status_code == 401, r.status_code)
r = client.get("/api/character?slot=0", headers={"Authorization": "Bearer " + token})
check("the calling device's token is dead too", r.status_code == 401,
      "'everywhere' has to include here")



section("CREDENTIAL ROTATION - the other half of E-6")
# Revoking sessions without being able to change the password is a door you
# keep closing on someone who is holding the key. This is the key change.
OLD_PW, NEW_PW = "RotateMeOnce1", "RotatedTwice22"
ROTATOR_IP = "192.0.2.120"

r = client.post(
    "/api/auth/register",
    json={"username": "rotator", "password": OLD_PW},
    environ_base={"REMOTE_ADDR": ROTATOR_IP},
)
check("an account to rotate", r.status_code == 201, r.get_json())
first = r.get_json()["token"]
elsewhere = login("rotator", OLD_PW, ip=ROTATOR_IP).get_json()["token"]


def change(current, new, tok, ip=ROTATOR_IP):
    return client.post(
        "/api/auth/password",
        json={"current_password": current, "new_password": new},
        headers={"Authorization": "Bearer " + tok} if tok else {},
        environ_base={"REMOTE_ADDR": ip},
    )


# A VALID TOKEN IS NOT PROOF OF IDENTITY HERE. A stolen token is the exact
# situation this endpoint exists for; letting one set a new password would hand
# the account to the thief rather than take it back.
check("without a token -> 401", change(OLD_PW, NEW_PW, None).status_code == 401)
check("with the wrong current password -> 401",
      change("NotThePassword9", NEW_PW, first).status_code == 401,
      "a session alone must not be enough")
check("a new password under the minimum -> 400",
      change(OLD_PW, "short", first).status_code == 400)
check("a 'new' password identical to the old -> 400",
      change(OLD_PW, OLD_PW, first).status_code == 400)

live = db().execute(
    "SELECT COUNT(*) AS c FROM sessions WHERE user_id = (SELECT id FROM users WHERE username = 'rotator')"
).fetchone()["c"]
check("none of those refusals revoked anything", live == 2, live)

r = change(OLD_PW, NEW_PW, first)
check("a correct change -> 200", r.status_code == 200, r.get_json())
body = r.get_json()
check("it reports what it revoked", body["revoked"] == live, (body, live))
check("and hands back a working replacement token",
      body["token"] not in (first, elsewhere))

# The three properties that make it a rotation rather than a password edit.
check("the other device is signed out",
      client.get("/api/character?slot=0",
                 headers={"Authorization": "Bearer " + elsewhere}).status_code == 401)
check("the token that made the change is signed out too",
      client.get("/api/character?slot=0",
                 headers={"Authorization": "Bearer " + first}).status_code == 401)
check("the replacement token works",
      client.get("/api/character?slot=0",
                 headers={"Authorization": "Bearer " + body["token"]}).status_code in (200, 404))
check("the old password no longer logs in",
      login("rotator", OLD_PW, ip=ROTATOR_IP).status_code == 401)
check("the new one does",
      login("rotator", NEW_PW, ip=ROTATOR_IP).status_code == 200)

stored = db().execute(
    "SELECT password_hash FROM users WHERE username = 'rotator'"
).fetchone()["password_hash"]
check("re-hashed with scrypt, not stored or reused",
      stored.startswith("scrypt:") and NEW_PW not in stored, stored[:24])

trail = " ".join(str(tuple(row)) for row in db().execute("SELECT * FROM login_attempts"))
check("neither password reached the log", OLD_PW not in trail and NEW_PW not in trail)
check("the rotation is in the audit trail", "password-changed" in trail)
check("so is the failed attempt at it", "bad-password-on-change" in trail)


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
sys.exit(1 if failed else 0)
