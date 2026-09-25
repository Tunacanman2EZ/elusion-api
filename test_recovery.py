"""
test_recovery.py - account recovery, end to end and adversarially.

    venv\\Scripts\\python.exe test_recovery.py

Throwaway database in your temp folder. Never touches elusion.db.

WHAT THIS IS GUARDING. Recovery is the one door that opens without the
password, so most of these checks are written from the attacker's side rather
than the player's:

  - can the endpoint be used to discover which emails have accounts?
  - can a six digit code be guessed?
  - can a stolen SESSION redirect recovery to the attacker's own inbox?
  - does a completed reset actually throw an intruder out?
  - can an unverified address - a typo at signup - be used to take an account?

The happy path is four checks. Everything else here is one of those questions.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_recovery_test.db")

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


# ---- intercept outgoing mail so the test can read the code -------------------
SENT = []


def fake_send_mail(to_address, subject, body):
    SENT.append({"to": to_address, "subject": subject, "body": body})
    return True


app_module.send_mail = fake_send_mail
# The endpoints hand mail to a background thread (see send_mail_async). Patched
# to the same synchronous recorder so the assertions below stay deterministic -
# a test that raced a thread would pass or fail by timing.
app_module.send_mail_async = fake_send_mail


def last_code():
    """The six digit code from the most recent email."""
    if not SENT:
        return None
    found = re.findall(r"\b(\d{6})\b", SENT[-1]["body"])
    return found[-1] if found else None


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def raw_sql(statement, args=()):
    con = sqlite3.connect(DB_PATH)
    con.execute(statement, args)
    con.commit()
    con.close()


def clear_throttle():
    """The login throttle is shared with these endpoints on purpose, so a test
    that deliberately fails a dozen times has to reset it or it throttles the
    NEXT section instead of the one that earned it."""
    raw_sql("DELETE FROM login_attempts")


# =============================================================================
print("\n--- setup ---")
reg = client.post("/api/auth/register", json={"username": "victim", "password": "password123"})
victim = reg.get_json()["token"]
check("account registered", reg.status_code == 201, str(reg.status_code))

res = client.get("/api/account/email", headers=auth(victim))
check("a fresh account has no recovery address",
      res.get_json().get("has_email") is False, str(res.get_json()))


# =============================================================================
print("\n--- setting the recovery address needs the PASSWORD, not just a session ---")

res = client.post("/api/account/email",
                  json={"email": "victim@example.com"}, headers=auth(victim))
check("no password is refused", res.status_code == 400, str(res.status_code))

# This is the session-theft scenario: an attacker holding a stolen token tries
# to point recovery at their own inbox.
res = client.post("/api/account/email",
                  json={"email": "attacker@evil.example", "password": "wrongpassword"},
                  headers=auth(victim))
check("a stolen session cannot redirect recovery", res.status_code == 401, str(res.status_code))

res = client.get("/api/account/email", headers=auth(victim))
check("and the address really was not changed",
      res.get_json().get("has_email") is False, str(res.get_json()))
clear_throttle()

res = client.post("/api/account/email",
                  json={"email": "not-an-email", "password": "password123"},
                  headers=auth(victim))
check("a malformed address is refused", res.status_code == 400, str(res.status_code))

res = client.post("/api/account/email",
                  json={"email": "Victim@Example.COM", "password": "password123"},
                  headers=auth(victim))
check("the right password sets it", res.status_code == 200, str(res.status_code))
check("it comes back masked and unverified",
      res.get_json().get("verified") is False and "*" in res.get_json().get("email", ""),
      str(res.get_json()))
check("a confirmation email went out", len(SENT) == 1 and SENT[-1]["to"] == "victim@example.com",
      str(SENT[-1] if SENT else None))
verify_code = last_code()
check("the email carries a 6 digit code", verify_code is not None and len(verify_code) == 6,
      str(verify_code))


# =============================================================================
print("\n--- an UNVERIFIED address cannot recover the account ---")

before = len(SENT)
res = client.post("/api/auth/recover", json={"email": "victim@example.com"})
check("recover still answers normally", res.status_code == 200, str(res.status_code))
check("but no reset code was sent to an unverified address", len(SENT) == before,
      "sent %d" % (len(SENT) - before))


# =============================================================================
print("\n--- verifying the address ---")

res = client.post("/api/account/email/verify", json={"code": "000000"}, headers=auth(victim))
check("a wrong confirmation code is refused", res.status_code == 400, str(res.status_code))

res = client.post("/api/account/email/verify", json={"code": verify_code}, headers=auth(victim))
check("the right code verifies it", res.status_code == 200, str(res.status_code))

res = client.get("/api/account/email", headers=auth(victim))
check("the account now shows verified", res.get_json().get("verified") is True, str(res.get_json()))
check("the address is masked, not readable",
      "victim@example.com" not in res.get_json().get("email", ""), str(res.get_json()))


# =============================================================================
print("\n--- it never reveals which addresses have accounts ---")

known = client.post("/api/auth/recover", json={"email": "victim@example.com"})
raw_sql("DELETE FROM auth_codes WHERE purpose = 'reset'")  # clear the cooldown
unknown = client.post("/api/auth/recover", json={"email": "nobody@example.com"})
missing = client.post("/api/auth/recover", json={"email": "not-an-address"})

check("known and unknown return the same status",
      known.status_code == unknown.status_code == 200,
      "%s %s" % (known.status_code, unknown.status_code))
check("known and unknown return the same body",
      known.get_json() == unknown.get_json(),
      "%s vs %s" % (known.get_json(), unknown.get_json()))
check("a malformed address answers the same too",
      missing.get_json() == known.get_json(), str(missing.get_json()))


# =============================================================================
print("\n--- requesting and using a code ---")

raw_sql("DELETE FROM auth_codes WHERE purpose = 'reset'")
before = len(SENT)
res = client.post("/api/auth/recover", json={"email": "victim@example.com"})
check("a verified address gets a code", len(SENT) == before + 1, str(len(SENT) - before))
reset_code = last_code()
check("the reset email names the account", "victim" in SENT[-1]["body"], SENT[-1]["body"][:60])

# The cooldown: asking again immediately must not send a second code.
before = len(SENT)
client.post("/api/auth/recover", json={"email": "victim@example.com"})
check("a second request inside the cooldown sends nothing", len(SENT) == before,
      "sent %d" % (len(SENT) - before))

res = client.post("/api/auth/reset",
                  json={"email": "victim@example.com", "code": reset_code, "new_password": "short"})
check("a too-short new password is refused", res.status_code == 400, str(res.status_code))

res = client.post("/api/auth/reset",
                  json={"email": "victim@example.com", "code": "111111",
                        "new_password": "brandnewpass1"})
check("a wrong code is refused", res.status_code == 400, str(res.status_code))
check("and says nothing useful about why",
      "wrong or has expired" in str(res.get_json().get("message", "")), str(res.get_json()))

# A second session, so we can prove the reset throws it out.
intruder = client.post("/api/auth/login",
                       json={"username": "victim", "password": "password123"}).get_json()["token"]
check("intruder holds a live session",
      client.get("/api/auth/session", headers=auth(intruder)).status_code == 200)

res = client.post("/api/auth/reset",
                  json={"email": "victim@example.com", "code": reset_code,
                        "new_password": "brandnewpass1"})
check("the right code resets the password", res.status_code == 200, str(res.status_code))
check("it reports the sessions it destroyed", res.get_json().get("revoked", 0) >= 1,
      str(res.get_json()))

check("the intruder's session is gone",
      client.get("/api/auth/session", headers=auth(intruder)).status_code == 401)
check("and so is the player's own",
      client.get("/api/auth/session", headers=auth(victim)).status_code == 401)

check("a warning email went to the address", "was just reset" in SENT[-1]["body"],
      SENT[-1]["body"][:60])

check("the old password no longer works",
      client.post("/api/auth/login",
                  json={"username": "victim", "password": "password123"}).status_code == 401)
res = client.post("/api/auth/login", json={"username": "victim", "password": "brandnewpass1"})
check("the new password does", res.status_code == 200, str(res.status_code))
victim = res.get_json()["token"]

res = client.post("/api/auth/reset",
                  json={"email": "victim@example.com", "code": reset_code,
                        "new_password": "anotherpass123"})
check("the code cannot be used twice", res.status_code == 400, str(res.status_code))
clear_throttle()


# =============================================================================
print("\n--- six digits is only safe because guessing is capped ---")

raw_sql("DELETE FROM auth_codes WHERE purpose = 'reset'")
client.post("/api/auth/recover", json={"email": "victim@example.com"})
real_code = last_code()

for i in range(app_module.RESET_MAX_ATTEMPTS):
    client.post("/api/auth/reset",
                json={"email": "victim@example.com", "code": "0000%02d" % i,
                      "new_password": "guessedpass123"})

res = client.post("/api/auth/reset",
                  json={"email": "victim@example.com", "code": real_code,
                        "new_password": "guessedpass123"})
check("the code burns after %d wrong guesses - even the REAL one now fails"
      % app_module.RESET_MAX_ATTEMPTS,
      res.status_code == 400, str(res.status_code))
check("so the password did not change",
      client.post("/api/auth/login",
                  json={"username": "victim", "password": "brandnewpass1"}).status_code == 200)
clear_throttle()


# =============================================================================
print("\n--- codes expire ---")

raw_sql("DELETE FROM auth_codes WHERE purpose = 'reset'")
client.post("/api/auth/recover", json={"email": "victim@example.com"})
expired_code = last_code()
raw_sql("UPDATE auth_codes SET expires_at = 1 WHERE purpose = 'reset'")

res = client.post("/api/auth/reset",
                  json={"email": "victim@example.com", "code": expired_code,
                        "new_password": "expiredpass123"})
check("an expired code is refused", res.status_code == 400, str(res.status_code))
clear_throttle()


# =============================================================================
print("\n--- the request endpoint is capped per address ---")

clear_throttle()
statuses = []
for _ in range(app_module.RESET_MAX_REQUESTS_PER_IP + 2):
    statuses.append(client.post("/api/auth/recover",
                                json={"email": "victim@example.com"}).status_code)
check("it starts answering 429 before it can be used to bomb an inbox",
      429 in statuses, str(statuses))
clear_throttle()


# =============================================================================
print("\n--- codes are stored hashed, never in the clear ---")

raw_sql("DELETE FROM auth_codes WHERE purpose = 'reset'")
client.post("/api/auth/recover", json={"email": "victim@example.com"})
plain = last_code()
con = sqlite3.connect(DB_PATH)
stored = con.execute("SELECT code_hash FROM auth_codes WHERE purpose = 'reset'").fetchone()[0]
con.close()
check("the raw code is not in the database", plain not in stored, "stored=%s" % stored[:24])
check("it is a real password hash", stored.startswith("scrypt:") or "$" in stored,
      stored[:24])


# =============================================================================
print("\n--- every account is asked for a recovery address ---")

clear_throttle()
SENT.clear()
fresh = client.post("/api/auth/register",
                    json={"username": "newcomer", "password": "password123"})
check("a new account is told it needs an email",
      fresh.get_json().get("needs_email") is True, str(fresh.get_json()))
check("and that it is not verified",
      fresh.get_json().get("email_verified") is False, str(fresh.get_json()))
newcomer = fresh.get_json()["token"]

res = client.get("/api/auth/session", headers=auth(newcomer))
check("the heartbeat says so too", res.get_json().get("needs_email") is True,
      str(res.get_json()))

res = client.post("/api/auth/login", json={"username": "newcomer", "password": "password123"})
check("and so does a plain login", res.get_json().get("needs_email") is True,
      str(res.get_json()))
newcomer = res.get_json()["token"]

# Give it an address but do NOT confirm it.
client.post("/api/account/email",
            json={"email": "newcomer@example.com", "password": "password123"},
            headers=auth(newcomer))
res = client.post("/api/auth/login", json={"username": "newcomer", "password": "password123"})
check("an UNCONFIRMED address still counts as needing one",
      res.get_json().get("needs_email") is True, str(res.get_json()))
newcomer = res.get_json()["token"]

client.post("/api/account/email/verify", json={"code": last_code()}, headers=auth(newcomer))
res = client.post("/api/auth/login", json={"username": "newcomer", "password": "password123"})
check("once confirmed, the prompt stops",
      res.get_json().get("needs_email") is False, str(res.get_json()))
check("and it reports verified",
      res.get_json().get("email_verified") is True, str(res.get_json()))
newcomer = res.get_json()["token"]

res = client.get("/api/auth/session", headers=auth(newcomer))
check("the heartbeat agrees", res.get_json().get("needs_email") is False, str(res.get_json()))


# =============================================================================
print("\n--- the address stays private ---")

# Nothing a player, a mod, or the owner can fetch may contain the raw address.
owner = client.post("/api/auth/register",
                    json={"username": "checker", "password": "password123"}).get_json()["token"]
leaks = []
for label, response in [
    ("login", client.post("/api/auth/login",
                          json={"username": "newcomer", "password": "password123"})),
    ("session", client.get("/api/auth/session", headers=auth(newcomer))),
    ("own email", client.get("/api/account/email", headers=auth(newcomer))),
    ("staff view", client.get("/api/staff/user/newcomer", headers=auth(owner))),
    ("staff list", client.get("/api/staff/users", headers=auth(owner))),
]:
    if "newcomer@example.com" in response.get_data(as_text=True):
        leaks.append(label)
check("the raw address appears in no response", leaks == [], "leaked in: %s" % leaks)

res = client.get("/api/account/email", headers=auth(newcomer))
check("the owner of the account sees it masked",
      "*" in res.get_json().get("email", ""), str(res.get_json()))


# =============================================================================
print("\n--- recovery must not leak WHICH addresses exist, via the clock ---")

# THE BUG THIS GUARDS. Sending mail inside the request made /api/auth/recover
# answer in ~4.5s for a real address and ~0.1s for an unknown one, because only
# the first had mail to send. Identical wording does not help if a stopwatch
# can tell them apart. Mail now goes to a background thread.
#
# Simulated with a deliberately slow sender: if the send were still inline, the
# known-address call would take at least SLOW_SEND seconds.
import threading
import time as _time

SLOW_SEND = 2.0


def slow_send_mail(to_address, subject, body):
    _time.sleep(SLOW_SEND)
    SENT.append({"to": to_address, "subject": subject, "body": body})
    return True


def slow_send_mail_async(to_address, subject, body):
    threading.Thread(target=slow_send_mail,
                     args=(to_address, subject, body), daemon=True).start()
    return True


app_module.send_mail = slow_send_mail
app_module.send_mail_async = slow_send_mail_async

clear_throttle()
raw_sql("DELETE FROM auth_codes WHERE purpose = 'reset'")

start = _time.monotonic()
client.post("/api/auth/recover", json={"email": "newcomer@example.com"})
known_seconds = _time.monotonic() - start

clear_throttle()
start = _time.monotonic()
client.post("/api/auth/recover", json={"email": "nobody-at-all@example.com"})
unknown_seconds = _time.monotonic() - start

check("a real address does not block on the mail server (%.2fs < %.1fs)"
      % (known_seconds, SLOW_SEND),
      known_seconds < SLOW_SEND,
      "took %.2fs - the send is still inline" % known_seconds)

gap = abs(known_seconds - unknown_seconds)
check("known and unknown take about the same time (gap %.2fs)" % gap,
      gap < 0.5, "gap of %.2fs is a stopwatch oracle" % gap)

# Put the honest recorder back for anything after this.
app_module.send_mail = fake_send_mail
app_module.send_mail_async = fake_send_mail
clear_throttle()


# =============================================================================
print("\n=================================")
print("passed: %d   failed: %d" % (passed, failed))
if failures:
    print("failed checks:")
    for name in failures:
        print("  - %s" % name)
print("=================================")
sys.exit(1 if failed else 0)
