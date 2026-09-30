"""
The pace of the game, as the server pays it. Run: python3 test_pacing.py

WHAT CHANGED AND WHY IT IS THE SERVER'S BUSINESS. The level curve went from
100 x 1.15 to 1,250 x 1.27 (eight hours to level 22), every normal enemy moved
into an element band with its XP scaled to its new health, and the slimes are
placed as larges that burst into eight smalls. The server grants the XP, owns
the level, and enforces the kill ceiling - so each of those has a way to go
wrong here that the game never sees:

- the stored xp_to_next was trusted, so a row holding the old 100 gave a new
  character a first level twelve times cheaper than the curve;
- apply_xp() priced every level after the first on the CHARACTER curve, even
  inside a skill grant;
- a small slime's kill ceiling is its large's placements times what one large
  releases, or honest slime fights are refused;
- killwatch called anything with 1,000+ health a boss, and a normal dark bush
  mage now has 1,555.
"""
import importlib.util, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_pacing_test.db")
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
import killwatch

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

def account(u):
    client.post("/api/auth/register", json={"username": u, "password": "hunter2hunter2"})
    tok = client.post("/api/auth/login", json={"username": u, "password": "hunter2hunter2"}).get_json()["token"]
    return {"Authorization": "Bearer " + tok}

def row_of(u, slot=0):
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()[0]
    r = conn.execute("SELECT * FROM saves WHERE user_id = ? AND slot = ?", (uid, slot)).fetchone()
    conn.close(); return r

E = gamedata.ENEMIES
BAND_OF = {"light": 1, "wind": 1, "water": 2, "ice": 2, "earth": 3, "fire": 4, "dark": 5}
FAMILY = {
    "light": ["lightsprite", "lightfiresprite", "lightbushmage", "lightbushsniper"],
    "wind": ["windsprite", "windfiresprite", "windbushmage", "windbushsniper"],
    "water": ["watersprite", "waterfiresprite", "waterbushmage", "waterbushsniper"],
    "ice": ["icesprite", "icefiresprite", "icebushmage", "icebushsniper"],
    "earth": ["earthsprite", "earthfiresprite", "earthbushsniper", "bushmage"],
    "fire": ["firebushmage", "firesprite"],
    "dark": ["darksprite", "darkfiresprite", "darkbushmage", "darkbushsniper"],
}
SLIMES = ["light", "wind", "water", "ice", "earth", "dark"]


# =============================================================================
section("THE CURVE IS 1,250 x 1.27 AND THE SERVER DERIVES IT")
# =============================================================================
check("gamedata.json carries the new curve",
      float(gamedata.CONSTANTS["xp_base"]) == 1250.0 and abs(float(gamedata.CONSTANTS["xp_growth"]) - 1.27) < 1e-9,
      (gamedata.CONSTANTS["xp_base"], gamedata.CONSTANTS["xp_growth"]))
check("level 1 costs 1,250", gamedata.xp_needed_for_level(1) == 1250)

lvl, xp, nxt, gained = gamedata.apply_xp(1, 0, 100, 150)
check("a stored xp_to_next of 100 no longer buys level 2 for 150 XP",
      lvl == 1 and xp == 150 and nxt == 1250, (lvl, xp, nxt, gained))
lvl, xp, nxt, gained = gamedata.apply_xp(1, 0, 999999, 1250)
check("and a stored figure that is too HIGH does not block the level either",
      lvl == 2 and xp == 0, (lvl, xp, nxt))

skill = lambda at: gamedata.xp_needed_for_skill_level("agility", at)
lvl, xp, nxt, gained = gamedata.apply_xp(1, 0, skill(1), skill(1) + skill(2), need=skill)
check("a skill grant that crosses two levels prices both on the SKILL curve",
      lvl == 3 and xp == 0 and nxt == skill(3), (lvl, xp, nxt, skill(1), skill(2)))

H = account("pacer")
client.put("/api/save", headers=H, json={"slot": 0, "class_id": "warrior", "name": "Pacer"})
check("a new character is told level 1 costs 1,250, not the table's DEFAULT 100",
      int(row_of("pacer")["xp_to_next"]) == 1250, row_of("pacer")["xp_to_next"])
r = client.post("/api/combat/kill", headers=H, json={"slot": 0, "enemy_id": "lightsprite"})
body = r.get_json() or {}
check("one light sprite is a fraction of the first level, not a level",
      r.status_code == 200 and int(body.get("level", 0)) == 1
      and int(body.get("xp", 0)) == int(E["lightsprite"]["xp_reward"])
      and int(body.get("xp_to_next", 0)) == 1250, body)

# THE MIGRATION: a row an older server left on the old curve.
conn = sqlite3.connect(DB_PATH)
uid = conn.execute("SELECT id FROM users WHERE username = 'pacer'").fetchone()[0]
conn.execute("UPDATE saves SET level = 10, xp = 300, xp_to_next = 351 WHERE user_id = ?", (uid,))
conn.commit()
app_module._migrate_xp_to_next_from_curve(conn)
conn.commit()
fixed = conn.execute("SELECT level, xp, xp_to_next FROM saves WHERE user_id = ?", (uid,)).fetchone()
check("the boot migration rewrites an old-curve xp_to_next from the level",
      fixed == (10, 300, gamedata.xp_needed_for_level(10)), fixed)
