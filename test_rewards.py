"""
Better loot carries more, and legendary is rare. Run: python3 test_rewards.py

WHAT THE SERVER HAS TO GET RIGHT. Every gear piece from jade up now adds a
bonus - plate health, cloth mana, weapons damage, plain rings and amulets a
little of all three - and max_hp / max_mana are server-derived, so the server
has to count every one of them or a save clamps them away. And the loot roll is
the server's: legendary (ember) gear is about one an hour in the dark band, 1 in
4 from the fire boss and the Crowned, and never from the other five bosses.
"""
import importlib.util, os, random, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_rewards_test.db")
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
gamedata = app_module.gamedata
I, E = gamedata.ITEMS, gamedata.ENEMIES

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

MATS = ["iron", "jade", "cobalt", "amethyst", "ember"]
PLATE = ["helm", "chest", "legs", "boots", "shield"]
CLOTH = ["hood", "robe", "trousers", "slippers"]
WEAPONS = ["sword", "maul", "staff", "scepter"]
BONUS = ("bonus_max_hp", "bonus_max_mana", "bonus_damage_percent")
b = lambda iid, f: int(I[iid].get(f, 0) or 0)


# =============================================================================
section("EVERY TIER FROM JADE UP CARRIES A BONUS")
# =============================================================================
for field, pieces in (("bonus_max_hp", PLATE), ("bonus_max_mana", CLOTH), ("bonus_damage_percent", WEAPONS)):
    for piece in pieces:
        ladder = [b(m + piece, field) for m in MATS]
        stray = [m + piece for m in MATS for f in BONUS if f != field and b(m + piece, f)]
        check("%s: iron plain, then %s climbing jade to ember, nothing else" % (piece, field),
              ladder[0] == 0 and all(ladder[i] > ladder[i - 1] for i in range(2, 5)) and ladder[1] > 0
              and not stray, (ladder, stray))
for jewel in ("amulet", "ring"):
    rows = [[b(m + jewel, f) for f in BONUS] + [int(I[m + jewel]["armor_value"])] for m in MATS]
    check("every plain %s adds health, mana, damage and armour, never shrinking by tier" % jewel,
          all(v > 0 for r in rows for v in r)
          and all(rows[i][k] >= rows[i - 1][k] for i in range(1, 5) for k in range(4)), rows)

check("ember is the most rewarding tier of every piece", all(
    b("ember" + p, f) == max(b(m + p, f) for m in MATS)
    for f, ps in (("bonus_max_hp", PLATE), ("bonus_max_mana", CLOTH), ("bonus_damage_percent", WEAPONS)) for p in ps))


# =============================================================================
section("THE SERVER COUNTS ALL OF IT")
# =============================================================================
plate = {"chest": "emberchest", "legs": "emberlegs", "helm": "emberhelm", "boots": "emberboots",
         "shield": "embershield", "ring": "emberring", "amulet": "emberamulet", "weapon": "embersword"}
bare = gamedata.max_stats_for("warrior", 22)
worn = gamedata.max_stats_for("warrior", 22, plate)
check("a level 22 warrior in full ember is +150 health (100 plate, 20 ring, 30 amulet)",
      worn["max_hp"] == bare["max_hp"] + 150, (worn, bare))
check("and +65 mana from the ring and amulet", worn["max_mana"] == bare["max_mana"] + 65, (worn, bare))
check("and 15% damage (8 sword, 3 ring, 4 amulet)",
      gamedata.gear_bonuses(plate)["bonus_damage_percent"] == 15, gamedata.gear_bonuses(plate))
cloth = {"helm": "emberhood", "chest": "emberrobe", "legs": "embertrousers", "boots": "emberslippers"}
check("a full ember cloth set is +120 mana on a mage",
      gamedata.max_stats_for("mage", 22, cloth)["max_mana"] == gamedata.max_stats_for("mage", 22)["max_mana"] + 120)
check("iron gear adds nothing but its armour",
      gamedata.max_stats_for("warrior", 1, {"chest": "ironchest", "weapon": "ironsword"}) == gamedata.max_stats_for("warrior", 1))


# =============================================================================
section("LEGENDARY IS RARE")
# =============================================================================
random.seed(20260930)
def gear_tiers(enemy_id, kills):
    e = E[enemy_id]; seen = {}
    for _ in range(kills):
        if random.random() >= float(e.get("bag_drop_chance", 0)):
            continue
        for c in gamedata.build_bag_contents(e):
            it = I.get(c["item_id"])
            if it and it["type_name"] in ("WEAPON", "ARMOR"):
                seen[int(it["tier"])] = seen.get(int(it["tier"]), 0) + 1
    return seen

DARK = ["darksprite", "darkfiresprite", "darkbushmage", "darkbushsniper"]
KILLS = 60000
ember = sum(gear_tiers(eid, KILLS).get(5, 0) for eid in DARK) / (KILLS * len(DARK))
per_hour = ember * 300
check("a dark-band player finds about one legendary an hour (5 kills a minute)",
      0.5 <= per_hour <= 1.5, round(per_hour, 2))
check("no normal enemy outside the dark band can drop legendary",
      all(int(E[e]["max_loot_tier"]) < 5 for e in E
          if E[e].get("grants_rewards", True) and not E[e].get("slots_are_gear") and not e.startswith("dark")))
for boss in ("fireboss", "boss"):
    t = gear_tiers(boss, 8000); n = sum(t.values())
    check("%s: legendary about 1 in 4, amethyst otherwise" % boss,
          0.21 <= t.get(5, 0) / n <= 0.29 and set(t) == {4, 5}, t)
for boss in ("lightboss", "windboss", "waterboss", "iceboss", "earthboss"):
    t = gear_tiers(boss, 6000); n = sum(t.values())
    check("%s: never legendary; amethyst about a third, cobalt otherwise" % boss,
          5 not in t and 0.30 <= t.get(4, 0) / n <= 0.40 and set(t) <= {3, 4}, t)


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
