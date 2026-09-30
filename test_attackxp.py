"""
Attack XP is banked at the kill, with the class specialty. Run: python3 test_attackxp.py

WHAT WAS WRONG. Attack trains only at the kill, and the server banked the
enemy's attack_xp_reward as it was - while SKILL_PROFICIENCY says a warrior
trains attack 1.5x, and only /api/skill/train (which never handles attack)
applied that table. The client, meanwhile, added attack XP of its own on every
hit and scaled the kill's amount by its own copy of the table, none of which
the server saw. So the attack bar climbed on screen and fell back at the next
login, levels and the damage they carry included.

NOW: the kill applies proficient_amount() - the same function the train route
uses - and answers with where the skill stands (level, xp, xp_to_next), which
the client copies onto its bar instead of adding up its own.
"""
import importlib.util, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_attackxp_test.db")
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

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

def account(u, class_id):
    client.post("/api/auth/register", json={"username": u, "password": "hunter2hunter2"})
    tok = client.post("/api/auth/login", json={"username": u, "password": "hunter2hunter2"}).get_json()["token"]
    h = {"Authorization": "Bearer " + tok}
    client.put("/api/save", headers=h, json={"slot": 0, "class_id": class_id, "name": class_id})
    return h

def banked_attack(u):
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()[0]
    row = conn.execute("SELECT level, xp FROM skills WHERE user_id = ? AND slot = 0 AND skill_id = 'attack'",
                       (uid,)).fetchone()
    logged = conn.execute("SELECT attack_xp FROM kill_reports WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                          (uid,)).fetchone()
    conn.close()
    return (int(row["level"]), int(row["xp"])) if row else (1, 0), (int(logged[0]) if logged else None)

ENEMY = "lightsprite"
RAW = int(gamedata.ENEMIES[ENEMY]["attack_xp_reward"])


# =============================================================================
section("ONE RULE FOR THE SPECIALTY")
# =============================================================================
pa = app_module.proficient_amount
check("a warrior's attack is 1.5x", pa("warrior", "attack", RAW) == int(RAW * 1.5), pa("warrior", "attack", RAW))
check("a mage's attack is as rolled", pa("mage", "attack", RAW) == RAW)
check("a mage's magic is 1.5x, the train route's rule", pa("mage", "magic", 10) == 15)
check("an unknown class trains at 1.0, and nothing goes negative",
      pa("nobody", "attack", 7) == 7 and pa("warrior", "attack", -5) == 0)
check("the table still says warrior -> attack 1.5",
      app_module.SKILL_PROFICIENCY["warrior"]["attack"] == 1.5)


# =============================================================================
section("THE KILL BANKS IT, AND SAYS WHERE THE SKILL NOW STANDS")
# =============================================================================
for user, cls, factor in (("axewarrior", "warrior", 1.5), ("staffmage", "mage", 1.0)):
    h = account(user, cls)
    r = client.post("/api/combat/kill", headers=h, json={"slot": 0, "enemy_id": ENEMY})
    body = r.get_json() or {}
    want = int(RAW * factor)
    (level, xp), logged = banked_attack(user)
    check("%s: a %s kill banks %d attack XP (%d x %s)" % (cls, ENEMY, want, RAW, factor),
          r.status_code == 200 and int(body.get("attack_xp_gained", -1)) == want, body.get("attack_xp_gained"))
    check("  the answer carries the banked level, XP and next threshold",
          int(body.get("attack_level", -1)) == level and int(body.get("attack_xp", -1)) == xp
          and int(body.get("attack_xp_to_next", -1)) == gamedata.xp_needed_for_skill_level("attack", level),
          (body.get("attack_level"), body.get("attack_xp"), body.get("attack_xp_to_next"), level, xp))
    check("  and the kill log records what was paid", logged == want, logged)

# Enough kills to cross a level, so the level in the answer is not just 1.
h = account("levelwarrior", "warrior")
need = gamedata.xp_needed_for_skill_level("attack", 1)
kills = need // int(RAW * 1.5) + 1
last = {}
for _ in range(kills):
    last = client.post("/api/combat/kill", headers=h, json={"slot": 0, "enemy_id": ENEMY}).get_json() or {}
(level, xp), _logged = banked_attack("levelwarrior")
check("a warrior crossing a level is told the new level, and it is the banked one",
      level >= 2 and int(last.get("attack_level", 0)) == level and int(last.get("attack_xp", -1)) == xp,
      (level, last.get("attack_level"), kills))


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
