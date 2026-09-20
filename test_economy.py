"""
test_economy.py — the gold ledger and the supply invariant.

Separate from test_api.py because this suite is about ONE property:

    SUM(gold_ledger.delta)  ==  SUM(saves.gold) + SUM(accounts.bank_gold)

Everything else here exists to move gold around and then assert that equation
still holds. As sinks arrive — the vendor, the kingdom tax on trades — each one
gets a case below, and the invariant check at the end of every case is what
proves the new path was recorded rather than silently minting or destroying.

Run:  python test_economy.py
Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_economy_test.db")

# BEFORE the import: app.py reads DB_PATH and OWNER_USERNAME at import time and
# calls init_db(), and none of that may touch the real database.
os.environ["ELUSION_DB"] = DB_PATH
# Deliberately an account name nothing below registers. The owner outranks
# every role check, so an owner-named test account would pass the staff gating
# assertions for the wrong reason and hide a broken decorator.
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(_spec)
sys.modules["elusion_app"] = app_module
_spec.loader.exec_module(app_module)
client = app_module.app.test_client()
# The catalogue the server actually loaded, for comparing shop rows
# against their source rather than against a second copy of the numbers.
gamedata = app_module.gamedata


# =============================================================================
# HARNESS
# =============================================================================

passed = 0
failed = 0
failures = []


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        failures.append((label, detail))
        print("  FAIL %s   %s" % (label, detail))


def status(label, response, expected):
    check("%s -> %d" % (label, expected), response.status_code == expected,
          "got %d %s" % (response.status_code, response.data[:160]))
    return response.get_json() if response.is_json else None


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def supply():
    conn = db_conn()
    try:
        return app_module.gold_supply(conn)
    finally:
        conn.close()


def balanced(label):
    s = supply()
    check("invariant holds after %s" % label, s["balanced"],
          "recorded=%d held=%d drift=%d" % (s["recorded"], s["held"], s["drift"]))
    return s


def account(username):
    client.post("/api/auth/register",
                json={"username": username, "password": "hunter2hunter2"})
    token = client.post("/api/auth/login",
                        json={"username": username, "password": "hunter2hunter2"}
                        ).get_json()["token"]
    return {"Authorization": "Bearer " + token}


def set_role(username, role):
    conn = db_conn()
    conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
    conn.commit()
    conn.close()


# =============================================================================
# THE LEDGER EXISTS AND STARTS HONEST
# =============================================================================

print("\nledger bootstrap")

conn = db_conn()
check("gold_ledger table was created", conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='gold_ledger'"
).fetchone() is not None)

rows = conn.execute("SELECT reason, delta FROM gold_ledger").fetchall()
check("exactly one opening row on a fresh database", len(rows) == 1, rows)
check("the opening row is an opening_balance",
      rows and rows[0]["reason"] == "opening_balance", rows)
check("a fresh world opens at zero", rows and int(rows[0]["delta"]) == 0, rows)
conn.close()

balanced("bootstrap")


# =============================================================================
# THE MINT PATH
# =============================================================================
# Gold is created in exactly one statement in the whole server: taking a gold
# pile out of a bag the server rolled. If a second mint site ever appears
# without going through gold_delta(), the invariant below is what says so.

print("\nminting through /api/loot/take")

H = account("ledgerplayer")
status("create a character", client.put(
    "/api/save", headers=H,
    json={"slot": 0, "class_id": "warrior", "name": "Ledger"}), 200)

before = supply()

# THE ROSTER EVERY FARMING LOOP IN THIS FILE ROTATES THROUGH.
#
# These loops used to spam one enemy. /api/combat/kill gained a SPAWN CEILING
# since: an enemy placed once, on a 30-second respawner, pays out at most
# (window / respawn + 1) times per window, so the eleventh identical kill comes
# back 429 and a loop that needs eighty never finishes.
#
# The ceiling is not wrong and neither were these tests - they were farming,
# and farming is the thing it exists to stop. Rotating is what a player does
# and what the ceiling is shaped around: every placed enemy carries its own
# allowance, so a lap of the field earns freely while standing on one spawn
# point does not.
#
# It has to be EARNED, not written. The supply invariant below is the point of
# this section, and a purse set directly in the database would break it - so
# the gold comes out of real kills or the test is not testing anything.
ROSTER = sorted(e for e in gamedata.ENEMIES
                if gamedata.ENEMIES[e].get("grants_rewards", True)
                and gamedata.spawn_count_for(e) > 0) or ["windslime"]

# Kill until a bag holds a gold pile. Bags are not guaranteed and neither is
# their content, so this loops rather than assuming the first kill pays.
found = None
for _n in range(80):
    body = client.post("/api/combat/kill", headers=H,
                       json={"slot": 0, "enemy_id": ROSTER[_n % len(ROSTER)]}).get_json() or {}
    if not body.get("bag_id"):
        continue
    piles = [c for c in body.get("contents", [])
             if "gold" in str(c.get("item_id", ""))]
    if piles:
        found = (body["bag_id"], piles[0])
        break

check("a gold pile dropped within 80 kills", found is not None)

if found:
    bag_id, pile = found
    quantity = int(pile["quantity"])
    body = status("take the gold pile", client.post(
        "/api/loot/take", headers=H,
        json={"bag_id": bag_id, "position": pile["position"]}), 200)
    check("the server credited it as gold", body and body.get("credited") == "gold", body)

    after = supply()
    check("minted exactly the pile", after["minted"] - before["minted"] == quantity,
          "pile=%d minted delta=%d" % (quantity, after["minted"] - before["minted"]))
    check("held rose by the same amount", after["held"] - before["held"] == quantity,
          "held delta=%d" % (after["held"] - before["held"]))
    check("nothing was burned", after["burned"] == before["burned"])

    conn = db_conn()
    row = conn.execute(
        "SELECT reason, delta FROM gold_ledger ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    check("the mint was recorded with reason 'loot'",
          row and row["reason"] == "loot" and int(row["delta"]) == quantity, dict(row) if row else None)

balanced("a mint")


# =============================================================================
# TRANSFERS ARE NOT MINTS
# =============================================================================
# Bank deposits move gold between a character's purse and the shared account.
# Both sides count as supply, so a transfer must write NO ledger rows and must
# not move the invariant. This is the case most likely to be got wrong by
# someone later routing every gold statement through gold_delta() for tidiness.

print("\nbank transfers must not touch the ledger")

conn = db_conn()
rows_before = conn.execute("SELECT COUNT(*) FROM gold_ledger").fetchone()[0]
carried_before = conn.execute(
    "SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0]
conn.close()

move = max(1, int(carried_before) // 2)
status("deposit to the bank", client.post(
    "/api/bank/gold", headers=H,
    json={"slot": 0, "op": "deposit", "amount": move}), 200)

s = supply()
check("gold moved into the bank", s["banked"] == move, s)
check("total held is unchanged by a deposit", s["held"] == carried_before, s)

conn = db_conn()
rows_after = conn.execute("SELECT COUNT(*) FROM gold_ledger").fetchone()[0]
conn.close()
check("a deposit wrote no ledger rows", rows_after == rows_before,
      "%d -> %d" % (rows_before, rows_after))

status("withdraw from the bank", client.post(
    "/api/bank/gold", headers=H,
    json={"slot": 0, "op": "withdraw", "amount": move}), 200)
check("total held is unchanged by a withdrawal", supply()["held"] == carried_before)

balanced("transfers")


# =============================================================================
# THE DETECTOR ACTUALLY DETECTS
# =============================================================================
# An audit that cannot fail proves nothing. This writes gold straight into the
# balances, which is what a dupe or a forgotten code path looks like from the
# ledger's point of view, and asserts the invariant notices.

print("\ndrift detection")

conn = db_conn()
conn.execute("UPDATE saves SET gold = gold + 5000 WHERE slot = 0")
conn.commit()
conn.close()

s = supply()
check("injected gold is reported as drift", not s["balanced"], s)
check("drift is the exact amount injected", s["drift"] == 5000, s)

conn = db_conn()
conn.execute("UPDATE saves SET gold = gold - 5000 WHERE slot = 0")
conn.commit()
conn.close()
balanced("undoing the injection")


# =============================================================================
# THE AUDIT ENDPOINT IS STAFF ONLY
# =============================================================================

print("\n/api/economy/supply gating")

status("no token", client.get("/api/economy/supply"), 401)
# 404 rather than 403 is deliberate - see require_role(). A 403 confirms the
# route exists to someone who has no business knowing it does.
status("a plain player", client.get("/api/economy/supply", headers=H), 404)

S = account("ledgerstaff")
set_role("ledgerstaff", "mod")
status("a mod", client.get("/api/economy/supply", headers=S), 404)

set_role("ledgerstaff", "dev")
body = status("a dev", client.get("/api/economy/supply", headers=S), 200)
check("the report carries the invariant", body and "balanced" in body and "drift" in body, body)
check("the report agrees with a direct read",
      body and body["held"] == supply()["held"], body)


# =============================================================================
# THE VENDOR: THE FIRST BURN
# =============================================================================
# Buying destroys gold rather than moving it. Nothing holds it afterwards,
# which is what makes a vendor a sink and why the burn has to show up in the
# ledger as a negative delta rather than as a transfer to someone's balance.

print("\nvendor purchases burn gold")

body = status("read the catalogue", client.get(
    "/api/shop/generalstore", headers=H), 200)
check("the catalogue has stock", body and len(body.get("stock", [])) > 0, body)
check("every row carries a server price",
      body and all(isinstance(r.get("price"), int) and r["price"] > 0
                   for r in body["stock"]),
      body.get("stock", [])[:3] if body else None)

status("an unknown shop", client.get("/api/shop/nosuchshop", headers=H), 404)

# Farm honestly rather than writing gold in: the point of the suite is that
# every gold has a ledger row, and a test that pokes the balance would be
# testing a world the server never agreed to.
# ROTATED THROUGH THE ROSTER RATHER THAN SPAMMING ONE ENEMY, because the
# server now refuses the second thing.
#
# This farmed windslime 250 times in a row. /api/combat/kill gained a spawn
# ceiling since: an enemy placed once, on a 30-second respawner, pays out at
# most (window / respawn + 1) times per window, so the eleventh windslime came
# back 429 and this loop could never reach 300 gold. The test was not wrong
# about the economy - it was farming, and farming is the thing the ceiling
# exists to stop.
#
# Rotating is what a player does and what the ceiling is shaped around: every
# placed enemy has its own allowance, so a lap of the field earns freely while
# standing on one spawn point does not. It also keeps the gold honest - the
# ledger invariant this suite checks would break if the purse were written
# directly, so the gold has to be genuinely earned.
conn = db_conn()
for _n in range(250):
    purse = conn.execute("SELECT gold FROM saves WHERE slot = 0").fetchone()
    if purse and int(purse["gold"]) >= 300:
        break
    killed = client.post("/api/combat/kill", headers=H,
                         json={"slot": 0, "enemy_id": ROSTER[_n % len(ROSTER)]}).get_json() or {}
    if not killed.get("bag_id"):
        continue
    piles = [c for c in killed.get("contents", [])
             if "gold" in str(c.get("item_id", ""))]
    if piles:
        client.post("/api/loot/take", headers=H,
                    json={"bag_id": killed["bag_id"], "position": piles[0]["position"]})
purse = int(conn.execute("SELECT gold FROM saves WHERE slot = 0").fetchone()["gold"])
conn.close()
check("farmed enough to shop with", purse >= 300, "have %d" % purse)

status("a shop that does not exist", client.post(
    "/api/shop/buy", headers=H,
    json={"slot": 0, "shop_id": "nosuchshop", "item_id": "tinyhealthpotion"}), 400)
status("an item the vendor does not stock", client.post(
    "/api/shop/buy", headers=H,
    json={"slot": 0, "shop_id": "generalstore", "item_id": "emberchest"}), 400)
status("more than the item stacks to", client.post(
    "/api/shop/buy", headers=H,
    json={"slot": 0, "shop_id": "generalstore",
          "item_id": "tinyhealthpotion", "quantity": 9999}), 400)
status("a negative quantity", client.post(
    "/api/shop/buy", headers=H,
    json={"slot": 0, "shop_id": "generalstore",
          "item_id": "tinyhealthpotion", "quantity": -5}), 400)

before = supply()
body = status("buy 3 tiny health potions", client.post(
    "/api/shop/buy", headers=H,
    json={"slot": 0, "shop_id": "generalstore",
          "item_id": "tinyhealthpotion", "quantity": 3}), 200)

check("the server priced it itself", body and body.get("unit_price") == 25, body)
check("total is unit x quantity", body and body.get("total_paid") == 75, body)

after = supply()
check("burned exactly what was paid", after["burned"] - before["burned"] == 75,
      "burned delta=%d" % (after["burned"] - before["burned"]))
check("held fell by the same amount", before["held"] - after["held"] == 75,
      "held delta=%d" % (before["held"] - after["held"]))
check("nothing was minted by a purchase", after["minted"] == before["minted"])

conn = db_conn()
row = conn.execute(
    "SELECT reason, delta FROM gold_ledger ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("the burn was recorded with reason 'shop_buy'",
      row and row["reason"] == "shop_buy" and int(row["delta"]) == -75,
      dict(row) if row else None)

balanced("a purchase")

# A refusal must leave the world exactly as it was - no gold taken, no item
# granted. The failure mode worth guarding is a vendor that charges and then
# discovers the backpack is full.
before = supply()
attempt = client.post("/api/shop/buy", headers=H,
                      json={"slot": 0, "shop_id": "generalstore",
                            "item_id": "ironchest", "quantity": 1})
# Whether this one is affordable depends on how the farming loop above landed,
# so the assertion is about consistency rather than a fixed outcome: a refusal
# must burn nothing, and a success must burn exactly the asking price.
if attempt.status_code == 400:
    check("a refused purchase burned nothing",
          supply()["burned"] == before["burned"])
elif attempt.status_code == 200:
    paid = attempt.get_json()["total_paid"]
    check("a successful purchase burned exactly the price",
          supply()["burned"] - before["burned"] == paid,
          "paid=%d" % paid)
balanced("a refused purchase")


# =============================================================================
# USE REQUIREMENTS REACH THE SHOP PANEL
# =============================================================================
# The panel draws "Needs Lv 10" from the row the server sends, not from its own
# .tres files, for the same reason it draws the price that way: a stale client
# must not be able to promise something the game then refuses. These assertions
# are about the ROW SHAPE - that both halves of the requirement are present on
# every row and carry the catalogue's own numbers.
#
# THE TWO HALVES ARE NOT INTERCHANGEABLE. required_level is a character level,
# which is what gates a potion you bought with gold; required_skill is the skill
# that made the thing, which is what gates a cooked fish - a fisher at character
# level 25 with cooking 70 has earned their Reef Clown. A test that only checked
# required_level would pass on a build where the skill half never shipped.

print("\nuse requirements on shop rows")

body = status("read the catalogue again", client.get(
    "/api/shop/generalstore", headers=H), 200)
stock = (body or {}).get("stock", [])

check("every row carries all three requirement fields",
      stock and all(
          "required_level" in r and "required_skill" in r
          and "required_skill_level" in r for r in stock),
      sorted(stock[0].keys()) if stock else None)

mismatched = [
    r["item_id"] for r in stock
    if r["required_level"] != gamedata.ITEMS[r["item_id"]].get("required_level", 1)
    or r["required_skill"] != gamedata.ITEMS[r["item_id"]].get("required_skill", "")
    or r["required_skill_level"] != gamedata.ITEMS[r["item_id"]].get("required_skill_level", 1)
]
check("rows restate the catalogue exactly", not mismatched, mismatched)

# The general store's potions are the ladder's first three rungs. Hard-coded
# rather than read back from the catalogue, because a test that derives its
# expectation from the thing under test cannot catch the ladder being retuned
# by accident - which is exactly how these numbers came to be checked at all.
ladder = {r["item_id"]: r["required_level"] for r in stock}
for item_id, expected in (("tinyhealthpotion", 1),
                          ("smallhealthpotion", 5),
                          ("mediumhealthpotion", 10)):
    check("%s needs level %d" % (item_id, expected),
          ladder.get(item_id) == expected,
          "got %r" % ladder.get(item_id))

check("gear on the same ladder: ironchest is tier 1 and open",
      ladder.get("ironchest") == 1, "got %r" % ladder.get("ironchest"))

# NOTHING THE SHOP SELLS IS SKILL-GATED, and that is the point rather than an
# oversight: a skill gate belongs on something you MADE, and a vendor sells
# things you BUY. If a cooked fish ever appears on a shelf this flips, and it
# should be a decision somebody made rather than one that slid in.
check("no vendor stock is skill-gated",
      all(r["required_skill"] == "" for r in stock),
      [r["item_id"] for r in stock if r["required_skill"]])

# THE GATE IS NOT ENFORCED HERE, AND THE TEST SAYS SO OUT LOUD. Using an item is
# client-side today, so buying above your level must still succeed - the shop's
# job is to TELL you, not to refuse. The day item use becomes server-authoritative
# this assertion is the one that has to be rewritten, and it is written this way
# so that nobody mistakes today's behaviour for a guarantee.
gated = next((r for r in stock if r["required_level"] > 1), None)
check("the catalogue has something above level 1", gated is not None)
if gated:
    attempt = client.post("/api/shop/buy", headers=H,
                          json={"slot": 0, "shop_id": "generalstore",
                                "item_id": gated["item_id"], "quantity": 1})
    check("buying above your level is allowed (advisory, not enforced)",
          attempt.status_code in (200, 400),
          "got %d - a 403 would mean the shop started enforcing" % attempt.status_code)
    balanced("buying a level-gated item")


# =============================================================================
# TWO PLAYERS, ONE TRADE
# =============================================================================
# The whole reason this suite exists. Trade is the only place two players'
# property changes hands, and it is the second gold sink - so every case below
# ends by asserting the invariant still holds.
#
# THE PROPERTY THAT MATTERS: a trade may only ever LOWER total supply. Gold
# crossing between purses is a transfer and must write no ledger rows; only the
# tax is an event. If a trade ever raised supply, or moved it without recording
# the burn, the drift check is what says so.

print("\ntrade: setup")

P1 = account("tradealice")
P2 = account("tradebob")
status("alice makes a character", client.put(
    "/api/save", headers=P1, json={"slot": 0, "class_id": "warrior", "name": "Alice"}), 200)
status("bob makes a character", client.put(
    "/api/save", headers=P2, json={"slot": 0, "class_id": "mage", "name": "Bob"}), 200)


def give(username, item_id, quantity):
    """
    Put an item in a backpack, written straight into carry_items.

    NOT THROUGH /api/staff/grant, which is @require_role("mod") - making a
    trader staff to arm them would test trade between two moderators, and the
    staff exemption in _reconcile_inventory() means that is not the same player
    the code has to be right for.

    DIRECT IS HONEST HERE IN A WAY IT IS NOT FOR GOLD. carry_items IS the
    server's record of what somebody owns, so writing it is indistinguishable
    from having earned it. Gold is different - it has a ledger the invariant
    checks against, which is why seed_gold() below writes a row too.
    """
    conn = db_conn()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    taken = {int(r["position"]) for r in conn.execute(
        "SELECT position FROM carry_items WHERE user_id = ? AND slot = 0", (uid,)).fetchall()}
    position = next(p for p in range(app_module.INVENTORY_CAPACITY) if p not in taken)
    conn.execute("INSERT INTO carry_items (user_id, slot, position, item_id, quantity)"
                 " VALUES (?, 0, ?, ?, ?)", (uid, position, item_id, quantity))
    conn.commit()
    conn.close()


def seed_gold(username, amount):
    """
    Put gold in a purse AND record it, so the invariant stays true.

    Deliberately NOT a bare UPDATE: this suite's own drift test proves a bare
    UPDATE is indistinguishable from a dupe, so seeding that way would leave
    every later case failing for a reason that has nothing to do with trade.
    """
    conn = db_conn()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]
    conn.execute("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = 0", (amount, uid))
    conn.execute("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
                 " VALUES (0, ?, 0, ?, 'test_seed', 'suite setup')", (uid, amount))
    conn.commit()
    conn.close()


def purse(username):
    conn = db_conn()
    row = conn.execute(
        "SELECT gold FROM saves JOIN users ON users.id = saves.user_id"
        " WHERE users.username = ? AND slot = 0", (username,)).fetchone()
    conn.close()
    return int(row["gold"])


def holding(username, item_id):
    conn = db_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS n FROM carry_items"
        " JOIN users ON users.id = carry_items.user_id"
        " WHERE users.username = ? AND slot = 0 AND item_id = ?", (username, item_id)).fetchone()
    conn.close()
    return int(row["n"])


def ledger_rows(reason=None):
    conn = db_conn()
    if reason:
        n = conn.execute("SELECT COUNT(*) FROM gold_ledger WHERE reason = ?", (reason,)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM gold_ledger").fetchone()[0]
    conn.close()
    return int(n)


seed_gold("tradealice", 5000)
seed_gold("tradebob", 5000)
balanced("seeding two traders")


# -----------------------------------------------------------------------------
print("\ntrade: refusals before anything can move")

status("trading with yourself", client.post(
    "/api/trade/offer", headers=P1, json={"slot": 0, "username": "tradealice"}), 400)
status("trading with a stranger", client.post(
    "/api/trade/offer", headers=P1, json={"slot": 0, "username": "nobodyatall"}), 400)
status("updating with no trade open", client.post(
    "/api/trade/update", headers=P1, json={"gold": 10}), 404)
status("confirming with no trade open", client.post(
    "/api/trade/confirm", headers=P1, json={}), 404)

body = status("no token", client.get("/api/trade"), 401)


# -----------------------------------------------------------------------------
print("\ntrade: gold for goods")

give("tradealice", "ironsword", 1)
check("alice holds the sword", holding("tradealice", "ironsword") == 1)

body = status("alice opens a trade with bob", client.post(
    "/api/trade/offer", headers=P1, json={"slot": 0, "username": "tradebob"}), 200)
check("the trade names both players",
      body and body["a"]["username"] == "tradealice" and body["b"]["username"] == "tradebob", body)
check("nobody has confirmed yet",
      body and not body["a"]["confirmed"] and not body["b"]["confirmed"], body)

status("bob cannot start a second trade", client.post(
    "/api/trade/offer", headers=P2, json={"slot": 0, "username": "tradealice"}), 409)

status("alice offers a sword she does not have twice", client.post(
    "/api/trade/update", headers=P1,
    json={"items": [{"item_id": "ironsword", "quantity": 2}]}), 400)
status("alice offers gold she does not have", client.post(
    "/api/trade/update", headers=P1, json={"gold": 999999}), 400)
status("alice offers an item that does not exist", client.post(
    "/api/trade/update", headers=P1, json={"items": [{"item_id": "nosuchthing", "quantity": 1}]}), 400)

body = status("alice puts up the sword", client.post(
    "/api/trade/update", headers=P1,
    json={"items": [{"item_id": "ironsword", "quantity": 1}]}), 200)
sword_value = gamedata.ITEMS["ironsword"]["value"]
check("her side is valued at the sword's value",
      body and body["a"]["offering_value"] == sword_value,
      "got %r want %d" % (body["a"]["offering_value"] if body else None, sword_value))

body = status("bob puts up 1000 gold", client.post(
    "/api/trade/update", headers=P2, json={"gold": 1000}), 200)

# The quote is the number each side is about to be charged, and it is charged on
# what they RECEIVE. Asserted against gamedata's own function rather than a
# hardcoded 50, so retuning the rate retunes the test with it.
want_a_tax = gamedata.trade_tax(1000)          # alice receives 1000 gold
want_b_tax = gamedata.trade_tax(sword_value)   # bob receives the sword
check("alice is quoted tax on the gold she receives",
      body and body["a"]["tax"] == want_a_tax, "got %r want %d" % (body["a"]["tax"], want_a_tax))
check("bob is quoted tax on the sword he receives",
      body and body["b"]["tax"] == want_b_tax, "got %r want %d" % (body["b"]["tax"], want_b_tax))

body = status("alice confirms", client.post("/api/trade/confirm", headers=P1, json={}), 200)
check("alice is confirmed and bob is not",
      body and body["a"]["confirmed"] and not body["b"]["confirmed"], body)
check("nothing has moved yet", holding("tradealice", "ironsword") == 1)

# CHANGING AN OFFER MUST UN-CONFIRM BOTH SIDES. The version of this bug that
# actually costs somebody an item is a REMOVAL after the other side agreed.
body = status("bob edits his offer", client.post(
    "/api/trade/update", headers=P2, json={"gold": 1000}), 200)
check("alice's confirmation was withdrawn by bob's edit",
      body and not body["a"]["confirmed"] and not body["b"]["confirmed"], body)

alice_before, bob_before = purse("tradealice"), purse("tradebob")
supply_before = supply()
tax_rows_before = ledger_rows("kingdom_tax")

status("alice re-confirms", client.post("/api/trade/confirm", headers=P1, json={}), 200)
body = status("bob confirms and it executes", client.post(
    "/api/trade/confirm", headers=P2, json={}), 200)

check("the trade reports done", body and body.get("state") == "done", body)
check("the kingdom took both cuts",
      body and body["kingdom_take"] == want_a_tax + want_b_tax, body)

check("the sword crossed to bob", holding("tradebob", "ironsword") == 1)
check("alice no longer has it", holding("tradealice", "ironsword") == 0)
check("alice gained the gold less her tax",
      purse("tradealice") == alice_before + 1000 - want_a_tax,
      "%d -> %d" % (alice_before, purse("tradealice")))
check("bob paid the gold and his tax",
      purse("tradebob") == bob_before - 1000 - want_b_tax,
      "%d -> %d" % (bob_before, purse("tradebob")))

after = supply()
check("supply fell by exactly the tax",
      supply_before["held"] - after["held"] == want_a_tax + want_b_tax,
      "fell by %d" % (supply_before["held"] - after["held"]))
check("nothing was minted by a trade", after["minted"] == supply_before["minted"])
check("exactly two burn rows were written",
      ledger_rows("kingdom_tax") - tax_rows_before == 2,
      "wrote %d" % (ledger_rows("kingdom_tax") - tax_rows_before))
balanced("a gold-for-goods trade")

status("the trade is over", client.post("/api/trade/confirm", headers=P1, json={}), 404)


# -----------------------------------------------------------------------------
print("\ntrade: the gold leg writes no ledger rows")
# The single easiest thing to get wrong. gold_delta() calls itself the only way
# gold may change, so routing the transfer through it looks tidy - and would
# write a -N and a +N claiming two supply events that never happened.

give("tradealice", "tinyhealthpotion", 1)
status("open", client.post("/api/trade/offer", headers=P1,
                           json={"slot": 0, "username": "tradebob"}), 200)
status("alice puts up a potion", client.post("/api/trade/update", headers=P1,
       json={"items": [{"item_id": "tinyhealthpotion", "quantity": 1}]}), 200)
status("bob puts up 40 gold", client.post("/api/trade/update", headers=P2, json={"gold": 40}), 200)

rows_before = ledger_rows()
status("alice confirms", client.post("/api/trade/confirm", headers=P1, json={}), 200)
status("bob confirms", client.post("/api/trade/confirm", headers=P2, json={}), 200)

# Two taxes, and nothing else. Forty gold moved between two purses and the
# ledger is silent about it, which is correct: both purses are supply.
check("a trade wrote exactly 2 ledger rows, both taxes",
      ledger_rows() - rows_before == 2, "wrote %d" % (ledger_rows() - rows_before))
balanced("the transfer leg")


# -----------------------------------------------------------------------------
print("\ntrade: barter is taxed too")
# If only the gold leg were taxed, every trade in the game would become barter
# within a day of somebody noticing.

give("tradealice", "ironsword", 1)
give("tradebob", "ironhelm", 1)
status("open", client.post("/api/trade/offer", headers=P1,
                           json={"slot": 0, "username": "tradebob"}), 200)
status("sword up", client.post("/api/trade/update", headers=P1,
       json={"items": [{"item_id": "ironsword", "quantity": 1}]}), 200)
status("helm up", client.post("/api/trade/update", headers=P2,
       json={"items": [{"item_id": "ironhelm", "quantity": 1}]}), 200)

before = supply()
status("alice confirms", client.post("/api/trade/confirm", headers=P1, json={}), 200)
body = status("bob confirms", client.post("/api/trade/confirm", headers=P2, json={}), 200)

check("no gold changed hands at all", body and body["kingdom_take"] > 0, body)
check("but supply still fell", supply()["held"] < before["held"],
      "%d -> %d" % (before["held"], supply()["held"]))
check("the items crossed",
      holding("tradebob", "ironsword") == 2 and holding("tradealice", "ironhelm") == 1,
      "bob swords=%d alice helms=%d" % (holding("tradebob", "ironsword"),
                                        holding("tradealice", "ironhelm")))
balanced("a pure barter")


# -----------------------------------------------------------------------------
print("\ntrade: splitting a trade is never cheaper")
# The floor of 1 gold is what makes this true, and it is the whole reason the
# rounding goes up. Without it every sub-20-value trade is free and moving
# 10,000 gold in dribbles pays nothing.

whole = gamedata.trade_tax(100)
split = 10 * gamedata.trade_tax(10)
check("ten small trades cost more than one big one", split > whole,
      "one=%d ten=%d" % (whole, split))
check("a trade worth 1 still pays something", gamedata.trade_tax(1) >= 1)
check("a trade worth nothing pays nothing", gamedata.trade_tax(0) == 0)


# -----------------------------------------------------------------------------
print("\ntrade: cancelling, and the tax you cannot afford")

status("open", client.post("/api/trade/offer", headers=P1,
                           json={"slot": 0, "username": "tradebob"}), 200)
body = status("alice cancels", client.post("/api/trade/cancel", headers=P1, json={}), 200)
check("it reports cancelled", body and body.get("cancelled") is True, body)
body = status("and now there is no trade", client.get("/api/trade", headers=P2), 200)
check("bob's panel sees nothing", body and body.get("trade") is None, body)

# A pauper receiving something valuable cannot pay the cut. Refused whole
# rather than half-done, and the world must be untouched afterwards.
POOR = account("tradepauper")
status("pauper character", client.put("/api/save", headers=POOR,
       json={"slot": 0, "class_id": "healer", "name": "Pauper"}), 200)
check("the pauper is broke", purse("tradepauper") == 0, purse("tradepauper"))

give("tradealice", "amethystsword", 1)
status("open with the pauper", client.post("/api/trade/offer", headers=P1,
       json={"slot": 0, "username": "tradepauper"}), 200)
status("alice offers something expensive", client.post("/api/trade/update", headers=P1,
       json={"items": [{"item_id": "amethystsword", "quantity": 1}]}), 200)

before = supply()
alice_had = purse("tradealice")
status("alice confirms", client.post("/api/trade/confirm", headers=P1, json={}), 200)
status("the pauper confirms and cannot cover the cut", client.post(
    "/api/trade/confirm", headers=POOR, json={}), 400)

check("the sword did not move", holding("tradealice", "amethystsword") == 1)
check("the pauper got nothing", holding("tradepauper", "amethystsword") == 0)
check("alice's purse is untouched", purse("tradealice") == alice_had)
check("supply is untouched", supply()["held"] == before["held"])
balanced("a refused trade")

# AND THE TRADE IS STILL OPEN. _execute_trade() claims the row by flipping it to
# 'done' as its FIRST act, then rolls back on any refusal - so this asserts the
# rollback really does put the claim back. If it did not, a trade that failed
# because somebody was briefly a gold short would be dead forever, and the only
# symptom would be players saying trade "randomly stops working".
body = status("the refused trade is still open", client.get("/api/trade", headers=P1), 200)
check("and it is the same trade, not a new one",
      body and body.get("trade") is not None, body)
check("nobody is still marked confirmed after a failed execution",
      body and not body["trade"]["a"]["confirmed"] and not body["trade"]["b"]["confirmed"],
      body.get("trade"))

status("clear it before the next case", client.post("/api/trade/cancel", headers=P1, json={}), 200)


# =============================================================================
# THE KINGDOM TAX BOARD
# =============================================================================
# The board is DERIVED from gold_ledger rather than counted into a column, so
# the property worth testing is that it agrees with the ledger it reads - not
# that it returns some number. Every assertion below compares it against a
# direct query rather than a figure written here, because a board checked
# against a constant stops being checked the moment the suite above changes.

print("\nkingdom tax board")

status("no token", client.get("/api/economy/kingdom"), 401)

# OPEN TO EVERY PLAYER, unlike /api/economy/supply which 404s for one. If this
# ever starts 404ing, somebody has stacked require_role on it by habit.
body = status("an ordinary player may read it", client.get(
    "/api/economy/kingdom", headers=P1), 200)

conn = db_conn()
burned_total = -int(conn.execute(
    "SELECT COALESCE(SUM(delta), 0) FROM gold_ledger WHERE delta < 0").fetchone()[0])
tax_total = -int(conn.execute(
    "SELECT COALESCE(SUM(delta), 0) FROM gold_ledger WHERE reason = 'kingdom_tax'").fetchone()[0])
conn.close()

check("the total is every gold destroyed",
      body and body["total"] == burned_total,
      "board=%r ledger=%d" % (body.get("total") if body else None, burned_total))
check("the trade tax is visible on its own",
      body and body["by_reason"].get("kingdom_tax") == tax_total,
      "board=%r ledger=%d" % (body["by_reason"].get("kingdom_tax") if body else None, tax_total))
check("the breakdown adds up to the total",
      body and sum(body["by_reason"].values()) == body["total"], body.get("by_reason"))
check("vendor spending counts as contribution too",
      body and body["by_reason"].get("shop_buy", 0) > 0, body.get("by_reason"))

# The board must agree with the supply report standing beside it: gold destroyed
# is gold destroyed, however it is being presented.
check("the board's total matches the supply report's burned figure",
      body and body["total"] == supply()["burned"],
      "board=%r supply=%d" % (body.get("total") if body else None, supply()["burned"]))

check("alice has a rank and it is a real one",
      body and body["you"]["username"] == "tradealice"
      and body["you"]["rank"] is not None and body["you"]["contributed"] > 0,
      body.get("you"))

check("the top list is ordered by contribution, descending",
      body and all(a["contributed"] >= b["contributed"]
                   for a, b in zip(body["top"], body["top"][1:])),
      body.get("top"))
check("the board is capped at the board size",
      body and len(body["top"]) <= app_module.KINGDOM_BOARD_SIZE, len(body["top"]))

# SOMEBODY WHO HAS NEVER PAID ANYTHING still gets a line. A board that omitted
# them would tell most of the playerbase they are not in the game.
fresh = account("kingdomnobody")
body = status("a player who has contributed nothing", client.get(
    "/api/economy/kingdom", headers=fresh), 200)
check("they see zero rather than nothing",
      body and body["you"]["contributed"] == 0 and body["you"]["rank"] is None,
      body.get("you"))
check("and they still see the realm's total", body and body["total"] == burned_total, body.get("total"))

# THE BOARD MOVES WHEN A TRADE IS TAXED, which is the whole point of it.
give("tradealice", "ironsword", 1)
before = status("read before", client.get("/api/economy/kingdom", headers=P1), 200)
status("open", client.post("/api/trade/offer", headers=P1,
                           json={"slot": 0, "username": "tradebob"}), 200)
status("sword up", client.post("/api/trade/update", headers=P1,
       json={"items": [{"item_id": "ironsword", "quantity": 1}]}), 200)
status("gold up", client.post("/api/trade/update", headers=P2, json={"gold": 200}), 200)
status("alice confirms", client.post("/api/trade/confirm", headers=P1, json={}), 200)
result = status("bob confirms", client.post("/api/trade/confirm", headers=P2, json={}), 200)
after = status("read after", client.get("/api/economy/kingdom", headers=P1), 200)

check("the realm's total rose by exactly this trade's take",
      before and after and result
      and after["total"] - before["total"] == result["kingdom_take"],
      "rose %d, take %d" % ((after["total"] - before["total"]) if (before and after) else -1,
                            result["kingdom_take"] if result else -1))
check("and alice's own line rose by her share",
      before and after and result
      and after["you"]["contributed"] - before["you"]["contributed"] == result["tax_paid"]["a"],
      "rose %d, paid %d" % ((after["you"]["contributed"] - before["you"]["contributed"])
                            if (before and after) else -1,
                            result["tax_paid"]["a"] if result else -1))
balanced("reading the board")


# =============================================================================
# THE TRADE PANEL'S CONTRACT
# =============================================================================
# tradepanel.gd reads specific keys out of these responses. A field that is
# renamed, or a response shape that differs between endpoints, is invisible on
# the server and shows up in game as a blank column or a wrong number.
#
# THIS IS THE FAILURE MODE THAT HAS ALREADY HAPPENED TWICE in this project: a
# client calling hud.apply_server_inventory(), which did not exist, and a shop
# panel reading data.message when api.gd had already put the text in res.error.
# Both were caught by a human playing, not by a test. This asserts the shape.

print("\ntrade panel contract")


def panel_side_ok(side, label):
    """Every key tradepanel.gd touches on one side of a trade."""
    for key in ("username", "slot", "items", "gold", "confirmed",
                "offering_value", "tax"):
        check("%s carries %s" % (label, key), key in side, sorted(side.keys()))
    check("%s items is a list" % label, isinstance(side.get("items"), list))
    for entry in side.get("items", []):
        check("%s item has item_id and quantity" % label,
              "item_id" in entry and "quantity" in entry, entry)


# The panel polls this first and branches on trade being null.
body = status("GET /api/trade with no trade open", client.get("/api/trade", headers=P1), 200)
check("it answers with a trade key", body is not None and "trade" in body, body)
check("and that key is null when there is none", body and body["trade"] is None, body)

give("tradealice", "ironsword", 1)
give("tradebob", "ironhelm", 1)

# OPEN. The panel renders straight from this response rather than re-polling,
# so it has to be the same shape as the poll's trade object.
opened = status("POST /api/trade/offer", client.post(
    "/api/trade/offer", headers=P1,
    json={"slot": 0, "username": "tradebob"}), 200)
check("offer returns the trade itself, not a wrapper",
      opened is not None and "a" in opened and "b" in opened, opened)
panel_side_ok(opened["a"], "offer.a")
panel_side_ok(opened["b"], "offer.b")

# The panel decides which column is "You" by matching Api.username against
# these. If both names were ever equal, or either missing, it would render the
# wrong side's items as the player's own.
check("the two sides are named, and differently",
      opened["a"]["username"] == "tradealice"
      and opened["b"]["username"] == "tradebob", 
      (opened["a"]["username"], opened["b"]["username"]))

# UPDATE. Same shape again - the panel re-renders from it so the other side's
# acceptance badge clears immediately rather than a poll later.
updated = status("POST /api/trade/update", client.post(
    "/api/trade/update", headers=P1,
    json={"items": [{"item_id": "ironsword", "quantity": 1}], "gold": 0}), 200)
check("update returns the same shape as offer",
      updated is not None and "a" in updated and "b" in updated, updated)
panel_side_ok(updated["a"], "update.a")
check("our side now lists what we offered",
      updated["a"]["items"] == [{"item_id": "ironsword", "quantity": 1}],
      updated["a"]["items"])

status("bob puts something up", client.post(
    "/api/trade/update", headers=P2,
    json={"items": [{"item_id": "ironhelm", "quantity": 1}], "gold": 25}), 200)

# The tax the panel prints in each column. Quoted on what that side RECEIVES,
# so a side offering nothing still owes nothing and a side receiving the sword
# owes something.
current = status("GET /api/trade while open", client.get("/api/trade", headers=P1), 200)
trade = (current or {}).get("trade") or {}
check("the poll returns a trade object now", bool(trade), current)
panel_side_ok(trade.get("a", {}), "poll.a")
panel_side_ok(trade.get("b", {}), "poll.b")
check("both sides are quoted a tax the panel can print",
      isinstance(trade["a"].get("tax"), int) and isinstance(trade["b"].get("tax"), int),
      (trade["a"].get("tax"), trade["b"].get("tax")))

# CONFIRM, ONE SIDE. The panel checks state != "done" and re-renders.
half = status("POST /api/trade/confirm (first side)", client.post(
    "/api/trade/confirm", headers=P1, json={}), 200)
check("a single confirm does not execute", half and half.get("state") != "done", half)
check("and it comes back as a trade the panel can render",
      half is not None and "a" in half and "b" in half, half)
check("our side reads as accepted", half["a"]["confirmed"] is True, half["a"])
check("theirs does not", half["b"]["confirmed"] is False, half["b"])

# CONFIRM, SECOND SIDE - the execution response, which is a DIFFERENT shape.
done = status("POST /api/trade/confirm (second side)", client.post(
    "/api/trade/confirm", headers=P2, json={}), 200)
check("it reports done", done and done.get("state") == "done", done)
for key in ("tax_paid", "kingdom_take", "a", "b"):
    check("the result carries %s" % key, key in done, sorted(done.keys()))
check("tax_paid is keyed by side, like the panel indexes it",
      "a" in done["tax_paid"] and "b" in done["tax_paid"], done.get("tax_paid"))

# The panel writes these two straight into the player. A missing key here is a
# purse that silently stops matching the server.
for side in ("a", "b"):
    check("result.%s carries gold" % side, "gold" in done[side], done[side].keys())
    check("result.%s carries inventory" % side, "inventory" in done[side], done[side].keys())
    check("result.%s inventory is a positional array" % side,
          isinstance(done[side]["inventory"], list)
          and len(done[side]["inventory"]) == app_module.INVENTORY_CAPACITY,
          len(done[side]["inventory"]) if isinstance(done[side]["inventory"], list) else None)

check("the sword reached bob", holding("tradebob", "ironsword") >= 1)
check("the helm reached alice", holding("tradealice", "ironhelm") >= 1)
balanced("a trade driven the way the panel drives it")

# CANCEL, which the panel reads a single boolean out of.
status("open another", client.post("/api/trade/offer", headers=P1,
                                   json={"slot": 0, "username": "tradebob"}), 200)
cancelled = status("POST /api/trade/cancel", client.post(
    "/api/trade/cancel", headers=P1, json={}), 200)
check("cancel answers with the flag the panel checks",
      cancelled is not None and cancelled.get("cancelled") is True, cancelled)
body = status("and the trade is gone", client.get("/api/trade", headers=P2), 200)
check("both sides see it gone", body and body.get("trade") is None, body)
balanced("cancelling from the panel")


# =============================================================================
# WHO ELSE IS HERE
# =============================================================================
# The trade panel lists people instead of asking you to type a name. What it
# lists has to be true: somebody who is offline, or somewhere else, or you, are
# all wrong answers, and the last one is the one that produces a trade offer a
# player cannot understand.

print("\nnearby players")

status("no token", client.get("/api/players/nearby"), 401)
status("no slot", client.get("/api/players/nearby", headers=P1), 400)
status("an empty slot", client.get("/api/players/nearby", headers=P1,
                                   query_string={"slot": 3}), 404)

body = status("alice asks who is here", client.get(
    "/api/players/nearby", headers=P1, query_string={"slot": 0}), 200)
check("it names the area", body and "area" in body, body)
check("and says how precise that is, so the panel need not guess",
      body and body.get("precision") == "area", body)
check("players is a list", body and isinstance(body.get("players"), list), body)

names = [p["username"] for p in (body or {}).get("players", [])]
check("bob is listed - same area, logged in", "tradebob" in names, names)
check("alice is NOT listed to herself", "tradealice" not in names, names)

for p in (body or {}).get("players", []):
    check("%s carries what the panel draws" % p.get("username"),
          all(k in p for k in ("username", "slot", "name", "level")), sorted(p.keys()))

# SOMEWHERE ELSE IS NOT NEARBY. The whole value of the list is that it is
# shorter than "everyone", so this is the assertion that it filters at all.
conn = db_conn()
uid = conn.execute("SELECT id FROM users WHERE username = 'tradebob'").fetchone()["id"]
conn.execute("UPDATE saves SET area = 'somewhereelse' WHERE user_id = ? AND slot = 0", (uid,))
conn.commit(); conn.close()

body = status("after bob walks off", client.get(
    "/api/players/nearby", headers=P1, query_string={"slot": 0}), 200)
check("bob is gone from the list",
      "tradebob" not in [p["username"] for p in (body or {}).get("players", [])],
      [p["username"] for p in (body or {}).get("players", [])])

conn = db_conn()
conn.execute("UPDATE saves SET area = 'elusion' WHERE user_id = ? AND slot = 0", (uid,))
conn.commit(); conn.close()

# OFFLINE IS NOT NEARBY EITHER, and "online" here means a live session rather
# than a recent save - a save happens on a timer, so it would list somebody who
# alt-tabbed away an hour ago.
conn = db_conn()
conn.execute("UPDATE sessions SET expires_at = 1 WHERE user_id = ?", (uid,))
conn.commit(); conn.close()

body = status("after bob's session expires", client.get(
    "/api/players/nearby", headers=P1, query_string={"slot": 0}), 200)
check("an offline player is not listed as nearby",
      "tradebob" not in [p["username"] for p in (body or {}).get("players", [])],
      [p["username"] for p in (body or {}).get("players", [])])

# Put bob back so anything after this still has a partner.
P2 = account("tradebob")
body = status("and back when he logs in again", client.get(
    "/api/players/nearby", headers=P1, query_string={"slot": 0}), 200)
check("bob returns to the list",
      "tradebob" in [p["username"] for p in (body or {}).get("players", [])],
      [p["username"] for p in (body or {}).get("players", [])])

# ONE ROW PER CHARACTER, not one per session. An account with two live tokens
# would otherwise appear twice, and the panel would draw the same person twice.
conn = db_conn()
conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES ('extra-token', ?, ?)",
             (uid, 4000000000))
conn.commit(); conn.close()
body = status("with bob logged in twice", client.get(
    "/api/players/nearby", headers=P1, query_string={"slot": 0}), 200)
listed = [p["username"] for p in (body or {}).get("players", [])]
check("he still appears exactly once", listed.count("tradebob") == 1, listed)

# The name it lists must be the one /api/trade/offer accepts, or the panel
# builds a button that cannot work.
target = next((p for p in (body or {}).get("players", []) if p["username"] == "tradebob"), None)
if target:
    opened = status("offering to a name straight off the list", client.post(
        "/api/trade/offer", headers=P1,
        json={"slot": 0, "username": target["username"], "to_slot": target["slot"]}), 200)
    check("the trade opens with exactly that player",
          opened and opened["b"]["username"] == target["username"], opened)
    status("tidy up", client.post("/api/trade/cancel", headers=P1, json={}), 200)
balanced("listing nearby players")


print("\n=== THE BOARD AT TWO HUNDRED ===\n")
#
# A top ten is not a scoreboard for a game with two hundred players - it is a
# wall with ten names on it that nobody else appears on, which makes "your
# contribution counts" into something only the richest ten ever see proof of.
#
# USERS INSERTED DIRECTLY, not registered. /api/auth/register runs scrypt, and
# two hundred of those is half a minute of a suite that has to stay quick. The
# rows are what the board joins against; nothing here needs a real password.

check("the board is sized for a real population",
      app_module.KINGDOM_BOARD_SIZE >= 200, app_module.KINGDOM_BOARD_SIZE)

_conn = db_conn()
_crowd = app_module.KINGDOM_BOARD_SIZE + 12
_now = int(time.time())
_conn.executemany(
    "INSERT INTO users (username, password_hash, created_at) VALUES (?, 'x', ?)",
    [("crowd%04d" % i, _now) for i in range(_crowd)],
)
_ids = [r[0] for r in _conn.execute(
    "SELECT id FROM users WHERE username LIKE 'crowd%' ORDER BY id")]
_conn.executemany("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)",
                  [(i,) for i in _ids])
# Each one gave a different amount, so the ordering is unambiguous and the
# player who falls off the end is a known one.
#
# TWO ROWS EACH, AND THAT IS NOT PADDING. A contribution is gold that EXISTED
# and then stopped existing, so a fixture that writes only the burn records the
# world destroying gold it never created - and the supply invariant fails three
# sections later, in a case that has nothing to do with this one. The first
# draft did exactly that. Mint it, then burn it: the balances end where they
# started and the board still sees a giver.
_rows = []
for rank, uid in enumerate(_ids):
    _amount = (rank + 1) * 7
    _rows.append((_now, uid, _amount, "test_fixture", "crowd fixture"))
    _rows.append((_now, uid, -_amount, "revive", "crowd fixture"))
_conn.executemany(
    "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) "
    "VALUES (?, ?, NULL, ?, ?, ?)",
    _rows,
)
_conn.commit()
_conn.close()

body = status("the board with a crowd on it", client.get(
    "/api/economy/kingdom", headers=P1), 200)

check("it returns exactly the cap, not everybody",
      len(body["top"]) == app_module.KINGDOM_BOARD_SIZE,
      [len(body["top"]), app_module.KINGDOM_BOARD_SIZE])
check("and counts every contributor, not only the shown ones",
      int(body["contributors"]) > len(body["top"]),
      [body["contributors"], len(body["top"])])

# ORDERED, STILL. A cap that returned an arbitrary two hundred would look
# identical to this one at a glance.
_given = [int(e["contributed"]) for e in body["top"]]
check("the rows are in descending order of gold given",
      _given == sorted(_given, reverse=True), _given[:5])
check("the top row is the biggest giver in the game",
      _given[0] == max(_given), _given[0])

# RANKS, WITH TIES. The client used to derive rank from a row's position in the
# list, which is only the same thing while nothing ties and the list is whole.
# It is neither: the board is capped, and two players who gave the same amount
# are joint. Competition ranking - 1, 1, 3 - because a tie means neither beat
# the other, and printing 1 and 2 says one of them did.
_conn = db_conn()
_tied = [("tie_a", 900), ("tie_b", 900), ("tie_c", 900), ("tie_d", 400), ("tie_e", 400)]
_now2 = int(time.time())
_conn.executemany(
    "INSERT INTO users (username, password_hash, created_at) VALUES (?, 'x', ?)",
    [(name, _now2) for name, _ in _tied],
)
for name, amount in _tied:
    uid = _conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()[0]
    _conn.execute("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (uid,))
    # Minted then burned, so the fixture leaves the supply invariant alone.
    _conn.execute(
        "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) "
        "VALUES (?, ?, NULL, ?, 'test_fixture', 'tie fixture')", (_now2, uid, amount))
    _conn.execute(
        "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) "
        "VALUES (?, ?, NULL, ?, 'revive', 'tie fixture')", (_now2, uid, -amount))
_conn.commit()
_conn.close()

body = status("the board with ties on it", client.get(
    "/api/economy/kingdom", headers=P1), 200)

_by_name = {e["username"]: e for e in body["top"]}
check("every row carries a rank from the server",
      all("rank" in e for e in body["top"]))

_nines = sorted(_by_name[n]["rank"] for n, a in _tied if a == 900 and n in _by_name)
check("three players tied on 900 share one rank",
      len(set(_nines)) == 1, _nines)

_fours = sorted(_by_name[n]["rank"] for n, a in _tied if a == 400 and n in _by_name)
check("and the two tied on 400 share another", len(set(_fours)) == 1, _fours)
# THE GAP AFTER A TIE, measured against the board rather than against the other
# fixture group - the first draft assumed the two groups were adjacent and they
# are not, with two hundred crowd players seeded between them. Three players
# sharing rank R means the next distinct rank is R + 3.
_all_ranks = sorted(set(e["rank"] for e in body["top"]))
_shared = _nines[0] if _nines else None
_share_count = len([e for e in body["top"] if e["rank"] == _shared])
_after = [r for r in _all_ranks if r > _shared]
check("the rank after a tie skips by the size of the tie",
      bool(_after) and _after[0] == _shared + _share_count,
      [_shared, _share_count, _after[:1]])

# The ordering and the ranking must never disagree about what counts as equal.
_seen = []
for entry in body["top"]:
    _seen.append((entry["rank"], entry["contributed"], entry["lusions"]))
check("rank never decreases as you go down the board",
      all(_seen[i][0] <= _seen[i + 1][0] for i in range(len(_seen) - 1)))
check("two rows share a rank only when they gave exactly the same",
      all((_seen[i][0] == _seen[i + 1][0])
          == (_seen[i][1:] == _seen[i + 1][1:])
          for i in range(len(_seen) - 1)))


# THE WHOLE POINT OF A SEPARATE "you": rank is over the field, not the page.
_conn = db_conn()
_last_id = _ids[0]                       # gave the least, so sits last overall
_conn.execute("UPDATE users SET username = ? WHERE id = ?", ("tinygiver", _last_id))
_conn.commit()
_conn.close()
_tiny = account("tinygiver_unused")      # a token for someone else entirely
body = status("read as a player outside the cap", client.get(
    "/api/economy/kingdom", headers=_tiny), 200)
check("a player outside the cap still gets their own line",
      "you" in body and body["you"]["username"] == "tinygiver_unused", body.get("you"))
check("and both currencies are on it",
      "contributed" in body["you"] and "lusions" in body["you"], body["you"])


# =============================================================================
# THE CLIENT CANNOT MINT ITS OWN GOLD
# =============================================================================
#
# EVERY OTHER CASE IN THIS FILE moves gold through a server path and then checks
# that the invariant held. This one does the opposite: it takes the shortest
# route a modified client has to the balance and proves the invariant is not
# reachable that way at all.
#
# It is here because this was real. `gold` is one of STATUS_FIELDS and had never
# been added to SERVER_OWNED_STATS, so PUT /api/player/status stored whatever
# figure arrived. One request, 200, and:
#
#     SUM(gold_ledger.delta) = 0        SUM(purses) = 1,000,000
#
# Nothing in this suite caught it, because nothing in this suite tried - every
# case reached the economy through the front door. An invariant only tells you
# about the paths somebody walked.

print("\n=== MINTING THROUGH THE STATUS ENDPOINT ===\n")

MINTER = account("midas")
status("a character to mint for", client.put(
    "/api/save", headers=MINTER,
    json={"slot": 0, "class_id": "warrior", "name": "Midas"}), 200)

before = supply()

body = status("PUT /api/player/status with a million gold", client.put(
    "/api/player/status", headers=MINTER, json={"slot": 0, "gold": 1_000_000}), 200)

check("the balance did not move", int(body["gold"]) == 0, body)
check("and the server reported gold as ignored",
      "gold" in body.get("ignored", []), body)

after = supply()
check("no gold was recorded into existence",
      after["recorded"] == before["recorded"],
      [before["recorded"], after["recorded"]])
check("and none appeared in a purse",
      after["held"] == before["held"], [before["held"], after["held"]])
balanced("a client trying to mint gold")

# The same attempt through the other write path, which always refused it. Kept
# so the two endpoints cannot drift apart again without something failing here.
status("PUT /api/save with a million gold", client.put(
    "/api/save", headers=MINTER,
    json={"slot": 0, "class_id": "warrior", "name": "Midas", "gold": 1_000_000}), 200)
body = status("read it back", client.get("/api/player/status?slot=0", headers=MINTER), 200)
check("the save endpoint refused it too", int(body["gold"]) == 0, body)
balanced("a client trying to mint gold through /api/save")


# =============================================================================

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("\nfailures:")
    for label, detail in failures:
        print("  %s   %s" % (label, detail))
sys.exit(1 if failed else 0)
