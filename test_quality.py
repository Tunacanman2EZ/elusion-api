"""
Quality rolls on dropped and bought gear. Run: python3 test_quality.py

WHAT A ROLL IS. The owner, 5 Oct: random stats on every item, "because it
gives loot a better value if a rare max roll". A dropped piece of gear rolls
every stat it has - damage, armour, max health, max mana, the damage bonus -
each on its own, QUALITY_LOW to QUALITY_HIGH percent of the catalogue number,
most often near 100; one drop in QUALITY_PERFECT_ODDS is Perfect, every stat at
QUALITY_PERFECT. A piece bought from the shop rolls the same way, at the till:
"item stats say ? and are revealed upon buying in shop only".

WHERE IT LIVES. In the item id: "jadechest~a104h96". Every cell, bag entry,
trade row and the equipment map already hold an id, and every route that moves
one checks "the item the game saw in this cell" - so a roll moves by the paths
that exist, and those checks guard the roll too. What had to change is every
question about what an id IS, and those go through gamedata.item_row(). This
suite holds both halves: the roll, and that each path carries it whole.

Runs against a throwaway database in the temp folder, never elusion.db.
"""
import collections, importlib.util, os, random, re, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Temp directory, not beside this file - see the note in test_healing.py.
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_quality_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "qowner"
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
failures = []


def check(label, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n=== %s ===\n" % title)


def account(username, class_id="warrior"):
    client.post("/api/auth/register", json={"username": username, "password": "password123"})
    token = client.post("/api/auth/login", json={"username": username, "password": "password123"}
                        ).get_json()["token"]
    headers = {"Authorization": "Bearer " + token}
    client.put("/api/save", headers=headers,
               json={"slot": 0, "class_id": class_id, "name": username.capitalize()})
    return headers


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


def seed_carry(username, cells):
    """carry_items exactly as given, {position: (item_id, quantity)}."""
    user = uid(username)
    sql("DELETE FROM carry_items WHERE user_id = ? AND slot = 0", (user,))
    for pos, (item, qty) in cells.items():
        sql("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 0, ?, ?, ?)",
            (user, pos, item, qty))


def seed_gold(username, amount):
    # Into the purse AND the ledger, so the supply invariant still holds.
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = 0", (amount, uid(username)))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (0, ?, 0, ?, 'test_seed', 'suite setup')", (uid(username), amount))


def carry_cells(username):
    return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in sql(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = 0",
        (uid(username),))}


def bank_cells(username):
    return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in sql(
        "SELECT position, item_id, quantity FROM bank_items WHERE user_id = ?", (uid(username),))}


def holds(username, item_id):
    return sum(q for (i, q) in carry_cells(username).values() if i == item_id)


def save_row(username):
    return sql("SELECT * FROM saves WHERE user_id = ? AND slot = 0", (uid(username),))[0]


def present(*headers):
    for h in headers:
        client.get("/api/server/broadcasts", headers=h)


def post(headers, path, body):
    return client.post(path, headers=headers, json=body)


Q = gamedata
LOW, HIGH, PERFECT = Q.QUALITY_LOW, Q.QUALITY_HIGH, Q.QUALITY_PERFECT
GEAR = sorted(i for i, row in Q.ITEMS.items()
              if row["type_name"] in Q.LOOT_GEAR_TYPES and Q.rolled_fields(row))


# =============================================================================
section("Q-1 THE RANGE IS THE GAME'S")
# =============================================================================
consts = Q.CONSTANTS
check("gamedata.json carries the range, the Perfect and the odds",
      all(k in consts for k in ("quality_low", "quality_high", "quality_perfect", "quality_perfect_odds")),
      sorted(k for k in consts if k.startswith("quality")))
check("  and they are what the server rolls with",
      (LOW, HIGH, PERFECT, Q.QUALITY_PERFECT_ODDS) == (
          consts.get("quality_low"), consts.get("quality_high"),
          consts.get("quality_perfect"), consts.get("quality_perfect_odds")),
      (LOW, HIGH, PERFECT, Q.QUALITY_PERFECT_ODDS))
check("the owner's numbers: 85 to 115, Perfect at 120, one in 100",
      (LOW, HIGH, PERFECT, Q.QUALITY_PERFECT_ODDS) == (85, 115, 120, 100))
