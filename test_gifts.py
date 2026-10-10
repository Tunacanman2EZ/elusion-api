"""
The gifts ledger: what the owner has given away. Run: python3 test_gifts.py

The owner, 10 Oct, after eleven clicks of Give item put 274,750,000 gold in the
bag of the first other player to try the game: "do not roll back but make a
ledger for anthing i give to players so its accounted for if i ever ask how
much did i inflate my server". THE GIFTS LEDGER in app.py.

  GL-1  OWNER ONLY. GET /api/staff/gifts is the same bare 404 to a player, a
        mod and a dev, and 401 with no login.
  GL-2  EVERY GIFT IS A ROW, in the gift's own transaction, with what it was
        worth: a give to a player, a grant to the owner himself, the owner's
        own gold to his purse or his bank, and gold taken back (negative).
        Coins and piles at what using them pays, lusions at what they cash
        into, anything else at the catalogue's price and what the shop pays.
        The give and the grant answer with the worth too.
  GL-3  A GIFT THAT DID NOT HAPPEN IS NO ROW: a full bag, gold below zero, a
        grant by somebody who is not the owner.
  GL-4  THE REPORT: the totals, to players and to yourself, by player, by item,
        the newest first, one player's alone, and the economy beside it.
  GL-5  WHAT A GIFT WAS WORTH IS KEPT: a price changed later moves nothing.
  GL-6  THE PAST IS READ BACK FROM THE LOGS, ONCE: every give and grant in
        staff_actions and the owner's gold in gold_ledger, marked "log";
        a second run reads nothing twice.
  GL-7  MONEY IS STILL THE LEDGER'S: the gold supply is untouched by the gifts
        ledger, and a given pile still mints its gold through gold_delta when
        it is used - the cash route and the ledger ask one function.
  GL-8  giftwatch.py ON THE SERVER: the same answer from the database, read
        only, and a plain sentence when the table is not there yet.
"""
import importlib.util, json, os, sqlite3, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_gifts_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "boss"
os.environ.pop("ELUSION_GAMEDATA", None)
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
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
        print("  ok   %s" % label)
    else:
        failed += 1
        failures.append(label)
        print("  FAIL %s   %s" % (label, detail))


def section(t):
    print("\n%s\n%s" % (t, "-" * len(t)))


PW = "hunter2hunter2"
_ip = [30]


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
    conn.row_factory = sqlite3.Row
    try:
        out = conn.execute(sql, args).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def one(sql, args=()):
    rows = raw(sql, args)
    return rows[0][0] if rows else None


def uid(name):
    return one("SELECT id FROM users WHERE username = ?", (name,))


def make(token, slot, class_id):
    r = client.put("/api/save", headers=bearer(token),
                   json={"slot": slot, "class_id": class_id, "name": class_id})
    assert r.status_code == 200, r.get_json()


def supply():
    """The gold in every purse and bank, after making sure the gold ledger
    still agrees with it."""
    conn = sqlite3.connect(DB_PATH)
    try:
        books = app_module.gold_supply(conn)
        assert books["balanced"], books
        return books["held"]
    finally:
        conn.close()


def give(token, username, item_id, quantity):
    return client.post("/api/staff/grant", headers=bearer(token),
                       json={"username": username, "item_id": item_id, "quantity": quantity})


def gifts():
    return raw("SELECT * FROM owner_gifts ORDER BY id")


def report(token, **args):
    query = "&".join("%s=%s" % (k, v) for k, v in args.items())
    return client.get("/api/staff/gifts" + ("?" + query if query else ""), headers=bearer(token))


owner_t = register("boss")
make(owner_t, 0, "warrior")
student_t = register("allmind")
make(student_t, 2, "tank")
other_t = register("friend")
make(other_t, 0, "mage")
mod_t = register("modder")
raw("UPDATE users SET role = 'mod' WHERE username = 'modder'")
dev_t = register("devver")
raw("UPDATE users SET role = 'dev' WHERE username = 'devver'")
OWNER, STUDENT, FRIEND = uid("boss"), uid("allmind"), uid("friend")
PILE = gamedata.item_row("goldpile")["value"]
SWORD = gamedata.item_row("ironsword")["value"]
SWORD_SELLS = gamedata.shop_sell_price(app_module.GIFT_SHOP_ID, "ironsword")
COIN = gamedata.item_row("goldcoin")["value"]

