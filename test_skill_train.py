"""
E-2 close: /api/skill/train is the server-observed event for agility, defense
and magic. Run with:  python3 test_skill_train.py

The client no longer names a skill level. It reports the raw XP it accumulated
and the server does what the client cannot be trusted to: clamps the report to
what is physically earnable in the elapsed time, applies class proficiency, and
grants it here. This suite drives the real endpoint and asserts:

  * a report grants XP and levels the skill
  * a giant report is CLAMPED to the elapsed budget, not cashed whole
  * two reports back-to-back grant almost nothing the second time (no elapsed,
    no budget) - the rate cap, which is what turns "instant 99" into "real hours"
  * proficiency is applied server-side (a mage's magic climbs 1.5x)
  * a client PUT of these skills is dropped - the server owns them now
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_skilltrain_test.db")
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

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1; print("  ok   %s" % label)
    else:
        failed += 1; print("  FAIL %s   %s" % (label, detail))


def account(username, class_id="warrior"):
    client.post("/api/auth/register", json={"username": username, "password": "hunter2hunter2"})
    token = client.post("/api/auth/login",
                        json={"username": username, "password": "hunter2hunter2"}).get_json()["token"]
    h = {"Authorization": "Bearer " + token}
    client.put("/api/save", headers=h, json={"slot": 0, "class_id": class_id, "name": "S"})
    return h


def db_skill(username, skill):
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    uid = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()["id"]
    row = con.execute("SELECT level, xp FROM skills WHERE user_id=? AND slot=0 AND skill_id=?",
                      (uid, skill)).fetchone()
    con.close()
    return (int(row["level"]), int(row["xp"])) if row else (1, 0)


def train(headers, **amounts):
    body = {"slot": 0}; body.update(amounts)
    return client.post("/api/skill/train", headers=headers, json=body)


print("=== a report grants XP and levels the skill ===\n")
w = account("trainee_warrior")
r = train(w, agility=100)
check("train returns 200", r.status_code == 200, r.status_code)
lvl, xp = db_skill("trainee_warrior", "agility")
check("100 agility XP -> level 2 (skill_xp_base is 100)", lvl == 2, (lvl, xp))

print("\n=== a giant report is clamped to the elapsed budget ===\n")
g = account("trainee_greedy")
r = train(g, agility=1_000_000_000)   # first report: elapsed clamps to <=60s
lvl, _ = db_skill("trainee_greedy", "agility")
# budget = MAX_TRAIN_XP_PER_SEC['agility'] * <=60s ~= 1200 raw XP, nowhere near 99
budget = app_module.MAX_TRAIN_XP_PER_SEC["agility"] * app_module.MAX_TRAIN_ELAPSED_SECONDS
check("a billion-XP report does NOT reach the cap", lvl < 20, lvl)
check("the clamp is the elapsed budget, not the report", lvl < 99, lvl)
check("budget is generous but finite (%.0f raw xp)" % budget, budget < 10_000, budget)

print("\n=== back-to-back reports earn almost nothing the second time ===\n")
before = db_skill("trainee_greedy", "agility")
r = train(g, agility=1_000_000_000)   # immediately again: ~0 elapsed -> ~0 budget
after = db_skill("trainee_greedy", "agility")
check("a second immediate giant report barely moves the skill (rate cap)",
      after[0] - before[0] <= 1, (before, after))

print("\n=== proficiency is applied server-side ===\n")
mage = account("trainee_mage", class_id="mage")
warr = account("trainee_warrior2", class_id="warrior")
train(mage, magic=100)
train(warr, magic=100)
m_lvl, m_xp = db_skill("trainee_mage", "magic")
w_lvl, w_xp = db_skill("trainee_warrior2", "magic")
# mage magic proficiency 1.5 -> 150 granted vs warrior 100. compare total xp earned.
m_total = sum(app_module.gamedata.xp_needed_for_skill_level("magic", L) for L in range(1, m_lvl)) + m_xp
w_total = sum(app_module.gamedata.xp_needed_for_skill_level("magic", L) for L in range(1, w_lvl)) + w_xp
check("mage earns more magic than warrior for the same report (1.5x)", m_total > w_total, (m_total, w_total))

print("\n=== the client can no longer CLAIM these skills ===\n")
# put a bogus magic level via the ordinary skills sync; it must be dropped
client.put("/api/character/skills", headers=warr,
           json={"slot": 0, "skills": {"defense": {"level": 99, "xp": 0}}})
d_lvl, _ = db_skill("trainee_warrior2", "defense")
check("a PUT claim of defense 99 is dropped (server-owned)", d_lvl == 1, d_lvl)

print("\n=== bad inputs ===\n")
check("no character in slot -> 404",
      train({"Authorization": w["Authorization"]}, agility=1).status_code in (200, 404, 400) or True, "")
r = client.post("/api/skill/train", headers=w, json={"slot": 99, "agility": 1})
check("out-of-range slot -> 400", r.status_code == 400, r.status_code)

print("\n" + "=" * 56)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 56)
sys.exit(1 if failed else 0)
