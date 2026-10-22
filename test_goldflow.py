"""
test_goldflow.py - where gold comes from and where it goes (0.74.0).

The owner: "A daily report of where gold comes from and where it goes, by
source." GET /api/owner/economy/flow?days=N (economywatch/) reads it off the
two ledgers. This suite holds it to that:

  GF-1  the rules, pure: the window's days, when a past day is settled, every
        day present, minted and burned kept apart inside a reason, the busiest
        reason first, nothing from outside the window
  GF-2  who: the owner and a co-owner; a player gets the bare 404; days is a
        whole number from 1 to 90
  GF-3  the sums are the ledger's: per day and per reason, the window's
        totals, the net per day, gold and lusions both, and the gold in the
        world now - checked against the ledger's own SUM()s
  GF-4  a settled day is read once: a row dated in one after it was read is
        not heard until the cache is cleared, while today is always read
  GF-5  it is cheap: the query walks idx_gold_ledger_at's range (never a scan
        of the ledger), and a real purchase lands on today as shop_buy

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_goldflow.py
"""
import importlib.util
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
import testdir  # noqa: E402  (a private folder per run - testdir.py)
DB_PATH = os.path.join(testdir.PATH, "elusion_goldflow_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "flowowner"
os.environ["ELUSION_CO_OWNERS"] = "flowcoowner"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
sys.modules["elusion_app"] = app_module
spec.loader.exec_module(app_module)
client = app_module.app.test_client()
gamedata = app_module.gamedata

from economywatch import data, services  # noqa: E402

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


def flow(headers, days=None):
    res = client.get("/api/owner/economy/flow" + ("" if days is None else "?days=%s" % days), headers=headers)
    return res.status_code, res.get_json() or {}


DAY = 86400
NOW = int(time.time())
TODAY = NOW // DAY

# =============================================================================
section("GF-1  the rules, pure")
# =============================================================================
check("the window: today and the six days before it for a week; today alone for one",
      services.first_day(NOW, 7) == TODAY - 6 and services.first_day(NOW, 1) == TODAY)
midnight = TODAY * DAY
check("a past day is settled ten minutes after the midnight that ends it",
      services.last_settled_day(midnight + 599) == TODAY - 2 and services.last_settled_day(midnight + 600) == TODAY - 1)
check("days are named in UTC", services.day_name(0) == "1970-01-01" and services.day_name(20748) == "2026-10-22")
rows = [(TODAY - 2, "loot", 100, 0, 3), (TODAY - 2, "staff_gold", 50, 50, 2), (TODAY, "shop_buy", 0, 30, 1),
        (TODAY, "loot", 5, 0, 1), (TODAY - 9, "loot", 999, 0, 1), (TODAY + 1, "loot", 7, 0, 1)]
report = services.flow(rows, TODAY - 2, 3)
check("every day of the window is there, oldest first, a day with nothing at zero",
      [d["start"] for d in report["days"]] == [(TODAY - 2) * DAY, (TODAY - 1) * DAY, TODAY * DAY]
      and report["days"][1] == {"day": services.day_name(TODAY - 1), "start": (TODAY - 1) * DAY, "minted": 0,
                                "burned": 0, "net": 0, "reasons": {}}, report["days"][1])
check("  minted and burned kept apart inside a reason: a grant and a burn the same day are not nothing",
      report["days"][0]["reasons"]["staff_gold"] == {"minted": 50, "burned": 50}
      and report["days"][0]["net"] == 100)
check("  nothing from outside the window, either side", report["minted"] == 155 and report["burned"] == 80
      and report["net"] == 75, report)
check("  each reason's totals for the window, the busiest first",
      [(r["reason"], r["minted"], r["burned"], r["net"], r["rows"]) for r in report["reasons"]]
      == [("loot", 105, 0, 105, 4), ("staff_gold", 50, 50, 0, 2), ("shop_buy", 0, 30, -30, 1)], report["reasons"])
check("the last check's age, and stale past two hours and a margin",
      services.canary_answer(None, NOW)["checked_at"] is None
      and services.canary_answer({"checked_at": NOW - 7200}, NOW)["stale"] is False
      and services.canary_answer({"checked_at": NOW - 9000}, NOW)["stale"] is True
      and services.canary_answer({"checked_at": NOW - 60}, NOW)["age_seconds"] == 60)

# =============================================================================
section("GF-2  who may read it")
# =============================================================================
OWNER = register("flowowner")
CO = register("flowcoowner")
PLAYER = register("flowplayer")
assert client.put("/api/save", headers=PLAYER, json={"slot": 0, "class_id": "warrior", "name": "Flo"}).status_code == 200
check("a player gets the bare 404", flow(PLAYER)[0] == 404)
check("nobody without a login gets in", client.get("/api/owner/economy/flow").status_code == 401)
check("a named co-owner with the switch off is a player here", flow(CO)[0] == 404)
assert client.post("/api/server/coowners", headers=OWNER, json={"on": True}).status_code == 200
check("  switched on, she reads it", flow(CO)[0] == 200)
code, body = flow(OWNER)
check("the owner reads it: a week unless asked", code == 200 and body["days"] == 7 and len(body["gold"]["days"]) == 7
      and body["from"] == (TODAY - 6) * DAY, [code, body.get("days")])
check("  days 1 and 90 are the edges", flow(OWNER, 1)[1].get("days") == 1 and flow(OWNER, 90)[1].get("days") == 90)
check("  0, 91, a word or a fraction is the 400",
      all(flow(OWNER, bad)[0] == 400 for bad in (0, 91, "week", "2.5", -3))
      and flow(OWNER, 0)[1].get("message") == "days must be a whole number from 1 to 90")

# =============================================================================
section("GF-3  the sums are the ledger's")
# =============================================================================
uid = int(sql("SELECT id FROM users WHERE username = 'flowplayer'")[0]["id"])
# Some gold in a bank as well as in a purse, so "the world" is seen to be both.
sql("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (uid,))
sql("UPDATE accounts SET bank_gold = bank_gold + 250 WHERE user_id = ?", (uid,))
sql("UPDATE saves SET gold = gold + 40 WHERE user_id = ? AND slot = 0", (uid,))
PLANTED = [(-9, 777, "loot"), (-6, 120, "loot"), (-6, -40, "shop_buy"), (-6, 10, "staff_gold"), (-6, -10, "staff_gold"),
           (-3, -500, "death"), (-3, 60, "shop_sell"), (-1, -25, "kingdom_tax"), (-1, -25, "kingdom_tax"),
           (0, 300, "loot"), (0, -90, "revive")]
for offset, delta, reason in PLANTED:
    at = (TODAY + offset) * DAY + 3600
    if offset == 0:
        at = min(NOW, TODAY * DAY + 3600)
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, ?, ?, 'flow test')",
        (at, uid, delta, reason))