# The live server ran the backfill at import on an empty database: nothing to
# read, and the marker is down.
check("a database with no gifts in its logs starts the ledger empty, and says when it started",
      not gifts() and one("SELECT value FROM server_settings WHERE key = ?",
                          (app_module.OWNER_GIFTS_SINCE_KEY,)) is not None)

# =========================================================================
section("GL-1 OWNER ONLY")
# =========================================================================
r_player, r_mod, r_dev = report(student_t), report(mod_t), report(dev_t)
check("a player, a mod and a dev all get the same bare 404",
      r_player.status_code == r_mod.status_code == r_dev.status_code == 404
      and r_player.get_json() == r_mod.get_json() == r_dev.get_json(),
      [r_player.status_code, r_mod.status_code, r_dev.status_code])
check("  and no login is a 401", client.get("/api/staff/gifts").status_code == 401)
check("the owner gets the report", report(owner_t).status_code == 200)

# =========================================================================
section("GL-2 EVERY GIFT IS A ROW, WITH WHAT IT WAS WORTH")
# =========================================================================
before_supply = supply()
r = give(owner_t, "allmind", "goldpile", 999)
rows = gifts()
check("a give of 999 Piles of Gold to a player is one row: gold, at what using them pays",
      r.status_code == 200 and len(rows) == 1 and rows[0]["kind"] == "gold"
      and rows[0]["gold"] == 999 * PILE and rows[0]["quantity"] == 999
      and rows[0]["user_id"] == STUDENT and rows[0]["username"] == "allmind"
      and rows[0]["actor_id"] == OWNER and rows[0]["source"] == "give" and rows[0]["slot"] == 2,
      dict(rows[0]) if rows else r.get_json())
check("  and the answer says what it was worth - 24,975,000 gold, before anybody asks",
      (r.get_json() or {}).get("worth", {}).get("gold") == 999 * PILE, r.get_json())
r = give(owner_t, "allmind", "ironsword", 1)
sword = gifts()[-1]
check("an item is kept at the catalogue's price, and what the shop would pay for it",
      sword["kind"] == "item" and sword["value"] == SWORD and sword["sells_for"] == SWORD_SELLS
      and sword["gold"] == 0 and r.get_json().get("worth", {}).get("value") == SWORD, dict(sword))
give(owner_t, "friend", "lusions", 50)
lus = gifts()[-1]
check("a pile of lusions is kept as the lusions it cashes into",
      lus["kind"] == "lusions" and lus["lusions"] == 50 and lus["gold"] == 0 and lus["user_id"] == FRIEND,
      dict(lus))
r = client.post("/api/staff/grant", headers=bearer(owner_t), json={"slot": 0, "item_id": "goldcoin", "quantity": 3})
mine = gifts()[-1]
check("a grant to the owner himself is a row too, to himself",
      r.status_code == 200 and mine["user_id"] == OWNER and mine["actor_id"] == OWNER
      and mine["gold"] == 3 * COIN and mine["source"] == "grant"
      and r.get_json().get("worth", {}).get("gold") == 3 * COIN, dict(mine))
client.post("/api/staff/gold", headers=bearer(owner_t), json={"slot": 0, "amount": 500})
client.post("/api/staff/gold", headers=bearer(owner_t), json={"slot": 0, "amount": 300, "bank": True})
client.post("/api/staff/gold", headers=bearer(owner_t), json={"slot": 0, "amount": -100})
straight = gifts()[-3:]
check("the owner's own gold is a row each: his purse, his bank (no slot), and some taken back",
      [(g["kind"], g["gold"], g["slot"], g["item_id"], g["source"]) for g in straight]
      == [("gold", 500, 0, "", "staff_gold"), ("gold", 300, None, "", "staff_gold"),
          ("gold", -100, 0, "", "staff_gold")], [dict(g) for g in straight])

