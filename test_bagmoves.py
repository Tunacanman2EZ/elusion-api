"""
The backpack and the bank are server-owned. Run: python3 test_bagmoves.py

WHAT CHANGED. The game used to save a bag by sending all thirty cells, and the
server trimmed the array to what it had granted (E-1). A gain was caught. The
arrangement, and every loss, were the client's word. Now each thing a player
does to a grid is one request the server carries out on its own copy:

    POST /api/character/inventory/move      a drag: move, merge or swap
    POST /api/character/inventory/discard   the bin
    POST /api/character/inventory/cash      using a pile of coins or lusions
    POST /api/bank/move, /api/bank/discard  the same two, in the bank

and PUT /api/character/inventory and PUT /api/account/bank ignore a player's
array (test_security.py E-1, test_api.py THE HOTBAR IS CARRIED).

WHAT THIS GUARDS. That each route does the one thing the game draws, on the
server's cells, and nothing when the game's picture is out of date - the 409
that hands the grid back is how a drag made just after a trade landed cannot
move or destroy the wrong thing. Most checks are about what must NOT happen.

Runs against a throwaway database in the temp folder, never elusion.db.
"""
import gc, importlib.util, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Temp directory, not beside this file - see the note in test_healing.py.
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_bagmoves_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "bagboss"
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


def account(username):
    client.post("/api/auth/register", json={"username": username, "password": "password123"})
    token = client.post("/api/auth/login", json={"username": username, "password": "password123"}
                        ).get_json()["token"]
    headers = {"Authorization": "Bearer " + token}
    client.put("/api/save", headers=headers,
               json={"slot": 0, "class_id": "warrior", "name": username.capitalize()})
    return headers


def raw():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def uid(username):
    conn = raw()
    try:
        return int(conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0])
    finally:
        conn.close()


def seed_carry(username, cells, slot=0):
    """carry_items exactly as given, {position: (item_id, quantity)}, as if the
    server had put them there. Everything else in the slot is cleared."""
    conn = raw()
    user = uid(username)
    conn.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (user, slot))
    conn.executemany("INSERT INTO carry_items (user_id, slot, position, item_id, quantity)"
                     " VALUES (?, ?, ?, ?, ?)",
                     [(user, slot, pos, item, qty) for pos, (item, qty) in cells.items()])
    conn.commit()
    conn.close()


def seed_bank(username, cells):
    conn = raw()
    user = uid(username)
    conn.execute("DELETE FROM bank_items WHERE user_id = ?", (user,))
    conn.executemany("INSERT INTO bank_items (user_id, position, item_id, quantity)"
                     " VALUES (?, ?, ?, ?)",
                     [(user, pos, item, qty) for pos, (item, qty) in cells.items()])
    conn.commit()
    conn.close()


def carried(headers):
    return client.get("/api/character?slot=0", headers=headers).get_json()["inventory"]


def banked(headers):
    return client.get("/api/account", headers=headers).get_json()["bank_inventory"]


def cell(cells, position):
    c = cells[position]
    return (c["item_id"], int(c["quantity"])) if c else None


def move(headers, source, target, item_id, slot=0):
    return client.post("/api/character/inventory/move", headers=headers, json={
        "slot": slot, "from": source, "to": target, "item_id": item_id})


def discard(headers, position, item_id, slot=0):
    return client.post("/api/character/inventory/discard", headers=headers, json={
        "slot": slot, "position": position, "item_id": item_id})


def cash(headers, position, item_id, slot=0):
    return client.post("/api/character/inventory/cash", headers=headers, json={
        "slot": slot, "position": position, "item_id": item_id})


BAG = app_module.INVENTORY_CAPACITY
CARRY = app_module.CARRY_CAPACITY
BANK = app_module.BANK_CAPACITY

# A stackable item with room to merge, and two that do not stack.
POTION = "smallhealthpotion"
POTION_MAX = app_module._stack_limit(POTION)
SWORD = "ironsword"
EMBER = "embersword"
check("the fixtures stack the way this suite assumes",
      POTION_MAX > 5 and app_module._stack_limit(SWORD) == 1
      and app_module._stack_limit(EMBER) == 1, (POTION_MAX,))

ana = account("ana")
ben = account("ben")
boss = account("bagboss")


# =============================================================================
section("MOVE - THE DRAG THE GAME DRAWS, DONE ON THE SERVER'S CELLS")
# =============================================================================

seed_carry("ana", {0: (POTION, 5), 1: (SWORD, 1)})
r = move(ana, 0, 7, POTION)
inv = (r.get_json() or {}).get("inventory") or []
check("onto an empty cell it moves", r.status_code == 200 and cell(inv, 7) == (POTION, 5)
      and inv[0] is None, (r.status_code, r.get_json()))
check("the answer is the bag as stored", inv == carried(ana))