check("the five stats roll, one letter each, in the export's order",
      [f for _, f in Q.QUALITY_FIELDS] == ["damage", "armor_value", "bonus_max_hp",
                                          "bonus_max_mana", "bonus_damage_percent"]
      and len({l for l, _ in Q.QUALITY_FIELDS}) == 5, Q.QUALITY_FIELDS)
check("every weapon and armour piece has something to roll", len(GEAR) == sum(
      1 for row in Q.ITEMS.values() if row["type_name"] in Q.LOOT_GEAR_TYPES), len(GEAR))


# =============================================================================
section("Q-2 READING A ROLL")
# =============================================================================
row = Q.item_row("jadeamulet~a104h96m110p88")
base = Q.ITEMS["jadeamulet"]
check("a rolled id is an item", Q.has_item("jadeamulet~a104h96m110p88") and row is not None)
check("  each stat is the catalogue number at its percent, halves up",
      (row["armor_value"], row["bonus_max_hp"], row["bonus_max_mana"], row["bonus_damage_percent"])
      == (Q.scale_stat(base["armor_value"], 104), Q.scale_stat(base["bonus_max_hp"], 96),
          Q.scale_stat(base["bonus_max_mana"], 110), Q.scale_stat(base["bonus_damage_percent"], 88)),
      row)
check("  it keeps the base piece's price, tier, slot, classes and level",
      all(row[k] == base[k] for k in ("value", "tier", "type_name", "equip_slot_name",
                                      "required_classes", "required_level", "max_stack")))
check("  and says what it is a roll of",
      row["base_id"] == "jadeamulet" and row["rolls"] == {
          "armor_value": 104, "bonus_max_hp": 96, "bonus_max_mana": 110, "bonus_damage_percent": 88}
      and row["perfect"] is False and row["display_name"] == base["display_name"], row)
check("a plain id is the catalogue row itself", Q.item_row("jadeamulet") is base)

# THE SAME SUM AS THE GAME. ItemRegistry.scale_stat() is integer arithmetic for
# the same reason; testrunner.gd's _test_quality_rolls_read_like_the_server
# holds these exact pairs, so changing one side's rounding fails both suites.
SHARED = [(20, 107, 21), (20, 115, 23), (20, 85, 17), (10, 85, 9), (10, 115, 12),
          (1, 85, 1), (1, 120, 1), (3, 120, 4), (3, 115, 3), (6, 96, 6), (100, 87, 87), (0, 120, 0)]
check("scale_stat matches the table the game's suite also holds",
      [Q.scale_stat(n, p) for n, p, _ in SHARED] == [want for _, _, want in SHARED],
      [(n, p, Q.scale_stat(n, p)) for n, p, _ in SHARED])
check("a roll never takes a stat to zero (85% of 1 is 1)",
      all(Q.scale_stat(1, p) == 1 for p in range(LOW, HIGH + 1)))

perfect = Q.perfect_id("jadeamulet")
prow = Q.item_row(perfect)
check("the Perfect roll is every stat at 120", perfect == "jadeamulet~a120h120m120p120", perfect)
check("  and is named Perfect", prow["display_name"] == "Perfect " + base["display_name"]
      and prow["perfect"] is True and Q.is_perfect(perfect), prow["display_name"])
check("an ordinary roll is not Perfect", not Q.is_perfect("jadeamulet~a104h96m110p88"))

MALFORMED = [
    "ironsword~", "ironsword~d", "ironsword~d999", "ironsword~d84", "ironsword~d116",
    "ironsword~d119", "ironsword~d084", "ironsword~d0100", "ironsword~D100", "ironsword~d100x",
    "ironsword~a100",                       # a stat the sword does not have
    "ironsword~d100d100",                   # a stat twice
    "jadeamulet~a100h100m100",              # one missing
    "jadeamulet~h100a100m100p100",          # out of order
    "jadeamulet~a120h120m120p115",          # a Perfect is every stat or none
    "nosuchthing~d100", "~d100", "ironsword~d100~d100",
    "tinyhealthpotion~d100",                # nothing on a potion rolls
]
bad = [m for m in MALFORMED if Q.split_variant(m) != (None, None) or Q.has_item(m) or Q.item_row(m)]
check("everything that only looks like a roll is not an item (%d spellings)" % len(MALFORMED), not bad, bad)
check("one roll has one spelling, so a cell check cannot be fooled by a second",
      Q.has_item("ironsword~d100") and not Q.has_item("ironsword~d100a100"))


