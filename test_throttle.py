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


section("THE LOCKOUT MUST NOT FEED ITSELF")
# THE LAUNCH-DAY OUTAGE, and it is in this file because the section above
# passes happily while it is broken.
#
# Every check up to here fires the gate a fixed number of times and then stops.
# Real clients do not stop - they retry on their own - and the first version of
# _ip_throttle_state() counted ANY row that was not 'register-%', including the
# rows the gate itself writes when it refuses. Each refusal carried the
# username it had just refused, so:
#
#     six typos behind one address trip the spray rule
#     -> every later attempt is refused and LOGGED as a new failed username
#     -> the window never falls below six distinct names
#     -> the lockout renews for as long as anyone keeps trying
#
# Measured before the fix: sixty players with the CORRECT password, retrying
# continuously behind one address, were refused for four full windows and only
# got in when every client went silent at the same moment.
#
# That is the normal case, not an edge case. A household, a student hall, a
# carrier's NAT - and the entire player base at once behind a proxy with
# ELUSION_TRUSTED_PROXIES left at 0, which wsgi.py only warns about.
#
# The window is shortened here rather than waiting ten real minutes. The
# RATIO is what is under test: after one full window of pressure, an address
# whose only failures were the gate's own refusals must be free again.

SHARED = "198.51.100.200"
_real_window = app_module.IP_WINDOW_SECONDS
_real_lockout = app_module.IP_LOCKOUT_SECONDS
app_module.IP_WINDOW_SECONDS = 2
app_module.IP_LOCKOUT_SECONDS = 2

import time as _time                                        # noqa: E402

for i in range(12):
    client.post("/api/auth/register",
                json={"username": "shared%02d" % i, "password": REAL_PASSWORD},
                environ_base={"REMOTE_ADDR": SHARED})

# Six people behind one address mistype their own password.
for i in range(app_module.IP_MAX_USERNAMES):
    login("shared%02d" % i, "WRONGPASSWORD", ip=SHARED)

check("six typos from one address do trip the gate",
      login("shared06", REAL_PASSWORD, ip=SHARED).status_code == 429,
      "the spray rule is not firing at all, which is a different bug")

# Now everyone retries CORRECTLY and continuously, as a game client does.
codes = []
_start = _time.time()
_n = 0
while _time.time() - _start < app_module.IP_WINDOW_SECONDS * 3:
    codes.append(login("shared%02d" % (_n % 12), REAL_PASSWORD, ip=SHARED).status_code)
    _n += 1
    _time.sleep(0.05)

check("the address frees itself while clients are still retrying",
      200 in codes,
      "%d attempts over three windows, every one refused - the gate is "
      "counting its own refusals as evidence again" % len(codes))

# AND THE GATE STILL WORKS. Half this section is worthless without this: a
# throttle that never fires also never feeds itself.
SPRAYER = "203.0.113.200"
sprayed = [login("shared%02d" % i, "GUESSEDPASSWORD", ip=SPRAYER).status_code
           for i in range(app_module.IP_MAX_USERNAMES + 6)]
check("a real spray is still stopped at IP_MAX_USERNAMES",
      429 in sprayed and sprayed.index(429) == app_module.IP_MAX_USERNAMES,
      sprayed)
check("and stays stopped while it keeps spraying",
      {login("shared%02d" % (i + 20), "GUESSEDPASSWORD", ip=SPRAYER).status_code
       for i in range(5)} == {429},
      "the sprayer got back in")

# A BANNED PLAYER IS NOT EVIDENCE EITHER - their password was CORRECT, and
# counting the refusal would let one banned account lock out their household.
HOUSE = "198.51.100.210"
for name in ("housea", "houseb"):
    client.post("/api/auth/register", json={"username": name, "password": REAL_PASSWORD},
                environ_base={"REMOTE_ADDR": HOUSE})
_con = db()
_con.execute("UPDATE users SET is_banned = 1, ban_expires_at = NULL, ban_reason = 'x', "
             "banned_by = 'owneraccount', banned_at = 0 WHERE username = 'housea'")
_con.commit()
banned_codes = {login("housea", REAL_PASSWORD, ip=HOUSE).status_code for _ in range(15)}
check("a banned player retrying is refused with 403, not counted as a spray",
      banned_codes == {403}, banned_codes)
check("and their housemate still logs in",
      login("houseb", REAL_PASSWORD, ip=HOUSE).status_code == 200,
      "one ban took out the whole address")

app_module.IP_WINDOW_SECONDS = _real_window
app_module.IP_LOCKOUT_SECONDS = _real_lockout


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


