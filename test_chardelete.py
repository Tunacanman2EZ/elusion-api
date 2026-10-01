"""
Deleting a character. Run: python3 test_chardelete.py

Asked for on day 1: every account has four fixed slots, one per class, and
there was no way to start a class again. POST /api/character/delete removes one
character and nothing of the account's.

  CD-1  IT TAKES THE CHARACTER, AND ONLY THE CHARACTER. Its save, its bag, its
        skills, its loot bags and heal grants go; the other characters, the
        bank, the lusions and the account stay.
  CD-2  THE GOLD GOES THROUGH THE LEDGER. The purse lives in saves, and a bare
        delete would take it out of the supply with no burn row - the
        invariant would be off by it for ever.
  CD-3  IT ASKS. The player types the character's name, and the server checks
        it again, so a stray call cannot take a character.
  CD-4  NOT IN THE MIDDLE OF A TRADE, and not somebody else's character.
  CD-5  A COPY IS KEPT, the newest few per account, so a mistake can be put
        back by hand.
  CD-6  THE SLOT STARTS AGAIN. A character created there afterwards is level
        1 with an empty bag - and a stale save from the old one cannot bring
        its items back.
  CD-7  NO CHARACTER ROUTE ANSWERS A BAD SLOT WITH A 500. /api/character/
        respawn did: it named a constant that does not exist.
"""
import importlib.util, json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_chardelete_test.db")
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

passed = failed = 0
def check(label, ok, detail=""):
    global passed, failed
    if ok: passed += 1; print("  ok   %s" % label)
    else:  failed += 1; print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