# =============================================================================
section("Q-3 THE ROLL ITSELF")
# =============================================================================
saved_rng = Q._rng
Q._rng = random.Random(20261005)
try:
    N = 20000
    stats = collections.Counter()
    perfects = 0
    every_value = []
    all_equal = multi = 0
    for _ in range(N):
        rolled = Q.roll_quality("jadeamulet")
        b, rolls = Q.split_variant(rolled)
        if b != "jadeamulet" or len(rolls) != 4:
            stats["malformed"] += 1
            continue
        if set(rolls.values()) == {PERFECT}:
            perfects += 1
            continue
        every_value.extend(rolls.values())
        multi += 1
        if len(set(rolls.values())) == 1:
            all_equal += 1
    swords = [Q.roll_quality("ironsword") for _ in range(2000)]
finally:
    Q._rng = saved_rng

check("every roll reads back as a roll of the piece", stats["malformed"] == 0, stats)
check("about one in a hundred is Perfect (%d in %d)" % (perfects, N), 0.007 <= perfects / N <= 0.013)
check("every other stat lands in 85 to 115",
      every_value and min(every_value) >= LOW and max(every_value) <= HIGH,
      (min(every_value), max(every_value)))
mean = sum(every_value) / len(every_value)
check("and averages 100, so drops are the catalogue on the whole (%.2f)" % mean, 99.5 <= mean <= 100.5)
near = sum(1 for v in every_value if 95 <= v <= 105) / len(every_value)
top = sum(1 for v in every_value if v >= 113) / len(every_value)
check("most sit near 100 (%.0f%% within 5)" % (near * 100), 0.5 <= near <= 0.65)
check("and the top of the range is the rare part (%.2f%% at 113 or more)" % (top * 100), 0 < top < 0.02)
check("every percent from 85 to 115 turns up", set(every_value) == set(range(LOW, HIGH + 1)),
      sorted(set(range(LOW, HIGH + 1)) - set(every_value)))
check("each stat rolls on its own (all four the same in %d of %d)" % (all_equal, multi),
      all_equal / multi < 0.01)
check("a one-stat sword rolls only its damage", all(re.fullmatch(r"ironsword~d\d{2,3}", s) for s in swords),
      swords[:3])

nothing = ["tinyhealthpotion", "ironfishingrod", "fishingworm", "coppercoin", "petsniper", "cookedmudfish"]
nothing = [i for i in nothing if i in Q.ITEMS]
check("a potion, a rod, a worm, a coin, a pet and a cooked fish never roll",
      all(Q.roll_quality(i) == i and Q.perfect_id(i) == i for i in nothing) and len(nothing) >= 5, nothing)
check("an unknown id is handed back as it came", Q.roll_quality("nosuchthing") == "nosuchthing")


# =============================================================================
section("Q-4 A DROP ROLLS, THE SHELF DOES NOT")
# =============================================================================
Q._rng = random.Random(4)
try:
    boss = next(e for e in Q.ENEMIES.values() if e.get("slots_are_gear") and e.get("grants_rewards", True))
    gear_drops = []
    potions = []
    for _ in range(400):
        for c in Q.build_bag_contents(boss):
            r = Q.item_row(c["item_id"])
            if r and r["type_name"] in Q.LOOT_GEAR_TYPES:
                gear_drops.append(c["item_id"])
            elif r and r["type_name"] == "CONSUMABLE":
                potions.append(c["item_id"])
finally:
    Q._rng = saved_rng
check("every piece of gear a bag holds carries a roll (%d pieces)" % len(gear_drops),
      gear_drops and all(Q.VARIANT_MARK in i and Q.has_item(i) for i in gear_drops), gear_drops[:3])
check("and a potion beside it does not", potions and all(Q.VARIANT_MARK not in i for i in potions),
      potions[:3])
check("the shelf holds only catalogue ids", all(
      Q.VARIANT_MARK not in i for shop in Q.SHOPS.values() for i in shop.get("stock", [])))
