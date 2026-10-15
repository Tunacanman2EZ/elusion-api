"""
test_tokens.py - a token's life: bound to its game, rotated, short for staff,
and not enough on its own for the most powerful routes (game 0.44.0).

    venv\\Scripts\\python.exe test_tokens.py

Throwaway database in your temp folder, like every other suite here. Never
touches elusion.db.

WHAT THIS IS GUARDING. The students' review of the hashed tokens: "Your ticket
closes the database-leak vulnerability, but it doesn't cover token theft from
anywhere else ... A leaked real token still hashes correctly and would return
200 OK, with full admin rights. Your missing defense is Token-lifecycle
hardening through rotation, short lifetime, binding, revocation, and secure
storage." app.py, TOKEN LIFECYCLE, says what each is here; this holds it:

  - T-1 A GAME FROM BEFORE 0.44.0 IS UNCHANGED until the owner raises the
    minimum build: its sessions are not hardened.
  - T-2 BINDING: a hardened token works only with the copy of the game it was
    issued to. Without it, or with another, it is theft - every session on the
    account ends, and every game is told why.
  - T-3 ROTATION: a token replaced by /resume, back from another copy of the
    game, is theft; back from the same copy (a second tab), it is "signed in
    somewhere else", as before.
  - T-4 SHORT LIFE: a hardened staff login lasts 12 hours, and half an hour
    with no game running it. A player's lasts thirty days.
  - T-5 STEP-UP: ranks, bans, gold and items, rollback and the server's
    switches need the password typed in the last 15 minutes; a token alone is
    not enough. Five wrong guesses at it is theft.
  - T-6 HSTS on every HTTPS answer.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_tokens_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "TOKOWNER"
for key in ("ELUSION_SMTP_HOST", "ELUSION_MAIL_FROM"):
    os.environ.pop(key, None)

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


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def sql(statement, args=()):
    conn = db_conn()
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


GAME_A = "a" * 32
GAME_B = "b" * 32


def game(build=6, install=GAME_A):
    """The headers a game of `build` sends, with its install id."""
    out = {"X-Elusion-Build": str(build)}
    if install:
        out["X-Elusion-Install"] = install
    return out


def auth(token, **headers):
    out = {"Authorization": "Bearer %s" % token}
    out.update(headers)
    return out


def register(name):
    client.post("/api/auth/register", json={"username": name, "password": "password123"})


def login(name, headers=None):
    res = client.post("/api/auth/login", json={"username": name, "password": "password123"},
                      headers=headers or {})
    return res.get_json() or {}, res


def session_row(token):
    rows = sql("SELECT * FROM sessions WHERE token_hash = ?", (app_module._token_hash(token),))
    return rows[0] if rows else None


for name in ("TOKOWNER", "tann", "tbob", "tcat"):
    register(name)
first_body, _ = login("TOKOWNER")
client.put("/api/save", headers={"Authorization": "Bearer %s" % first_body["token"]},
           json={"slot": 0, "class_id": "warrior", "name": "Boss"})

# THE SERVER LOG, read back: theft must be written there, and no token ever.
import logging
logged = []


class Catch(logging.Handler):
    def emit(self, record):
        logged.append(record.getMessage())


app_module.app.logger.addHandler(Catch())

print("\n--- T-1 a game from before 0.44.0 is unchanged ---")
body, _ = login("tann")
old = body["token"]
row = session_row(old)
check("a login with no build or install id is not hardened", row is not None and row["hardened"] == 0
      and row["install_hash"] == "", dict(row) if row else None)
check("  its token works with no install id, as it always did",
      client.get("/api/auth/session", headers=auth(old)).status_code == 200)
body, _ = login("tann", game(build=5))
check("a build-5 game sending its install id is not hardened either: the build says what it sends",
      session_row(body["token"])["hardened"] == 0)

print("\n--- T-2 binding: a hardened token works only with its own game ---")
body, _ = login("tann", game())
tok = body["token"]
row = session_row(tok)
check("a build-6 login with its install id is hardened, bound to that id's hash (never the id)",
      row["hardened"] == 1 and row["install_hash"] == app_module._install_hash(GAME_A)
      and GAME_A not in str(dict(row)), dict(row))
check("  and works from that game", client.get("/api/auth/session", headers=auth(tok, **game())).status_code == 200)
stolen = client.get("/api/auth/session", headers=auth(tok))
check("THE SAME TOKEN WITHOUT ITS GAME - copied from a log, a clipboard, a crash dump - is refused as theft",
      stolen.status_code == 401 and stolen.get_json().get("token_stolen"), stolen.get_json())
check("  and the theft ended every session on the account, the real game's too",
      client.get("/api/auth/session", headers=auth(tok, **game())).status_code == 401
      and not sql("SELECT 1 FROM sessions WHERE user_id = (SELECT id FROM users WHERE username='tann')"))
real = client.get("/api/auth/session", headers=auth(tok, **game())).get_json()
check("  the real game is told why - sign in again, change the password if it was not you",
      real.get("token_stolen") and "change your password" in real.get("message", ""), real)
body, res = login("tann", game())
check("  the password still works: the thief never had it", res.status_code == 200 and body.get("token"))
tok = body["token"]
other = client.get("/api/auth/session", headers=auth(tok, **game(install=GAME_B)))
check("another copy of the game with the token: theft too",
      other.status_code == 401 and other.get_json().get("token_stolen"), other.get_json())
body, _ = login("tann", game())
tok = body["token"]
bad = client.get("/api/auth/session", headers=auth(tok, **game(install="not-an-id")))
check("  and a malformed install id counts as none", bad.status_code == 401 and bad.get_json().get("token_stolen"))

print("\n--- T-3 rotation: a replaced token back from elsewhere is theft ---")
body, _ = login("tbob", game())
first = body["token"]
res = client.post("/api/auth/resume", headers=auth(first, **game()), json={"install": GAME_A})
second = (res.get_json() or {}).get("token")
check("opening the game swaps the token (/resume), as before", res.status_code == 200 and second and second != first)
check("  the old one is remembered as rotated, with its game",
      sql("SELECT why, install_hash FROM ended_sessions WHERE token_hash = ?",
          (app_module._token_hash(first),))[0]["why"] == "rotated")
again = client.get("/api/auth/session", headers=auth(first, **game()))
check("the replaced token from the SAME game - a second tab - is 'signed in somewhere else', nothing more",
      again.status_code == 401 and again.get_json().get("signed_in_elsewhere")
      and client.get("/api/auth/session", headers=auth(second, **game())).status_code == 200, again.get_json())
thief = client.get("/api/auth/session", headers=auth(first))
check("the replaced token from ANYWHERE ELSE is theft: somebody kept it",
      thief.status_code == 401 and thief.get_json().get("token_stolen"), thief.get_json())
check("  and the new token, the real game's, is ended with it",
      client.get("/api/auth/session", headers=auth(second, **game())).get_json().get("token_stolen"))
body, _ = login("tbob", game())
tok = body["token"]
res = client.post("/api/auth/resume", headers=auth(tok), json={})
check("a thief resuming a hardened token without its game gets nothing - not even a fresh token",
      res.status_code == 401 and "token" not in (res.get_json() or {}), res.get_json())
body, _ = login("tcat")
plain = body["token"]
client.post("/api/auth/resume", headers=auth(plain), json={})
res = client.get("/api/auth/session", headers=auth(plain, **game(install=GAME_B)))
check("an unhardened rotated token is 'signed in somewhere else' from anywhere - an older game is as before",
      res.status_code == 401 and res.get_json().get("signed_in_elsewhere"), res.get_json())

print("\n--- T-4 short life for staff ---")
body, _ = login("TOKOWNER", game())
boss = body["token"]
now = int(time.time())
check("a hardened staff login ends within STAFF_TOKEN_TTL (12 hours)",
      0 < body["expires_at"] - now <= app_module.STAFF_TOKEN_TTL, body["expires_at"] - now)
body, _ = login("tann", game())
player = body["token"]
check("  a player's still lasts thirty days", body["expires_at"] - now > 29 * 86400)
sql("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?",
    (now - app_module.STAFF_IDLE_SECONDS - 60, app_module._token_hash(boss)))
res = client.get("/api/auth/session", headers=auth(boss, **game()))
check("a staff session with no game running it for half an hour is over, and says so",
      res.status_code == 401 and res.get_json().get("staff_session_ended") and session_row(boss) is None, res.get_json())
sql("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (now - 7200, app_module._token_hash(player)))
check("  a player's idle two hours is fine", client.get("/api/auth/session", headers=auth(player, **game())).status_code == 200)
body, _ = login("TOKOWNER", game())
boss = body["token"]
sql("UPDATE sessions SET created_at = ? WHERE token_hash = ?",
    (now - app_module.STAFF_TOKEN_TTL - 60, app_module._token_hash(boss)))
res = client.get("/api/auth/session", headers=auth(boss, **game()))
check("  as is one twelve hours after its login, however busy", res.status_code == 401
      and res.get_json().get("staff_session_ended"), res.get_json())
body, _ = login("TOKOWNER", game())
boss = body["token"]
res = client.post("/api/auth/resume", headers=auth(boss, **game()), json={"install": GAME_A})
check("  and resuming one does not stretch it", res.status_code == 200
      and res.get_json()["expires_at"] <= body["expires_at"], (res.get_json() or {}).get("expires_at"))
boss = res.get_json()["token"]

print("\n--- T-5 step-up: the most powerful routes need the password, not only a token ---")
gold = client.post("/api/staff/gold", headers=auth(boss, **game()), json={"amount": 5, "slot": 0})
check("a session from a resume - nobody typed the password - is asked for it before gold",
      gold.status_code == 403 and gold.get_json().get("confirm_password"), gold.get_json())
body, _ = login("TOKOWNER", game())
boss = body["token"]
gold = client.post("/api/staff/gold", headers=auth(boss, **game()), json={"amount": 5, "slot": 0})
check("right after typing the password at login, gold goes", gold.status_code == 200, gold.get_json())
sql("UPDATE sessions SET confirmed_at = ? WHERE token_hash = ?",
    (int(time.time()) - app_module.STEP_UP_SECONDS - 5, app_module._token_hash(boss)))
gold = client.post("/api/staff/gold", headers=auth(boss, **game()), json={"amount": 5, "slot": 0})
check("fifteen minutes on, the token alone is not enough: 403, asking for the password",
      gold.status_code == 403 and gold.get_json().get("confirm_password")
      and gold.get_json().get("message") == app_module.STEP_UP_MESSAGE, gold.get_json())
check("  while reading staff pages still works on the token",
      client.get("/api/staff/users", headers=auth(boss, **game())).status_code == 200)
res = client.post("/api/auth/confirm", headers=auth(boss, **game()), json={"password": "wrongpassword"})
check("a wrong password to confirm is 403 - never 401, which the game reads as signed out",
      res.status_code == 403 and res.get_json().get("confirm_password"), res.get_json())
res = client.post("/api/auth/confirm", headers=auth(boss, **game()), json={"password": "password123"})
check("  the right one confirms", res.status_code == 200 and res.get_json().get("confirmed"), res.get_json())
gold = client.post("/api/staff/gold", headers=auth(boss, **game()), json={"amount": 5, "slot": 0})
check("  and the route goes", gold.status_code == 200, gold.get_json())
player_gold = client.post("/api/staff/gold", headers=auth(player, **game()), json={"amount": 5, "slot": 0})
check("a player is still told the route does not exist (404), not asked for a password",
      player_gold.status_code == 404, player_gold.get_json())
body, _ = login("TOKOWNER")
plain_boss = body["token"]
sql("UPDATE sessions SET confirmed_at = 0 WHERE token_hash = ?", (app_module._token_hash(plain_boss),))
check("an older game's owner session is not asked (the minimum build is the switch)",
      client.post("/api/staff/gold", headers=auth(plain_boss), json={"amount": 5, "slot": 0}).status_code == 200)
fresh = {r.rule: getattr(app_module.app.view_functions[r.endpoint], "_elusion_fresh_password", False)
         for r in app_module.app.url_map.iter_rules() if r.rule.startswith("/api/")}
asks = sorted(path for path, on in fresh.items() if on)
check("the routes that ask: ranks, bans, gold, levels, skills, items, rollback, the server's switches, events",
      asks == sorted(["/api/staff/ban", "/api/staff/unban", "/api/staff/role", "/api/staff/gold", "/api/staff/level",
                      "/api/staff/skill", "/api/staff/grant", "/api/staff/rollback", "/api/server/maintenance",
                      "/api/server/minbuild", "/api/server/pvp", "/api/server/trade",
                      # The stirring lands' two switches (game 0.46.0, test_world.py).
                      "/api/server/world", "/api/server/world/stir",
                      # Scheduling and calling off events (game 0.48.0, test_events.py).
                      "/api/world/events", "/api/world/events/cancel"]), asks)
check("  and not a moderator's everyday ones: mute, kick, a deleted line, reports",
      not any(fresh.get(p) for p in ("/api/staff/mute", "/api/staff/kick", "/api/chat/delete",
                                     "/api/staff/reports/resolve")))
body, _ = login("TOKOWNER", game())
boss = body["token"]
codes = [client.post("/api/auth/confirm", headers=auth(boss, **game()), json={"password": "guess%d" % i}).status_code
         for i in range(app_module.CONFIRM_MAX_MISSES)]
check("CONFIRM_MAX_MISSES wrong guesses at the password on one session is theft: every session ends",
      codes[:-1] == [403] * (app_module.CONFIRM_MAX_MISSES - 1) and codes[-1] == 401
      and client.get("/api/auth/session", headers=auth(boss, **game())).get_json().get("token_stolen"), codes)
check("a confirm needs a login", client.post("/api/auth/confirm", json={"password": "x"}).status_code == 401)

print("\n--- T-6 HTTPS only ---")
res = client.get("/api/auth/session", headers=auth(player, **game()), base_url="https://localhost")
check("an HTTPS answer carries HSTS for a year",
      res.headers.get("Strict-Transport-Security") == "max-age=31536000", dict(res.headers))
res = client.get("/api/auth/session", headers=auth(player, **game()))
check("  a plain-HTTP one (a local run) does not", "Strict-Transport-Security" not in res.headers)

print("\n--- T-7 the switch: every unbound login ends when the minimum build reaches 6 ---")
old_body, _ = login("tcat", game(build=5))
old_tok = old_body["token"]
hard_body, _ = login("tbob", game())
hard_tok = hard_body["token"]
check("before the switch, an older game's unbound login works",
      client.get("/api/auth/session", headers=auth(old_tok, **game(build=5))).status_code == 200)
sql("INSERT OR REPLACE INTO server_settings (key, value) VALUES (?, ?)",
    (app_module.MIN_BUILD_KEY, '{"build": %d}' % app_module.HARDENED_BUILD))
res = client.get("/api/auth/session", headers=auth(old_tok, **game()))
check("once the minimum build is 6, an unbound login is over - whatever build it claims to come from",
      res.status_code == 401 and res.get_json().get("old_login"), res.get_json())
check("  it is ended, not only refused, and says why: the game was updated",
      session_row(old_tok) is None and "updated" in res.get_json().get("message", ""))
res = client.post("/api/auth/resume", headers=auth(old_tok, **game(install=GAME_B)), json={"install": GAME_B})
check("  and a resume with it binds nothing - no fresh token for whoever holds it",
      res.status_code == 401 and "token" not in (res.get_json() or {}), res.get_json())
check("a bound login carries on through the switch",
      client.get("/api/auth/session", headers=auth(hard_tok, **game())).status_code == 200)
body, res = login("tcat", game())
check("  and the player whose login ended signs in again, bound",
      res.status_code == 200 and session_row(body["token"])["hardened"] == 1, res.get_json())
sql("DELETE FROM server_settings WHERE key = ?", (app_module.MIN_BUILD_KEY,))

print("\n--- the server log ---")
thefts = [line for line in logged if "[SECURITY] token theft suspected" in line]
check("every theft is written in the server log, with who and why",
      len(thefts) >= 6 and any("a bound token came without its copy of the game" in t for t in thefts)
      and any("a replaced token came back from another copy" in t for t in thefts)
      and any("wrong passwords to confirm" in t for t in thefts), thefts)
tokens_seen = [r["token_hash"] for r in sql("SELECT token_hash FROM ended_sessions")]
check("  and no token, or its hash, in any line of it",
      not any(h in line for h in tokens_seen for line in logged) and not any(
          t in line for t in (tok, boss, player, plain, first, second) for line in logged))


print("\n=== %d passed, %d failed ===" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