r = move(ana, 7, BAG + 2, POTION)
check("a key is a cell like any other - onto key 3",
      cell(r.get_json()["inventory"], BAG + 2) == (POTION, 5))
move(ana, BAG + 2, 7, POTION)

r = move(ana, 1, 7, SWORD)
inv = r.get_json()["inventory"]
check("onto a different item it swaps", cell(inv, 7) == (SWORD, 1) and cell(inv, 1) == (POTION, 5),
      inv[:8])

# MERGE, with the stack limit honoured on the server's own copy of it.
seed_carry("ana", {0: (POTION, 5), 3: (POTION, POTION_MAX - 2)})
inv = move(ana, 0, 3, POTION).get_json()["inventory"]
check("onto the same stackable item it merges up to the stack limit",
      cell(inv, 3) == (POTION, POTION_MAX), inv[:4])
check("  and what did not fit stays where it was", cell(inv, 0) == (POTION, 3), inv[:4])
check("  and not one potion was made or lost",
      sum(c["quantity"] for c in inv if c and c["item_id"] == POTION) == 5 + POTION_MAX - 2)

inv = move(ana, 0, 3, POTION).get_json()["inventory"]
check("onto a full stack nothing moves", cell(inv, 3) == (POTION, POTION_MAX)
      and cell(inv, 0) == (POTION, 3), inv[:4])

seed_carry("ana", {0: (POTION, 2), 4: (POTION, 2)})
inv = move(ana, 0, 4, POTION).get_json()["inventory"]
check("a merge that fits empties the cell it came from",
      cell(inv, 4) == (POTION, 4) and inv[0] is None, inv[:5])

seed_carry("ana", {0: (SWORD, 1), 1: (SWORD, 1)})
inv = move(ana, 0, 1, SWORD).get_json()["inventory"]
check("two of an item that does not stack swap rather than merge into a cell of two",
      cell(inv, 0) == (SWORD, 1) and cell(inv, 1) == (SWORD, 1), inv[:2])

inv = move(ana, 0, 0, SWORD).get_json()["inventory"]
check("onto itself is a no-op, answered", cell(inv, 0) == (SWORD, 1))


# =============================================================================
section("MOVE - AN OUT-OF-DATE PICTURE MOVES NOTHING")
# =============================================================================
# The game says what it saw in `from`. A trade or a loot take may have changed
# the cell since; acting on it anyway would move the wrong thing.

seed_carry("ana", {0: (SWORD, 1), 1: (POTION, 5)})
before = carried(ana)
r = move(ana, 0, 5, POTION)
body = r.get_json() or {}
check("a move naming the wrong item is a 409", r.status_code == 409, (r.status_code, body))
check("  that carries the bag the server holds, for the game to adopt",
      body.get("resync", {}).get("inventory") == before
      and body.get("resync", {}).get("reason") == "stale_save", body.get("resync"))
check("  and nothing moved", carried(ana) == before)

r = move(ana, 9, 5, SWORD)
check("a move from an empty cell is the same 409", r.status_code == 409 and carried(ana) == before,
      r.status_code)

for label, body in [
        ("from missing", {"slot": 0, "to": 1, "item_id": SWORD}),
        ("to missing", {"slot": 0, "from": 0, "item_id": SWORD}),
        ("from -1", {"slot": 0, "from": -1, "to": 1, "item_id": SWORD}),
        ("to past the last key", {"slot": 0, "from": 0, "to": CARRY, "item_id": SWORD}),
        ("from a word", {"slot": 0, "from": "first", "to": 1, "item_id": SWORD}),
        ("from true", {"slot": 0, "from": True, "to": 1, "item_id": SWORD}),
        ("no item_id", {"slot": 0, "from": 0, "to": 1}),
        ("an item_id of 65 characters", {"slot": 0, "from": 0, "to": 1, "item_id": "x" * 65}),
        ("no slot", {"from": 0, "to": 1, "item_id": SWORD})]:
    r = client.post("/api/character/inventory/move", headers=ana, json=body)
    check("%s is a 400" % label, r.status_code == 400, r.status_code)
check("  and none of them moved anything", carried(ana) == before)

check("a slot with no character is a 404", move(ana, 0, 1, SWORD, slot=3).status_code == 404)
check("no token is a 401", client.post("/api/character/inventory/move", json={
    "slot": 0, "from": 0, "to": 1, "item_id": SWORD}).status_code == 401)

# THE CALLER'S OWN BAG, BY CONSTRUCTION. There is no user id to tamper with.
seed_carry("ben", {0: (EMBER, 1)})
r = move(ana, 0, 6, EMBER)
check("ana cannot move what is in ben's cell 0 - slot 0 means HER slot 0",
      r.status_code == 409 and cell(carried(ben), 0) == (EMBER, 1), r.status_code)