app_module._migrate_xp_to_next_from_curve(conn)
check("and running it again changes nothing", conn.total_changes >= 1 and
      conn.execute("SELECT xp_to_next FROM saves WHERE user_id = ?", (uid,)).fetchone()[0]
      == gamedata.xp_needed_for_level(10))
conn.close()


# =============================================================================
section("EVERY NORMAL IS IN ITS ELEMENT'S BAND")
# =============================================================================
band_xp = {}
element_hp = {}
for el, fam in FAMILY.items():
    band = BAND_OF[el]
    for eid in fam:
        e = E[eid]
        check("%s: loot tier %d, about 0.37 XP a point of health" % (eid, band),
              int(e["max_loot_tier"]) == band and 0.30 <= e["xp_reward"] / e["max_hp"] <= 0.42,
              (e["max_loot_tier"], e["xp_reward"], e["max_hp"]))
        band_xp.setdefault(band, []).append(int(e["xp_reward"]))
    element_hp[el] = sum(E[e]["max_hp"] for e in fam) / len(fam)
ladder = ["light", "wind", "water", "ice", "earth", "fire", "dark"]
check("health climbs light, wind, water, ice, earth, fire, dark",
      all(element_hp[ladder[i]] > element_hp[ladder[i - 1]] for i in range(1, len(ladder))),
      {k: round(v) for k, v in element_hp.items()})

# EIGHT HOURS. Five kills a minute in the band that matches your level.
avg = {b: sum(v) / len(v) for b, v in band_xp.items()}
minutes, at = 0.0, {}
for level in range(1, 22):
    band = 1 if level < 5 else 2 if level < 10 else 3 if level < 16 else 4
    minutes += gamedata.xp_needed_for_level(level) / avg[band] / 5.0
    at[level + 1] = minutes / 60.0
check("level 5 in about a quarter of an hour", 0.18 < at[5] < 0.35, round(at[5], 2))
check("level 10 in about an hour", 0.7 < at[10] < 1.3, round(at[10], 2))
check("level 16 in about three hours", 2.3 < at[16] < 3.5, round(at[16], 2))
check("level 22 in about eight hours", 7.0 < at[22] < 9.0, round(at[22], 2))


# =============================================================================
section("A LARGE SLIME IS PLACED, PAYS NOTHING, AND RELEASES EIGHT")
# =============================================================================
for el in SLIMES:
    large, small = E[el + "slimelarge"], E[el + "slime"]
    check("%s: the large splits into the %s small, same element" % (el, el),
          large.get("split_into") == el + "slime" and large["element"] == small["element"],
          (large.get("split_into"), large["element"], small["element"]))
    check("  the large grants nothing and the small does",
          not large["grants_rewards"] and small["grants_rewards"])
    check("  the small's kill ceiling is eight per placed large",
          int(small["placed_count"]) == 8 * int(large["placed_count"]) and int(large["placed_count"]) > 0,
          (small["placed_count"], large["placed_count"]))

r = client.post("/api/combat/kill", headers=H, json={"slot": 0, "enemy_id": "windslimelarge"})
check("a kill claim for the large itself is refused", r.status_code == 400, r.status_code)
codes = [client.post("/api/combat/kill", headers=H,
                     json={"slot": 0, "enemy_id": "windslime"}).status_code for _ in range(8)]
check("one whole slime fight - eight smalls - is paid in full", codes == [200] * 8, codes)
check("the original poison slime is untouched: its smalls stay ceiling-exempt",
      int(E["poisonslimesmall"]["placed_count"]) == 0 and not E["poisonslimelarge"]["grants_rewards"])


# =============================================================================
section("BOSSES ARE BOSSES BY WHAT THEY DROP, NOT BY HOW MUCH HEALTH")
# =============================================================================
bosses = sorted(eid for eid, e in E.items() if e.get("slots_are_gear"))
check("seven bosses", len(bosses) == 7, bosses)
check("killwatch calls every one of them a boss", all(killwatch.is_boss(E[b]) for b in bosses))
big_normals = sorted(eid for eid, e in E.items()
                     if not e.get("slots_are_gear") and int(e["max_hp"]) >= killwatch.BOSS_HP_THRESHOLD)
check("there ARE normals over the old 1,000-health line now", len(big_normals) >= 4, big_normals)
check("and killwatch does not call any of them a boss",
      not any(killwatch.is_boss(E[e]) for e in big_normals), big_normals)
check("a catalogue with no slots_are_gear falls back to health",
      killwatch.is_boss({"max_hp": 5000}) and not killwatch.is_boss({"max_hp": 200}))

check("the fire boss is 9,200 health", int(E["fireboss"]["max_hp"]) == 9200, E["fireboss"]["max_hp"])
check("the Crowned is the biggest fight and drops the best tier",
      int(E["boss"]["max_hp"]) == max(int(E[b]["max_hp"]) for b in bosses)
      and int(E["boss"]["max_loot_tier"]) == 6, (E["boss"]["max_hp"], E["boss"]["max_loot_tier"]))
check("and one Crowned kill still levels a fresh character",
      int(E["boss"]["xp_reward"]) >= gamedata.xp_needed_for_level(1), E["boss"]["xp_reward"])


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
