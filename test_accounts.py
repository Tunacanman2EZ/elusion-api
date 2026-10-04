"""
Signing in, the way players will on day 1. Run: python3 test_accounts.py

A sweep of the login screen against the real server found these, and each
section below holds one of them:

  A-1  ONE LOGIN AT A TIME. Two games on one account lost items: both hold a
       picture of the same backpack, and the bag write replaces the whole bag,
       so the game that had not seen an unequip saved the sword out of
       existence. A login now ends the account's other sessions, and the game
       left behind is told it was signed in somewhere else.
  A-2  REOPENING THE GAME swaps the token (POST /api/auth/resume), so a second
       copy of the game on the same computer - which finds the same remembered
       token - cannot run beside the first either.
  A-3  THE MISS THAT LOCKS AN ACCOUNT SAYS SO, in minutes. It used to be one
       more "incorrect password", and the player found out about the lock by
       typing the right one.
  A-4  NO MAIL, NO DEMAND. With no mail set up the server still asked every
       new player for a recovery address, answered "check that inbox", and sent
       nothing - and the login screen does not let anyone past that prompt.
"""
import importlib.util, os, sqlite3, sys, tempfile, time
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_accounts_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
os.environ.pop("ELUSION_GAMEDATA", None)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

# Mail is recorded, never sent. Whether the server CAN send is set per section.
SENT = []
def _record_mail(to_address, subject, body):
    SENT.append({"to": to_address, "subject": subject, "body": body})
    return True
app_module.send_mail = _record_mail
app_module.send_mail_async = _record_mail
MAIL = {"on": False}
app_module.mail_can_send = lambda: MAIL["on"]

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

PW = "hunter2hunter2"
_ip_counter = [10]
def fresh_ip():
    """Each section from its own address, so the per-address throttle one
    section trips cannot answer for the next."""
    _ip_counter[0] += 1
    return "198.51.100.%d" % _ip_counter[0]

def register(name, ip):
    return client.post("/api/auth/register", json={"username": name, "password": PW},
                       environ_base={"REMOTE_ADDR": ip})

def login(name, ip, password=PW, **extra):
    body = {"username": name, "password": password}
    body.update(extra)
    return client.post("/api/auth/login", json=body, environ_base={"REMOTE_ADDR": ip})

def bearer(token):
    return {"Authorization": "Bearer " + token}

def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()

def uid(name):
    return raw("SELECT id FROM users WHERE username = ?", (name,))[0][0]


# =============================================================================
section("A-1  one login at a time")
# =============================================================================
ip = fresh_ip()
r = register("twin", ip)
check("an account to sign in twice", r.status_code == 201, r.get_json())
first = r.get_json()["token"]
r = login("twin", ip)
check("signing in again works", r.status_code == 200, r.get_json())
second = r.get_json()["token"]

r = client.get("/api/account", headers=bearer(first))
body = r.get_json() or {}
check("the earlier session is refused", r.status_code == 401, r.status_code)
check("and the refusal says it was signed in somewhere else",
      body.get("signed_in_elsewhere") is True and "somewhere else" in body.get("message", ""), body)
r = client.get("/api/auth/session", headers=bearer(first))
check("the heartbeat says the same, which is where the game hears it",
      r.status_code == 401 and (r.get_json() or {}).get("signed_in_elsewhere") is True, r.get_json())
