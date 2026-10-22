"""
test_larder.py - the larder (0.74.0): what a monster drops beside its bag,
meals of several ingredients, the buff a meal gives, and bait.

The owner, 22 Oct: "we have a whole lot of materials and ways to expand cooking
in clockwork lets get those done", and "i want to make recipes people can make
with the food also". Every one of these creates or destroys an item, and two of
them decide how hard a character is allowed to hit, so the question each
section asks is CLAUDE.md's: could a client profit by lying here - and does a
real player ever get refused?

  L-1  MATERIAL DROPS. Each of a monster's material_drops rolls on its own, at
       its chance times the owner's material_drop_scale (never past certain);
       they join a bag that dropped, or make a bag that holds ONLY them - no
       gold, ever: the same dice with materials on and off pay the same coins.
       The kill route asks economyrules for the scale, and a material bag is
       taken like any other, minting nothing.
  L-2  RECIPES. The book; a meal cooked - every ingredient taken, the burn
       rolled exactly as a fish's is from the recipe's own level and mastery,
       the meal and its XP paid, the answer in the fire's shape; burnt takes
       the ingredients and pays nothing; every refusal takes nothing.
  L-3  MEALS AND BUFFS. Eating one records the character's food buff, one at
       a time; the consume answer, /api/character and the /api/save listing
       carry it, and null once it is over. The presence ticket carries it, and
       the books' bounds rise by a MIGHT meal's percent - an unbuffed fighter
       is bounded exactly as before, and a buff of any other kind changes no
       damage bound. The aura's heal covers a VIGOUR meal's health. A deleted
       character takes its meal with it.
  L-4  BAIT. The best bait carried is used (most rolls, a tie to the first
       id), one is spent, the catch rolled that many times and the best fish
       kept; none is the refusal it always was, and a full bag spends nothing.

After every section the economy's invariants still hold: canary.py's own
checks, run on this suite's database.

Run:  python test_larder.py
"""
import importlib.util
import math
import os
import random
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_larder_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "larderowner"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata
import canary  # noqa: E402
import economyrules  # noqa: E402
import gathering  # noqa: E402
import presence  # noqa: E402  (after ELUSION_DB is set: it reads it on import)

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


def section(title):
    print("\n=== %s ===\n" % title)


