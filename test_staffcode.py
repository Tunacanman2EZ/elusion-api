"""
test_staffcode.py - staff logins need a code from their email.

    venv\\Scripts\\python.exe test_staffcode.py

Throwaway database in your temp folder. Never touches elusion.db.

WHAT THIS IS GUARDING. A staff name is public - the crown, the MOD and DEV
badges - so their passwords are the ones worth guessing. For a staff account
with a confirmed address, a correct password answers 202 and emails a code,
and only the code gets a token. See STAFF LOGIN CODES in app.py.

Written mostly from the attacker's side: an attacker who HAS the password.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_staffcode_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "CHECKER"
os.environ.pop("ELUSION_STAFF_LOGIN_CODES", None)

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
app_module.send_mail_async = fake_send_mail
# A server that CAN send mail. Without a provider the step stands aside (see
# the section on that below), so every other section needs one set.
app_module.SMTP_HOST = "smtp.example.test"
app_module.MAIL_FROM = "game@example.test"
app_module.MAIL_CONSOLE = False


def code_in(mail):
    found = re.findall(r"\b(\d{6})\b", mail["body"])
    return found[-1] if found else None


def raw_sql(statement, args=()):
    con = sqlite3.connect(DB_PATH)
    con.execute(statement, args)
    con.commit()
    con.close()


def read_sql(statement, args=()):
    con = sqlite3.connect(DB_PATH)
    row = con.execute(statement, args).fetchone()
    con.close()
    return row


def clear_throttle():
    raw_sql("DELETE FROM login_attempts")
    raw_sql("UPDATE users SET failed_logins = 0, lockout_until = 0")


def login(username, password="password123", code=None):
    body = {"username": username, "password": password}
    if code is not None:
        body["code"] = code
    res = client.post("/api/auth/login", json=body)
    return res.status_code, (res.get_json() or {})


def register(username, email="", role=None):
    res = client.post("/api/auth/register", json={"username": username, "password": "password123"})
    check("%s registered" % username, res.status_code == 201, str(res.status_code))
    if email:
        raw_sql("UPDATE users SET email = ?, email_verified = 1 WHERE username = ?", (email, username))
    if role:
        raw_sql("UPDATE users SET role = ? WHERE username = ?", (role, username))


def age_code(username, seconds):
    """Move this account's pending code into the past, as if time had gone by."""
    raw_sql("UPDATE auth_codes SET created_at = created_at - ?, expires_at = expires_at - ? "
            "WHERE purpose = 'staff-login' AND user_id = (SELECT id FROM users WHERE username = ?)",
            (seconds, seconds, username))


# =============================================================================
print("\n--- setup ---")
register("CHECKER", "owner@example.test")          # the owner, by ELUSION_OWNER
register("warden", "warden@example.test", "mod")    # a mod with an address
register("newmod", "", "mod")                       # a mod with none
register("player1", "player1@example.test")         # not staff


# =============================================================================
print("\n--- a player logs in as always ---")
SENT.clear()
status, body = login("player1")
check("a player with an address gets a token straight away", status == 200 and body.get("token"), (status, body))
check("  and no mail", SENT == [], SENT)
check("  and is not flagged", body.get("staff_unprotected") is False, body)


# =============================================================================
print("\n--- the owner's password is not enough ---")
SENT.clear()
status, body = login("CHECKER")
check("a correct owner password answers 202, not 200", status == 202, (status, body))
check("  with no token in it", "token" not in body, body)
check("  and says a code is needed, and where it went (masked)",
      body.get("code_required") is True and body.get("sent_to") == "o***r@example.test", body)
check("one email, to the owner's address", len(SENT) == 1 and SENT[0]["to"] == "owner@example.test", SENT)
first_code = code_in(SENT[0]) if SENT else None
check("  with a six-digit code, and a warning that the password is known",
      first_code is not None and "CHECKER" in SENT[0]["body"] and "change your password" in SENT[0]["body"],
      SENT[:1])

status, body = login("CHECKER")
check("asking again within a minute sends no second email", status == 202 and len(SENT) == 1, (status, len(SENT)))

status, body = login("CHECKER", code="000000" if first_code != "000000" else "111111")
check("a wrong code is 400 - never 401, which the client would take as 'try to register'",
      status == 400 and body.get("code_required") is True and "token" not in body, (status, body))
row = read_sql("SELECT failed_logins FROM users WHERE username = 'CHECKER'")
check("  and counts as a failed login", row is not None and row[0] == 1, row)

status, body = login("CHECKER", password="password124", code=first_code)
check("the right code with the WRONG password is still 401", status == 401 and "token" not in body, (status, body))

spaced = "%s %s" % (first_code[:3], first_code[3:])
status, body = login("CHECKER", code=spaced)
check("the right code gets a token (spaces copied from the mail are fine)",
      status == 200 and body.get("token") and body.get("role") == "owner", (status, body))
session = client.get("/api/auth/session", headers={"Authorization": "Bearer %s" % body.get("token")})
check("  and the token works", session.status_code == 200, session.status_code)
row = read_sql("SELECT failed_logins FROM users WHERE username = 'CHECKER'")
check("  and a full login clears the streak", row is not None and row[0] == 0, row)
check("  and the owner is not flagged as unprotected", body.get("staff_unprotected") is False, body)

