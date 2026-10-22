"""
test_patronage.py - patronage (0.74.0): gold given to the crown for a title on
the kingdom board.

The owner's third economy idea: "things players choose to spend gold on:
cosmetics on the kingdom board". A gift is a SINK - burned through
gold_delta(), reason donation - and what it buys is a word beside a name, by
what the ACCOUNT has given in all. The questions: is the gold really gone and
recorded, can a gift be made of nothing or of more than the purse, and does
the board show the title - and the gift as gold given - at once?

  K-1  REFUSED, each burning nothing: a bad slot, an empty one, an amount
       that is not a whole positive number, one under the smallest gift, one
       past the purse; no login.
  K-2  GIVEN: the purse down, a donation row in the ledger, the account's
       lifetime total across its characters, the title it has reached, the
       next and where it starts; the top title has no next.
  K-3  THE BOARD: every row carries its title ("" for none, and for a player
       who only ever paid in lusions), the reader's own line its donated,
       title, next_title and next_at; a gift counts as gold given, shows in
       by_reason, and is on the board the moment it is made.

After every section canary.py's own invariants hold on this suite's database.

Run:  python test_patronage.py
"""
import importlib.util
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_patronage_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "patronowner"
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


def purse(username, slot=0):
    return int(sql("SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (uid(username), slot))[0]["gold"])


def give_gold(username, amount, slot=0):
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (amount, uid(username), slot))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (0, ?, ?, ?, 'test_seed', '')",
        (uid(username), slot, amount))


def gifts(username=None):
    if username is None:
        return [int(r["delta"]) for r in sql("SELECT delta FROM gold_ledger WHERE reason = 'donation' ORDER BY id")]
    return [int(r["delta"]) for r in sql("SELECT delta FROM gold_ledger WHERE reason = 'donation' AND user_id = ?"
                                         " ORDER BY id", (uid(username),))]


def invariants(label):
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        broken = [(name, detail) for name, ok, detail in canary.check_all(conn) if not ok]
    finally:
        conn.close()
    check("canary: every invariant holds after %s" % label, not broken, broken)


def donate(headers, amount, slot=0):
    body = {"slot": slot}
    if amount is not None:
        body["amount"] = amount
    return client.post("/api/kingdom/donate", headers=headers, json=body)


def board(headers):
    app_module._kingdom_cache_clear()
    return client.get("/api/economy/kingdom", headers=headers).get_json() or {}


TITLES = sorted(((int(t["at"]), t["title"]) for t in gamedata.CONSTANTS["patron_titles"]))
SMALLEST = int(gamedata.CONSTANTS["donation_minimum"])


# =============================================================================
section("SETUP")
# =============================================================================
check("the catalogue has the patron titles, rising, and a smallest gift",
      len(TITLES) >= 2 and SMALLEST >= 1 and [a for a, _t in TITLES] == sorted({a for a, _t in TITLES}), (TITLES, SMALLEST))
lord_t = register("patronlord")
make_char(lord_t)
make_char(lord_t, slot=1, class_id="mage")
miser_t = register("patronmiser")
make_char(miser_t)
invariants("setup")


# =============================================================================
section("K-1  REFUSED")
# =============================================================================
give_gold("patronlord", SMALLEST * 3)
before = (purse("patronlord"), gifts())
for label, amount, slot, code in (("a bad slot", SMALLEST, 9, 400), ("an empty slot", SMALLEST, 3, 404),
                                  ("no amount", None, 0, 400), ("a string", "lots", 0, 400),
                                  ("true", True, 0, 400), ("a fraction", SMALLEST + 0.5, 0, 400),
                                  ("nothing", 0, 0, 400), ("a debt", -SMALLEST, 0, 400),
                                  ("one under the smallest gift", SMALLEST - 1, 0, 400),
                                  ("more than the purse", SMALLEST * 3 + 1, 0, 400)):
    res = donate(lord_t, amount, slot=slot)
    check("%s: %d, nothing burned" % (label, code),
          res.status_code == code and (purse("patronlord"), gifts()) == before, (res.status_code, res.get_json()))
check("  the smallest gift is said in its words",
      (donate(lord_t, SMALLEST - 1).get_json() or {}).get("message")
      == "The smallest gift the crown takes is %d gold." % SMALLEST)
check("  and so is a purse too light",
      (donate(lord_t, SMALLEST * 3 + 1).get_json() or {}).get("message")
      == "That is %d gold and you have %d." % (SMALLEST * 3 + 1, SMALLEST * 3))
check("  no login is the 401", client.post("/api/kingdom/donate", json={"slot": 0, "amount": SMALLEST}).status_code == 401)
invariants("refusals")