# =========================================================================
section("GL-3 A GIFT THAT DID NOT HAPPEN IS NO ROW")
# =========================================================================
counted = len(gifts())
# The friend's bag, filled for a moment and put back as it was: the pile of
# lusions in it is cashed in GL-7.
kept_bag = [tuple(r) for r in raw("SELECT user_id, slot, position, item_id, quantity FROM carry_items"
                                  " WHERE user_id = ?", (FRIEND,))]
for position in range(app_module.CARRY_CAPACITY):
    raw("INSERT OR REPLACE INTO carry_items (user_id, slot, position, item_id, quantity)"
        " VALUES (?, 0, ?, 'ironhelm', 1)", (FRIEND, position))
r = give(owner_t, "friend", "goldpile", 5)
check("a full bag is a 409 and no row", r.status_code == 409 and len(gifts()) == counted, r.status_code)
raw("DELETE FROM carry_items WHERE user_id = ?", (FRIEND,))
for row in kept_bag:
    raw("INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)", row)
r = client.post("/api/staff/gold", headers=bearer(owner_t), json={"slot": 0, "amount": -10 ** 8})
check("gold below zero is refused and no row", r.status_code == 400 and len(gifts()) == counted, r.status_code)
r = client.post("/api/staff/grant", headers=bearer(dev_t), json={"username": "friend", "item_id": "goldpile", "quantity": 1})
check("a grant by a dev is a 404 and no row", r.status_code == 404 and len(gifts()) == counted, r.status_code)

# =========================================================================
section("GL-4 THE REPORT")
# =========================================================================
r = report(owner_t)
body = r.get_json()
total_gold = 999 * PILE + 3 * COIN + 500 + 300 - 100
check("the totals: every gift, the gold, the lusions, and the items at the shop's prices and its buying price",
      body["totals"]["gifts"] == 7 and body["totals"]["gold"] == total_gold and body["totals"]["lusions"] == 50
      and body["totals"]["item_value"] == SWORD and body["totals"]["item_sells_for"] == SWORD_SELLS,
      body["totals"])
check("  to players and to yourself, apart, adding up to the whole",
      body["to_players"]["gold"] == 999 * PILE and body["to_players"]["gifts"] == 3
      and body["to_yourself"]["gold"] == 3 * COIN + 700 and body["to_yourself"]["gifts"] == 4
      and body["to_players"]["gold"] + body["to_yourself"]["gold"] == body["totals"]["gold"],
      [body["to_players"], body["to_yourself"]])
check("  by player, the most gold first",
      [p["username"] for p in body["by_player"]] == ["allmind", "boss", "friend"]
      and body["by_player"][0]["gold"] == 999 * PILE and body["by_player"][2]["lusions"] == 50,
      body["by_player"])
check("  by item, the biggest first", body["by_item"][0]["item_id"] == "goldpile"
      and body["by_item"][0]["quantity"] == 999, body["by_item"][:2])
check("  the newest gifts first", body["recent"][0]["gold"] == -100 and body["recent"][-1]["item_id"] == "goldpile"
      and len(body["recent"]) == 7, [g["item_id"] for g in body["recent"]])
gold_now = supply()
check("  and the economy beside it: the gold in purses and banks now, the gold ever made, the share",
      body["economy"]["gold_now"] == gold_now
      and body["share_of_gold_now"] == round(total_gold / gold_now, 4)
      and body["economy"]["gold_ever_made"] >= 0, [body["economy"], body["share_of_gold_now"], gold_now])
r = report(owner_t, username="allmind", recent=1)
alone = r.get_json()
check("one player's alone: what they were given, nobody else's",
      r.status_code == 200 and alone["totals"]["gifts"] == 2 and alone["totals"]["gold"] == 999 * PILE
      and alone["username"] == "allmind" and len(alone["recent"]) == 1, alone["totals"])
check("  an account that does not exist is a 404", report(owner_t, username="nobodyhere").status_code == 404)

