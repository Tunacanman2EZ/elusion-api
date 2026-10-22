"""
test_economyrules.py - the economy's numbers, set on the server (0.74.0).

The owner: "Economy numbers you can change without shipping ... Today, changing
the trade tax or a price means a new version. If those numbers lived on the
server, you could adjust them in a minute." economyrules/ holds them, and the
shop and the trade tax read them. This suite holds it to that:

  ER-1  the rules, pure: every default is the catalogue's own number; the
        bounds, whole numbers, booleans and NaN; the shop never pays what it
        charges; and the price and tax arithmetic with nothing set is the
        arithmetic from before, for every catalogue item and every tax to a
        million
  ER-2  an empty table is the game as it was: the shelf, the buy-back and the
        trade quote are the catalogue's numbers exactly
  ER-3  who: a player reads the four the game draws and nothing else; a
        co-owner reads the owner's table and cannot change it; the owner
        changes it, asked for the password again on a stale hardened login
  ER-4  the refusals: not a rule, not a number, out of bounds, not whole, and
        a shop that would pay what it charges
  ER-5  every change logged, with who and when - and a change that changes
        nothing is not
  ER-6  the cache: a row written by hand is not seen until the cache is
        cleared or its window ends; the owner's change clears it at once; a
        row out of bounds reads as the default
  ER-7  the shop follows the rules: the shelf, the till and the buy-back at
        the owner's multipliers, through the ledger
  ER-8  the trade tax follows the rules: the quote and the charge, at the
        owner's rate and floor, and back to the game's own when reset
  ER-9  value() for everybody else: the larder's and the smith's scales, a db
        of None, a key that is not a rule

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_economyrules.py
"""
import importlib.util
import math
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_economyrules_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "ruleowner"
os.environ["ELUSION_CO_OWNERS"] = "rulecoowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
sys.modules["elusion_app"] = app_module
spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

import economyrules  # noqa: E402
from economyrules import services  # noqa: E402

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
    print("\n--- %s ---" % title)


_ip = [0]