status, body = login("CHECKER", code=first_code)
check("a used code does not work twice", status == 400, (status, body))


# =============================================================================
print("\n--- guessing codes costs what guessing passwords costs ---")
clear_throttle()
SENT.clear()
login("CHECKER")
wrong = "123456"
results = []
for attempt in range(app_module.LOGIN_MAX_ATTEMPTS):
    # A burned code needs a new one; the resend wait is moved past each time.
    age_code("CHECKER", 120)
    status, _ = login("CHECKER")
    status, body = login("CHECKER", code=wrong)
    results.append(status)
check("each wrong code is refused", all(s in (400, 429) for s in results), results)
fresh = [m for m in SENT if m["to"] == "owner@example.test"]
age_code("CHECKER", 120)
login("CHECKER")
good = code_in(SENT[-1]) if SENT else None
status, body = login("CHECKER", code=good)
check("a lockout's worth of wrong codes freezes the account, even for the right code",
      status == 429 and "token" not in body, (status, body))
clear_throttle()

SENT.clear()
login("CHECKER")
real = code_in(SENT[-1])
misses = 0
for _ in range(app_module.RESET_MAX_ATTEMPTS):
    status, _body = login("CHECKER", code="999999" if real != "999999" else "888888")
    misses += 1
status, body = login("CHECKER", code=real)
check("five wrong codes burn the code: the real one no longer works", status == 400, (status, body))
clear_throttle()

SENT.clear()
login("CHECKER")
age_code("CHECKER", app_module.RESET_CODE_TTL_SECONDS + 5)
expired = code_in(SENT[-1])
status, body = login("CHECKER", code=expired)
check("an expired code is refused", status == 400, (status, body))
clear_throttle()


# =============================================================================
print("\n--- other codes are not login codes ---")
SENT.clear()
res = client.post("/api/auth/recover", json={"email": "owner@example.test"})
reset_mail = [m for m in SENT if m["to"] == "owner@example.test"]
reset_code = code_in(reset_mail[-1]) if reset_mail else None
check("a password-reset code was sent", reset_code is not None, SENT)
age_code("CHECKER", 120)
login("CHECKER")
status, body = login("CHECKER", code=reset_code)
check("  and it does not open a staff login", status == 400, (status, body))
clear_throttle()


# =============================================================================
print("\n--- who needs one ---")
SENT.clear()
status, body = login("warden")
check("a mod with a confirmed address needs a code too", status == 202 and len(SENT) == 1, (status, body))

SENT.clear()
status, body = login("newmod")
check("a mod with no address logs in on the password - there is nowhere to send one",
      status == 200 and body.get("token") and SENT == [], (status, body, SENT))
check("  and the answer says the account is unprotected", body.get("staff_unprotected") is True, body)

raw_sql("UPDATE users SET email_verified = 0 WHERE username = 'warden'")
SENT.clear()
status, body = login("warden")
check("an address typed but never confirmed does not count", status == 200 and SENT == [], (status, SENT))
raw_sql("UPDATE users SET email_verified = 1 WHERE username = 'warden'")


# =============================================================================
print("\n--- the ways the step stands aside, and says so ---")
clear_throttle()
app_module.SMTP_HOST = ""
SENT.clear()
status, body = login("CHECKER")
check("with no mail set up, the owner logs in on the password (not locked out)",
      status == 200 and body.get("token") and SENT == [], (status, body))
check("  flagged unprotected", body.get("staff_unprotected") is True, body)
app_module.SMTP_HOST = "smtp.example.test"

app_module.STAFF_LOGIN_CODES = False
SENT.clear()
status, body = login("CHECKER")
check("ELUSION_STAFF_LOGIN_CODES=off lets the owner in on the password",
      status == 200 and SENT == [] and body.get("staff_unprotected") is True, (status, body))
app_module.STAFF_LOGIN_CODES = True


# =============================================================================
print("\n--- order: nothing is sent before the password and the ban say yes ---")
clear_throttle()
SENT.clear()
status, body = login("CHECKER", password="password124")
check("a wrong owner password is the same 401 a player gets, and sends nothing",
      status == 401 and SENT == [] and "code_required" not in body, (status, body))

raw_sql("UPDATE users SET is_banned = 1, ban_expires_at = NULL, ban_reason = 'test', "
        "banned_by = 'CHECKER', banned_at = ? WHERE username = 'warden'", (int(time.time()),))
SENT.clear()
status, body = login("warden")
check("a banned mod gets the ban, and no code", status == 403 and SENT == [], (status, body))
raw_sql("UPDATE users SET is_banned = 0 WHERE username = 'warden'")


# =============================================================================
print("\n--- the log ---")
clear_throttle()
age_code("CHECKER", 120)
login("CHECKER")
login("CHECKER", code="12345x")
reasons ={r[0] for r in sqlite3.connect(DB_PATH).execute("SELECT DISTINCT reason FROM login_attempts")}
check("code-sent and bad-code are recorded", {"code-sent", "bad-code"} <= reasons, reasons)
check("  and neither counts toward the per-address throttle (the account lockout covers codes)",
      "code-sent" not in app_module.THROTTLE_EVIDENCE_REASONS
      and "bad-code" not in app_module.THROTTLE_EVIDENCE_REASONS)


# =============================================================================
print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  Failures:")
    for label in failures:
        print("    - %s" % label)
sys.exit(0 if failed == 0 else 1)
