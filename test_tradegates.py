"""Trade gates: the owner's trade switch, and the hold on a fresh mythic or
Perfect.

E-3 is open - a kill is asserted by the game, not watched by the server - so a
cheated kill mints real loot, and trading is the one road from one account to
another. These are the two gates on that road (E3_SCOPE.md, option B):

  G-1  the switch: off refuses a NEW trade with 503, says so on /api/status,
       is the owner's alone, is announced and logged, reads OFF when its row
       cannot be read and ON when there is no row
  G-2  off lets an open trade finish - changed, accepted and executed - and
       cancelled; on again opens the door
  G-3  a mythic taken from a loot bag, or a Perfect bought from the shop, is
       held from trade for TRADE_HOLD_SECONDS; an ordinary piece is not
  G-4  the hold is the account's: moving the piece to the bank or another
       character does not free it; a second, older copy of the same id may go
  G-5  a hold placed after the offer refuses the trade at execution, with
       nothing moved and nobody left agreed
  G-6  a finished hold frees the piece; placing a hold prunes only the
       account's own finished ones

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_tradegates.py
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_tradegates_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "boss"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

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


def auth(token):
    return {"Authorization": "Bearer %s" % token}


def register(name):
    return client.post("/api/auth/register",
                       json={"username": name, "password": "password123"}).get_json()["token"]


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


def post(token, path, body):
    return client.post(path, headers=auth(token), json=body)


def put_item(username, position, item_id, quantity=1, slot=0):
    sql("INSERT OR REPLACE INTO carry_items (user_id, slot, position, item_id, quantity)"
        " VALUES (?, ?, ?, ?, ?)", (uid(username), slot, position, item_id, quantity))


def holding(username, item_id, slot=0):
    return sum(int(r["quantity"]) for r in sql(
        "SELECT quantity FROM carry_items WHERE user_id = ? AND slot = ? AND item_id = ?",
        (uid(username), slot, item_id)))


def holds(username, item_id=None):
    if item_id is None:
        return sql("SELECT * FROM trade_holds WHERE user_id = ?", (uid(username),))
    return sql("SELECT * FROM trade_holds WHERE user_id = ? AND item_id = ?", (uid(username), item_id))


def status():
    return client.get("/api/status").get_json() or {}


def trade_of(token):
    return (client.get("/api/trade", headers=auth(token)).get_json() or {})


def accept(token):
    rev = (trade_of(token).get("trade") or {}).get("revision", -1)
    return post(token, "/api/trade/confirm", {"revision": rev})


def cancel_all(*tokens):
    for token in tokens:
        post(token, "/api/trade/cancel", {})


def online(*tokens):
    for token in tokens:
        client.get("/api/server/broadcasts?slot=0", headers=auth(token))


def give_gold(username, amount):
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = 0", (amount, uid(username)))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (0, ?, 0, ?, 'test_seed', 'suite setup')", (uid(username), amount))


def bag_with(username, item_id, bag_id):
    sql("INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at) VALUES (?, ?, 0, 'slime', ?)",
        (bag_id, uid(username), int(time.time())))
    sql("INSERT INTO loot_bag_items (bag_id, position, item_id, quantity) VALUES (?, 0, ?, 1)",
        (bag_id, item_id))


BOSS = register("boss")
ALICE = register("alice")
BOB = register("bob")
for token, name in ((BOSS, "Boss"), (ALICE, "Aldra"), (BOB, "Bram")):
    client.put("/api/save", headers=auth(token), json={"slot": 0, "class_id": "warrior", "name": name})
client.put("/api/save", headers=auth(ALICE), json={"slot": 1, "class_id": "mage", "name": "Aldra Alt"})
for name in ("alice", "bob"):
    give_gold(name, 100000)
online(ALICE, BOB)

MYTHIC = gamedata.roll_quality(gamedata.mythic_pool("warrior")[0])
PERFECT = gamedata.perfect_id("ironsword")
PLAIN = "ironsword~d100"
if not gamedata.has_item(PLAIN):
    PLAIN = gamedata.roll_quality("ironsword")
    while gamedata.is_perfect(PLAIN):
        PLAIN = gamedata.roll_quality("ironsword")


# =============================================================================
print("\n--- G-1 the switch ---")
# =============================================================================
check("with no row, trading is on, and /api/status says so", status().get("trade") is True, status())
res = post(ALICE, "/api/server/trade", {"on": False})
check("a player is told the switch does not exist", res.status_code == 404, res.status_code)
res = post(BOSS, "/api/server/trade", {"on": "no"})
check("'on' must be true or false", res.status_code == 400, res.status_code)
res = post(BOSS, "/api/server/trade", {})
check("  and must be there", res.status_code == 400, res.status_code)

before_trades = len(sql("SELECT * FROM trades"))
res = post(BOSS, "/api/server/trade", {"on": False})
body = res.get_json() or {}
check("the owner switches trading off", res.status_code == 200 and body.get("trade") is False, body)
check("  /api/status says so, to anybody, with no login", status().get("trade") is False, status())
logged = sql("SELECT detail FROM staff_actions WHERE action = 'trade' ORDER BY id DESC LIMIT 1")
check("  it is in the staff log", logged and logged[0]["detail"] == "off", [dict(r) for r in logged])
said = sql("SELECT body FROM broadcasts ORDER BY id DESC LIMIT 1")
check("  and announced, saying an open trade can still finish",
      said and "paused" in said[0]["body"] and "can still finish" in said[0]["body"],
      [dict(r) for r in said])
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
body = res.get_json() or {}
check("a new trade is refused with 503 - the door is shut, nobody is in the wrong",
      res.status_code == 503 and "switched off" in body.get("message", "") and body.get("trade") is False,
      [res.status_code, body])
check("  and nothing is opened", len(sql("SELECT * FROM trades")) == before_trades)

sql("UPDATE server_settings SET value = 'not json' WHERE key = 'trade'")
check("a row that cannot be read reads OFF - a corrupt row must not open the border",
      status().get("trade") is False and post(ALICE, "/api/trade/offer",
                                              {"slot": 0, "username": "bob"}).status_code == 503)
res = post(BOSS, "/api/server/trade", {"on": True})
check("the owner switches it back on", res.status_code == 200 and (res.get_json() or {}).get("trade") is True)
check("  /api/status says so", status().get("trade") is True)
said = sql("SELECT body FROM broadcasts ORDER BY id DESC LIMIT 1")
check("  announced as open again", said and "open again" in said[0]["body"], [dict(r) for r in said])


# =============================================================================
print("\n--- G-2 an open trade may finish ---")
# =============================================================================
potion = sorted(i for i, r in gamedata.ITEMS.items()
                if r.get("type_name") == "CONSUMABLE" and "potion" in i)[0]
put_item("alice", 0, potion, 3)
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
check("a trade opened while trading is on", res.status_code == 200, res.get_json())
res = post(BOSS, "/api/server/trade", {"on": False})
check("  is counted as still open when the switch goes off",
      (res.get_json() or {}).get("open_trades") == 1, res.get_json())
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": potion, "quantity": 1}], "gold": 0})
check("  can still be changed", res.status_code == 200, res.get_json())
alice_before = holding("alice", potion)
accept(ALICE)
res = accept(BOB)
check("  and accepted and finished - the switch never leaves a trade half-done",
      res.status_code == 200 and holding("bob", potion) == 1 and holding("alice", potion) == alice_before - 1,
      [res.status_code, res.get_json()])
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
check("but the next one is refused", res.status_code == 503, res.status_code)
post(BOSS, "/api/server/trade", {"on": True})
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
check("a trade opened and the switch thrown can still be cancelled", res.status_code == 200)
post(BOSS, "/api/server/trade", {"on": False})
res = post(ALICE, "/api/trade/cancel", {})
check("  cancelled", res.status_code == 200 and not (trade_of(ALICE).get("trade") or {}).get("state") == "open",
      res.get_json())
post(BOSS, "/api/server/trade", {"on": True})
check("on again, a new trade opens", post(ALICE, "/api/trade/offer",
                                          {"slot": 0, "username": "bob"}).status_code == 200)
cancel_all(ALICE, BOB)


# =============================================================================
print("\n--- G-3 a fresh mythic or Perfect is held ---")
# =============================================================================
check("a mythic and a Perfect are the held kinds; an ordinary roll and a potion are not",
      app_module.trade_held_kind(MYTHIC) and app_module.trade_held_kind(PERFECT)
      and not app_module.trade_held_kind(PLAIN) and not app_module.trade_held_kind(potion),
      [MYTHIC, PERFECT, PLAIN])
sql("DELETE FROM carry_items WHERE user_id = ?", (uid("alice"),))
bag_with("alice", MYTHIC, "bag-mythic")
started = int(time.time())
res = post(ALICE, "/api/loot/take", {"bag_id": "bag-mythic", "position": 0})
rows = holds("alice", MYTHIC)
check("a mythic taken from a loot bag lands in the bag", res.status_code == 200 and holding("alice", MYTHIC) == 1,
      res.get_json())
check("  held from trade for TRADE_HOLD_SECONDS (48 hours)",
      len(rows) == 1 and app_module.TRADE_HOLD_SECONDS == 48 * 3600
      and started + app_module.TRADE_HOLD_SECONDS <= int(rows[0]["held_until"])
      <= int(time.time()) + app_module.TRADE_HOLD_SECONDS, [dict(r) for r in rows])

post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": MYTHIC, "quantity": 1}], "gold": 0})
message = (res.get_json() or {}).get("message", "")
check("offering it is refused, saying when it can go and why",
      res.status_code == 400 and "can be traded in 48 hours" in message and "Mythic and Perfect" in message,
      [res.status_code, message])
check("  and nothing was put on the table",
      not ((trade_of(ALICE).get("trade") or {}).get("a") or {}).get("items"), trade_of(ALICE))

bag_with("alice", PLAIN, "bag-plain")
post(ALICE, "/api/loot/take", {"bag_id": "bag-plain", "position": 0})
check("an ordinary piece from a bag is not held", len(holds("alice", PLAIN)) == 0)
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": PLAIN, "quantity": 1}], "gold": 0})
check("  and goes on the table", res.status_code == 200, res.get_json())
cancel_all(ALICE, BOB)

real_roll = gamedata.roll_quality
gamedata.roll_quality = lambda item_id: gamedata.perfect_id(item_id)
try:
    res = post(ALICE, "/api/shop/buy", {"slot": 0, "shop_id": "generalstore", "item_id": "ironsword"})
finally:
    gamedata.roll_quality = real_roll
bought = (res.get_json() or {}).get("item_id")
check("a Perfect bought from the shop is held too - cheated gold buys as well as it kills",
      res.status_code == 200 and bought == PERFECT and len(holds("alice", PERFECT)) == 1,
      [res.status_code, res.get_json()])
res = post(ALICE, "/api/shop/buy", {"slot": 0, "shop_id": "generalstore", "item_id": "ironsword"})
ordinary = (res.get_json() or {}).get("item_id", "")
check("  an ordinary purchase is not",
      res.status_code != 200 or gamedata.is_perfect(ordinary) or len(holds("alice", ordinary)) == 0,
      [res.status_code, ordinary])


# =============================================================================
print("\n--- G-4 the hold is the account's ---")
# =============================================================================
sql("DELETE FROM carry_items WHERE user_id = ? AND item_id = ?", (uid("alice"), MYTHIC))
put_item("alice", 5, MYTHIC, 1, slot=1)
sql("UPDATE sessions SET playing_slot = 1 WHERE user_id = ?", (uid("alice"),))
post(ALICE, "/api/trade/offer", {"slot": 1, "username": "bob"})
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": MYTHIC, "quantity": 1}], "gold": 0})
check("moved to another character, it is still held", res.status_code == 400, res.get_json())
cancel_all(ALICE, BOB)

sql("INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, 0, ?, 1)",
    (uid("alice"), MYTHIC))
post(ALICE, "/api/trade/offer", {"slot": 1, "username": "bob"})
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": MYTHIC, "quantity": 1}], "gold": 0})
check("a second copy of the same id, older, may go - one of two is free",
      res.status_code == 200, res.get_json())
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": MYTHIC, "quantity": 2}], "gold": 0})
check("  but not both", res.status_code == 400, res.get_json())
cancel_all(ALICE, BOB)
sql("DELETE FROM bank_items WHERE user_id = ?", (uid("alice"),))
sql("UPDATE sessions SET playing_slot = 0 WHERE user_id = ?", (uid("alice"),))
sql("DELETE FROM carry_items WHERE user_id = ? AND item_id = ?", (uid("alice"), MYTHIC))


# =============================================================================
print("\n--- G-5 a hold that arrives after the offer ---")
# =============================================================================
second = gamedata.roll_quality(gamedata.mythic_pool("warrior")[0])
while second == MYTHIC:
    second = gamedata.roll_quality(gamedata.mythic_pool("warrior")[0])
put_item("alice", 7, second, 1)
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": second, "quantity": 1}], "gold": 0})
check("a mythic with no hold goes on the table", res.status_code == 200, res.get_json())
sql("INSERT INTO trade_holds (user_id, item_id, created_at, held_until) VALUES (?, ?, ?, ?)",
    (uid("alice"), second, int(time.time()), int(time.time()) + 3600))
accept(ALICE)
res = accept(BOB)
message = (res.get_json() or {}).get("message", "")
check("a hold placed since refuses the trade at execution, in the same words",
      res.status_code == 400 and "can be traded in 1 hour" in message, [res.status_code, message])
state = trade_of(ALICE).get("trade") or {}
check("  nothing moved, and nobody is left agreed",
      holding("alice", second) == 1 and holding("bob", second) == 0
      and not (state.get("a") or {}).get("confirmed") and not (state.get("b") or {}).get("confirmed"),
      state)
cancel_all(ALICE, BOB)


# =============================================================================
print("\n--- G-6 a finished hold ---")
# =============================================================================
sql("UPDATE trade_holds SET held_until = ? WHERE user_id = ?", (int(time.time()) - 1, uid("alice")))
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": second, "quantity": 1}], "gold": 0})
check("once the hold has run out, the piece may be traded", res.status_code == 200, res.get_json())
cancel_all(ALICE, BOB)
sql("INSERT INTO trade_holds (user_id, item_id, created_at, held_until) VALUES (?, 'x', 0, 1)", (uid("bob"),))
expired_alice = len([r for r in holds("alice") if int(r["held_until"]) <= int(time.time())])
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
app_module.place_trade_hold(db, uid("alice"), MYTHIC)
db.commit()
db.close()
check("placing a hold clears the account's own finished ones",
      expired_alice > 0 and all(int(r["held_until"]) > int(time.time()) for r in holds("alice")),
      [dict(r) for r in holds("alice")])
check("  and leaves everybody else's rows alone", len(holds("bob")) == 1)

conn = sqlite3.connect(DB_PATH)
balanced = app_module.gold_supply(conn)["balanced"]
conn.close()
check("and the gold still adds up after all of it", balanced)


# =============================================================================
print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