check("and a roll cannot be bought: the shop has no price for one",
      Q.shop_price("generalstore", "ironsword~d107") is None
      and Q.shop_price("generalstore", "ironsword") is not None)


# =============================================================================
section("Q-5 THE PATHS CARRY IT WHOLE")
# =============================================================================
OWNER = account("qowner")
ALICE = account("qalice")
BOB = account("qbob", "mage")

# A KILL. darksprite's mythic odds at one in one, so the win is certain; the
# mythic in the bag is rolled like any drop.
saved_odds = int(Q.ENEMIES["darksprite"].get("mythic_odds", 0))
Q.ENEMIES["darksprite"]["mythic_odds"] = 1
try:
    kill = post(ALICE, "/api/combat/kill", {"slot": 0, "enemy_id": "darksprite"}).get_json() or {}
finally:
    Q.ENEMIES["darksprite"]["mythic_odds"] = saved_odds
won = str(kill.get("mythic", ""))
check("a kill's mythic is rolled", Q.base_id(won) == "doubleaxe" and Q.VARIANT_MARK in won
      and Q.has_item(won), won)
check("  and the bag holds that very roll",
      any(c["item_id"] == won for c in kill.get("contents", [])), kill.get("contents"))
res = post(ALICE, "/api/loot/take", {"bag_id": kill.get("bag_id"), "position": 0})
check("taking it puts the roll in the backpack", res.status_code == 200 and holds("qalice", won) == 1,
      [res.status_code, carry_cells("qalice")])

# A STAFF GRANT, three ways.
res = post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "jadeamulet", "quality": "perfect"})
body = res.get_json() or {}
check("the owner can grant a Perfect piece", res.status_code == 200
      and body.get("granted_item_id") == Q.perfect_id("jadeamulet"), [res.status_code, body.get("granted_item_id")])
res = post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "jadeamulet", "quality": "roll"})
rolled_grant = (res.get_json() or {}).get("granted_item_id", "")
check("  a rolled one, like a drop", res.status_code == 200 and Q.base_id(rolled_grant) == "jadeamulet"
      and Q.VARIANT_MARK in rolled_grant and Q.has_item(rolled_grant), rolled_grant)
res = post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "jadeamulet"})
check("  and, by default, the plain catalogue piece at 100%",
      res.status_code == 200 and (res.get_json() or {}).get("granted_item_id") == "jadeamulet")
res = post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "jadeamulet", "quality": "store"})
check("  \"store\", the name the build-3 game sends, is read as plain",
      res.status_code == 200 and (res.get_json() or {}).get("granted_item_id") == "jadeamulet")
res = post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "tinyhealthpotion", "quantity": 3, "quality": "roll"})
check("a potion asked to roll is granted as a potion",
      res.status_code == 200 and (res.get_json() or {}).get("granted_item_id") == "tinyhealthpotion")
check("a quality the server does not know is 400",
      post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "ironsword", "quality": "legendary"}).status_code == 400)
check("so is an id that looks like a roll and is not one",
      post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "ironsword~d300"}).status_code == 400)
check("two of one rolled sword is refused: a rolled piece stacks to one",
      post(OWNER, "/api/staff/grant", {"slot": 0, "item_id": "ironsword~d107", "quantity": 2}).status_code == 400)
check("a player cannot grant at all", post(ALICE, "/api/staff/grant",
      {"slot": 0, "item_id": "ironsword", "quality": "perfect"}).status_code == 404)

# A PURCHASE ROLLS AT THE TILL. The shelf lists the catalogue piece; what is
# handed over is rolled the way a drop is, at the shelf's price.
seed_gold("qalice", 500000)
seed_carry("qalice", {})
gold_before = int(save_row("qalice")["gold"])
res = post(ALICE, "/api/shop/buy", {"slot": 0, "shop_id": "generalstore", "item_id": "jadeamulet"})
body = res.get_json() or {}
got = str(body.get("item_id", ""))
check("a piece of gear bought from the shop arrives rolled",
      res.status_code == 200 and Q.base_id(got) == "jadeamulet" and Q.VARIANT_MARK in got and Q.has_item(got),
      [res.status_code, got])
check("  the answer names the roll that arrived, and the shelf id it was bought as",
      body.get("stock_id") == "jadeamulet" and holds("qalice", got) == 1, body.get("stock_id"))
