"""
The kill record. Run: python3 test_killrecord.py

The owner, 7 Oct: "lets make a button in game that records all players kills
with icons of the enemies". kill_tally counts every PAID kill per character and
kind of monster, for good; GET /api/kills is one character's, GET
/api/kills/everyone is everybody's. See THE KILL RECORD in app.py.

  KR-1  A PAID KILL IS COUNTED, in the kill's own transaction: one row per
        character per monster, first and last kill kept.
  KR-2  A REFUSED KILL IS NOT. An unknown monster, one that grants nothing, or
        one past the rate limit changes no count.
  KR-3  YOUR RECORD IS YOUR CHARACTER'S: most killed first, a total, how many
        kinds, the day counting began; another character's and another
        account's are not in it. A bad slot is a 400, no token a 401.
  KR-4  EVERYONE'S RECORD: each monster's total, how many players, and who has
        killed the most - an account, its characters added together, a tie
        going to whoever started first - and your own share beside it.
  KR-5  THE OPENING COUNT, once: a database that already has kill_reports is
        counted into the record at boot, without a deleted character's kills,
        the start date is the oldest kill it found, and a second boot changes
        nothing.
  KR-6  DELETING A CHARACTER DELETES ITS RECORD, and only its record.
"""
import importlib.util, os, sqlite3, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_killrecord_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
os.environ.pop("ELUSION_GAMEDATA", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
sys.path.insert(0, HERE)


def load_app(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, "app.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


app_module = load_app("elusion_app")
client = app_module.app.test_client()
gamedata = app_module.gamedata

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

PW = "hunter2hunter2"
_ip = [40]
def register(name):
    _ip[0] += 1
    r = client.post("/api/auth/register", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["token"]

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

def one(sql, args=()):
    rows = raw(sql, args)
    return rows[0][0] if rows else None

def uid(name):
    return one("SELECT id FROM users WHERE username = ?", (name,))

def make(token, slot, class_id):
    r = client.put("/api/save", headers=bearer(token),
                   json={"slot": slot, "class_id": class_id, "name": class_id})
    assert r.status_code == 200, r.get_json()

def kill(token, enemy_id, slot=0):
    return client.post("/api/combat/kill", headers=bearer(token),
                       json={"slot": slot, "enemy_id": enemy_id})

def tally(user_id, slot, enemy_id):
    rows = raw("SELECT kills, first_at, last_at FROM kill_tally"
               " WHERE user_id = ? AND slot = ? AND enemy_id = ?", (user_id, slot, enemy_id))
    return rows[0] if rows else None

def mine(token, slot=0):
    return client.get("/api/kills?slot=%s" % slot, headers=bearer(token))

def everyone(token):
    return client.get("/api/kills/everyone", headers=bearer(token))

# Two monsters that pay and are placed many times (a ceiling far above these
# tests), and one that grants nothing.
SLIME = "darkslime"
SPRITE = "firesprite"
NOTHING = "darkslimelarge"
assert gamedata.ENEMIES[SLIME]["grants_rewards"] and gamedata.ENEMIES[SPRITE]["grants_rewards"]
assert not gamedata.ENEMIES[NOTHING]["grants_rewards"]


# =============================================================================
section("KR-1  a paid kill is counted, one row per character per monster")
# =============================================================================
hunter = register("hunter")
make(hunter, 0, "warrior")
make(hunter, 1, "mage")
me = uid("hunter")
before = int(time.time())
for _ in range(3):
    check("a kill is paid", kill(hunter, SLIME).status_code == 200)
kill(hunter, SPRITE)
row = tally(me, 0, SLIME)
check("three slimes are three on the record", row is not None and row[0] == 3, row)
check("  with when the first and the last were", row is not None and before <= row[1] <= row[2], row)
raw("UPDATE kill_tally SET first_at = 100 WHERE user_id = ? AND slot = 0 AND enemy_id = ?", (me, SLIME))
kill(hunter, SLIME)
row = tally(me, 0, SLIME)
check("  and the first stays the first while the last moves on",
      row is not None and row[0] == 4 and row[1] == 100 and row[2] >= before, row)
raw("UPDATE kill_tally SET kills = 3 WHERE user_id = ? AND slot = 0 AND enemy_id = ?", (me, SLIME))
check("a sprite is a row of its own", (tally(me, 0, SPRITE) or [0])[0] == 1)
check("the other character has none", tally(me, 1, SLIME) is None)
kill(hunter, SLIME, slot=1)
check("  until it kills one itself", (tally(me, 1, SLIME) or [0])[0] == 1)
_record = open(os.path.join(HERE, "app.py")).read().split("def _record_kill(")[1].split("\ndef ")[0]
check("the count is written beside kill_reports, in the kill's transaction (no commit of its own)",
      "INSERT INTO kill_tally" in _record and "commit" not in _record.replace("NO COMMIT", ""))


# =============================================================================
section("KR-2  a refused kill is not counted")
# =============================================================================
check("an unknown monster is refused", kill(hunter, "nosuchmonster").status_code == 400)
check("  and counted nowhere", one("SELECT COUNT(*) FROM kill_tally WHERE enemy_id = 'nosuchmonster'") == 0)
check("a monster that grants nothing is refused", kill(hunter, NOTHING).status_code == 400)
check("  and counted nowhere", one("SELECT COUNT(*) FROM kill_tally WHERE enemy_id = ?", (NOTHING,)) == 0)
# last_kill_at is in milliseconds; a bucket emptied a minute from now.
raw("UPDATE saves SET kill_tokens = 0, last_kill_at = ? WHERE user_id = ? AND slot = 0",
    (int(time.time() * 1000) + 60000, me))
slimes = tally(me, 0, SLIME)[0]
check("a kill past the rate limit is refused", kill(hunter, SLIME).status_code == 429)
check("  and the record stands where it was", tally(me, 0, SLIME)[0] == slimes)
raw("UPDATE saves SET kill_tokens = 50, last_kill_at = 0 WHERE user_id = ? AND slot = 0", (me,))


# =============================================================================
section("KR-3  your record is your character's")
# =============================================================================
r = mine(hunter, 0)
body = r.get_json() or {}
check("GET /api/kills answers", r.status_code == 200, r.status_code)
check("  most killed first", [k["enemy_id"] for k in body.get("kills", [])] == [SLIME, SPRITE], body.get("kills"))
check("  with each count, and a total and how many kinds",
      body.get("total") == 4 and body.get("kinds") == 2 and body["kills"][0]["kills"] == 3, body)
check("  and the first and last kill", body["kills"][0]["first_at"] <= body["kills"][0]["last_at"])
check("  and the day counting began", isinstance(body.get("since"), int) and body["since"] > 0, body.get("since"))
check("the other character's is its own", (mine(hunter, 1).get_json() or {}).get("total") == 1)
other = register("bystander")
make(other, 0, "tank")
check("another account's record is not in it", (mine(other, 0).get_json() or {}).get("kills") == [])
check("a bad slot is a 400", mine(hunter, 9).status_code == 400 and mine(hunter, "x").status_code == 400)
check("no token is a 401", client.get("/api/kills?slot=0").status_code == 401
      and client.get("/api/kills/everyone").status_code == 401)


# =============================================================================
section("KR-4  everyone's record")
# =============================================================================
kill(other, SLIME)
rival = register("rival")
make(rival, 0, "healer")
make(rival, 1, "mage")
rv = uid("rival")
# The rival's two characters together: 2 + 2 = 4 slimes, more than the
# hunter's 3 + 1 = 4? Equal - and the hunter started first, so the hunter leads.
for slot in (0, 1):
    kill(rival, SLIME, slot=slot)
    kill(rival, SLIME, slot=slot)
raw("UPDATE kill_tally SET first_at = first_at + 100 WHERE user_id = ?", (rv,))
body = everyone(hunter).get_json() or {}
slime = next((k for k in body.get("kills", []) if k["enemy_id"] == SLIME), {})
check("each monster's total, all accounts together", slime.get("kills") == 3 + 1 + 1 + 4, slime)
check("  and how many players have killed it", slime.get("players") == 3, slime)
check("a tie on kills goes to whoever started first", slime.get("top") == "hunter" and slime.get("top_kills") == 4, slime)
kill(rival, SLIME)
slime = next((k for k in (everyone(hunter).get_json() or {}).get("kills", []) if k["enemy_id"] == SLIME), {})
check("one more and the rival leads - its characters added together", slime.get("top") == "rival"
      and slime.get("top_kills") == 5, slime)
check("  and your own share is beside it", slime.get("yours") == 4, slime)
body = everyone(other).get_json() or {}
check("most killed first, with a grand total and how many players",
      body.get("kills", [{}])[0].get("enemy_id") == SLIME and body.get("total") == 11 and body.get("players") == 3, body)
check("  and the same start date as yours", body.get("since") == (mine(hunter, 0).get_json() or {}).get("since"))


# =============================================================================
section("KR-5  the opening count, once")
# =============================================================================
raw("DELETE FROM kill_tally")
raw("DELETE FROM server_settings WHERE key = 'kill_tally_since'")
raw("DELETE FROM kill_reports")
base = int(time.time()) - 5000
for i in range(4):
    raw("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 0, ?, 1, 0, 1, ?)",
        (me, SPRITE, base + i))
# A character in slot 1 that was deleted after two kills, and its successor's one.
raw("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 1, ?, 1, 0, 1, ?)",
    (me, SLIME, base + 10))
raw("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 1, ?, 1, 0, 1, ?)",
    (me, SLIME, base + 11))
raw("INSERT INTO character_deletions (user_id, slot, class_id, name, level, gold, deleted_at, snapshot)"
    " VALUES (?, 1, 'mage', 'old', 1, 0, ?, '{}')", (me, base + 20))
raw("INSERT INTO kill_reports (user_id, slot, enemy_id, xp, attack_xp, level_at, at) VALUES (?, 1, ?, 1, 0, 1, ?)",
    (me, SLIME, base + 30))
again = load_app("elusion_app_second_boot")
check("a database with kill reports is counted at boot", (tally(me, 0, SPRITE) or [0])[0] == 4, tally(me, 0, SPRITE))
check("  without a deleted character's kills", (tally(me, 1, SLIME) or [0])[0] == 1, tally(me, 1, SLIME))
check("  and counting began at the oldest kill it found",
      one("SELECT value FROM server_settings WHERE key = 'kill_tally_since'") == str(base))
load_app("elusion_app_third_boot")
check("a second boot changes nothing", (tally(me, 0, SPRITE) or [0])[0] == 4)
raw("DELETE FROM kill_tally")
raw("DELETE FROM server_settings WHERE key = 'kill_tally_since'")
raw("DELETE FROM kill_reports")
load_app("elusion_app_empty_boot")
since = int(one("SELECT value FROM server_settings WHERE key = 'kill_tally_since'") or 0)
check("with nothing to count, counting begins at the boot", abs(since - int(time.time())) < 30, since)


# =============================================================================
section("KR-6  deleting a character deletes its record")
# =============================================================================
kill(hunter, SLIME, slot=0)
kill(hunter, SLIME, slot=1)
r = client.post("/api/character/delete", headers=bearer(hunter), json={"slot": 1, "confirm": "mage"})
check("the mage is deleted", r.status_code == 200, r.get_json())
check("  and its kills with it", tally(me, 1, SLIME) is None)
check("  and the warrior's stand", (tally(me, 0, SLIME) or [0])[0] == 1)


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
