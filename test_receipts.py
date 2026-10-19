"""Kill receipts (0.57.0; E3_SCOPE.md option C, step 2): with the owner's
switch on, a kill is paid only against the server's own count of that death -
an AGREED row the books (presence.py, combatbook.py) wrote into combat_kills
for this account, this character and this monster.

  R-1  the switch: no row reads OFF, it is the owner's alone (404 to anybody
       else), it is said on /api/status, logged, and a row that cannot be read
       reads OFF
  R-2  off, a kill is paid on the game's word exactly as before
  R-3  on, with nothing in the books: 202 "not yet", and nothing changes - no
       XP, no record, no token spent
  R-4  on, with an AGREED row: paid once, and the row is used; a second claim
       on the same death is "not yet" again
  R-5  what never pays: SHORT and NOT DUE, another account's death, another
       monster's, another character's, one older than RECEIPT_SECONDS; and
       what does: a row the books could not put a character to
  R-6  a game too old to ask again (below RECEIPT_BUILD) is told to update
       (409) instead of a 202 it would read as nothing - and is still paid
       when the receipt is there
  R-7  asking again costs nothing: a run of "not yet" spends no token, so the
       kill is paid once the books have it
  R-8  two deaths pay two kills, a third claim waits; a death is taken once
       even when two requests reach for it; a server with no books at all
       has no receipt to give; old receipts are pruned with the kill reports

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_receipts.py
"""
import importlib.util
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_receipts_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "boss"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata
import kill  # noqa: E402
import kill.data as kill_data  # noqa: E402

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


def register(name):
    return client.post("/api/auth/register",
                       json={"username": name, "password": "password123"}).get_json()["token"]


def headers(token, build=kill.RECEIPT_BUILD):
    out = {"Authorization": "Bearer %s" % token}
    if build is not None:
        out["X-Elusion-Build"] = str(build)
    return out


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def uid(username):
    return int(sql("SELECT id FROM users WHERE username = ?", (username,))[0]["id"])


def status():
    return client.get("/api/status").get_json() or {}


def switch(token, on):
    return client.post("/api/server/receipts", headers=headers(token), json={"on": on})


def kill_claim(token, enemy, slot=0, build=kill.RECEIPT_BUILD):
    return client.post("/api/combat/kill", headers=headers(token, build), json={"slot": slot, "enemy_id": enemy})


def died(username, enemy, verdict="agreed", slot=0, ago=0):
    """A death the books judged, as presence.py writes it."""
    sql("INSERT INTO combat_kills (at, area, enemy_id, origin, leader_id, user_id, slot, verdict, damage)"
        " VALUES (?, 'field', ?, 'o', ?, ?, ?, ?, 100)",
        (int(time.time()) - ago, enemy, uid(username), uid(username), slot, verdict))
    return int(sql("SELECT MAX(id) AS id FROM combat_kills")[0]["id"])


def reports(username):
    return int(sql("SELECT COUNT(*) AS n FROM kill_reports WHERE user_id = ?", (uid(username),))[0]["n"])


def xp_of(username, slot=0):
    row = sql("SELECT level, xp, kill_tokens FROM saves WHERE user_id = ? AND slot = ?", (uid(username), slot))[0]
    return (int(row["level"]), int(row["xp"]), float(row["kill_tokens"]))


def receipts_of(username):
    return [int(r["combat_kill_id"]) for r in
            sql("SELECT combat_kill_id FROM kill_receipts WHERE user_id = ? ORDER BY combat_kill_id", (uid(username),))]


# THE MONSTER: one that pays, and that the spawn ceiling does not count (it is
# born in a fight, placed nowhere) - so a long run of kills here is about the
# receipts and nothing else. Another that pays, for "another monster".
ENEMY = "poisonslimesmall" if "poisonslimesmall" in gamedata.ENEMIES else \
    next(e for e, v in gamedata.ENEMIES.items() if v.get("grants_rewards", True))
OTHER = next(e for e, v in gamedata.ENEMIES.items() if v.get("grants_rewards", True) and e != ENEMY)

BOSS = register("boss")
ANN = register("ann")
BO = register("bob")
for token, name in ((BOSS, "Boss"), (ANN, "Ann"), (BO, "Bob")):
    client.put("/api/save", headers=headers(token), json={"slot": 0, "class_id": "warrior", "name": name})
