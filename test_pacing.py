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

The general store now sells every piece from iron to amethyst, so the last
section checks the shelf against the catalogue and the saving pace: the next
band's set should cost a few hours of the gold the current band pays.
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


# =============================================================================
section("THE STORE SELLS IRON TO AMETHYST, AND THE NEXT SET IS HOURS AWAY")
# =============================================================================
# THE DESIGN. The general store stocks every weapon and armour piece from iron
# (tier 1) to amethyst (tier 4) at its catalogue value, so a player farming one
# band saves up for the next band's set. Ember (tier 5) and the bosses' gear
# stay drop-only. Anyone may buy any piece. The level gate is on wearing it,
# not on buying it.
#
# WHY THE SERVER CHECKS IT. The stock list lives in a .tres the server reads
# second-hand through gamedata.json. A re-export that drops a tier, or an
# ember piece that slips onto the shelf, changes the economy without a single
# line of server code changing.
store = gamedata.SHOPS.get("generalstore") or {}
stock = list(store.get("stock", []))
gear = {iid: it for iid, it in gamedata.ITEMS.items() if it["type_name"] in ("WEAPON", "ARMOR")}
missing = sorted(iid for iid, it in gear.items() if 1 <= int(it["tier"]) <= 4 and iid not in stock)
check("every weapon and armour piece of iron to amethyst is on the shelf", stock and not missing, missing)
over = sorted(iid for iid in stock if iid in gear and int(gear[iid]["tier"]) >= 5)
check("and nothing of ember or above is", not over, over)
check("every stocked id is a real item", all(iid in gamedata.ITEMS for iid in stock),
      [iid for iid in stock if iid not in gamedata.ITEMS])
check("nothing is stocked twice", len(stock) == len(set(stock)))
check("the store sells at catalogue value", float(store.get("price_multiplier", 0)) == 1.0,
      store.get("price_multiplier"))
check("so every piece costs exactly its value",
      all(gamedata.shop_price("generalstore", iid) == int(gear[iid]["value"]) for iid in stock if iid in gear))
check("an ember piece has no price at all", gamedata.shop_price("generalstore", "embersword") is None)

# FISHING. Only the iron rod could be had (a drop) and worms dropped one at a
# time, so the deeper fish were out of reach. The owner chose the store: the
# worm and every rod but ember, at value.
RODS = ["iron", "jade", "cobalt", "amethyst"]
check("the worm and the iron to amethyst rods are on the shelf",
      all(iid in stock for iid in ["fishingworm"] + [r + "fishingrod" for r in RODS]),
      [iid for iid in ["fishingworm"] + [r + "fishingrod" for r in RODS] if iid not in stock])
check("  at their value",
      all(gamedata.shop_price("generalstore", iid) == int(gamedata.ITEMS[iid]["value"])
          for iid in ["fishingworm"] + [r + "fishingrod" for r in RODS]))
check("  and the ember rod is still found, never bought", "emberfishingrod" not in stock)

# ONE BAND, ONE LEVEL. The shop draws a shelf per band titled with the level its
# pieces need, so a band that mixed two levels would have no honest title.
level_of = {}
for iid in stock:
    if iid in gear:
        level_of.setdefault(int(gear[iid]["tier"]), set()).add(int(gear[iid]["required_level"]))
check("each band's pieces all need one level", all(len(v) == 1 for v in level_of.values()), level_of)
levels = [min(level_of.get(t, {0})) for t in (1, 2, 3, 4)]
check("and it climbs iron 1, jade 5, cobalt 10, amethyst 16", levels == [1, 5, 10, 16], levels)

# SAVING PACE. Gold a band pays an hour, at the five kills a minute the level
# curve above is priced on, sampled through the real roll_kill_rewards() with a
# seeded generator so the numbers are the same on every run.
import random
coin_ids = {iid for iid, _ in gamedata.gold_denominations()}
gold_an_hour = {}
_real_rng = gamedata._rng
try:
    gamedata._rng = random.Random(2026)
    for el, fam in FAMILY.items():
        for eid in fam:
            for _ in range(3000):
                bag = gamedata.roll_kill_rewards(eid)["contents"]
                gold_an_hour.setdefault(BAND_OF[el], []).append(
                    sum(int(gamedata.ITEMS[c["item_id"]]["value"]) * int(c["quantity"])
                        for c in bag if c["item_id"] in coin_ids))