# =============================================================================
section("K-2  GIVEN")
# =============================================================================
first_at, first_title = TITLES[0]
second_at, second_title = TITLES[1]
res = donate(lord_t, float(SMALLEST))
body = res.get_json() or {}
check("the smallest gift, sent as the game sends a number (%r): burned, recorded, no title yet" % float(SMALLEST),
      res.status_code == 200 and body.get("donated") == SMALLEST and body.get("gold") == SMALLEST * 2 == purse("patronlord")
      and gifts() == [-SMALLEST]
      and (body.get("title"), body.get("next_title"), body.get("next_at")) == ("", first_title, first_at), body)
give_gold("patronlord", first_at, slot=1)
res = donate(lord_t, first_at - SMALLEST, slot=1)
body = res.get_json() or {}
check("from the mage, the rest of the first title: the account's total is both characters' - %s" % first_title,
      res.status_code == 200 and body.get("donated") == first_at and body.get("title") == first_title
      and (body.get("next_title"), body.get("next_at")) == (second_title, second_at)
      and body.get("gold") == SMALLEST == purse("patronlord", slot=1), body)
top_at, top_title = TITLES[-1]
give_gold("patronlord", top_at)
res = donate(lord_t, top_at - first_at)
body = res.get_json() or {}
check("past the last title: %s, and nothing further - next_title empty, next_at 0" % top_title,
      res.status_code == 200 and body.get("donated") == top_at and body.get("title") == top_title
      and (body.get("next_title"), body.get("next_at")) == ("", 0) and isinstance(body.get("next_at"), int), body)
check("every gift is one ledger row, reason donation, the sum the lifetime total",
      -sum(gifts("patronlord")) == top_at and len(gifts("patronlord")) == 3, gifts("patronlord"))
invariants("gifts")


# =============================================================================
section("K-3  THE BOARD")
# =============================================================================
give_gold("patronmiser", 500)
sql("UPDATE saves SET gold = gold - 40 WHERE user_id = ?", (uid("patronmiser"),))
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (1, ?, 0, -40, 'shop_buy', 'a potion')",
    (uid("patronmiser"),))
shown = board(lord_t)
rows = {r["username"]: r for r in shown.get("top", [])}
check("every row on the board has its title: the patron's, and none for one who only shopped",
      rows.get("patronlord", {}).get("title") == top_title and rows.get("patronmiser", {}).get("title") == "", rows)
check("  a gift is gold given: the patron's contribution is the gifts, and by_reason has them",
      rows.get("patronlord", {}).get("contributed") == top_at and shown.get("by_reason", {}).get("donation") == top_at,
      (rows.get("patronlord"), shown.get("by_reason")))
check("  and the lifetime figure stays off the rows - the title is what is shown",
      "donated" not in rows.get("patronlord", {}), rows.get("patronlord"))
you = shown.get("you", {})
check("the reader's own line: donated, title, next_title, next_at",
      (you.get("donated"), you.get("title"), you.get("next_title"), you.get("next_at")) == (top_at, top_title, "", 0), you)
mine = board(miser_t).get("you", {})
check("  and a reader who has given nothing: 0, no title, the first one to reach for",
      (mine.get("donated"), mine.get("title"), mine.get("next_title"), mine.get("next_at")) == (0, "", first_title, first_at),
      mine)
solo_t = register("patronsolo")
make_char(solo_t)
sql("INSERT INTO accounts (user_id, lusions) VALUES (?, 0) ON CONFLICT(user_id) DO NOTHING", (uid("patronsolo"),))
sql("UPDATE accounts SET lusions = lusions + 5 WHERE user_id = ?", (uid("patronsolo"),))
sql("INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail) VALUES (0, ?, 0, 5, 'test_seed', '')",
    (uid("patronsolo"),))
sql("UPDATE accounts SET lusions = lusions - 5 WHERE user_id = ?", (uid("patronsolo"),))
sql("INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail) VALUES (1, ?, 0, -5, 'revive', '')",
    (uid("patronsolo"),))
solo = {r["username"]: r for r in board(solo_t).get("top", [])}.get("patronsolo", {})
check("  a player who only paid in lusions is on the board with no title", solo.get("title") == "" and solo.get("lusions") == 5,
      solo)

# THE MOMENT IT IS MADE: the snapshot is dropped by the gift, not by a test.
give_gold("patronmiser", first_at)
client.get("/api/economy/kingdom", headers=miser_t)
res = donate(miser_t, first_at)
seen = client.get("/api/economy/kingdom", headers=miser_t).get_json() or {}
check("a gift is on the board at once: the miser's row titled %s without waiting out the cache" % first_title,
      res.status_code == 200 and {r["username"]: r for r in seen.get("top", [])}.get("patronmiser", {}).get("title")
      == first_title and seen.get("you", {}).get("title") == first_title, seen.get("you"))
invariants("the board")


print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