check("  at the shelf's price, whatever it rolled",
      body.get("total_paid") == Q.shop_price("generalstore", "jadeamulet")
      and int(save_row("qalice")["gold"]) == gold_before - Q.shop_price("generalstore", "jadeamulet"),
      body.get("total_paid"))
ledger = sql("SELECT detail FROM gold_ledger WHERE user_id = ? AND reason = 'shop_buy' ORDER BY rowid DESC LIMIT 1",
             (uid("qalice"),))
check("  and the ledger records which roll was paid for",
      bool(ledger) and ledger[0]["detail"].startswith(got + " x1"), ledger[0]["detail"] if ledger else None)
seed_carry("qalice", {})
rolls = []
for _ in range(25):
    seed_carry("qalice", {})   # the bag holds twenty; emptied so every buy lands
    r = post(ALICE, "/api/shop/buy", {"slot": 0, "shop_id": "generalstore", "item_id": "ironsword"})
    rolls.append(str((r.get_json() or {}).get("item_id", "")))
check("every purchase rolls on its own (25 iron swords, %d different rolls)" % len(set(rolls)),
      all(Q.base_id(i) == "ironsword" and Q.has_item(i) and Q.VARIANT_MARK in i for i in rolls)
      and len(set(rolls)) >= 5, rolls[:5])
seed_carry("qalice", {})
res = post(ALICE, "/api/shop/buy", {"slot": 0, "shop_id": "generalstore", "item_id": "tinyhealthpotion", "quantity": 3})
check("a potion has nothing to roll and comes as it is",
      res.status_code == 200 and (res.get_json() or {}).get("item_id") == "tinyhealthpotion"
      and holds("qalice", "tinyhealthpotion") == 3, res.get_json())
check("and the shop will not sell a roll by name", post(ALICE, "/api/shop/buy",
      {"slot": 0, "shop_id": "generalstore", "item_id": "jadeamulet~a120h120m120p120"}).status_code in (400, 404))

# A DRAG. Two pieces of one roll do not merge: a rolled piece stacks to one.
twin = "ironsword~d107"
seed_carry("qalice", {0: (twin, 1), 1: (twin, 1), 2: ("ironsword~d93", 1)})
res = post(ALICE, "/api/character/inventory/move", {"slot": 0, "from": 0, "to": 1, "item_id": twin})
check("dragging one roll onto its twin swaps them, it does not stack them",
      res.status_code == 200 and carry_cells("qalice").get(0) == (twin, 1)
      and carry_cells("qalice").get(1) == (twin, 1), [res.status_code, carry_cells("qalice")])
res = post(ALICE, "/api/character/inventory/move", {"slot": 0, "from": 2, "to": 9, "item_id": "ironsword~d93"})
check("a move keeps the roll", res.status_code == 200 and carry_cells("qalice").get(9) == ("ironsword~d93", 1),
      carry_cells("qalice"))
res = post(ALICE, "/api/character/inventory/move", {"slot": 0, "from": 9, "to": 3, "item_id": "ironsword~d107"})
check("naming a different roll of the same sword is the game's picture out of date (409)",
      res.status_code == 409 and carry_cells("qalice").get(9) == ("ironsword~d93", 1), res.status_code)
res = post(ALICE, "/api/character/inventory/move", {"slot": 0, "from": 9, "to": 3, "item_id": "ironsword"})
check("  and so is naming the plain sword", res.status_code == 409, res.status_code)

# THE BANK.
seed_carry("qalice", {4: (twin, 1)})
res = post(ALICE, "/api/bank/items", {"slot": 0, "op": "deposit", "item_id": twin, "quantity": 1, "position": 4})
check("a roll goes into the bank as itself",
      res.status_code == 200 and twin in [i for i, _ in bank_cells("qalice").values()] and holds("qalice", twin) == 0,
      [res.status_code, bank_cells("qalice")])
res = post(ALICE, "/api/bank/items", {"slot": 0, "op": "withdraw", "item_id": twin, "quantity": 1})
check("  and comes out as itself", res.status_code == 200 and holds("qalice", twin) == 1
      and twin not in [i for i, _ in bank_cells("qalice").values()], [res.status_code, carry_cells("qalice")])