# =============================================================================
section("DISCARD - THE BIN")
# =============================================================================

seed_carry("ana", {0: (SWORD, 1), 2: (POTION, 5), BAG: (POTION, 3)})
r = discard(ana, 2, POTION)
body = r.get_json() or {}
inv = body.get("inventory") or []
check("the bin destroys the whole stack in that cell", r.status_code == 200 and inv[2] is None,
      (r.status_code, body))
check("  and says what it destroyed", body.get("discarded") == {"item_id": POTION, "quantity": 5},
      body.get("discarded"))
check("  and only that cell - the same potions on key 1 are untouched",
      cell(inv, BAG) == (POTION, 3) and cell(inv, 0) == (SWORD, 1), inv)

r = discard(ana, BAG, POTION)
check("a key can be binned too", r.status_code == 200 and r.get_json()["inventory"][BAG] is None)

before = carried(ana)
r = discard(ana, 0, POTION)
check("a bin naming the wrong item is a 409 and destroys nothing",
      r.status_code == 409 and carried(ana) == before
      and (r.get_json() or {}).get("resync", {}).get("inventory") == before, r.status_code)
check("an empty cell is the same 409", discard(ana, 11, SWORD).status_code == 409)
check("a position past the last key is a 400", discard(ana, CARRY, SWORD).status_code == 400)


# =============================================================================
section("CASH - A PILE OF COINS BECOMES GOLD, ON THE SERVER")
# =============================================================================
# The one client path that added gold locally. The server has ignored a client's
# gold since E-8, so using a pile in the game emptied the cell (saved) and added
# gold (not saved): the pile was destroyed. Now the server does both halves.

coin = next((i for i, d in gamedata.ITEMS.items()
             if d.get("type_name") == "CURRENCY" and app_module.gold_item_value(i) > 1), None)
check("the catalogue has a gold coin worth more than 1", coin is not None)
per = app_module.gold_item_value(coin)


def purse(username):
    """The character's gold as stored - GET /api/character does not carry it."""
    conn = raw()
    try:
        return int(conn.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = 0",
                                (uid(username),)).fetchone()[0])
    finally:
        conn.close()