check("the new session works", client.get("/api/account", headers=bearer(second)).status_code == 200)
check("the account holds exactly one session",
      raw("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid("twin"),))[0][0] == 1)

# THE BUG ITSELF, as it was reproduced: an item unequipped by one game, then
# a stale bag saved by the other.
client.put("/api/save", headers=bearer(second), json={"slot": 0, "class_id": "warrior", "name": "Twin"})
chest = sorted(i["item_id"] for i in gamedata.ITEMS.values()
               if gamedata.equip_slot_for(i["item_id"]) == "chest"
               and int(i.get("required_level", 1)) <= 1
               and ("warrior" in (i.get("required_classes") or []) or not i.get("required_classes")))[0]
raw("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 0, 0, ?, 1)",
    (uid("twin"), chest))
game_a = login("twin", ip).get_json()["token"]
game_b = login("twin", ip).get_json()["token"]
r = client.post("/api/character/equip", headers=bearer(game_b), json={"slot": 0, "item_id": chest})
check("game B wears the chest piece", r.status_code == 200, r.get_json())
r = client.post("/api/character/unequip", headers=bearer(game_b), json={"slot": 0, "equip_slot": "chest"})
check("and takes it off again, into the bag", r.status_code == 200, r.get_json())
r = client.put("/api/character/inventory", headers=bearer(game_a), json={"slot": 0, "inventory": [None] * 20})
check("game A's bag, from before all that, is refused", r.status_code == 401, r.status_code)
bag = [c["item_id"] for c in (client.get("/api/character?slot=0", headers=bearer(game_b))
                              .get_json().get("inventory") or []) if c]
check("and the chest piece is still in the bag", chest in bag, bag)

# What does NOT end a session.
r = login("twin", ip, password="not-the-password")
check("a wrong password signs nobody out",
      r.status_code == 401 and client.get("/api/account", headers=bearer(game_b)).status_code == 200)
ip_other = fresh_ip()
register("bystander", ip_other)
other = login("bystander", ip_other).get_json()["token"]
login("twin", ip)
check("another account's session is untouched", client.get("/api/account", headers=bearer(other)).status_code == 200)

# A staff login that still owes its code has proved only the password.
MAIL["on"] = True
ip_staff = fresh_ip()
register("modwatch", ip_staff)
raw("UPDATE users SET role = 'mod', email = 'mod@example.com', email_verified = 1 WHERE username = 'modwatch'")
staff_live = "stray-modwatch-session"
raw("INSERT INTO sessions (token_hash, user_id, expires_at, last_seen_at) VALUES (?, ?, ?, ?)",
    (hashlib.sha256(staff_live.encode()).hexdigest(), uid("modwatch"), int(time.time()) + 3600, int(time.time())))
r = login("modwatch", ip_staff)
check("a staff login is asked for its code (202)", r.status_code == 202, r.get_json())
check("and that half-login signs nobody out",
      client.get("/api/account", headers=bearer(staff_live)).status_code == 200)
code = [w for w in SENT[-1]["body"].split() if w.isdigit() and len(w) == 6][0]
r = login("modwatch", ip_staff, code=code)
check("the finished login does", r.status_code == 200
      and client.get("/api/account", headers=bearer(staff_live)).status_code == 401, r.get_json())
MAIL["on"] = False

# The other ways a token stops working say nothing about "elsewhere".
r = client.get("/api/account", headers=bearer("no-such-token"))
check("a token nobody issued gets the plain 401",
      r.status_code == 401 and "signed_in_elsewhere" not in (r.get_json() or {}), r.get_json())
twin_now = login("twin", ip).get_json()["token"]
client.post("/api/auth/logout", headers=bearer(twin_now))
r = client.get("/api/account", headers=bearer(twin_now))
check("a logged-out token gets the plain 401 - logging out is not being signed in over",
      r.status_code == 401 and "signed_in_elsewhere" not in (r.get_json() or {}), r.get_json())

stored = [row[0] for row in raw("SELECT token_hash FROM ended_sessions")]
check("ended sessions are remembered by hash, never by token",
      stored and first not in stored and all(len(h) == 64 for h in stored), stored[:2])
raw("UPDATE ended_sessions SET ended_at = ? WHERE token_hash = ?",
    (int(time.time()) - 86400 - 5, app_module._token_hash(first)))
login("twin", ip)
check("and forgotten after a day, on the next login",
      not raw("SELECT 1 FROM ended_sessions WHERE token_hash = ?", (app_module._token_hash(first),)))


# =============================================================================
section("A-2  reopening the game")
# =============================================================================
ip = fresh_ip()
register("opener", ip)
remembered = login("opener", ip).get_json()["token"]
# A login from a while ago - so "ends when it would have" cannot be told apart
# from "a fresh thirty days" by both landing in the same second.
expiry = int(time.time()) + 3 * 86400
raw("UPDATE sessions SET expires_at = ? WHERE token_hash = ?", (expiry, hashlib.sha256(remembered.encode()).hexdigest()))
r = client.post("/api/auth/resume", headers=bearer(remembered))
body = r.get_json() or {}
check("a remembered login is resumed", r.status_code == 200, body)
resumed = body.get("token", "")
check("on a new token", resumed not in ("", remembered), body)
check("answering what the heartbeat answers",
      body.get("username") == "opener" and body.get("role") == "player" and "needs_email" in body, body)
check("the new token works", client.get("/api/account", headers=bearer(resumed)).status_code == 200)
r = client.get("/api/auth/session", headers=bearer(remembered))
check("and the remembered one - which another copy of the game would also be holding - is refused, saying why",
      r.status_code == 401 and (r.get_json() or {}).get("signed_in_elsewhere") is True, r.get_json())
check("the login still ends when it would have: resuming does not renew it",
      body.get("expires_at") == expiry
      and raw("SELECT expires_at FROM sessions WHERE token_hash = ?", (hashlib.sha256(resumed.encode()).hexdigest(),))[0][0] == expiry,
      (body.get("expires_at"), expiry))
r = client.post("/api/auth/resume", headers=bearer("no-such-token"))
check("a token nobody issued cannot resume", r.status_code == 401, r.status_code)


# =============================================================================
section("A-3  the miss that locks says so")
# =============================================================================
ip = fresh_ip()
register("fumbler", ip)
answers = [login("fumbler", ip, password="wrong-%d-guess" % i) for i in range(app_module.LOGIN_MAX_ATTEMPTS)]
check("the misses before the limit are the ordinary 401",
      all(a.status_code == 401 for a in answers[:-1]), [a.status_code for a in answers])
last = answers[-1].get_json() or {}
check("the miss that reaches it is 429", answers[-1].status_code == 429, last)
check("and names the wait in minutes", "Try again in 15 minutes." in last.get("message", ""), last)
r = login("fumbler", ip)
check("the right password waits it out too, in minutes, not seconds",
      r.status_code == 429 and "minute" in r.get_json().get("message", "")
      and "second" not in r.get_json().get("message", ""), r.get_json())
ip = fresh_ip()
answers = [login("nobody_here", ip, password="wrong-%d-guess" % i) for i in range(app_module.LOGIN_MAX_ATTEMPTS)]
check("a name with no account never answers 429 for the account lock - the 401 still hides who exists",
      all(a.status_code == 401 for a in answers), [a.status_code for a in answers])
words = app_module._wait_words
check("waits read as a person would say them",
      (words(900), words(899), words(61), words(60), words(59), words(1), words(0))
      == ("15 minutes", "15 minutes", "2 minutes", "1 minute", "59 seconds", "1 second", "1 second"),
      (words(900), words(899), words(61), words(60), words(59), words(1), words(0)))


# =============================================================================
section("A-4  no mail, no demand")
# =============================================================================
MAIL["on"] = False
ip = fresh_ip()
r = register("nomail", ip)
check("with no mail set up, a new account is not asked for an address",
      r.status_code == 201 and r.get_json().get("needs_email") is False, r.get_json())
token = login("nomail", ip).get_json()
check("nor on login", token.get("needs_email") is False, token)
token = token["token"]
check("nor on the heartbeat",
      client.get("/api/auth/session", headers=bearer(token)).get_json().get("needs_email") is False)
resumed = client.post("/api/auth/resume", headers=bearer(token)).get_json()
check("nor on reopening the game", resumed.get("needs_email") is False, resumed)
token = resumed["token"]
before = len(SENT)
r = client.post("/api/account/email", headers=bearer(token), json={"email": "nomail@example.com", "password": PW})
check("giving one anyway is 503, not 'check that inbox'",
      r.status_code == 503 and "cannot send email" in (r.get_json() or {}).get("message", ""), r.get_json())
check("and nothing was stored or sent",
      len(SENT) == before and client.get("/api/account/email", headers=bearer(token)).get_json().get("has_email") is False)
r = client.post("/api/account/email", headers=bearer(token), json={"email": "nomail@example.com", "password": "not-it-at-all"})
check("a wrong password is still the 401 it always was", r.status_code == 401, r.status_code)
r = client.post("/api/account/email", headers=bearer(token), json={"email": "not an address", "password": PW})
check("and a malformed address the 400", r.status_code == 400, r.status_code)

MAIL["on"] = True
ip = fresh_ip()
r = register("withmail", ip)
check("with mail, a new account is asked", r.get_json().get("needs_email") is True, r.get_json())
r = client.post("/api/account/email", headers=bearer(r.get_json()["token"]),
                json={"email": "withmail@example.com", "password": PW})
check("and giving one sends the code", r.status_code == 200 and SENT[-1]["to"] == "withmail@example.com",
      r.get_json())
MAIL["on"] = False


# =============================================================================
section("A-5  a leaked database holds no working login")
# =============================================================================
# Found on day 2, auditing the server for the owner's security course:
# passwords were scrypt hashes, but sessions.token held every live token as it
# was issued, so a copy of elusion.db logged its holder in as anybody signed in.
# The table now keeps only the SHA-256. Every check here reads what the
# DATABASE holds, because that is what leaks; the game never sees the hash.
ip = fresh_ip()
register("leakproof", ip)
live = login("leakproof", ip).get_json()["token"]
digest = hashlib.sha256(live.encode()).hexdigest()
columns = [row[1] for row in raw("PRAGMA table_info(sessions)")]
check("the sessions table has no column for the token itself",
      "token" not in columns and "token_hash" in columns, columns)
stored = raw("SELECT * FROM sessions WHERE user_id = ?", (uid("leakproof"),))
check("the row holds the token's SHA-256",
      len(stored) == 1 and digest in stored[0], stored)
everything = [cell for row in raw("SELECT * FROM sessions") for cell in row]
check("and no cell anywhere in the table is a live token", live not in everything)
r = client.get("/api/account", headers=bearer(digest))
check("what a leak would hand over does not log anyone in (401)", r.status_code == 401, r.status_code)
check("while the token the game holds still works",
      client.get("/api/account", headers=bearer(live)).status_code == 200)
resumed = client.post("/api/auth/resume", headers=bearer(live)).get_json()["token"]
check("reopening the game stores the new token as a hash too",
      raw("SELECT token_hash FROM sessions WHERE user_id = ?", (uid("leakproof"),))
      == [(hashlib.sha256(resumed.encode()).hexdigest(),)])
client.post("/api/auth/logout", headers=bearer(resumed))
check("and logging out removes the row it hashed to",
      raw("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid("leakproof"),))[0][0] == 0)


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