section("THE SPAWN CEILING - kills bounded by what the world contains")
# The token bucket above caps the RATE of kill claims at a number somebody
# chose. This caps the COUNT at what actually exists: an enemy placed three
# times, on a respawner, cannot die more than three times per respawn however
# loudly a client insists.
#
# WHY NOT A DAMAGE BUDGET, since that is the idea everyone reaches for first.
# The server knows the player's gear and every enemy's hp, so bounding total hp
# destroyed by dps x elapsed looks strictly better. It was measured and it is
# not: the tank's aura damages every enemy in range and the mage's cast
# explodes, so a budget that does not refuse honest AoE needs roughly 8x
# headroom - and a cheater inherits all of it, landing LOOSER than the token
# bucket it would have replaced. Bounding by what exists has no such slack.
#
# HALF THESE CHECKS ARE ABOUT NOT REFUSING ANYONE. A ceiling that bites a real
# player is worse than no ceiling, because the report is "the game randomly
# stops giving me xp" and the cause is invisible. The window arithmetic exists
# to make that impossible rather than unlikely - see KILL_WINDOW_SECONDS.

import gamedata as _gd                                      # noqa: E402

_spawn_user = "spawncap"
r = client.post("/api/auth/register",
                json={"username": _spawn_user, "password": REAL_PASSWORD},
                environ_base={"REMOTE_ADDR": "192.0.2.90"})
_sh = {"Authorization": "Bearer " + r.get_json()["token"]}
client.put("/api/save", headers=_sh,
           json={"slot": 0, "class_id": "warrior", "name": "Cap", "area": "field"})

# The real catalogue may predate the placement export, in which case the
# ceiling is inactive by design. Inject a count so the RULE is tested either
# way - a check that silently does nothing on an un-exported gamedata.json is
# the failure mode this whole file exists to avoid.
_target = "darkbushmage"
_saved_enemy = dict(_gd.ENEMIES[_target])
_saved_flag = _gd.SPAWNS_EXPORTED
_gd.ENEMIES[_target]["placed_count"] = 2
_gd.SPAWNS_EXPORTED = True


def _kill(enemy_id):
    return client.post("/api/combat/kill", headers=_sh,
                       json={"slot": 0, "enemy_id": enemy_id}).status_code


def _age_kills(seconds):
    """Move recorded kills back in time, so a long session runs in an instant."""
    conn = db()
    conn.execute("UPDATE kill_reports SET at = at - ?", (seconds,))
    conn.execute("UPDATE saves SET last_kill_at = last_kill_at - ?", (seconds * 1000,))
    conn.commit()


_ceiling = int(2 * (app_module.KILL_WINDOW_SECONDS
                    / app_module.KILL_RESPAWN_FLOOR_SECONDS + 1))
_paid = 0
for _i in range(_ceiling + 5):
    if _kill(_target) == 200:
        _paid += 1
    if _i % 20 == 19:
        _age_kills(30)          # let the token bucket refill without ageing past the window

check("two placed enemies pay out exactly their ceiling",
      _paid == _ceiling,
      "%d paid, ceiling is %d (2 placed x (%d/%d + 1))"
      % (_paid, _ceiling, app_module.KILL_WINDOW_SECONDS,
         app_module.KILL_RESPAWN_FLOOR_SECONDS))
check("and the refusal is 429, not a 400",
      _kill(_target) == 429,
      "this is 'not yet', not 'never' - a 4xx that reads as permanent would "
      "send a player looking for a broken account")

# THE WINDOW MOVES. A ceiling that never releases is a ban, not a rate limit.
_age_kills(app_module.KILL_WINDOW_SECONDS + 5)
check("once the window has passed, the same enemy pays again",
      _kill(_target) == 200,
      "the ceiling is permanent, which is not what a rolling window means")

# FAIL OPEN, TWICE, and both are load-bearing.
_gd.ENEMIES[_target]["placed_count"] = 0
_age_kills(app_module.KILL_WINDOW_SECONDS + 5)
_zero = [_kill(_target) for _ in range(12)]
check("an enemy placed nowhere is never judged",
      429 not in _zero,
      "placed_count 0 means 'spawned somewhere I cannot see' - the poison "
      "slime's smalls come from its own script and appear in no scene")

_gd.SPAWNS_EXPORTED = False
_gd.ENEMIES[_target]["placed_count"] = 1
_age_kills(app_module.KILL_WINDOW_SECONDS + 5)
_unexported = [_kill(_target) for _ in range(12)]
check("a gamedata.json with no placement data disables the ceiling",
      429 not in _unexported,
      "a half-upgraded server must keep accepting kills, not refuse every one")

_gd.SPAWNS_EXPORTED = _saved_flag
_gd.ENEMIES[_target] = _saved_enemy


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
sys.exit(1 if failed else 0)