def ledger_sum():
    conn = raw()
    try:
        minted = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger").fetchone()[0]
        held = conn.execute("SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0]
        bank = conn.execute("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts").fetchone()[0]
        return int(minted), int(held) + int(bank)
    finally:
        conn.close()


seed_carry("ana", {4: (coin, 3), 5: (SWORD, 1)})
gold_before = purse("ana")
r = cash(ana, 4, coin)
body = r.get_json() or {}
check("cashing three coins answers 200", r.status_code == 200, (r.status_code, body))
check("  the purse rises by three coins' worth",
      purse("ana") == gold_before + 3 * per and body.get("gold") == purse("ana"),
      (gold_before, purse("ana"), per))
check("  the pile is gone from the bag", body.get("inventory", [None] * 5)[4] is None)
check("  and the answer says what it was worth",
      body.get("cashed") == {"item_id": coin, "quantity": 3, "currency": "gold", "amount": 3 * per},
      body.get("cashed"))
minted, held = ledger_sum()
check("THE LEDGER STILL BALANCES: every gold minted is gold somebody holds",
      minted == held, (minted, held))
conn = raw()
row = conn.execute("SELECT reason, delta FROM gold_ledger ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("  under its own reason", row is not None and row["reason"] == "pile"
      and int(row["delta"]) == 3 * per, dict(row) if row else None)

r = cash(ana, 5, SWORD)
check("a sword is not money: 409, and it stays", r.status_code == 409
      and cell(carried(ana), 5) == (SWORD, 1), r.status_code)
r = cash(ana, 4, coin)
check("cashing an empty cell is a 409 and pays nothing", r.status_code == 409
      and purse("ana") == gold_before + 3 * per, r.status_code)

premium = gamedata.CONSTANTS.get("lusions_item_id", app_module.LUSIONS_ITEM_ID)
seed_carry("ana", {6: (premium, 4)})
lusions_before = int(client.get("/api/account", headers=ana).get_json()["lusions"])
r = cash(ana, 6, premium)
body = r.get_json() or {}
lusion_each = max(1, int((gamedata.ITEMS.get(premium) or {}).get("value", 1) or 1))
check("a pile of lusions goes to the lusion balance",
      r.status_code == 200 and body.get("lusions") == lusions_before + 4 * lusion_each
      and body.get("cashed", {}).get("currency") == "lusions", (r.status_code, body))
conn = raw()
row = conn.execute("SELECT reason, delta FROM lusion_ledger ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("  through the lusion ledger", row is not None and row["reason"] == "pile", dict(row) if row else None)


# =============================================================================
section("CONSUME ANSWERS WITH THE BAG")
# =============================================================================
# The game used to take one off its own copy and save the whole bag. That save
# no longer writes anything, so the answer has to carry the bag to adopt.

TINY = "tinyhealthpotion"     # level 1, no skill: any new character may drink it
seed_carry("ana", {BAG + 1: (TINY, 2)})
r = client.post("/api/character/consume", headers=ana,
                json={"slot": 0, "item_id": TINY, "position": BAG + 1})
inv = (r.get_json() or {}).get("inventory")
check("a potion drunk from key 2 answers with the bag, one fewer on the key",
      r.status_code == 200 and inv is not None and cell(inv, BAG + 1) == (TINY, 1),
      (r.status_code, inv))


# =============================================================================
section("THE BANK - THE SAME TWO, ACCOUNT-WIDE")
# =============================================================================

seed_bank("ana", {0: (POTION, 5), 1: (SWORD, 1), 2: (POTION, POTION_MAX - 1)})
r = client.post("/api/bank/move", headers=ana, json={"from": 1, "to": 30, "item_id": SWORD})
cells = (r.get_json() or {}).get("bank_inventory") or []
check("a bank move answers with the account, the sword moved",
      r.status_code == 200 and cell(cells, 30) == (SWORD, 1) and cells[1] is None,
      (r.status_code, r.get_json()))
cells = client.post("/api/bank/move", headers=ana,
                    json={"from": 0, "to": 2, "item_id": POTION}).get_json()["bank_inventory"]
check("a bank merge stops at the stack limit and leaves the rest",
      cell(cells, 2) == (POTION, POTION_MAX) and cell(cells, 0) == (POTION, 4), cells[:3])

before = banked(ana)
r = client.post("/api/bank/move", headers=ana, json={"from": 0, "to": 9, "item_id": SWORD})
check("a stale bank move is a 409 with the account, and moves nothing",
      r.status_code == 409 and (r.get_json() or {}).get("account", {}).get("bank_inventory") == before
      and banked(ana) == before, r.status_code)
check("a bank cell past the last is a 400", client.post("/api/bank/move", headers=ana, json={
    "from": 0, "to": BANK, "item_id": POTION}).status_code == 400)

r = client.post("/api/bank/discard", headers=ana, json={"position": 30, "item_id": SWORD})
body = r.get_json() or {}
check("the bank's bin destroys the stack and says so",
      r.status_code == 200 and body["bank_inventory"][30] is None
      and body.get("discarded") == {"item_id": SWORD, "quantity": 1}, (r.status_code, body))
check("a stale bank bin is a 409 and destroys nothing",
      client.post("/api/bank/discard", headers=ana, json={"position": 2, "item_id": SWORD}
                  ).status_code == 409 and cell(banked(ana), 2) == (POTION, POTION_MAX))
check("ben's bank is his own", client.post("/api/bank/discard", headers=ben, json={
    "position": 2, "item_id": POTION}).status_code == 409 and cell(banked(ana), 2) == (POTION, POTION_MAX))
check("no token is a 401", client.post("/api/bank/move", json={
    "from": 0, "to": 1, "item_id": POTION}).status_code == 401)


# =============================================================================
section("THE WHOLE WRITES - A PLAYER'S IS IGNORED, STAFF'S STILL WORKS")
# =============================================================================

seed_carry("ana", {0: (SWORD, 1)})
r = client.put("/api/character/inventory", headers=ana,
               json={"slot": 0, "inventory": [None, {"item_id": SWORD, "quantity": 1}]})
check("a player's whole bag is answered 200 with `ignored` and changes nothing",
      r.status_code == 200 and r.get_json().get("ignored") == ["inventory"]
      and cell(carried(ana), 0) == (SWORD, 1), r.get_json())

seed_bank("ana", {0: (SWORD, 1)})
r = client.put("/api/account/bank", headers=ana,
               json={"bank_inventory": [None, {"item_id": SWORD, "quantity": 1}]})
check("a player's whole bank is answered 200 with `ignored` and changes nothing",
      r.status_code == 200 and r.get_json().get("ignored") == ["bank_inventory"]
      and cell(banked(ana), 0) == (SWORD, 1), r.get_json().get("ignored"))

seed_carry("bagboss", {0: (SWORD, 1)})
r = client.put("/api/character/inventory", headers=boss,
               json={"slot": 0, "inventory": [None, {"item_id": SWORD, "quantity": 1}]})
check("the owner's still writes, for tooling - staff can grant anything already",
      r.status_code == 200 and "ignored" not in r.get_json()
      and cell(carried(boss), 1) == (SWORD, 1), r.get_json())


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("  failing: %s" % ", ".join(failures))
print("=" * 60)

gc.collect()
try:
    os.unlink(DB_PATH)
except OSError as exc:
    print("  note: could not remove %s (%s)" % (DB_PATH, exc))

raise SystemExit(1 if failed else 0)