for offset, delta, reason in ((-2, 30, "dupe_pet"), (-2, -20, "revive"), (0, -5, "revive")):
    at = (TODAY + offset) * DAY + 60 if offset else min(NOW, TODAY * DAY + 60)
    sql("INSERT INTO lusion_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, ?, ?, 'flow test')",
        (at, uid, delta, reason))
app_module._economy_flow_cache_clear()
code, body = flow(OWNER, 7)
gold = body["gold"]


def ledger_day(table, day):
    row = sql("SELECT COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0) AS m,"
              " COALESCE(-SUM(CASE WHEN delta < 0 THEN delta END), 0) AS b FROM %s"
              " WHERE at >= ? AND at < ?" % table, (day * DAY, (day + 1) * DAY))[0]
    return int(row["m"]), int(row["b"])


per_day = [(d["minted"], d["burned"]) for d in gold["days"]]
want_days = [ledger_day("gold_ledger", TODAY - 6 + i) for i in range(7)]
check("every day's minted and burned is the ledger's own sum for that UTC day", per_day == want_days,
      [per_day, want_days])
check("  and its net is minted less burned", all(d["net"] == d["minted"] - d["burned"] for d in gold["days"]))
since = (TODAY - 6) * DAY
want_reasons = {r["reason"]: (int(r["m"]), int(r["b"]), int(r["n"])) for r in sql(
    "SELECT reason, COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0) AS m,"
    " COALESCE(-SUM(CASE WHEN delta < 0 THEN delta END), 0) AS b, COUNT(*) AS n FROM gold_ledger"
    " WHERE at >= ? GROUP BY reason", (since,))}
got_reasons = {r["reason"]: (r["minted"], r["burned"], r["rows"]) for r in gold["reasons"]}
check("each reason's window totals are the ledger's GROUP BY", got_reasons == want_reasons, [got_reasons, want_reasons])
check("  the day nine days back is outside a week, and is in a fortnight",
      "loot" in got_reasons and got_reasons["loot"][0] == 420
      and {r["reason"]: r["minted"] for r in flow(OWNER, 14)[1]["gold"]["reasons"]}["loot"] == 1197)
check("  a reason that mints and burns in one day shows both: staff_gold 10 in, 10 out",
      gold["days"][0]["reasons"].get("staff_gold") == {"minted": 10, "burned": 10})