client.put("/api/save", headers=headers(ANN), json={"slot": 1, "class_id": "mage", "name": "Ann Two"})


# =============================================================================
print("\n--- R-1 the switch ---")
# =============================================================================
check("with no row the receipts are off, and /api/status says so", status().get("kill_receipts") is False, status())
res = switch(ANN, True)
check("a player is told the switch does not exist", res.status_code == 404, res.status_code)
check("'on' must be true or false", switch(BOSS, "yes").status_code == 400
      and client.post("/api/server/receipts", headers=headers(BOSS), json={}).status_code == 400)
res = switch(BOSS, True)
body = res.get_json() or {}
check("the owner switches them on, and is told the oldest build allowed in",
      res.status_code == 200 and body.get("kill_receipts") is True and body.get("min_client_build") == 0, body)
check("  /api/status says so, to anybody, with no login", status().get("kill_receipts") is True, status())
logged = sql("SELECT detail FROM staff_actions WHERE action = 'receipts' ORDER BY id DESC LIMIT 1")
check("  it is in the staff log", logged and logged[0]["detail"] == "on", [dict(r) for r in logged])
said = sql("SELECT body FROM broadcasts ORDER BY id DESC LIMIT 1")
check("  and not announced - nothing a player does changes", not said or "receipt" not in said[0]["body"].lower())
sql("UPDATE server_settings SET value = 'not json' WHERE key = ?", (app_module.RECEIPTS_KEY,))
with app_module.app.app_context():
    corrupt_reads = app_module.kill_receipts_on()
check("a row that cannot be read reads OFF - off is how every kill was paid",
      status().get("kill_receipts") is False and corrupt_reads is False)
res = switch(BOSS, False)
check("the owner switches them off", res.status_code == 200 and (res.get_json() or {}).get("kill_receipts") is False)


# =============================================================================
print("\n--- R-2 off: paid on the game's word ---")
# =============================================================================
before = reports("ann")
res = kill_claim(ANN, ENEMY)
check("with the switch off a kill nobody saw is paid, as before",
      res.status_code == 200 and reports("ann") == before + 1 and receipts_of("ann") == [], res.get_json())
res = kill_claim(ANN, ENEMY, build=None)
check("  by an old game as well", res.status_code == 200, res.status_code)


# =============================================================================
print("\n--- R-3 on, with nothing in the books ---")
# =============================================================================
switch(BOSS, True)
before_reports, before_xp = reports("ann"), xp_of("ann")
res = kill_claim(ANN, ENEMY)
body = res.get_json() or {}
check("a kill the books have not counted is 202 - not yet - and says when to ask again",
      res.status_code == 202 and body.get("pending") is True
      and body.get("retry_after_ms") == kill.RECEIPT_RETRY_MS and "not finished" in body.get("message", ""), body)
check("  and nothing is paid: no XP, no record, no token, no bag",
      reports("ann") == before_reports and xp_of("ann") == before_xp
      and "bag_id" not in body and "xp" not in body, [xp_of("ann"), before_xp, body])


# =============================================================================
print("\n--- R-4 on, with the books' receipt ---")
# =============================================================================
row = died("ann", ENEMY)
res = kill_claim(ANN, ENEMY)
body = res.get_json() or {}
check("a death the books agreed is paid", res.status_code == 200 and reports("ann") == before_reports + 1, body)
check("  and its row is used - kill_receipts holds it", receipts_of("ann") == [row], receipts_of("ann"))
res = kill_claim(ANN, ENEMY)
check("  a second claim on the same death is not yet again", res.status_code == 202
      and reports("ann") == before_reports + 1, res.status_code)


# =============================================================================
print("\n--- R-5 what pays and what does not ---")
# =============================================================================
for verdict in ("short", "not_due"):
    died("ann", ENEMY, verdict=verdict)
    check("a %s death never pays" % verdict.upper().replace("_", " "), kill_claim(ANN, ENEMY).status_code == 202)