# WEARING IT. The server's maximum counts the rolled bonus, not the catalogue's.
sql("UPDATE saves SET level = ? WHERE user_id = ? AND slot = 0",
    (int(Q.ITEMS["jadeamulet"]["required_level"]), uid("qalice")))
post(ALICE, "/api/character/unequip", {"slot": 0, "equip_slot": "amulet"})
bare = Q.max_stats_for("warrior", int(Q.ITEMS["jadeamulet"]["required_level"]))
perfect_amulet = Q.perfect_id("jadeamulet")
low_amulet = "jadeamulet~a85h85m85p85"
seed_carry("qalice", {0: (perfect_amulet, 1), 1: (low_amulet, 1), 2: ("jadeamulet", 1)})
for item_id in (perfect_amulet, low_amulet, "jadeamulet"):
    res = post(ALICE, "/api/character/equip", {"slot": 0, "item_id": item_id})
    after = save_row("qalice")
    import json as _json
    worn = _json.loads(after["equipment"] or "{}")
    want = Q.item_row(item_id)
    check("wearing %s: the equipment map holds the roll" % item_id,
          res.status_code == 200 and worn.get("amulet") == item_id, [res.status_code, worn])
    check("  max health is +%d and max mana +%d, the rolled numbers" % (want["bonus_max_hp"], want["bonus_max_mana"]),
          int(after["max_hp"]) - bare["max_hp"] == want["bonus_max_hp"]
          and int(after["max_mana"]) - bare["max_mana"] == want["bonus_max_mana"],
          (int(after["max_hp"]) - bare["max_hp"], int(after["max_mana"]) - bare["max_mana"]))
check("a Perfect Jade Amulet is +12 health where the catalogue's is +10",
      Q.item_row(perfect_amulet)["bonus_max_hp"] == 12 and Q.ITEMS["jadeamulet"]["bonus_max_hp"] == 10)
res = post(ALICE, "/api/character/unequip", {"slot": 0, "equip_slot": "amulet"})
check("taking it off brings the maximum back to bare",
      res.status_code == 200 and int(save_row("qalice")["max_hp"]) == bare["max_hp"])
check("and both rolls are back in the bag, each as itself",
      holds("qalice", perfect_amulet) == 1 and holds("qalice", low_amulet) == 1 and holds("qalice", "jadeamulet") == 1,
      carry_cells("qalice"))

# A TRADE.
seed_gold("qbob", 50000)
present(ALICE, BOB)
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "qbob"})
check("alice opens a trade", res.status_code == 200, res.get_json())
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": perfect_amulet, "quantity": 1}]})
body = res.get_json() or {}
check("she can offer the Perfect amulet by its rolled id", res.status_code == 200, body)
check("  valued at the catalogue price - a roll changes what a piece does, not its price",
      (body.get("a") or {}).get("offering_value") == Q.ITEMS["jadeamulet"]["value"], body.get("a"))
check("she cannot offer a roll she does not hold", post(ALICE, "/api/trade/update",
      {"items": [{"item_id": "jadeamulet~a110h110m110p110", "quantity": 1}]}).status_code == 400)
check("nor one that is not a roll at all", post(ALICE, "/api/trade/update",
      {"items": [{"item_id": "jadeamulet~a999", "quantity": 1}]}).status_code == 400)
post(ALICE, "/api/trade/update", {"items": [{"item_id": perfect_amulet, "quantity": 1}]})
for h in (ALICE, BOB):
    rev = ((client.get("/api/trade", headers=h).get_json() or {}).get("trade") or {}).get("revision", -1)
    res = post(h, "/api/trade/confirm", {"revision": rev})
check("the trade runs", res.status_code == 200 and (res.get_json() or {}).get("state") == "done", res.get_json())
check("bob has the Perfect amulet, Perfect", holds("qbob", perfect_amulet) == 1, carry_cells("qbob"))
check("and alice still has her other two", holds("qalice", perfect_amulet) == 0
      and holds("qalice", low_amulet) == 1 and holds("qalice", "jadeamulet") == 1, carry_cells("qalice"))

# A SALE.
cells = carry_cells("qalice")
pos = next(p for p, (i, _) in cells.items() if i == low_amulet)
gold_before = int(save_row("qalice")["gold"])
res = post(ALICE, "/api/shop/sell", {"slot": 0, "shop_id": "generalstore", "position": pos,
                                     "item_id": low_amulet, "quantity": 1})