def register(name):
    _ip[0] += 1
    res = client.post("/api/auth/register", json={"username": name, "password": "password123"},
                      environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert res.status_code == 201, res.get_json()
    return {"Authorization": "Bearer %s" % res.get_json()["token"]}


def sql(statement, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(statement, args).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def user_id(name):
    return int(sql("SELECT id FROM users WHERE username = ?", (name,))[0]["id"])


def seed_gold(name, amount):
    """Gold in the purse AND a ledger row, so the invariant still holds."""
    uid = user_id(name)
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = 0", (amount, uid))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (0, ?, 0, ?, 'test_seed', '')",
        (uid, amount))


def give(name, item_id, quantity):
    uid = user_id(name)
    taken = {int(r["position"]) for r in sql("SELECT position FROM carry_items WHERE user_id = ? AND slot = 0", (uid,))}
    position = next(p for p in range(app_module.INVENTORY_CAPACITY) if p not in taken)
    sql("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 0, ?, ?, ?)",
        (uid, position, item_id, quantity))
    return position


def purse(name):
    return int(sql("SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (user_id(name),))[0]["gold"])


def put_rule(headers, key, value):
    res = client.put("/api/owner/economy/rules", headers=headers, json={"key": key, "value": value})
    return res.status_code, res.get_json() or {}


def public_rules(headers):
    return client.get("/api/economy/rules", headers=headers).get_json()["rules"]


def rule_row(body, key):
    return next((row for row in body.get("rules", []) if row["key"] == key), {})


def balanced():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return app_module.gold_supply(conn)["balanced"]
    finally:
        conn.close()


# THE ARITHMETIC FROM BEFORE 0.74.0, word for word (gamedata.py as shipped),
# so "an empty table is the game as it was" is checked against the old code
# itself and not against a second reading of the new.
def old_gold_price(shop, definition):
    multiplier = float(shop.get("price_multiplier", 1.0))
    return max(1, int(math.ceil(int(definition.get("value", 0)) * multiplier)))


def old_shop_price(shop_id, item_id):
    shop = gamedata.SHOPS.get(shop_id)
    if shop is None:
        return None
    if item_id in (shop.get("free") or []):
        return 0 if gamedata.item_row(item_id) is not None else None
    if item_id not in shop.get("stock", []):
        return None
    definition = gamedata.item_row(item_id)
    if definition is None:
        return None
    return old_gold_price(shop, definition)


def old_vendor_pays(shop_id, item_id):
    shop = gamedata.SHOPS.get(shop_id)
    if shop is None:
        return None
    definition = gamedata.item_row(item_id)
    if definition is None:
        return None
    if str(definition.get("type_name", "")) not in gamedata.SELLABLE_TYPES:
        return None
    multiplier = float(shop.get("sell_multiplier", 0.0) or 0.0)
    price = int(math.floor(int(definition.get("value", 0)) * multiplier))
    base = gamedata.base_id(item_id)
    if base in shop.get("stock", []) or base in (shop.get("free") or []):
        base_row = gamedata.item_row(base)
        if base_row is not None:
            price = min(price, old_gold_price(shop, base_row) - 1)
    return price if price > 0 else None


def old_trade_tax(taxable_value):
    taxable_value = int(taxable_value)
    if taxable_value <= 0:
        return 0
    rate = float(gamedata.CONSTANTS.get("kingdom_tax_rate", 0.05))
    minimum = int(gamedata.CONSTANTS.get("kingdom_tax_minimum", 1))
    return max(minimum, int(math.ceil(taxable_value * rate)))


STORE = gamedata.SHOPS["generalstore"]

# =============================================================================
section("ER-1  the rules, pure")
# =============================================================================
check("every rule the skeleton named is here, and nothing was renamed",
      list(economyrules.RULES) == ["trade_tax_rate", "trade_tax_minimum", "shop_price_multiplier",
                                   "shop_sell_multiplier", "material_drop_scale", "temper_gold_scale",
                                   "certification_gold_scale"], list(economyrules.RULES))
check("  each default is the catalogue's own number: the tax 5% and 1, the store's 1.0 and 0.05, the scales 1",
      economyrules.default("trade_tax_rate") == float(gamedata.CONSTANTS["kingdom_tax_rate"])
      and economyrules.default("trade_tax_minimum") == float(gamedata.CONSTANTS["kingdom_tax_minimum"])
      and economyrules.default("shop_price_multiplier") == float(STORE["price_multiplier"])
      and economyrules.default("shop_sell_multiplier") == float(STORE["sell_multiplier"])
      and all(economyrules.default(k) == 1.0 for k in ("material_drop_scale", "temper_gold_scale",
                                                       "certification_gold_scale")))
check("  and every default inside its own bounds",
      all(r["min"] <= r["default"] <= r["max"] for r in economyrules.RULES.values()))
check("check(): a number in bounds is taken as a float",
      services.check("trade_tax_rate", 0.1) == (0.1, "") and services.check("trade_tax_minimum", 3) == (3.0, ""))
check("  out of bounds, either side, is refused saying them",
      services.check("trade_tax_rate", 0.51)[1] == "trade_tax_rate must be from 0 to 0.5 (you sent 0.51)."
      and services.check("shop_price_multiplier", 0.2)[1].startswith("shop_price_multiplier must be from 0.25 to 4"))
check("  the edges themselves are in", services.check("trade_tax_rate", 0)[1] == ""
      and services.check("trade_tax_rate", 0.5)[1] == "" and services.check("shop_price_multiplier", 4)[1] == "")
check("  a boolean, a string, NaN and infinity are not numbers",
      all(services.check("trade_tax_rate", v)[1] == "trade_tax_rate must be a number."
          for v in (True, "0.1", float("nan"), float("inf"), None, [0.1])))
check("  the tax's floor is whole gold, and at least 1 - a floor of 0 is the laundering the floor exists to stop",
      services.check("trade_tax_minimum", 2.5)[1] == "trade_tax_minimum must be a whole number of gold."
      and services.check("trade_tax_minimum", 0)[1] != "" and services.check("trade_tax_minimum", 1)[1] == "")
check("  a key that is not a rule", services.check("gold_from_nowhere", 1)[1]
      == "There is no economy rule called 'gold_from_nowhere'.")
check("the shop must pay less than it charges: sell at or above price is refused, either way round",
      services.relational_refusal("shop_sell_multiplier", 0.5, {"shop_price_multiplier": 0.5}) != ""
      and services.relational_refusal("shop_price_multiplier", 0.3, {"shop_sell_multiplier": 0.3}) != ""
      and services.relational_refusal("shop_sell_multiplier", 0.2, {"shop_price_multiplier": 0.25}) == ""
      and services.relational_refusal("trade_tax_rate", 0.5, {"shop_price_multiplier": 0.25}) == "")
check("numbers shown as typed", [services.shown(v) for v in (0.05, 1.0, 2.5, 0, 10000.0)]
      == ["0.05", "1", "2.5", "0", "10000"])

prices_same = [i for i in gamedata.ITEMS if gamedata.shop_price("generalstore", i) != old_shop_price("generalstore", i)]
pays_same = [i for i in gamedata.ITEMS if gamedata.vendor_pays("generalstore", i) != old_vendor_pays("generalstore", i)]
check("with nothing set, every catalogue item's shelf price is the old arithmetic's (%d items)" % len(gamedata.ITEMS),
      prices_same == [], prices_same[:5])
check("  and what the shop pays for every one", pays_same == [], pays_same[:5])
taxes_same = [v for v in range(0, 1000001) if gamedata.trade_tax(v) != old_trade_tax(v)]
check("  and the trade tax on every value from 0 to a million", taxes_same == [], taxes_same[:5])
check("with the owner's markup, a price is the arithmetic the owner meant: 1.1 of 100 is 110, not 111",
      gamedata._gold_price(STORE, {"value": 100}, 1.1) == 110 and gamedata._gold_price(STORE, {"value": 100}) == 100)
check("  and the tax takes the owner's rate and floor when handed them",
      gamedata.trade_tax(1000, rate=0.2) == 200 and gamedata.trade_tax(10, rate=0.05, minimum=3) == 3
      and gamedata.trade_tax(0, rate=0.5, minimum=9) == 0)

# =============================================================================
section("ER-2  an empty table is the game as it was")
# =============================================================================
OWNER = register("ruleowner")
CO = register("rulecoowner")
PLAYER = register("ruleplayer")
TRADER = register("ruletrader")
for headers, cls, name in ((PLAYER, "warrior", "Pia"), (TRADER, "mage", "Tam")):
    assert client.put("/api/save", headers=headers, json={"slot": 0, "class_id": cls, "name": name}).status_code == 200
seed_gold("ruleplayer", 50000)
seed_gold("ruletrader", 50000)

economyrules.clear_cache()
check("no row in the owner's table", sql("SELECT COUNT(*) AS n FROM economy_rules")[0]["n"] == 0)
shelf = client.get("/api/shop/generalstore", headers=PLAYER).get_json()
check("the shelf's every price is the catalogue's own",
      all(row["price"] == old_shop_price("generalstore", row["item_id"]) for row in shelf["stock"])
      and len(shelf["stock"]) > 50, [r for r in shelf["stock"] if r["price"] != old_shop_price("generalstore", r["item_id"])][:2])
check("  and every buy-back price, and the two multipliers it says",
      shelf["sell_prices"] == {i: old_vendor_pays("generalstore", i) for i in gamedata.ITEMS
                               if old_vendor_pays("generalstore", i) is not None}
      and shelf["sell_multiplier"] == float(STORE["sell_multiplier"])
      and shelf["price_multiplier"] == float(STORE["price_multiplier"]))
check("every player's game is told the four it draws, each the game's own",
      public_rules(PLAYER) == {"shop_price_multiplier": 1.0, "shop_sell_multiplier": 0.05, "trade_tax_rate": 0.05,
                               "trade_tax_minimum": 1.0}, public_rules(PLAYER))

# =============================================================================
section("ER-3  who may read and who may change")
# =============================================================================
check("a player: the four numbers, and the bare 404 from the owner's table and its change",
      client.get("/api/owner/economy/rules", headers=PLAYER).status_code == 404
      and put_rule(PLAYER, "trade_tax_rate", 0.1)[0] == 404)
check("  nobody without a login gets in", client.get("/api/economy/rules").status_code == 401
      and client.get("/api/owner/economy/rules").status_code == 401
      and client.put("/api/owner/economy/rules", json={"key": "trade_tax_rate", "value": 0.1}).status_code == 401)
check("a named co-owner with the owner's switch off is a player here", client.get(
    "/api/owner/economy/rules", headers=CO).status_code == 404)
assert client.post("/api/server/coowners", headers=OWNER, json={"on": True}).status_code == 200
co_view = client.get("/api/owner/economy/rules", headers=CO)
check("  switched on, she reads the table - told she may not change it", co_view.status_code == 200
      and co_view.get_json()["can_change"] is False and len(co_view.get_json()["rules"]) == len(economyrules.RULES))
check("  and her change is the owner's alone: the bare 404, nothing written",
      put_rule(CO, "trade_tax_rate", 0.1)[0] == 404 and sql("SELECT COUNT(*) AS n FROM economy_rules")[0]["n"] == 0)
owner_view = client.get("/api/owner/economy/rules", headers=OWNER).get_json()
check("the owner reads every rule: value, default, bounds, whole, about, set, when and who",
      owner_view["can_change"] is True and rule_row(owner_view, "trade_tax_minimum") == {
          "key": "trade_tax_minimum", "value": 1.0, "default": 1.0, "min": 1.0, "max": 10000.0, "whole": True,
          "about": economyrules.RULES["trade_tax_minimum"]["about"], "set": False, "updated_at": None,
          "updated_by": ""}, rule_row(owner_view, "trade_tax_minimum"))
owner_id = user_id("ruleowner")
kept = sql("SELECT hardened, confirmed_at FROM sessions WHERE user_id = ?", (owner_id,))[0]
sql("UPDATE sessions SET hardened = 1, confirmed_at = 1 WHERE user_id = ?", (owner_id,))
stale = client.put("/api/owner/economy/rules", headers=OWNER, json={"key": "trade_tax_rate", "value": 0.1})
sql("UPDATE sessions SET hardened = ?, confirmed_at = ? WHERE user_id = ?",
    (kept["hardened"], kept["confirmed_at"], owner_id))
check("  a change on a hardened login with a stale confirm asks for the password again, and writes nothing",
      stale.status_code == 403 and (stale.get_json() or {}).get("confirm_password") is True
      and sql("SELECT COUNT(*) AS n FROM economy_rules")[0]["n"] == 0, stale.get_json())

# =============================================================================
section("ER-4  the refusals")
# =============================================================================
for label, body, code, words in (
        ("not a rule", {"key": "gold_from_nowhere", "value": 1}, 400, "There is no economy rule called"),
        ("no value", {"key": "trade_tax_rate"}, 400, '"value"'),
        ("not a number", {"key": "trade_tax_rate", "value": "lots"}, 400, "must be a number"),
        ("a boolean", {"key": "trade_tax_rate", "value": True}, 400, "must be a number"),
        ("over the top", {"key": "trade_tax_rate", "value": 0.75}, 400, "from 0 to 0.5"),
        ("under the bottom", {"key": "shop_price_multiplier", "value": 0.1}, 400, "from 0.25 to 4"),
        ("half a coin", {"key": "trade_tax_minimum", "value": 2.5}, 400, "whole number"),
        ("a buy-back above half", {"key": "shop_sell_multiplier", "value": 0.6}, 400, "from 0 to 0.5"),
):
    res = client.put("/api/owner/economy/rules", headers=OWNER, json=body)
    check("%s: the %d, saying so" % (label, code), res.status_code == code
          and words in (res.get_json() or {}).get("message", ""), res.get_json())
res = client.put("/api/owner/economy/rules", headers=OWNER, json=["trade_tax_rate", 0.1])
check("a body that is not an object is the 400", res.status_code == 400)
code, body = put_rule(OWNER, "shop_price_multiplier", 0.3)
code2, body2 = put_rule(OWNER, "shop_sell_multiplier", 0.3)
check("the shop paying as much as it charges is the 409, naming both numbers",
      code == 200 and code2 == 409 and "shop_sell_multiplier (0.3)" in body2.get("message", "")
      and "shop_price_multiplier (0.3)" in body2.get("message", ""), body2)
code3, _ = put_rule(OWNER, "shop_price_multiplier", 0.25)
code4, body4 = put_rule(OWNER, "shop_price_multiplier", None)
check("  and nothing refused was written: the price back to the default, the sell multiplier never set",
      code3 == 200 and code4 == 200 and not rule_row(body4, "shop_sell_multiplier")["set"]
      and not rule_row(body4, "shop_price_multiplier")["set"], body4.get("rules"))

# =============================================================================
section("ER-5  every change logged, with who and when")
# =============================================================================
before = int(time.time())
code, body = put_rule(OWNER, "trade_tax_rate", 0.1)
row = sql("SELECT * FROM economy_rules WHERE key = 'trade_tax_rate'")
log = sql("SELECT * FROM economy_rule_changes WHERE key = 'trade_tax_rate' ORDER BY id DESC LIMIT 1")
staff = sql("SELECT actor_name, action, target_name, detail FROM staff_actions WHERE action = 'economy_rule'"
            " ORDER BY id DESC LIMIT 1")
check("set: the row holds the number, who and when", code == 200 and body["changed"] is True
      and row and row[0]["value"] == 0.1 and row[0]["updated_by"] == "ruleowner" and row[0]["updated_at"] >= before)
check("  the change log: from the default (null) to 0.1, by the owner, now",
      log and log[0]["old_value"] is None and log[0]["new_value"] == 0.1 and log[0]["by_name"] == "ruleowner"
      and log[0]["by_id"] == owner_id and log[0]["at"] >= before, [dict(r) for r in log])
check("  the staff log too", staff and dict(staff[0]) == {
    "actor_name": "ruleowner", "action": "economy_rule", "target_name": "trade_tax_rate",
    "detail": "trade_tax_rate: default (0.05) -> 0.1"}, [dict(r) for r in staff])
check("  and the owner's read shows it set, by whom, and the change first",
      rule_row(body, "trade_tax_rate")["set"] is True and rule_row(body, "trade_tax_rate")["updated_by"] == "ruleowner"
      and body["changes"][:1] and body["changes"][0]["key"] == "trade_tax_rate"
      and body["changes"][0]["new_value"] == 0.1)
changes_before = sql("SELECT COUNT(*) AS n FROM economy_rule_changes")[0]["n"]
code, body = put_rule(OWNER, "trade_tax_rate", 0.1)
check("the same number again changes nothing and logs nothing",
      code == 200 and body["changed"] is False
      and sql("SELECT COUNT(*) AS n FROM economy_rule_changes")[0]["n"] == changes_before)
code, body = put_rule(OWNER, "trade_tax_rate", None)
log = sql("SELECT * FROM economy_rule_changes WHERE key = 'trade_tax_rate' ORDER BY id DESC LIMIT 1")
check("put back to the default: the row goes, and the log says 0.1 to the default",
      code == 200 and sql("SELECT COUNT(*) AS n FROM economy_rules WHERE key = 'trade_tax_rate'")[0]["n"] == 0
      and bool(log) and log[0]["old_value"] == 0.1 and log[0]["new_value"] is None)
code, body = put_rule(OWNER, "trade_tax_rate", None)
check("  and the default asked for again is no change", code == 200 and body["changed"] is False)

# =============================================================================
section("ER-6  the cache")
# =============================================================================
economyrules.clear_cache()
check("cleared, a rule nobody set reads its default", public_rules(PLAYER)["shop_price_multiplier"] == 1.0)
sql("INSERT INTO economy_rules (key, value, updated_at, updated_by) VALUES ('shop_price_multiplier', 2.0, 0, 'hand')")
check("a row written by hand is not heard inside the cache's window",
      public_rules(PLAYER)["shop_price_multiplier"] == 1.0)
economyrules.clear_cache()
check("  and is, once the cache is cleared", public_rules(PLAYER)["shop_price_multiplier"] == 2.0)
sql("DELETE FROM economy_rules WHERE key = 'shop_price_multiplier'")
kept_window = services.CACHE_SECONDS
services.CACHE_SECONDS = 0
check("  or once its window has run out", public_rules(PLAYER)["shop_price_multiplier"] == 1.0)
services.CACHE_SECONDS = kept_window
public_rules(PLAYER)   # fill the cache
put_rule(OWNER, "shop_sell_multiplier", 0.1)
check("the owner's change is heard at once - the change clears the cache",
      public_rules(PLAYER)["shop_sell_multiplier"] == 0.1)
put_rule(OWNER, "shop_sell_multiplier", None)
sql("INSERT INTO economy_rules (key, value, updated_at, updated_by) VALUES ('shop_sell_multiplier', 0.9, 0, 'hand')")
economyrules.clear_cache()
owner_view = client.get("/api/owner/economy/rules", headers=OWNER).get_json()
check("a row out of bounds reads as the default, to the game and on the owner's table",
      public_rules(PLAYER)["shop_sell_multiplier"] == 0.05 and not rule_row(owner_view, "shop_sell_multiplier")["set"]
      and economyrules.owner_value(None, "shop_sell_multiplier") is None)
sql("DELETE FROM economy_rules WHERE key = 'shop_sell_multiplier'")
economyrules.clear_cache()

# =============================================================================
section("ER-7  the shop follows the rules")
# =============================================================================
put_rule(OWNER, "shop_price_multiplier", 1.5)
put_rule(OWNER, "shop_sell_multiplier", 0.1)
shelf = client.get("/api/shop/generalstore", headers=PLAYER).get_json()
potion = next(r for r in shelf["stock"] if r["item_id"] == "smallhealthpotion")
want = int(math.ceil(int(gamedata.ITEMS["smallhealthpotion"]["value"]) * 1.5))
check("the shelf prices at the owner's markup: a %d-gold potion is %d" % (gamedata.ITEMS["smallhealthpotion"]["value"],
                                                                          want),
      potion["price"] == want and shelf["price_multiplier"] == 1.5 and shelf["sell_multiplier"] == 0.1, potion)
check("  a free piece is still free", all(r["price"] == 0 for r in shelf["stock"] if r.get("free")))
gold_before = purse("ruleplayer")
res = client.post("/api/shop/buy", headers=PLAYER, json={"slot": 0, "shop_id": "generalstore",
                                                        "item_id": "smallhealthpotion", "quantity": 2})
burn = sql("SELECT delta FROM gold_ledger WHERE reason = 'shop_buy' ORDER BY id DESC LIMIT 1")
check("  the till charges it: two potions, %d gold gone, through the ledger" % (2 * want),
      res.status_code == 200 and res.get_json()["total_paid"] == 2 * want
      and purse("ruleplayer") == gold_before - 2 * want and burn and burn[0]["delta"] == -2 * want, res.get_json())
sword_pays = int(math.floor(int(gamedata.ITEMS["ironsword"]["value"]) * 0.1))
check("the buy-back pays the owner's share: an iron sword fetches %d" % sword_pays,
      shelf["sell_prices"].get("ironsword") == sword_pays, shelf["sell_prices"].get("ironsword"))
position = give("ruleplayer", "ironsword", 1)
gold_before = purse("ruleplayer")
res = client.post("/api/shop/sell", headers=PLAYER, json={"slot": 0, "shop_id": "generalstore",
                                                         "position": position, "item_id": "ironsword"})
check("  and the till pays it, minted through the ledger", res.status_code == 200
      and res.get_json()["total_received"] == sword_pays and purse("ruleplayer") == gold_before + sword_pays,
      res.get_json())
put_rule(OWNER, "shop_price_multiplier", 0.25)
put_rule(OWNER, "shop_sell_multiplier", 0.2)
shelf = client.get("/api/shop/generalstore", headers=PLAYER).get_json()
stocked = [i for i in STORE["stock"] if i in shelf["sell_prices"]]
check("never at or above what it charges, even at the cheapest shop the owner may set",
      stocked and all(shelf["sell_prices"][i] < next(r["price"] for r in shelf["stock"] if r["item_id"] == i)
                      for i in stocked), stocked[:3])
put_rule(OWNER, "shop_price_multiplier", None)
put_rule(OWNER, "shop_sell_multiplier", None)
shelf = client.get("/api/shop/generalstore", headers=PLAYER).get_json()
check("put back, the shelf is the catalogue's again",
      all(row["price"] == old_shop_price("generalstore", row["item_id"]) for row in shelf["stock"])
      and shelf["sell_prices"].get("ironsword") == old_vendor_pays("generalstore", "ironsword"))
check("the books balance after all of it", balanced())

# =============================================================================
section("ER-8  the trade tax follows the rules")
# =============================================================================
for h in (PLAYER, TRADER):
    client.get("/api/server/broadcasts", headers=h)
res = client.post("/api/trade/offer", headers=PLAYER, json={"slot": 0, "username": "ruletrader"})
assert res.status_code == 200, res.get_json()
client.post("/api/trade/update", headers=PLAYER, json={"gold": 1000})
quote = client.post("/api/trade/update", headers=TRADER, json={"gold": 10}).get_json()
check("with nothing set, the quote is the game's own: 5% of 1000 and the floor on 10",
      quote["b"]["tax"] == old_trade_tax(1000) == 50 and quote["a"]["tax"] == old_trade_tax(10) == 1, quote)
put_rule(OWNER, "trade_tax_rate", 0.2)
put_rule(OWNER, "trade_tax_minimum", 5)
quote = client.get("/api/trade", headers=PLAYER).get_json()["trade"]
check("the owner's 20% and floor of 5: the quote follows at once",
      quote["b"]["tax"] == 200 and quote["a"]["tax"] == 5, [quote["a"]["tax"], quote["b"]["tax"]])


def confirm(headers):
    trade = client.get("/api/trade", headers=headers).get_json()["trade"]
    return client.post("/api/trade/confirm", headers=headers, json={"revision": trade["revision"]})


p_before, t_before = purse("ruleplayer"), purse("ruletrader")
confirm(PLAYER)
done = confirm(TRADER)
taxes = sorted(-int(r["delta"]) for r in sql("SELECT delta FROM gold_ledger WHERE reason = 'kingdom_tax'"
                                             " ORDER BY id DESC LIMIT 2"))
check("  and the charge is the quote: 5 and 200 burned as kingdom_tax", done.status_code == 200 and taxes == [5, 200]
      and purse("ruleplayer") == p_before - 1000 + 10 - 5 and purse("ruletrader") == t_before - 10 + 1000 - 200,
      [done.status_code, taxes])
put_rule(OWNER, "trade_tax_rate", None)
put_rule(OWNER, "trade_tax_minimum", None)
client.post("/api/trade/offer", headers=PLAYER, json={"slot": 0, "username": "ruletrader"})
client.post("/api/trade/update", headers=PLAYER, json={"gold": 1000})
quote = client.get("/api/trade", headers=TRADER).get_json()["trade"]
check("put back, the quote is the game's own again", quote["b"]["tax"] == 50, quote["b"])
client.post("/api/trade/cancel", headers=PLAYER)
check("the books balance after the trade", balanced())

# =============================================================================
section("ER-9  value() for everybody else")
# =============================================================================
with app_module.app.app_context():
    db = app_module.get_db()
    check("the larder's and the smith's scales: 1 while nobody set them",
          [economyrules.value(db, k) for k in ("material_drop_scale", "temper_gold_scale", "certification_gold_scale")]
          == [1.0, 1.0, 1.0])
put_rule(OWNER, "material_drop_scale", 2.5)
with app_module.app.app_context():
    db = app_module.get_db()
    check("  the owner's number once set", economyrules.value(db, "material_drop_scale") == 2.5
          and economyrules.owner_value(db, "material_drop_scale") == 2.5)
    check("  a db of None is the default", economyrules.value(None, "material_drop_scale") == 1.0)
    try:
        economyrules.value(db, "gold_from_nowhere")
        raised = False
    except KeyError:
        raised = True
    check("  and a key that is not a rule is a KeyError, not a guess", raised)
put_rule(OWNER, "material_drop_scale", None)
conn = sqlite3.connect(os.path.join(testdir.PATH, "elusion_economyrules_bare.db"))
conn.execute("CREATE TABLE t (x)")
check("a database without the owner's table (from before 0.74.0) answers every default, and never raises",
      economyrules.value(conn, "trade_tax_rate") == 0.05 and economyrules.shop_terms(conn, "generalstore") == {}
      and economyrules.tax_terms(conn) == {})
conn.close()
check("the shop terms are the general store's alone", economyrules.shop_terms(None, "anothershop") == {})

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