PW = "hunter2hunter2"
_ip = [20]
def register(name):
    _ip[0] += 1
    r = client.post("/api/auth/register", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["token"]

def bearer(token):
    return {"Authorization": "Bearer " + token}

def raw(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()

def one(sql, args=()):
    return raw(sql, args)[0][0]

def uid(name):
    return one("SELECT id FROM users WHERE username = ?", (name,))

def supply():
    conn = sqlite3.connect(DB_PATH)
    try:
        return app_module.gold_supply(conn)
    finally:
        conn.close()

def delete(token, slot, confirm):
    return client.post("/api/character/delete", headers=bearer(token),
                       json={"slot": slot, "confirm": confirm})

def make(token, slot, class_id):
    r = client.put("/api/save", headers=bearer(token),
                   json={"slot": slot, "class_id": class_id, "name": class_id})
    assert r.status_code == 200, r.get_json()

def mint(user_id, slot, amount):
    """Gold the way the server mints it, so the invariant holds before the test."""
    raw("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (amount, user_id, slot))
    raw("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (1, ?, ?, ?, 'test', '')",
        (user_id, slot, amount))

ITEM = sorted(gamedata.ITEMS)[0]


# =============================================================================
section("CD-1  it takes the character, and only the character")
# =============================================================================
token = register("deleter")
me = uid("deleter")
make(token, 0, "warrior")
make(token, 1, "mage")
for slot in (0, 1):
    raw("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, 0, ?, 3)",
        (me, slot, ITEM))
    raw("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, ?, 'attack', 7, 40)",
        (me, slot))
    raw("INSERT INTO consume_grants (user_id, slot, item_id, at) VALUES (?, ?, 'x', 1)", (me, slot))
    raw("INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at) VALUES (?, ?, ?, 'slime', 1)",
        ("bag%d" % slot, me, slot))
    raw("INSERT INTO loot_bag_items (bag_id, position, item_id, quantity) VALUES (?, 0, ?, 1)",
        ("bag%d" % slot, ITEM))
client.get("/api/account", headers=bearer(token))
raw("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (me,))
raw("UPDATE accounts SET lusions = 12 WHERE user_id = ?", (me,))
raw("INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, 0, ?, 5)", (me, ITEM))
raw("UPDATE saves SET level = 12 WHERE user_id = ? AND slot = 0", (me,))

r = delete(token, 0, "warrior")
body = r.get_json() or {}
check("the warrior is deleted", r.status_code == 200 and body.get("deleted") is True, body)
check("  its save is gone", one("SELECT COUNT(*) FROM saves WHERE user_id = ? AND slot = 0", (me,)) == 0)
for table in ("carry_items", "skills", "consume_grants", "loot_bags"):
    check("  its %s are gone" % table,
          one("SELECT COUNT(*) FROM %s WHERE user_id = ? AND slot = 0" % table, (me,)) == 0)
check("  and the items in its loot bags",
      one("SELECT COUNT(*) FROM loot_bag_items WHERE bag_id = 'bag0'") == 0)
check("the answer says what went with it", body.get("items_lost") == 1 and body.get("slot") == 0, body)

check("the mage is untouched",
      one("SELECT level FROM saves WHERE user_id = ? AND slot = 1", (me,)) == 1
      and one("SELECT COUNT(*) FROM carry_items WHERE user_id = ? AND slot = 1", (me,)) == 1
      and one("SELECT COUNT(*) FROM skills WHERE user_id = ? AND slot = 1", (me,)) == 1
      and one("SELECT COUNT(*) FROM loot_bag_items WHERE bag_id = 'bag1'") == 1)
check("the bank is the account's, and stays",
      one("SELECT quantity FROM bank_items WHERE user_id = ? AND position = 0", (me,)) == 5)
check("so are the lusions", one("SELECT lusions FROM accounts WHERE user_id = ?", (me,)) == 12)
r = client.get("/api/save", headers=bearer(token))
check("the character list no longer has it",
      [s["slot"] for s in (r.get_json() or {}).get("slots", [])] == [1], r.get_json())
check("the account still works", client.get("/api/account", headers=bearer(token)).status_code == 200)


# =============================================================================
section("CD-2  the gold goes through the ledger")
# =============================================================================
make(token, 2, "tank")
mint(me, 2, 777)
check("the books balance before", supply()["balanced"], supply())
r = delete(token, 2, "tank")
check("a tank carrying 777 gold is deleted", r.status_code == 200, r.get_json())
check("  and the answer says the gold went with it", (r.get_json() or {}).get("gold_lost") == 777, r.get_json())
check("the books still balance - the gold was burned, not dropped", supply()["balanced"], supply())
rows = raw("SELECT delta, reason FROM gold_ledger WHERE user_id = ? AND slot = 2 AND reason != 'test'", (me,))
check("  by one ledger row with its own reason",
      rows == [(-777, app_module.CHARACTER_DELETE_REASON)], rows)
make(token, 3, "healer")
r = delete(token, 3, "healer")
check("a character with no gold writes no ledger row",
      r.status_code == 200
      and one("SELECT COUNT(*) FROM gold_ledger WHERE user_id = ? AND slot = 3", (me,)) == 0)


# =============================================================================
section("CD-3  it asks")
# =============================================================================
make(token, 0, "warrior")
r = client.post("/api/character/delete", headers=bearer(token), json={"slot": 0})
check("no confirmation, no delete", r.status_code == 400, r.status_code)
r = delete(token, 0, "mage")
check("the wrong name, no delete", r.status_code == 400, r.status_code)
check("  and it says what it wants", "name" in (r.get_json() or {}).get("message", ""), r.get_json())
r = delete(token, 0, "   ")
check("spaces are not a name", r.status_code == 400, r.status_code)
check("the warrior is still there after all three",
      one("SELECT COUNT(*) FROM saves WHERE user_id = ? AND slot = 0", (me,)) == 1)
r = delete(token, 0, "  WARRIOR ")
check("the name in capitals, with spaces round it, is the name", r.status_code == 200, r.get_json())
r = client.post("/api/character/delete", json={"slot": 1, "confirm": "mage"})
check("no token, no delete", r.status_code == 401, r.status_code)
r = delete(token, 0, "warrior")
check("an empty slot is a 404", r.status_code == 404, r.status_code)
for bad in ("zero", -1, 4, None, 1.5):
    r = delete(token, bad, "mage")
    check("slot %r is a 400" % (bad,), r.status_code == 400, r.status_code)
r = client.post("/api/character/delete", headers=bearer(token), data="not json",
                content_type="application/json")
check("a body that is not JSON is a 400", r.status_code == 400, r.status_code)


# =============================================================================
section("CD-4  not in a trade, and not somebody else's")
# =============================================================================
other_token = register("bystander")
them = uid("bystander")
make(other_token, 0, "warrior")
make(token, 0, "warrior")
raw("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
    " VALUES ('t1', ?, 1, ?, 0, 'open', 1, 1)", (them, me))
r = delete(token, 0, "warrior")
check("the character a trade is offered TO cannot be deleted", r.status_code == 409, r.get_json())
check("  and it says why", "trade" in (r.get_json() or {}).get("message", ""), r.get_json())
r = delete(other_token, 0, "warrior")
check("the other side's character is not the one in the trade (it offered from slot 1)",
      r.status_code == 200, r.get_json())
raw("UPDATE trades SET a_user = ?, a_slot = 0, b_user = ?, b_slot = 0 WHERE trade_id = 't1'", (me, them))
r = delete(token, 0, "warrior")
check("nor the character a trade is offered FROM", r.status_code == 409, r.status_code)
r = delete(token, 1, "mage")
check("a character the trade does not name can go", r.status_code == 200, r.get_json())
raw("UPDATE trades SET state = 'cancelled' WHERE trade_id = 't1'")
r = delete(token, 0, "warrior")
check("once the trade is cancelled, it can go too", r.status_code == 200, r.get_json())

make(token, 2, "tank")
make(other_token, 1, "mage")
r = delete(other_token, 2, "tank")
check("there is no way to name somebody else's character: their slot 2 is your empty slot",
      r.status_code == 404, r.status_code)
check("  and theirs is still there", one("SELECT COUNT(*) FROM saves WHERE user_id = ? AND slot = 2", (me,)) == 1)
r = client.post("/api/character/delete", headers=bearer(other_token),
                json={"slot": 2, "confirm": "tank", "user_id": me, "username": "deleter"})
check("  even when the body names them", r.status_code == 404
      and one("SELECT COUNT(*) FROM saves WHERE user_id = ? AND slot = 2", (me,)) == 1, r.status_code)


# =============================================================================
section("CD-5  a copy is kept")
# =============================================================================
raw("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 2, 4, ?, 9)", (me, ITEM))
raw("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 2, 'defense', 5, 11)", (me,))
raw("UPDATE saves SET level = 6 WHERE user_id = ? AND slot = 2", (me,))
before = one("SELECT COUNT(*) FROM character_deletions WHERE user_id = ?", (me,))
delete(token, 2, "tank")
row = raw("SELECT slot, class_id, level, snapshot FROM character_deletions WHERE user_id = ?"
          " ORDER BY id DESC LIMIT 1", (me,))[0]
snap = json.loads(row[3])
check("the deletion is recorded", one("SELECT COUNT(*) FROM character_deletions WHERE user_id = ?", (me,)) == before + 1)
check("  with what it was", row[:3] == (2, "tank", 6), row[:3])
check("  and a copy of its bag", snap.get("carry_items") == [{"position": 4, "item_id": ITEM, "quantity": 9}],
      snap.get("carry_items"))
check("  its skills", snap.get("skills") == [{"skill_id": "defense", "level": 5, "xp": 11}], snap.get("skills"))
check("  and its save row", (snap.get("save") or {}).get("class_id") == "tank"
      and (snap.get("save") or {}).get("level") == 6, snap.get("save"))
for _ in range(app_module.CHARACTER_DELETIONS_KEPT + 3):
    make(token, 3, "healer")
    delete(token, 3, "healer")
kept = one("SELECT COUNT(*) FROM character_deletions WHERE user_id = ?", (me,))
check("creating and deleting in a loop keeps only the newest %d" % app_module.CHARACTER_DELETIONS_KEPT,
      kept == app_module.CHARACTER_DELETIONS_KEPT, kept)
check("  and never the other account's",
      one("SELECT COUNT(*) FROM character_deletions WHERE user_id = ?", (them,)) == 1)


# =============================================================================
section("CD-6  the slot starts again")
# =============================================================================
make(token, 1, "mage")
raw("UPDATE saves SET level = 20 WHERE user_id = ? AND slot = 1", (me,))
raw("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, 1, 0, ?, 2)", (me, ITEM))
old_bag = client.get("/api/character?slot=1", headers=bearer(token)).get_json()["inventory"]
delete(token, 1, "mage")
r = client.put("/api/character/inventory", headers=bearer(token), json={"slot": 1, "items": old_bag})
check("a stale bag save for the deleted character is refused", r.status_code == 404, r.status_code)
check("  and puts nothing back", one("SELECT COUNT(*) FROM carry_items WHERE user_id = ? AND slot = 1", (me,)) == 0)
make(token, 1, "mage")
r = client.get("/api/character?slot=1", headers=bearer(token))
fresh = r.get_json() or {}
check("a mage made in the slot afterwards is level 1",
      one("SELECT level FROM saves WHERE user_id = ? AND slot = 1", (me,)) == 1, fresh.get("status"))
check("  with an empty bag", all(c is None for c in fresh.get("inventory", [0])), fresh.get("inventory"))
check("  and no skills carried over", fresh.get("skills") == {}, fresh.get("skills"))
r = client.put("/api/character/inventory", headers=bearer(token), json={"slot": 1, "items": old_bag})
check("the old bag saved over the new mage does not bring the items back",
      one("SELECT COUNT(*) FROM carry_items WHERE user_id = ? AND slot = 1", (me,)) == 0,
      (r.status_code, raw("SELECT * FROM carry_items WHERE user_id = ? AND slot = 1", (me,))))
make(token, 2, "tank")
r = delete(token, 2.0, "tank")
check("slot 2.0 - every number from Godot is a float - is slot 2", r.status_code == 200, r.get_json())


# =============================================================================
section("CD-7  no character route answers a bad slot with a 500")
# =============================================================================
ROUTES = ["/api/character/delete", "/api/character/respawn", "/api/character/revive",
          "/api/character/equip", "/api/character/unequip", "/api/character/consume"]
for path in ROUTES:
    for bad in ("zero", 9):
        r = client.post(path, headers=bearer(token), json={"slot": bad, "confirm": "x"})
        check("%s with slot %r is refused, not a crash" % (path, bad),
              400 <= r.status_code < 500, r.status_code)
for bad in ("zero", 9):
    r = client.put("/api/character/inventory", headers=bearer(token), json={"slot": bad, "items": []})
    check("PUT /api/character/inventory with slot %r is refused, not a crash" % (bad,),
          400 <= r.status_code < 500, r.status_code)
    r = client.put("/api/save", headers=bearer(token), json={"slot": bad, "class_id": "mage", "name": "mage"})
    check("PUT /api/save with slot %r is refused, not a crash" % (bad,),
          400 <= r.status_code < 500, r.status_code)


print("\n%d passed, %d failed" % (passed, failed))
sys.exit(1 if failed else 0)