body = res.get_json() or {}
want = Q.shop_sell_price("generalstore", "jadeamulet")
check("the shop buys a roll at its base piece's price (%s)" % want,
      res.status_code == 200 and body.get("unit_price") == want
      and int(save_row("qalice")["gold"]) == gold_before + want, [res.status_code, body.get("unit_price")])
check("  and the roll is gone from the bag", holds("qalice", low_amulet) == 0)

# THE CAP, on a roll. A shop never pays what it charges; the shelf holds no
# roll, so the cap has to be the base piece's price or a roll would skip it.
# It only bites on a mistake, so the mistake is made here on purpose.
shop = Q.SHOPS["generalstore"]
saved_rate = shop.get("sell_multiplier")
shop["sell_multiplier"] = 5.0
try:
    capped = Q.shop_sell_price("generalstore", "ironsword~d107")
    charged = Q.shop_price("generalstore", "ironsword")
finally:
    shop["sell_multiplier"] = saved_rate
check("a roll is still never bought back at the price the shop sells it for",
      capped == charged - 1, (capped, charged))

# THE BIN.
seed_carry("qalice", {7: (won, 1)})
res = post(ALICE, "/api/character/inventory/discard", {"slot": 0, "position": 7, "item_id": "doubleaxe"})
check("the bin will not take a roll named as the plain piece", res.status_code == 409
      and holds("qalice", won) == 1, res.status_code)
res = post(ALICE, "/api/character/inventory/discard", {"slot": 0, "position": 7, "item_id": won})
check("and takes it by its rolled id", res.status_code == 200 and holds("qalice", won) == 0,
      res.status_code)


# =============================================================================
section("Q-6 THE MYTHIC NOTICE NAMES THE ROLL")
# =============================================================================
text = app_module.mythic_find_text("qalice", Q.perfect_id("meteorite"), "darksprite")
check("a Perfect find is told as one", text == "qalice found the Perfect Meteorite on a Dark Sprite!", text)
text = app_module.mythic_find_text("qalice", "meteorite~d97p103", "darksprite")
check("an ordinary roll is told by the piece's name, never its id",
      text == "qalice found the Meteorite on a Dark Sprite!", text)


# =============================================================================
section("Q-7 NOTHING ASKS ITEMS WHAT AN ID IS")
# =============================================================================
# THE TRAP THIS DESIGN HAS. gamedata.ITEMS is the catalogue and has no rolled
# ids in it, so gamedata.ITEMS.get("ironsword~d107") is None - an item nobody
# has heard of: unlimited stacking, "No such item" on a trade, no slot to wear
# it in. Every question about an id goes through item_row() / has_item().
# Iterating ITEMS (the shelf, the loot pool) is fine and stays.
app_src = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
FINE = re.compile(r"for\s+\w+\s+in\s+gamedata\.ITEMS\s*:|gamedata\.ITEMS\.(?:values|items)\(\)|len\(gamedata\.ITEMS\)")
by_id = [line.strip() for line in app_src.splitlines()
         if "gamedata.ITEMS" in line and not line.lstrip().startswith("#")
         and len(re.findall(r"gamedata\.ITEMS", line)) != len(FINE.findall(line))]
check("app.py looks nothing up in gamedata.ITEMS by id (it walks it, and counts it)", not by_id, by_id)
gd_src = open(os.path.join(HERE, "gamedata.py"), encoding="utf-8").read()
allowed = ("def split_variant", "def item_row", "def roll_quality", "def perfect_id")
offenders = []
for match in re.finditer(r"ITEMS\.get\(\s*item_id|ITEMS\[\s*item_id\s*\]|item_id\s+(?:not\s+)?in\s+ITEMS\b", gd_src):
    owner = gd_src.rfind("\ndef ", 0, match.start())
    name = gd_src[owner + 1:gd_src.find("(", owner)]
    if not name.startswith(allowed):
        offenders.append(name)
check("and gamedata.py asks ITEMS by id only inside the four functions that read a roll",
      not offenders, offenders)


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
if failures:
    for f in failures:
        print("  - " + f)
sys.exit(1 if failed else 0)