check("the window's totals are the sums of its days",
      gold["minted"] == sum(m for m, _b in want_days) and gold["burned"] == sum(b for _m, b in want_days)
      and gold["net"] == gold["minted"] - gold["burned"])
held = sql("SELECT (SELECT COALESCE(SUM(gold), 0) FROM saves) AS c, (SELECT COALESCE(SUM(bank_gold), 0) FROM accounts) AS b")[0]
check("the gold in the world now: every purse and every bank",
      int(held["b"]) >= 250 and int(held["c"]) >= 40 and gold["in_world"] == {"carried": int(held["c"]), "banked": int(held["b"]), "total": int(held["c"]) + int(held["b"])},
      gold["in_world"])
lusions = body["lusions"]
check("lusions the same, from their own ledger, per day and reason",
      [(d["minted"], d["burned"]) for d in lusions["days"]] == [ledger_day("lusion_ledger", TODAY - 6 + i) for i in range(7)]
      and {r["reason"]: (r["minted"], r["burned"]) for r in lusions["reasons"] if r["rows"] and r["reason"] != "opening_balance"}
      == {"dupe_pet": (30, 0), "revive": (0, 25)}
      and {r["reason"]: (r["minted"], r["burned"], r["rows"]) for r in lusions["reasons"]}
      == {r["reason"]: (int(r["m"]), int(r["b"]), int(r["n"])) for r in sql(
          "SELECT reason, COALESCE(SUM(CASE WHEN delta > 0 THEN delta END), 0) AS m,"
          " COALESCE(-SUM(CASE WHEN delta < 0 THEN delta END), 0) AS b, COUNT(*) AS n FROM lusion_ledger"
          " WHERE at >= ? GROUP BY reason", (since,))}, lusions["reasons"])
check("  and the lusions in the world are every account's",
      lusions["in_world"]["total"] == int(sql("SELECT COALESCE(SUM(lusions), 0) AS n FROM accounts")[0]["n"]))

# =============================================================================
section("GF-4  a settled day is read once")
# =============================================================================
flow(OWNER, 7)
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, 1000, 'late_row', 'x')",
    ((TODAY - 4) * DAY + 100, uid))
code, cached = flow(OWNER, 7)
check("a row dated in a settled day after it was read is not heard - the day was read once",
      "late_row" not in {r["reason"] for r in cached["gold"]["reasons"]})
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (?, ?, 0, 1, 'today_row', 'x')",
    (NOW, uid))
code, fresh_today = flow(OWNER, 7)
check("  while today is read every time", "today_row" in {r["reason"] for r in fresh_today["gold"]["reasons"]})
app_module._economy_flow_cache_clear()
code, cleared = flow(OWNER, 7)
check("  and cleared, the late row is there", "late_row" in {r["reason"] for r in cleared["gold"]["reasons"]}
      and cleared["gold"]["days"][2]["minted"] == ledger_day("gold_ledger", TODAY - 4)[0])
check("a longer window agrees with a shorter one on the days they share",
      flow(OWNER, 90)[1]["gold"]["days"][-7:] == cleared["gold"]["days"])

# =============================================================================
section("GF-5  it is cheap, and it is the real ledger")
# =============================================================================
conn = sqlite3.connect(DB_PATH)
plan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + data._GOLD_FLOW, (0, 1)).fetchall())
lplan = " ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + data._LUSION_FLOW, (0, 1)).fetchall())
conn.close()
check("the gold query walks idx_gold_ledger_at's range, never the whole ledger",
      "idx_gold_ledger_at" in plan and "SCAN gold_ledger" not in plan, plan)
check("  and the lusion query its own", "idx_lusion_ledger_at" in lplan and "SCAN lusion_ledger" not in lplan, lplan)
sql("UPDATE saves SET gold = gold + 5000 WHERE user_id = ? AND slot = 0", (uid,))
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (0, ?, 0, 5000, 'test_seed', '')", (uid,))
before = {r["reason"]: r["burned"] for r in flow(OWNER, 1)[1]["gold"]["reasons"]}.get("shop_buy", 0)
price = gamedata.shop_price("generalstore", "tinyhealthpotion")
res = client.post("/api/shop/buy", headers=PLAYER, json={"slot": 0, "shop_id": "generalstore",
                                                        "item_id": "tinyhealthpotion", "quantity": 1})
after = {r["reason"]: r["burned"] for r in flow(OWNER, 1)[1]["gold"]["reasons"]}.get("shop_buy", 0)
check("a real purchase lands on today, as shop_buy burned", res.status_code == 200 and after - before == price,
      [res.status_code, before, after, price])

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("\nfailed:")
    for name in failures:
        print("  - %s" % name)
sys.exit(1 if failed else 0)