died("bob", ENEMY)
check("another account's death does not pay this one", kill_claim(ANN, ENEMY).status_code == 202)
died("ann", OTHER)
check("another monster's death does not pay this one", kill_claim(ANN, ENEMY).status_code == 202)
died("ann", ENEMY, slot=1)
check("another character's death does not pay this one", kill_claim(ANN, ENEMY, slot=0).status_code == 202)
check("  but pays that character", kill_claim(ANN, ENEMY, slot=1).status_code == 200)
died("ann", ENEMY, ago=kill.RECEIPT_SECONDS + 5)
check("a death older than RECEIPT_SECONDS belongs to another kill", kill_claim(ANN, ENEMY).status_code == 202)
unplaced = died("ann", ENEMY, slot=-1)
check("a death the books could not put a character to pays this one",
      kill_claim(ANN, ENEMY).status_code == 200 and unplaced in receipts_of("ann"))
check("Bob's death still pays Bob", kill_claim(BO, ENEMY).status_code == 200)


# =============================================================================
print("\n--- R-6 a game too old to ask again ---")
# =============================================================================
res = kill_claim(ANN, ENEMY, build=kill.RECEIPT_BUILD - 1)
body = res.get_json() or {}
check("with no receipt yet, an old game is told to update - not a 202 it would read as nothing",
      res.status_code == 409 and "update" in body.get("message", ""), [res.status_code, body])
check("  no header at all is an old game too", kill_claim(ANN, ENEMY, build=None).status_code == 409)
died("ann", ENEMY)
check("  and with the receipt there it is paid", kill_claim(ANN, ENEMY, build=kill.RECEIPT_BUILD - 1).status_code == 200)


# =============================================================================
print("\n--- R-7 asking again costs nothing ---")
# =============================================================================
before_reports, before_xp = reports("ann"), xp_of("ann")
answers = [kill_claim(ANN, ENEMY).status_code for _ in range(int(app_module.KILL_BUCKET_CAPACITY) + 10)]
check("more 'not yet's than the bucket holds, and every one is a 202",
      set(answers) == {202}, sorted(set(answers)))
check("  none spent a token, wrote a record or moved the XP",
      xp_of("ann") == before_xp and reports("ann") == before_reports, [xp_of("ann"), before_xp])
died("ann", ENEMY)
check("  so when the books have it, it is paid", kill_claim(ANN, ENEMY).status_code == 200)


# =============================================================================
print("\n--- R-8 one death, one kill ---")
# =============================================================================
first, second = died("ann", ENEMY), died("ann", ENEMY)
codes = [kill_claim(ANN, ENEMY).status_code for _ in range(3)]
check("two deaths pay two kills, the oldest first, and a third claim waits",
      codes == [200, 200, 202] and receipts_of("ann")[-2:] == [first, second], [codes, receipts_of("ann")])

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
lone = died("ann", ENEMY)
found = kill_data.receipt_for(conn, uid("ann"), 0, ENEMY, int(time.time()) - kill.RECEIPT_SECONDS)
first_take = kill_data.use_receipt(conn, found, uid("ann"), 0, int(time.time()))
second_take = kill_data.use_receipt(conn, found, uid("ann"), 0, int(time.time()))
conn.commit()
check("two requests reaching for one death: the first takes it, the second is told it is gone",
      found == lone and first_take is True and second_take is False, [found, lone, first_take, second_take])
check("  and once taken it is no receipt at all",
      kill_data.receipt_for(conn, uid("ann"), 0, ENEMY, int(time.time()) - kill.RECEIPT_SECONDS) is None)
conn.close()

bare = sqlite3.connect(":memory:")
bare.row_factory = sqlite3.Row
check("a server with no books at all has no receipt to give - not an error",
      kill_data.receipt_for(bare, 1, 0, ENEMY, 0) is None)
bare.close()

sql("INSERT INTO kill_receipts (combat_kill_id, user_id, slot, at) VALUES (999999, ?, 0, ?)",
    (uid("ann"), int(time.time()) - app_module.KILL_LOG_RETENTION_SECONDS - 10))
died("ann", ENEMY)
kill_claim(ANN, ENEMY)
check("receipts older than the kill reports' two weeks are pruned with them",
      999999 not in receipts_of("ann"), receipts_of("ann"))

switch(BOSS, False)
check("and off again, a kill nobody saw is paid once more", kill_claim(ANN, ENEMY).status_code == 200)


# =============================================================================
print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