# =========================================================================
section("GL-5 WHAT A GIFT WAS WORTH IS KEPT")
# =========================================================================
item = gamedata.item_row("goldpile")
original = gamedata.ITEMS["goldpile"]["value"]
gamedata.ITEMS["goldpile"]["value"] = 1
try:
    later = report(owner_t).get_json()
finally:
    gamedata.ITEMS["goldpile"]["value"] = original
check("a pile's price changed tomorrow does not rewrite what was given today",
      later["totals"]["gold"] == total_gold, later["totals"]["gold"])

# =========================================================================
section("GL-6 THE PAST IS READ BACK FROM THE LOGS, ONCE")
# =========================================================================
kept = [dict(g) for g in gifts()]
raw("DELETE FROM owner_gifts")
raw("DELETE FROM server_settings WHERE key = ?", (app_module.OWNER_GIFTS_SINCE_KEY,))
app_module._backfill_owner_gifts()
back = gifts()
from_log = [(g["user_id"], g["kind"], g["item_id"], g["quantity"], g["gold"], g["slot"], g["source"]) for g in back]
check("every give and grant in the staff log comes back, worth what it was, marked from the log",
      (STUDENT, "gold", "goldpile", 999, 999 * PILE, 2, "log") in from_log
      and (STUDENT, "item", "ironsword", 1, 0, 2, "log") in from_log
      and (FRIEND, "lusions", "lusions", 50, 0, 0, "log") in from_log
      and (OWNER, "gold", "goldcoin", 3, 3 * COIN, 0, "log") in from_log, from_log)
check("  and the owner's own gold from gold_ledger, purse and bank, given and taken",
      (OWNER, "gold", "", 500, 500, 0, "log") in from_log and (OWNER, "gold", "", 300, 300, None, "log") in from_log
      and (OWNER, "gold", "", -100, -100, 0, "log") in from_log, from_log)
check("  the same totals as when they were written as they happened",
      sum(g["gold"] for g in back) == sum(g["gold"] for g in kept) and len(back) == len(kept)
      and {g["at"] for g in back} <= {g["at"] for g in kept} | {g["at"] + 1 for g in kept} | {g["at"] - 1 for g in kept},
      [len(back), len(kept)])
app_module._backfill_owner_gifts()
check("a second run reads nothing twice", len(gifts()) == len(back), len(gifts()))
raw("INSERT INTO staff_actions (actor_id, actor_name, action, target_id, target_name, detail, created_at)"
    " VALUES (?, 'boss', 'give', ?, 'friend', 'something that is not a gift line', 5)", (OWNER, FRIEND))
raw("DELETE FROM owner_gifts")
raw("DELETE FROM server_settings WHERE key = ?", (app_module.OWNER_GIFTS_SINCE_KEY,))
app_module._backfill_owner_gifts()
check("  a log line it cannot read is passed over, not guessed at", len(gifts()) == len(back), len(gifts()))
since = int(one("SELECT value FROM server_settings WHERE key = ?", (app_module.OWNER_GIFTS_SINCE_KEY,)))
check("  and the ledger says it counts from the first gift the logs remember",
      since == min(g["at"] for g in back) and report(owner_t).get_json()["ledger_since"] == since, since)

raw("UPDATE owner_gifts SET at = at - 86400 WHERE source = 'log' AND item_id = ''")
newest = [g["at"] for g in report(owner_t, recent=100).get_json()["recent"]]
check("  the newest come first by WHEN, not by the order the past was read back in",
      newest == sorted(newest, reverse=True), newest)

# =========================================================================
section("GL-7 MONEY IS STILL THE LEDGER'S")
# =========================================================================
check("the gold supply holds: the gifts ledger is a record, not money", supply() == gold_now)
pile_cell = one("SELECT position FROM carry_items WHERE user_id = ? AND slot = 2 AND item_id = 'goldpile'", (STUDENT,))
purse_before = one("SELECT gold FROM saves WHERE user_id = ? AND slot = 2", (STUDENT,))
r = client.post("/api/character/inventory/cash", headers=bearer(student_t),
                json={"slot": 2, "position": pile_cell, "item_id": "goldpile"})