finally:
    gamedata._rng = _real_rng
gold_an_hour = {b: 300.0 * sum(v) / len(v) for b, v in gold_an_hour.items()}

MATERIAL = {1: "iron", 2: "jade", 3: "cobalt", 4: "amethyst"}
PLATE_SET = ["sword", "helm", "chest", "legs", "boots", "shield", "amulet", "ring"]
CLOTH_SET = ["staff", "hood", "robe", "trousers", "slippers", "amulet", "ring"]
def set_cost(tier, pieces):
    """What the store asks for the whole set, or None when a piece is not on sale."""
    prices = [gamedata.shop_price("generalstore", MATERIAL[tier] + p) for p in pieces]
    return None if None in prices else sum(prices)
for tier in (2, 3, 4):
    for name, pieces in (("plate", PLATE_SET), ("cloth", CLOTH_SET)):
        cost = set_cost(tier, pieces)
        if cost is None:
            check("the %s %s set is all on sale" % (MATERIAL[tier], name), False,
                  [MATERIAL[tier] + p for p in pieces if gamedata.shop_price("generalstore", MATERIAL[tier] + p) is None])
            continue
        hours = cost / gold_an_hour[tier - 1]
        check("the %s %s set (%s gold) is %.1f hours of %s-band gold: between 2 and 6"
              % (MATERIAL[tier], name, "{:,}".format(cost), hours, MATERIAL[tier - 1]),
              2.0 <= hours <= 6.0, (cost, round(gold_an_hour[tier - 1])))
plate_costs = [set_cost(t, PLATE_SET) for t in (1, 2, 3, 4)]
check("each set costs more than the one before it",
      None not in plate_costs and all(plate_costs[i] > plate_costs[i - 1] for i in (1, 2, 3)), plate_costs)

# BUY IT AT ANY LEVEL, WEAR IT AT ITS OWN. Through the real routes: the
# purchase goes into the backpack, and the equip route refuses it until the
# character reaches the level.
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE saves SET level = 1, gold = 600 WHERE user_id = ? AND slot = 0", (uid,))
conn.commit(); conn.close()
r = client.post("/api/shop/buy", headers=H, json={"slot": 0, "shop_id": "generalstore", "item_id": "jadesword"})
body = r.get_json() or {}
check("a level 1 character may buy a jade sword, at 520", r.status_code == 200 and body.get("total_paid") == 520,
      (r.status_code, body))
r = client.post("/api/character/equip", headers=H, json={"slot": 0, "item_id": "jadesword"})
check("but may not wear it yet", r.status_code in (400, 403) and "level" in str(r.get_json()).lower(),
      (r.status_code, r.get_json()))
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE saves SET level = 5 WHERE user_id = ? AND slot = 0", (uid,))
conn.commit(); conn.close()
r = client.post("/api/character/equip", headers=H, json={"slot": 0, "item_id": "jadesword"})
check("at level 5 the same sword goes on", r.status_code == 200, (r.status_code, r.get_json()))
r = client.post("/api/shop/buy", headers=H, json={"slot": 0, "shop_id": "generalstore", "item_id": "embersword"})
check("and no amount of gold buys an ember sword", r.status_code == 400, r.status_code)
conn = sqlite3.connect(DB_PATH)
conn.execute("UPDATE saves SET gold = 2160 WHERE user_id = ? AND slot = 0", (uid,))
conn.commit(); conn.close()
r = client.post("/api/shop/buy", headers=H,
                json={"slot": 0, "shop_id": "generalstore", "item_id": "fishingworm", "quantity": 20})
check("twenty worms cost 480", r.status_code == 200 and (r.get_json() or {}).get("total_paid") == 480,
      (r.status_code, r.get_json()))
r = client.post("/api/shop/buy", headers=H, json={"slot": 0, "shop_id": "generalstore", "item_id": "jadefishingrod"})
check("a jade rod costs 1,680", r.status_code == 200 and (r.get_json() or {}).get("total_paid") == 1680,
      (r.status_code, r.get_json()))
r = client.post("/api/fishing/catch", headers=H, json={"slot": 0})
check("and with them a fresh character can fish", r.status_code == 200, (r.status_code, r.get_json()))


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