def sql(query, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def register(username):
    r = client.post("/api/auth/register", json={"username": username, "password": "hunter2hunter2"})
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def make_char(headers, slot=0, class_id="warrior"):
    r = client.put("/api/save", headers=headers, json={"slot": slot, "class_id": class_id, "name": class_id})
    assert r.status_code == 200, r.get_json()


def uid(username):
    return int(sql("SELECT id FROM users WHERE username = ?", (username,))[0]["id"])


def bag(username, entries, slot=0):
    """The character's carry, replaced: [(position, item_id, quantity)]."""
    sql("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (uid(username), slot))
    for position, item_id, quantity in entries:
        sql("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
            (uid(username), slot, position, item_id, quantity))


def cells(username, slot=0):
    return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in sql(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?", (uid(username), slot))}


def holding(username, item_id, slot=0):
    return sum(q for i, q in cells(username, slot).values() if i == item_id)


def set_skill(username, name, level, slot=0):
    sql("INSERT INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, ?, ?, ?, 0)"
        " ON CONFLICT(user_id, slot, skill_id) DO UPDATE SET level = excluded.level, xp = 0",
        (uid(username), slot, name, level))


def skill(username, name, slot=0):
    rows = sql("SELECT level, xp FROM skills WHERE user_id = ? AND slot = ? AND skill_id = ?",
               (uid(username), slot, name))
    return (int(rows[0]["level"]), int(rows[0]["xp"])) if rows else (1, 0)


def give_gold(username, amount, slot=0):
    """Into the purse AND the ledger, so the supply invariant still holds."""
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (amount, uid(username), slot))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (0, ?, ?, ?, 'test_seed', '')",
        (uid(username), slot, amount))


def ledger_rows():
    return int(sql("SELECT COUNT(*) AS n FROM gold_ledger")[0]["n"])


def invariants(label):
    """canary.py's own checks, read-only, on this suite's database."""
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        broken = [(name, detail) for name, ok, detail in canary.check_all(conn) if not ok]
    finally:
        conn.close()
    check("canary: every invariant holds after %s" % label, not broken, broken)


class Die:
    """A die that says what it is told: random() the values given in turn and
    then `rest`, randint() its low end, randrange() 0, triangular() its mode."""
    def __init__(self, values=(), rest=0.5):
        self.values, self.rest = list(values), rest

    def random(self):
        return self.values.pop(0) if self.values else self.rest

    @staticmethod
    def randint(low, high):
        return low

    @staticmethod
    def randrange(*args):
        return 0

    @staticmethod
    def triangular(low, high, mode):
        return mode


def name_of(item_id):
    return (gamedata.item_row(item_id) or {}).get("display_name", item_id)


CURRENCY = {item_id for item_id, row in gamedata.ITEMS.items() if row.get("type_name") == "CURRENCY"}
DROPPERS = sorted(e for e, row in gamedata.ENEMIES.items() if row.get("material_drops"))
MATERIAL_IDS = {d["item_id"] for e in DROPPERS for d in gamedata.ENEMIES[e]["material_drops"]}
RECIPES = gamedata.recipe_ids()
BAITS = sorted(i for i in gamedata.ITEMS if gamedata.bait_rolls(i) > 0)


# =============================================================================
section("SETUP")
# =============================================================================
check("the catalogue has monsters with materials, recipes and baits to test",
      len(DROPPERS) >= 10 and len(RECIPES) >= 10 and len(BAITS) >= 3, (len(DROPPERS), len(RECIPES), BAITS))
cook_t = register("larderchef")
make_char(cook_t)
make_char(cook_t, slot=1, class_id="mage")
invariants("setup")


# =============================================================================
section("L-1  MATERIAL DROPS")
# =============================================================================
bad = [(e, d) for e in DROPPERS for d in gamedata.ENEMIES[e]["material_drops"]
       if not gamedata.has_item(d["item_id"]) or not 0 < float(d["chance"]) <= 1 or not 1 <= int(d["min"]) <= int(d["max"])]
check("every material drop names a real item, a chance in (0, 1] and 1 <= min <= max", not bad, bad[:5])
check("  and none of them can come out of the ordinary loot pool (droppable is false)",
      all(not gamedata.ITEMS[m].get("droppable", True) for m in MATERIAL_IDS),
      sorted(m for m in MATERIAL_IDS if gamedata.ITEMS[m].get("droppable", True)))

# A monster none of whose drops is certain, so a die can lose every one.
enemy_id = next(e for e in DROPPERS if all(float(d["chance"]) < 0.5 for d in gamedata.ENEMIES[e]["material_drops"]))
enemy = gamedata.ENEMIES[enemy_id]
saved_rng = gamedata._rng
try:
    gamedata._rng = Die(rest=0.0)
    every = gamedata.roll_material_drops(enemy, 1.0)
    gamedata._rng = Die(rest=0.999999)
    none = gamedata.roll_material_drops(enemy, 1.0)
    gamedata._rng = Die(rest=0.999999)
    certain = gamedata.roll_material_drops(enemy, 1000.0)
    gamedata._rng = Die(rest=0.0)
    off = gamedata.roll_material_drops(enemy, 0.0)
finally:
    gamedata._rng = saved_rng
wanted = {}
for d in enemy["material_drops"]:
    wanted[d["item_id"]] = wanted.get(d["item_id"], 0) + int(d["min"])
check("%s: a die that always wins drops every material once, each at its min" % enemy_id,
      {e["item_id"]: e["quantity"] for e in every} == wanted, (every, wanted))
check("  a die that always loses drops none", none == [], none)
check("  the scale times the chance, never past certain: at x1000 even that die loses nothing",
      {e["item_id"] for e in certain} == set(wanted), certain)
check("  and a scale of nothing drops nothing, however the die falls", off == [], off)

tally = {}
counts = 4000
for _ in range(counts):
    for e in gamedata.roll_material_drops(enemy, 1.0):
        tally[e["item_id"]] = tally.get(e["item_id"], 0) + 1
for d in enemy["material_drops"]:
    share = tally.get(d["item_id"], 0) / float(counts)
    spread = 4.0 * math.sqrt(float(d["chance"]) * (1 - float(d["chance"])) / counts) + 0.002
    check("  %s drops %s about %.0f%% of the time (%.1f%% in %d)" % (enemy_id, d["item_id"], 100 * float(d["chance"]),
                                                                     100 * share, counts),
          abs(share - float(d["chance"])) <= spread, share)

# THE SAME DICE, MATERIALS ON AND OFF. Materials are rolled after everything
# else a kill rolls, so with one seed the bag, its coins and its items come out
# the same whatever the materials do - "a material must never be a reason for
# more gold to drop" - and a bag the bag roll did not make holds only them.
same, only_materials, gold_in_material_bag, joined = True, 0, [], 0
for seed in range(600):
    eid = DROPPERS[seed % len(DROPPERS)]
    gamedata._rng = random.Random(seed)
    plain = gamedata.roll_kill_rewards(eid, "warrior", 1.0, 1.0, material_drop_scale=0.0)
    gamedata._rng = random.Random(seed)
    rich = gamedata.roll_kill_rewards(eid, "warrior", 1.0, 1.0, material_drop_scale=5.0)
    gamedata._rng = saved_rng
    rest = [c for c in rich["contents"] if c["item_id"] not in {m["item_id"] for m in rich["materials"]}]
    # The coins by what they are worth: a bag the materials would push past the
    # panel's cells has its smallest coins folded together (fold_coins()).
    worth = [(gamedata.change_total([c for c in bag if c["item_id"] in CURRENCY]),
              sorted((c["item_id"], c["quantity"]) for c in bag if c["item_id"] not in CURRENCY))
             for bag in (rest, plain["contents"])]
    if worth[0] != worth[1]:
        same = False
    if not plain["contents"] and rich["contents"]:
        only_materials += 1
        if any(c["item_id"] in CURRENCY for c in rich["contents"]) or rich["contents"] != rich["materials"]:
            gold_in_material_bag.append((eid, rich["contents"]))
    if plain["contents"] and rich["materials"]:
        joined += 1
gamedata._rng = saved_rng
check("the same dice with materials on and off pay exactly the same gold and items (600 kills)", same)
check("  bags made by materials alone happen (%d), and every one holds the materials and nothing else"
      % only_materials, only_materials > 0 and gold_in_material_bag == [], gold_in_material_bag[:3])
check("  and materials join a bag that dropped as well (%d)" % joined, joined > 0)

# A FULL BAG: the materials never push a coin out of the panel's cells.
import loot  # noqa: E402
check("the roll's copy of the panel's cells is loot/'s own", gamedata.LOOT_BAG_CELLS == loot.LOOT_BAG_CAPACITY,
      (gamedata.LOOT_BAG_CELLS, loot.LOOT_BAG_CAPACITY))
crowded = [{"item_id": "petboss", "quantity": 1}] * 7 + [{"item_id": "silvercoin", "quantity": 3},
                                                        {"item_id": "coppercoin", "quantity": 4},
                                                        {"item_id": "goldcoin", "quantity": 2}]
folded = gamedata.fold_coins(crowded)
check("fold_coins: ten entries into nine, the two smallest coins one stack of the smaller, not a coin lost",
      len(folded) == gamedata.LOOT_BAG_CELLS and gamedata.change_total([c for c in folded if c["item_id"] in CURRENCY])
      == gamedata.change_total([c for c in crowded if c["item_id"] in CURRENCY])
      and {"item_id": "coppercoin", "quantity": 3 * gamedata.item_value("silvercoin") + 4} in folded, folded)
check("  and a bag that fits is left exactly as it was", gamedata.fold_coins(crowded[:9]) == crowded[:9])
bosses = sorted(e for e in DROPPERS if gamedata.ENEMIES[e].get("slots_are_gear"))
saved_odds = {e: gamedata.ENEMIES[e].get("pet_odds") for e in bosses}
cut_coins, cut_items, crowded_bags, rolls = 0, 0, 0, 0
try:
    for e in bosses:
        gamedata.ENEMIES[e]["pet_odds"] = 1
        for _ in range(400):
            roll = gamedata.roll_kill_rewards(e, "warrior")
            rolls += 1
            crowded_bags += 1 if len(roll["contents"]) == gamedata.LOOT_BAG_CELLS else 0
            cut = roll["contents"][loot.LOOT_BAG_CAPACITY:]
            cut_coins += sum(1 for c in cut if c["item_id"] in CURRENCY)
            cut_items += sum(1 for c in cut if c["item_id"] not in CURRENCY)
finally:
    for e, odds in saved_odds.items():
        gamedata.ENEMIES[e]["pet_odds"] = odds
check("bosses with a pet every kill and their materials: no coin and no item past the panel's cells (%d bags, %d full)"
      % (rolls, crowded_bags), rolls > 0 and cut_coins == 0 and cut_items == 0, (cut_coins, cut_items))

# THE KILL ROUTE: the scale is economyrules', asked on the kill.
hunter_t = register("larderhunter")
make_char(hunter_t)


def kill(enemy, slot=0):
    sql("UPDATE saves SET kill_tokens = 50, last_kill_at = 0 WHERE user_id = ?", (uid("larderhunter"),))
    sql("DELETE FROM kill_reports WHERE user_id = ?", (uid("larderhunter"),))
    return client.post("/api/combat/kill", headers=hunter_t, json={"slot": slot, "enemy_id": enemy})


asked = []
real_roll, real_value = gamedata.roll_kill_rewards, economyrules.value


def spy(enemy_id, class_id="", bag_scale=1.0, xp_scale=1.0, material_drop_scale=1.0):
    asked.append(material_drop_scale)
    return real_roll(enemy_id, class_id, bag_scale, xp_scale, material_drop_scale=material_drop_scale)


gamedata.roll_kill_rewards = spy
try:
    r1 = kill(enemy_id)
    economyrules.value = lambda db, key: 2.5 if key == "material_drop_scale" else real_value(db, key)
    r2 = kill(enemy_id)
finally:
    gamedata.roll_kill_rewards = real_roll
    economyrules.value = real_value
check("the kill route asks economyrules for material_drop_scale: its default, then the owner's 2.5",
      r1.status_code == 200 and r2.status_code == 200 and asked == [economyrules.default("material_drop_scale"), 2.5],
      (asked, r1.status_code, r2.status_code))

# A MATERIAL BAG, END TO END: the bag roll says no, no pet, no mythic, and the
# materials say yes - the bag is stored, holds only them, and taking them
# mints nothing.
saved_enemy = gamedata.ENEMIES[enemy_id]
gamedata.ENEMIES[enemy_id] = dict(enemy, bag_drop_chance=0.0, pet_drop_id="", mythic_odds=0)
economyrules.value = lambda db, key: 1000.0 if key == "material_drop_scale" else real_value(db, key)
gamedata._rng = Die(rest=0.5)
try:
    res = kill(enemy_id)
finally:
    gamedata._rng = saved_rng
    gamedata.ENEMIES[enemy_id] = saved_enemy
    economyrules.value = real_value
body = res.get_json() or {}
stored = sql("SELECT item_id, quantity FROM loot_bag_items WHERE bag_id = ? ORDER BY position", (body.get("bag_id", ""),))
check("a kill whose bag roll said no still leaves a bag when its materials drop - and only the materials",
      res.status_code == 200 and body.get("bag_id") and {c["item_id"] for c in body.get("contents", [])} == set(wanted)
      and not any(c["item_id"] in CURRENCY for c in body.get("contents", []))
      and sorted((r["item_id"], r["quantity"]) for r in stored) == sorted(wanted.items()), body)
gold_before, rows_before = int(sql("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (uid("larderhunter"),))[0]["gold"]), \
    ledger_rows()
for entry in body.get("contents", []):
    took = client.post("/api/loot/take", headers=hunter_t, json={"bag_id": body["bag_id"], "position": entry["position"]})
    check("  %s taken from the bag into the backpack" % entry["item_id"], took.status_code == 200, took.get_json())
check("  and taking them minted no gold and wrote no ledger row",
      int(sql("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (uid("larderhunter"),))[0]["gold"]) == gold_before
      and ledger_rows() == rows_before
      and all(holding("larderhunter", item_id) >= quantity for item_id, quantity in wanted.items()))
invariants("material drops")


# =============================================================================
section("L-2  RECIPES")
# =============================================================================
book = client.get("/api/cooking/recipes", headers=cook_t)
recipes = (book.get_json() or {}).get("recipes", [])
check("the book: every meal in the catalogue, lowest level first", book.status_code == 200
      and [r["id"] for r in recipes] == RECIPES and [r["level"] for r in recipes] == sorted(r["level"] for r in recipes),
      [r["id"] for r in recipes][:5])
first = recipes[0] if recipes else {}
check("  each line: id, name, level, xp, ingredients and buff, read off the catalogue",
      all({"id", "name", "level", "xp", "ingredients", "buff"} <= set(r) for r in recipes)
      and first.get("ingredients") == [{"item_id": i, "quantity": q}
                                       for i, q in gamedata.recipe_of(first.get("id"))["ingredients"]]
      and first.get("name") == name_of(first.get("id")), first)
nested = [r for r in recipes if any(gamedata.recipe_of(i["item_id"]) for i in r["ingredients"])]
check("  and a meal can be an ingredient of another (%s)" % ", ".join(r["id"] for r in nested), len(nested) >= 1)
check("  the book needs a login", client.get("/api/cooking/recipes").status_code == 401)

meal = RECIPES[0]
recipe = gamedata.recipe_of(meal)
need = recipe["ingredients"]


def cook_meal(recipe_id, headers=cook_t, slot=0):
    return client.post("/api/cooking/recipe", headers=headers, json={"slot": slot, "recipe_id": recipe_id})


def stock(extra=0, more=()):
    """The meal's ingredients, `extra` over what it needs, one cell each, and
    `more` after them."""
    entries = [(i, item_id, quantity + extra) for i, (item_id, quantity) in enumerate(need)]
    entries += [(len(entries) + j, item_id, quantity) for j, (item_id, quantity) in enumerate(more)]
    bag("larderchef", entries)


set_skill("larderchef", "cooking", max(recipe["level"], recipe["mastery_level"]))
stock()
xp_before = skill("larderchef", "cooking")[1]
res = cook_meal(meal)
body = res.get_json() or {}
check("%s cooked: every ingredient taken, the meal in the bag, its XP paid" % meal,
      res.status_code == 200 and body.get("burnt") is False and body.get("item_id") == meal
      and body.get("output") == meal and body.get("xp") == recipe["xp"] > 0
      and all(holding("larderchef", item_id) == 0 for item_id, _q in need) and holding("larderchef", meal) == 1
      and skill("larderchef", "cooking") != (max(recipe["level"], recipe["mastery_level"]), xp_before)
      and body.get("skills", {}).get("cooking", {}).get("xp") == skill("larderchef", "cooking")[1], body)
check("  the answer says what it took",
      body.get("consumed") == [{"item_id": i, "quantity": q} for i, q in need], body.get("consumed"))
fish = next(i for i, row in gamedata.ITEMS.items() if row.get("cooks_into"))
set_skill("larderchef", "cooking", 99)
bag("larderchef", [(0, fish, 1)])
fire = client.post("/api/cooking/cook", headers=cook_t, json={"slot": 0, "item_id": fish}).get_json() or {}
check("  and it has every key the fire's own answer has, so one window reads both",
      set(fire) <= set(body) and set(body) - set(fire) == {"consumed"}, (sorted(fire), sorted(body)))

# The hotbar is spent last: a stack on a key is the player's own arrangement.
first_id, first_q = need[0]
bag("larderchef", [(i, item_id, quantity) for i, (item_id, quantity) in enumerate(need)]
    + [(app_module.INVENTORY_CAPACITY, first_id, 5)])
set_skill("larderchef", "cooking", 99)
cook_meal(meal)
check("  the bag's stack is used before the one on a hotbar key",
      cells("larderchef").get(app_module.INVENTORY_CAPACITY) == (first_id, 5), cells("larderchef"))

# THE BURN IS THE FISH'S: burn_chance() off the recipe's level and mastery.
# A recipe above level 1, so its own level and a fish's floor of 1 differ.
mid = [r for r in RECIPES if gamedata.recipe_of(r)["level"] > 1
       and gamedata.recipe_of(r)["mastery_level"] - gamedata.recipe_of(r)["level"] >= 4][0]
mr = gamedata.recipe_of(mid)
level = mr["level"] + (mr["mastery_level"] - mr["level"]) // 2
chance = gamedata.burn_chance({"cook_level": mr["level"], "cook_mastery_level": mr["mastery_level"]}, level)
try:
    gamedata._rng = Die(rest=chance - 1e-9)
    below = gamedata.roll_recipe(mid, level)
    gamedata._rng = Die(rest=chance + 1e-9)
    above = gamedata.roll_recipe(mid, level)
    gamedata._rng = Die(rest=0.0)
    mastered = gamedata.roll_recipe(mid, mr["mastery_level"])
    at_floor = gamedata.roll_recipe(mid, mr["level"])
finally:
    gamedata._rng = saved_rng
check("%s at cooking %d burns %.1f%% of the time - the fish's curve, from the recipe's level and mastery"
      % (mid, level, 100 * chance),
      0 < chance < float(gamedata.CONSTANTS.get("cook_burn_max", 0.4)) and below["burnt"] is True
      and above["burnt"] is False, (chance, below, above))
check("  never at its mastery level, and at its worst at its own level",
      mastered["burnt"] is False and at_floor["burnt"] is True and at_floor["xp"] == 0, (mastered, at_floor))

set_skill("larderchef", "cooking", recipe["level"])
stock()
gamedata._rng = Die(rest=0.0)
try:
    res = cook_meal(meal)
finally:
    gamedata._rng = saved_rng
body = res.get_json() or {}
check("burnt: the ingredients are gone all the same, no meal, no XP",
      gamedata.burn_chance({"cook_level": recipe["level"], "cook_mastery_level": recipe["mastery_level"]},
                           recipe["level"]) > 0
      and res.status_code == 200 and body.get("burnt") is True and body.get("output") == "" and body.get("xp") == 0
      and all(holding("larderchef", item_id) == 0 for item_id, _q in need) and holding("larderchef", meal) == 0
      and body.get("consumed") == [{"item_id": i, "quantity": q} for i, q in need], body)

# THE REFUSALS, each taking nothing.
high = next(r for r in RECIPES if gamedata.recipe_of(r)["level"] > 1)
hr = gamedata.recipe_of(high)
set_skill("larderchef", "cooking", hr["level"] - 1)
bag("larderchef", [(i, item_id, quantity) for i, (item_id, quantity) in enumerate(hr["ingredients"])])
before = cells("larderchef")
check("a cook below the recipe's level: 403 'You need cooking level %d.', nothing taken" % hr["level"],
      (cook_meal(high).status_code, (cook_meal(high).get_json() or {}).get("message"))
      == (403, "You need cooking level %d." % hr["level"]) and cells("larderchef") == before)
set_skill("larderchef", "cooking", 99)
short_id, short_q = need[-1]
bag("larderchef", [(i, item_id, quantity - (1 if item_id == short_id else 0))
                   for i, (item_id, quantity) in enumerate(need) if quantity - (1 if item_id == short_id else 0) > 0])
before = cells("larderchef")
res = cook_meal(meal)
check("one %s short: 404 'You are missing %s.', nothing taken" % (short_id, name_of(short_id)),
      (res.status_code, (res.get_json() or {}).get("message")) == (404, "You are missing %s." % name_of(short_id))
      and cells("larderchef") == before, res.get_json())
sql("INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, 0, ?, 50)", (uid("larderchef"), short_id))
check("  and one in the bank does not count: the recipe is cooked from what is carried",
      cook_meal(meal).status_code == 404)
fill = [("pebbleofnothing%d" % i, 1) for i in range(app_module.INVENTORY_CAPACITY - len(need))]
stock(extra=1, more=fill)
before = cells("larderchef")
res = cook_meal(meal)
check("a full bag (every ingredient's stack only partly used): 409, nothing taken, no XP",
      res.status_code == 409 and "backpack is full" in (res.get_json() or {}).get("message", "")
      and cells("larderchef") == before, res.get_json())
check("not a recipe: a raw fish, a sword, nothing at all - 409, 409, 400",
      [cook_meal(fish).status_code, cook_meal("ironsword").status_code, cook_meal("").status_code] == [409, 409, 400])
check("  a bad slot, an empty one, no login - 400, 404, 401",
      [cook_meal(meal, slot=9).status_code, cook_meal(meal, slot=3).status_code,
       client.post("/api/cooking/recipe", json={"slot": 0, "recipe_id": meal}).status_code] == [400, 404, 401])

# A meal as an ingredient: the nested one is cooked from the meal itself.
outer = nested[0]["id"]
set_skill("larderchef", "cooking", 99)
bag("larderchef", [(i, item_id, quantity) for i, (item_id, quantity) in enumerate(gamedata.recipe_of(outer)["ingredients"])])
res = cook_meal(outer)
check("%s, cooked from a meal among its ingredients" % outer,
      res.status_code == 200 and holding("larderchef", outer) == 1
      and all(holding("larderchef", i) == 0 for i, _q in gamedata.recipe_of(outer)["ingredients"]), res.get_json())
invariants("recipes")


# =============================================================================
section("L-3  MEALS AND BUFFS")
# =============================================================================
def buffed_meal(kind):
    return next(m for m in RECIPES if (gamedata.buff_of(m) or ("",))[0] == kind)


def eat(item_id, headers=cook_t, slot=0, position=None):
    body = {"slot": slot, "item_id": item_id}
    if position is not None:
        body["position"] = position
    return client.post("/api/character/consume", headers=headers, json=body)


check("every meal is a consumable anybody may eat - a character level, no skill gate",
      all(gamedata.item_row(m)["type_name"] == "CONSUMABLE" and not gamedata.item_row(m).get("required_skill")
          for m in RECIPES), [m for m in RECIPES if gamedata.item_row(m).get("required_skill")])
vigour = buffed_meal("VIGOUR")
might = buffed_meal("MIGHT")
v_kind, v_amount, v_seconds = gamedata.buff_of(vigour)
m_kind, m_amount, m_seconds = gamedata.buff_of(might)
sql("UPDATE saves SET level = 99 WHERE user_id = ?", (uid("larderchef"),))
bag("larderchef", [(0, vigour, 2), (1, might, 1), (2, "smallhealthpotion", 1)])
grants_before = len(sql("SELECT id FROM consume_grants WHERE user_id = ?", (uid("larderchef"),)))
res = eat(vigour)
body = res.get_json() or {}
buff = body.get("food_buff") or {}
now = int(time.time())
check("%s eaten: the answer's food_buff - VIGOUR, %d, ends %ds from now" % (vigour, v_amount, v_seconds),
      res.status_code == 200 and (buff.get("item_id"), buff.get("kind"), buff.get("amount")) == (vigour, "VIGOUR", v_amount)
      and abs(buff.get("ends_at", 0) - (now + v_seconds)) <= 3 and isinstance(buff.get("ends_at"), int), body)
check("  it heals what the catalogue says, written down for the healing check",
      body.get("restores") == {"target": "HP", "amount": gamedata.restore_for(vigour)[1]}
      and len(sql("SELECT id FROM consume_grants WHERE user_id = ?", (uid("larderchef"),))) == grants_before + 1)
check("  one row in food_buffs, the kind by its name",
      [tuple(r) for r in sql("SELECT item_id, kind, amount, ends_at FROM food_buffs WHERE user_id = ?",
                             (uid("larderchef"),))] == [(vigour, "VIGOUR", v_amount, buff.get("ends_at"))])
char = client.get("/api/character?slot=0", headers=cook_t).get_json() or {}
listing = client.get("/api/save", headers=cook_t).get_json() or {}
by_slot = {s["slot"]: s for s in listing.get("slots", [])}
check("  /api/character carries the same food_buff the game loads at login", char.get("food_buff") == buff, char.get("food_buff"))
check("  and the /api/save listing, for this character only - the mage's is null",
      by_slot.get(0, {}).get("food_buff") == buff and by_slot.get(1, {}).get("food_buff", "missing") is None,
      {k: v.get("food_buff") for k, v in by_slot.items()})
res = eat("smallhealthpotion")
check("  a potion after it: its answer still carries the meal", (res.get_json() or {}).get("food_buff") == buff,
      res.get_json())
res = eat(might)
second = (res.get_json() or {}).get("food_buff") or {}
check("a second meal replaces the first: MIGHT now, one row",
      (second.get("kind"), second.get("amount")) == ("MIGHT", m_amount)
      and len(sql("SELECT * FROM food_buffs WHERE user_id = ?", (uid("larderchef"),))) == 1, second)

# THE BOOKS. The ticket carries the meal while it lasts, and the bounds rise.
def ticket(headers=cook_t, slot=0):
    raw = client.post("/api/presence/ticket", headers=headers, json={"slot": slot}).get_json()["ticket"]
    found = presence.lookup_ticket(raw)
    return found[1] if found else {}


identity = ticket()
check("the presence ticket carries the meal in force",
      identity.get("buff", {}).get("kind") == "MIGHT" and identity["buff"].get("amount") == m_amount, identity.get("buff"))
fed = gamedata.combat_bounds(identity)
plain = gamedata.combat_bounds({k: v for k, v in identity.items() if k != "buff"})
check("  and the books' bounds read from it rise by the MIGHT meal's %d%%" % m_amount,
      fed["max_hit"] >= math.floor(plain["max_hit"] * (1 + m_amount / 100.0))
      and fed["dps"] >= round(plain["dps"] * (1 + m_amount / 100.0), 2) - 0.02 and fed["speed"] == plain["speed"],
      (fed, plain))
sql("UPDATE food_buffs SET ends_at = ? WHERE user_id = ?", (int(time.time()) - 1, uid("larderchef")))
check("  once it is over the ticket has no buff key at all - the ticket it always was",
      "buff" not in ticket(), ticket().get("buff"))

# Every class, a few loadouts, every buff kind: only MIGHT moves a damage
# bound, and an unbuffed identity is bounded exactly as one with no key.
mismatch, unmoved, wrong_kind = [], [], []
loadouts = [None, ["ironsword", "ironstaff", "ironwand"], ["meteorite", "doubleaxe", "dynamite"]]
for cls in sorted(gamedata.COMBAT.get("classes", {})):
    for gear in loadouts:
        base_identity = {"cls": cls, "lvl": 30, "skills": {"attack": 40, "magic": 40, "agility": 40},
                         "pets": ["petsniper"]}
        if gear is not None:
            base_identity["gear"] = [g for g in gear if gamedata.has_item(g)]
        plain = gamedata.combat_bounds(base_identity)
        if plain != gamedata.combat_bounds(dict(base_identity, buff={"kind": "MIGHT", "amount": 0})):
            mismatch.append((cls, gear))
        for kind in ("GUARD", "VIGOUR", "WISDOM"):
            if gamedata.combat_bounds(dict(base_identity, buff={"kind": kind, "amount": 50})) != plain:
                wrong_kind.append((cls, kind))
        for amount in (3, 12, 50):
            fed = gamedata.combat_bounds(dict(base_identity, buff={"kind": "MIGHT", "amount": amount}))
            if fed["max_hit"] < plain["max_hit"] * (1 + amount / 100.0) - (1 + amount / 100.0) \
                    or fed["dps"] < plain["dps"] * (1 + amount / 100.0) - 0.02:
                unmoved.append((cls, gear, amount, fed, plain))
check("every class and loadout: no meal, or a MIGHT of nothing, is bounded exactly as before", mismatch == [], mismatch)
check("  a GUARD, VIGOUR or WISDOM meal moves no damage bound", wrong_kind == [], wrong_kind[:3])
check("  and a MIGHT meal raises the biggest hit and the damage a second by its percent, at least",
      unmoved == [], unmoved[:2])

# A real buffed hit is never one the books call too big: held to the buffed
# bound, the biggest hit a MIGHT meal makes possible is booked in full.
import combatbook  # noqa: E402
cls = "warrior"
honest = {"cls": cls, "lvl": 30, "skills": {"attack": 40, "magic": 1, "agility": 1}, "gear": ["ironsword"]}
plain = gamedata.combat_bounds(honest)
fed = gamedata.combat_bounds(dict(honest, buff={"kind": "MIGHT", "amount": 50}))
class Book:
    """The one thing a fighter asks of its book when a fresh ticket shows a
    held hit was possible after all."""
    credited = 0

    def credit(self, fighter, monster_id, extra):
        Book.credited += extra


hit = int((plain["max_hit"] + fed["max_hit"]) * combatbook.SLACK / 2)
booked = int(plain["max_hit"] * combatbook.SLACK)
held_plain = combatbook.Fighter(1, 0)
held_plain.set_bounds(plain, 0.0)
held_plain.pending.append([0.0, Book(), "m1", hit, booked])
held_plain.set_bounds(plain, 1.0)
held_fed = combatbook.Fighter(1, 0)
held_fed.set_bounds(plain, 0.0)
held_fed.pending.append([0.0, Book(), "m1", hit, booked])
held_fed.set_bounds(fed, 1.0)
check("a hit past the unbuffed bound's room is held as too big - and booked in full once the meal's ticket comes",
      plain["max_hit"] * combatbook.SLACK < hit <= fed["max_hit"] * combatbook.SLACK
      and len(held_plain.pending) == 1 and held_fed.pending == [] and Book.credited == hit - booked,
      (hit, plain["max_hit"], fed["max_hit"], Book.credited))

# THE AURA'S HEAL covers a VIGOUR meal's health.
presence.DB_PATH = DB_PATH
bag("larderchef", [(0, vigour, 1)])
sql("DELETE FROM food_buffs WHERE user_id = ?", (uid("larderchef"),))
bare_grant = presence.write_heal_grants([(uid("larderchef"), 0, 0.25)], int(time.time()))
eat(vigour)
fed_grant = presence.write_heal_grants([(uid("larderchef"), 0, 0.25)], int(time.time()))
check("the aura's heal grant is sized on the buffed health while a VIGOUR meal lasts",
      bare_grant and fed_grant and fed_grant[0][4] - bare_grant[0][4] == math.ceil(
          (gamedata.max_stats_for("warrior", 99, {})["max_hp"] + v_amount) * 0.25)
      - math.ceil(gamedata.max_stats_for("warrior", 99, {})["max_hp"] * 0.25), (bare_grant, fed_grant))

# THE STATUS ROUTE'S CEILING counts the meal too (0.74.0, found at the merge):
# it clamps health to app.py's _derived_stats(), and without the meal a fed
# character above its bare maximum was cut back to it on every save.
bare_top = gamedata.max_stats_for("warrior", 99, {})["max_hp"]
sql("UPDATE saves SET hp = ?, max_hp = ? WHERE user_id = ? AND slot = 0",
    (bare_top + v_amount, bare_top + v_amount, uid("larderchef")))
res = client.put("/api/player/status", headers=cook_t, json={"slot": 0, "hp": bare_top + v_amount})
kept = sql("SELECT hp, max_hp FROM saves WHERE user_id = ? AND slot = 0", (uid("larderchef"),))
check("a save while a VIGOUR meal lasts keeps the health it lifts the ceiling to",
      res.status_code == 200 and [tuple(r) for r in kept] == [(bare_top + v_amount, bare_top + v_amount)],
      (res.status_code, [tuple(r) for r in kept], bare_top, v_amount))
sql("UPDATE food_buffs SET ends_at = ? WHERE user_id = ?", (int(time.time()) - 5, uid("larderchef")))
res = client.put("/api/player/status", headers=cook_t, json={"slot": 0, "hp": bare_top + v_amount})
after = sql("SELECT hp, max_hp FROM saves WHERE user_id = ? AND slot = 0", (uid("larderchef"),))
check("  and once it is over, the next save comes back down to the bare ceiling",
      res.status_code == 200 and [tuple(r) for r in after] == [(bare_top, bare_top)], [tuple(r) for r in after])

sql("UPDATE food_buffs SET ends_at = ? WHERE user_id = ?", (int(time.time()) - 5, uid("larderchef")))
char = client.get("/api/character?slot=0", headers=cook_t).get_json() or {}
listing = client.get("/api/save", headers=cook_t).get_json() or {}
check("a meal that is over is null on the load and the listing",
      char.get("food_buff", "missing") is None
      and all(s.get("food_buff", "missing") is None for s in listing.get("slots", [])), (char.get("food_buff"), listing))

lofty = next((m for m in RECIPES if int(gamedata.item_row(m).get("required_level", 1)) > 1), None)
check("some meal asks a character level (%s)" % lofty, lofty is not None)
if lofty is not None:
    sql("UPDATE saves SET level = 1 WHERE user_id = ? AND slot = 0", (uid("larderchef"),))
    bag("larderchef", [(0, lofty, 1)])
    res = eat(lofty)
    check("a meal above the character's level: 403, kept, no buff",
          res.status_code == 403 and holding("larderchef", lofty) == 1
          and sql("SELECT * FROM food_buffs WHERE user_id = ? AND ends_at > ?", (uid("larderchef"), int(time.time()))) == [],
          res.get_json())
    sql("UPDATE saves SET level = 99 WHERE user_id = ? AND slot = 0", (uid("larderchef"),))

bag("larderchef", [(0, might, 1)], slot=1)
eat(might, slot=1)
check("the mage eats too: its own row", len(sql("SELECT * FROM food_buffs WHERE user_id = ? AND slot = 1",
                                                (uid("larderchef"),))) == 1)
res = client.post("/api/character/delete", headers=cook_t, json={"slot": 1, "confirm": "mage"})
check("  and deleting the mage takes its meal with it", res.status_code == 200
      and sql("SELECT * FROM food_buffs WHERE user_id = ? AND slot = 1", (uid("larderchef"),)) == [], res.get_json())
invariants("meals")


# =============================================================================
section("L-4  BAIT")
# =============================================================================
angler_t = register("larderangler")
make_char(angler_t)
rod = next(i for i in gamedata.ITEMS if i.endswith(gathering.FISHING_ROD_SUFFIX))
by_rolls = sorted(BAITS, key=lambda b: (-gamedata.bait_rolls(b), b))
best, middle = by_rolls[0], by_rolls[1]
worm = gathering.FISHING_BAIT_ID
check("the worm is bait at one roll, and the new baits roll more",
      gamedata.bait_rolls(worm) == 1 and gamedata.bait_rolls(best) > gamedata.bait_rolls(middle) > 1, by_rolls)


def cast():
    sql("UPDATE saves SET cast_tokens = ? WHERE user_id = ?", (app_module.CAST_BUCKET_CAPACITY, uid("larderangler")))
    return client.post("/api/fishing/catch", headers=angler_t, json={"slot": 0})


casts = []
real_catch = gamedata.roll_fishing_catch
FISH = sorted((i for i, row in gamedata.ITEMS.items() if row["type_name"] == "FISH"), key=lambda i: gamedata.ITEMS[i]["tier"])
script = []


def scripted(rod_tier, level):
    casts.append((rod_tier, level))
    if script:
        item_id = script.pop(0)
        return {"item_id": item_id, "quantity": 1, "xp": int(gamedata.ITEMS[item_id].get("fishing_xp", 0))}
    return real_catch(rod_tier, level)


bag("larderangler", [(0, rod, 1)])
res = cast()
check("no bait at all is the refusal it always was", (res.status_code, (res.get_json() or {}).get("message"))
      == (403, "You need worms for bait."), res.get_json())

gamedata.roll_fishing_catch = scripted
try:
    bag("larderangler", [(0, rod, 1), (1, worm, 3)])
    casts.clear()
    res = cast()
    body = res.get_json() or {}
    check("worms only: the worm is the bait, one spent, the catch rolled once",
          res.status_code == 200 and body.get("bait") == worm and holding("larderangler", worm) == 2 and len(casts) == 1,
          body)
    bag("larderangler", [(0, rod, 1), (1, worm, 3), (2, middle, 2), (app_module.INVENTORY_CAPACITY, best, 1)])
    casts.clear()
    low, top = FISH[0], FISH[-1]
    mid_fish = next(f for f in FISH if gamedata.ITEMS[f]["tier"] > gamedata.ITEMS[low]["tier"]) if len(FISH) > 1 else low
    script[:] = [low, top, mid_fish][:gamedata.bait_rolls(best)]
    res = cast()
    body = res.get_json() or {}
    check("the best bait carried (%s, on a hotbar key) is used: one spent, the others kept" % best,
          res.status_code == 200 and body.get("bait") == best and holding("larderangler", best) == 0
          and holding("larderangler", middle) == 2 and holding("larderangler", worm) == 3, body)
    check("  rolled %d times, and the highest-tier fish kept (%s)" % (gamedata.bait_rolls(best), top),
          len(casts) == gamedata.bait_rolls(best) and body.get("item_id") == top and holding("larderangler", top) >= 1,
          (casts, body.get("item_id")))
    casts.clear()
    script[:] = [low, low]
    tie_a = next((f for f in FISH if f != low and gamedata.ITEMS[f]["tier"] == gamedata.ITEMS[low]["tier"]), None)
    if tie_a is not None:
        script[:] = [tie_a, low]
    res = cast()
    body = res.get_json() or {}
    check("  then the next best, %s, twice - a tie of tiers keeps the first rolled" % middle,
          res.status_code == 200 and body.get("bait") == middle and len(casts) == gamedata.bait_rolls(middle)
          and body.get("item_id") == (tie_a or low), (casts, body))
    full = [(0, rod, 1), (1, middle, 2)] + [(p, "pebbleofnothing%d" % p, 1)
                                            for p in range(2, app_module.INVENTORY_CAPACITY)]
    bag("larderangler", full)
    casts.clear()
    res = cast()
    check("a full bag: 409, and the bait is not spent", res.status_code == 409 and holding("larderangler", middle) == 2,
          res.get_json())
finally:
    gamedata.roll_fishing_catch = real_catch
    script.clear()

# The rule itself: most rolls, a tie to the first id; the worm always bait.
rows = {"a": {"bait_rolls": 2}, "b": {"bait_rolls": 2}, "c": {"bait_rolls": 1}, "rock": {}}
check("best_bait: the most rolls, a tie to the first id, nothing for no bait",
      gathering.services.best_bait(["c", "b", "a", "rock"], rows.get) == ("a", 2)
      and gathering.services.best_bait(["rock"], rows.get) == ("", 0)
      and gathering.services.best_bait([worm], {}.get) == (worm, 1))
check("best_catch: the highest tier, a tie to the first",
      gathering.services.best_catch([{"item_id": "x"}, {"item_id": "y"}, {"item_id": "z"}],
                                    {"x": {"tier": 1}, "y": {"tier": 3}, "z": {"tier": 3}}.get) == {"item_id": "y"})
invariants("bait")


print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