check("a given pile, used, pays into the purse through gold_delta - exactly what the ledger said it was worth",
      r.status_code == 200 and one("SELECT gold FROM saves WHERE user_id = ? AND slot = 2", (STUDENT,))
      == purse_before + 999 * PILE
      and one("SELECT delta FROM gold_ledger WHERE user_id = ? AND reason = 'pile' ORDER BY id DESC LIMIT 1",
              (STUDENT,)) == 999 * PILE, r.get_json())
check("  and the gold supply follows it", supply() == gold_now + 999 * PILE)
lus_cell = one("SELECT position FROM carry_items WHERE user_id = ? AND slot = 0 AND item_id = 'lusions'", (FRIEND,))
lus_before = one("SELECT lusions FROM accounts WHERE user_id = ?", (FRIEND,)) or 0
r = client.post("/api/character/inventory/cash", headers=bearer(other_t),
                json={"slot": 0, "position": lus_cell, "item_id": "lusions"})
check("a given pile of lusions cashes into lusions, as before",
      r.status_code == 200 and one("SELECT lusions FROM accounts WHERE user_id = ?", (FRIEND,)) == lus_before + 50,
      r.get_json())
sword_cell = one("SELECT position FROM carry_items WHERE user_id = ? AND slot = 2 AND item_id = 'ironsword'", (STUDENT,))
r = client.post("/api/character/inventory/cash", headers=bearer(student_t),
                json={"slot": 2, "position": sword_cell, "item_id": "ironsword"})
check("  and a sword is still not money", r.status_code == 409, r.status_code)
check("the cash route and the gifts ledger ask one function what money is worth",
      app_module.money_item_worth("goldpile") == ("gold", PILE) and app_module.money_item_worth("lusions") == ("lusions", 1)
      and app_module.money_item_worth("ironsword") == (None, 0)
      and "money_item_worth(item_id)" in open(os.path.join(HERE, "app.py")).read().split("def cash_carried_pile")[1][:4000])

# =========================================================================
section("GL-8 giftwatch.py ON THE SERVER")
# =========================================================================
out = subprocess.run([sys.executable, os.path.join(HERE, "giftwatch.py"), "--db", DB_PATH],
                     capture_output=True, text=True, timeout=60)
text = out.stdout
check("it answers the owner's question in words, with the same numbers",
      out.returncode == 0 and "{:,}".format(total_gold) in text and "allmind" in text
      and "{:,}".format(999 * PILE) in text and "to players" in text and "to themselves" in text,
      text[:600] + out.stderr[:300])
check("  and sets it beside the gold in the game now",
      "{:,}".format(supply()) in text, text[:800])
out = subprocess.run([sys.executable, os.path.join(HERE, "giftwatch.py"), "--db", DB_PATH, "--player", "friend"],
                     capture_output=True, text=True, timeout=60)
check("  one player's alone with --player", out.returncode == 0 and "friend" in out.stdout
      and "{:,}".format(999 * PILE) not in out.stdout and "50 lusions" in out.stdout, out.stdout[:500])
source = open(os.path.join(HERE, "giftwatch.py")).read()
check("  read only: opened mode=ro, so it cannot write", "mode=ro" in source and "INSERT" not in source
      and "UPDATE" not in source and "DELETE" not in source)
bare = os.path.join(tempfile.gettempdir(), "elusion_gifts_bare.db")
if os.path.exists(bare):
    os.remove(bare)
sqlite3.connect(bare).execute("CREATE TABLE users (id INTEGER)").connection.close()
out = subprocess.run([sys.executable, os.path.join(HERE, "giftwatch.py"), "--db", bare],
                     capture_output=True, text=True, timeout=60)
check("a database from before the ledger gets a plain sentence, not a traceback",
      out.returncode != 0 and "Traceback" not in out.stderr and "start" in (out.stdout + out.stderr).lower(),
      out.stdout + out.stderr)

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
for f in failures:
    print("  - " + f)
sys.exit(1 if failed else 0)
