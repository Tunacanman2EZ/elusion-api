"""Trade tests: that a trade reaches the right person, means what they saw,
and leaves both of them holding what it says it gave them.

test_economy.py holds the MONEY of a trade - the tax, the burn, the supply
invariant. test_ownership.py holds that no route lets a client name a trade.
This is everything around those: who a trade is with, what "accept" agrees
to, and what each client is told afterwards.

  T-1  a trade goes to the character the other player is PLAYING - the one
       their client names on its poll, else their most recent save - never to
       whichever happens to be in slot 0
  T-2  and only to somebody who is online
  T-3  "nearby" lists the character each player is playing, not their alts
  T-4  accept names the revision it saw; an offer changed underneath the
       button refuses the accept instead of executing it
  T-5  an identical offer sent again is not a change and un-accepts nobody
  T-6  the player who accepted FIRST gets the result: the bag and purse are
       delivered by the trade poll, the broadcast poll, or the refusal of a
       stale whole-bag save - which is what used to delete what they received
  T-7  items leave the bag before the hotbar's keys
  T-8  the broadcast poll says a trade is waiting, and for whom
  T-9  history: newest first, finished trades only, only your own, bounded;
       and the last trade's outcome for a window that was watching it
  T-10 old cancelled trades are pruned; finished ones are kept
  T-11 the indexes the new reads rely on exist and are used

Runs against a THROWAWAY database in the temp folder, like every other suite.
Run: python test_trades.py
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_trades_test.db")
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"

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


def save(token, slot, class_id, name):
    return client.put("/api/save", headers=auth(token),
                      json={"slot": slot, "class_id": class_id, "name": name})


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


def give_gold(username, amount, slot=0):
    # Into the purse AND the ledger, so the supply invariant still holds.
    sql("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?",
        (amount, uid(username), slot))
    sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
        " VALUES (0, ?, ?, ?, 'test_seed', 'suite setup')", (uid(username), slot, amount))


def put_item(username, position, item_id, quantity, slot=0):
    sql("INSERT OR REPLACE INTO carry_items (user_id, slot, position, item_id, quantity)"
        " VALUES (?, ?, ?, ?, ?)", (uid(username), slot, position, item_id, quantity))


def cells(username, slot=0):
    return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in sql(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?",
        (uid(username), slot))}


def holding(username, item_id, slot=0):
    return sum(q for (i, q) in cells(username, slot).values() if i == item_id)


def purse(username, slot=0):
    return int(sql("SELECT gold FROM saves WHERE user_id = ? AND slot = ?",
                   (uid(username), slot))[0]["gold"])


def beat(token, slot=None):
    """The broadcast poll - the heartbeat - optionally saying who is played."""
    path = "/api/server/broadcasts" if slot is None else "/api/server/broadcasts?slot=%s" % slot
    return client.get(path, headers=auth(token))


def post(token, path, body):
    return client.post(path, headers=auth(token), json=body)


def trade_of(token):
    return (client.get("/api/trade", headers=auth(token)).get_json() or {})


def revision(token):
    return (trade_of(token).get("trade") or {}).get("revision", -1)


def accept(token, rev=None):
    return post(token, "/api/trade/confirm",
                {"revision": revision(token) if rev is None else rev})


def cancel_all(*tokens):
    for token in tokens:
        post(token, "/api/trade/cancel", {})


def clear_flags():
    sql("UPDATE saves SET resync_trade = NULL")


def balanced():
    conn = sqlite3.connect(DB_PATH)
    try:
        return app_module.gold_supply(conn)["balanced"]
    finally:
        conn.close()


ALICE = register("alice")
BOB = register("bob")
CAROL = register("carol")
save(ALICE, 0, "warrior", "Aldra")
save(ALICE, 1, "mage", "Aldra Alt")
save(BOB, 0, "mage", "Bram")
save(BOB, 1, "healer", "Bram Two")
save(CAROL, 0, "tank", "Cass")
for name in ("alice", "bob", "carol"):
    give_gold(name, 5000)
    give_gold(name, 5000, slot=1) if name != "carol" else None


# =============================================================================
print("\n--- T-1 a trade goes to the character being played ---")
# =============================================================================
# Bob saved slot 0 most recently, but is playing slot 1 and his client says so.
sql("UPDATE saves SET updated_at = 1000 WHERE user_id = ? AND slot = 1", (uid("bob"),))
sql("UPDATE saves SET updated_at = 2000 WHERE user_id = ? AND slot = 0", (uid("bob"),))
check("the poll accepts a slot", beat(BOB, 1).status_code == 200)
stamped = sql("SELECT playing_slot FROM sessions WHERE user_id = ?", (uid("bob"),))
check("and records which character the session is playing",
      [r["playing_slot"] for r in stamped] == [1], [dict(r) for r in stamped])

res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
body = res.get_json() or {}
check("a typed name opens a trade", res.status_code == 200, body)
check("with the character he is playing, not the one saved last",
      body.get("b", {}).get("slot") == 1, body.get("b"))
check("and the payload names that character",
      body.get("b", {}).get("name") == "Bram Two", body.get("b"))
cancel_all(ALICE)

res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob", "to_slot": 0})
check("an explicit to_slot for a character he is NOT playing is refused",
      res.status_code == 409 and "different character" in (res.get_json() or {}).get("message", ""),
      "%d %s" % (res.status_code, res.get_json()))
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob", "to_slot": 1})
check("an explicit to_slot that matches is fine", res.status_code == 200, res.get_json())
cancel_all(ALICE)
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob", "to_slot": "x"})
check("a malformed to_slot is a 400", res.status_code == 400, res.get_json())

res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "BOB"})
check("the name is matched without case, and reported as stored",
      res.status_code == 200 and (res.get_json() or {}).get("b", {}).get("username") == "bob",
      res.get_json())
cancel_all(ALICE)

# A malformed slot on the heartbeat is ignored, not refused - the poll is also
# how the client learns it is still connected.
check("a malformed slot on the poll still answers 200", beat(BOB, "banana").status_code == 200)
check("and changes nothing it was not told",
      [r["playing_slot"] for r in sql("SELECT playing_slot FROM sessions WHERE user_id = ?",
                                      (uid("bob"),))] == [1])
check("an out-of-range slot is ignored the same way", beat(BOB, 9).status_code == 200
      and [r["playing_slot"] for r in sql("SELECT playing_slot FROM sessions WHERE user_id = ?",
                                          (uid("bob"),))] == [1])

# Nothing said yet: the most recent save decides.
sql("UPDATE sessions SET playing_slot = NULL WHERE user_id = ?", (uid("bob"),))
beat(BOB)
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
check("with no slot said, the most recently saved character is used",
      res.status_code == 200 and (res.get_json() or {}).get("b", {}).get("slot") == 0,
      res.get_json())
cancel_all(ALICE)

# A slot said that has no character behind it is not believed.
sql("UPDATE sessions SET playing_slot = 3 WHERE user_id = ?", (uid("bob"),))
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
check("a said slot with no save behind it falls back to the last save",
      res.status_code == 200 and (res.get_json() or {}).get("b", {}).get("slot") == 0,
      res.get_json())
cancel_all(ALICE)

# The same rule answers inside SQL (the nearby list) and in Python.
with app_module.app.app_context():
    db = app_module.get_db()
    beat(BOB, 1)
    check("_playing_slot agrees with what was said", app_module._playing_slot(db, uid("bob")) == 1)
    check("_playing_slot is None for an account with no character",
          app_module._playing_slot(db, uid("alice") + 1000) is None)
beat(BOB, 0)


# =============================================================================
print("\n--- T-2 only to somebody who is online ---")
# =============================================================================
sql("UPDATE sessions SET last_seen_at = 1 WHERE user_id = ?", (uid("bob"),))
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "BOB"})
check("an offer to a player who is not online is refused",
      res.status_code == 400 and "not online" in (res.get_json() or {}).get("message", ""),
      "%d %s" % (res.status_code, res.get_json()))
check("and says so with his name as it is spelled, not as it was typed",
      (res.get_json() or {}).get("message", "").startswith("bob "), res.get_json())
check("and opens nothing", trade_of(ALICE).get("trade") is None)

# A stale session's said slot is not believed either: whoever that was, they
# are not at the keyboard now.
sql("UPDATE sessions SET playing_slot = 1 WHERE user_id = ?", (uid("bob"),))
with app_module.app.app_context():
    check("a said slot on a session gone quiet is ignored",
          app_module._playing_slot(app_module.get_db(), uid("bob")) == 0)
beat(BOB, 0)
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
check("once he is back, the same offer goes through", res.status_code == 200, res.get_json())
body = res.get_json() or {}
check("the payload says both sides are online",
      body.get("a", {}).get("online") is True and body.get("b", {}).get("online") is True, body)
sql("UPDATE sessions SET last_seen_at = 1 WHERE user_id = ?", (uid("bob"),))
check("and says so when he goes quiet, on the next read",
      (trade_of(ALICE).get("trade") or {}).get("b", {}).get("online") is False)
beat(BOB, 0)
cancel_all(ALICE)


# =============================================================================
print("\n--- T-3 nearby lists the character being played ---")
# =============================================================================
# Both of bob's characters stand in alice's area; he is playing slot 1.
area = sql("SELECT area FROM saves WHERE user_id = ? AND slot = 0", (uid("alice"),))[0]["area"]
sql("UPDATE saves SET area = ? WHERE user_id = ?", (area, uid("bob")))
beat(BOB, 1)
listed = (client.get("/api/players/nearby?slot=0", headers=auth(ALICE)).get_json() or {}).get("players", [])
bobs = [p for p in listed if p["username"] == "bob"]
check("bob appears once", len(bobs) == 1, listed)
check("as the character he is playing", bobs and bobs[0]["slot"] == 1 and bobs[0]["name"] == "Bram Two",
      bobs)
sql("UPDATE users SET name_hue = 200 WHERE username = 'bob'")
listed = (client.get("/api/players/nearby?slot=0", headers=auth(ALICE)).get_json() or {}).get("players", [])
bobs = [p for p in listed if p["username"] == "bob"]
check("with his rank and the colour he chose, like every other list",
      bobs and bobs[0].get("role") == "player" and bobs[0].get("name_hue") == 200, bobs)
beat(BOB, 0)
listed = (client.get("/api/players/nearby?slot=0", headers=auth(ALICE)).get_json() or {}).get("players", [])
bobs = [p for p in listed if p["username"] == "bob"]
check("and follows him when he switches", bobs and bobs[0]["slot"] == 0, bobs)
res = post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob", "to_slot": bobs[0]["slot"]})
check("a slot taken off the list is always one the offer accepts", res.status_code == 200,
      res.get_json())
cancel_all(ALICE)


# =============================================================================
print("\n--- T-4 accept names the revision it saw ---")
# =============================================================================
put_item("alice", 0, "ironsword", 1)
put_item("alice", 1, "ironhelm", 1)
beat(BOB, 0)
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
first = revision(BOB)
check("a new trade starts at revision 0", first == 0, first)

res = post(ALICE, "/api/trade/update", {"items": [{"item_id": "ironsword", "quantity": 1}]})
shown = (res.get_json() or {}).get("revision")
check("a change moves the revision", shown == first + 1, shown)
res = post(BOB, "/api/trade/update", {"gold": 100})
seen_by_bob = (res.get_json() or {}).get("revision")
check("so does a change from the other side", seen_by_bob == first + 2, seen_by_bob)

res = post(BOB, "/api/trade/confirm", {})
check("an accept that names no revision is refused",
      res.status_code == 400 and "revision" in (res.get_json() or {}).get("message", ""),
      "%d %s" % (res.status_code, res.get_json()))
res = post(BOB, "/api/trade/confirm", {"revision": True})
check("a boolean is not a revision", res.status_code == 400, res.get_json())
res = post(BOB, "/api/trade/confirm", {"revision": "2"})
check("nor is a string", res.status_code == 400, res.get_json())

# THE RACE. Bob is looking at a sword. Alice swaps it for a helm, and bob's
# accept - sent for the sword - lands after the swap.
post(ALICE, "/api/trade/update", {"items": [{"item_id": "ironhelm", "quantity": 1}]})
res = post(BOB, "/api/trade/confirm", {"revision": seen_by_bob})
body = res.get_json() or {}
check("an accept for an offer that has since changed is refused",
      res.status_code == 409 and "changed" in body.get("message", ""),
      "%d %s" % (res.status_code, body))
check("and hands back the offer as it now stands",
      (body.get("trade") or {}).get("a", {}).get("items") == [{"item_id": "ironhelm", "quantity": 1}]
      and (body.get("trade") or {}).get("revision") == seen_by_bob + 1, body.get("trade"))
check("bob is not recorded as accepting anything",
      not (trade_of(BOB).get("trade") or {}).get("b", {}).get("confirmed"))
res = accept(ALICE)
check("alice accepts her own current offer", res.status_code == 200, res.get_json())
check("and nothing has moved - bob never agreed to the helm",
      holding("alice", "ironhelm") == 1 and holding("bob", "ironhelm") == 0)

# An accept for a revision from the future is no better than one from the past.
res = post(BOB, "/api/trade/confirm", {"revision": revision(BOB) + 5})
check("an accept for a revision that does not exist yet is refused too",
      res.status_code == 409, res.get_json())
cancel_all(ALICE)
res = post(BOB, "/api/trade/confirm", {"revision": 0})
check("and one after the trade was cancelled finds nothing to accept",
      res.status_code == 404, res.get_json())


# =============================================================================
print("\n--- T-5 the same offer again un-accepts nobody ---")
# =============================================================================
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
post(ALICE, "/api/trade/update", {"items": [{"item_id": "ironsword", "quantity": 1}]})
post(BOB, "/api/trade/update", {"gold": 100})
accept(BOB)
before = revision(ALICE)
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": "ironsword", "quantity": 1}]})
body = res.get_json() or {}
check("re-sending the same offer answers 200", res.status_code == 200, body)
check("without moving the revision", body.get("revision") == before, body.get("revision"))
check("and bob's acceptance stands", body.get("b", {}).get("confirmed") is True, body.get("b"))
res = post(ALICE, "/api/trade/update", {"items": [{"item_id": "ironsword", "quantity": 1}], "gold": 1})
body = res.get_json() or {}
check("a real change - one gold - still withdraws it",
      body.get("b", {}).get("confirmed") is False and body.get("revision") == before + 1, body)
cancel_all(ALICE)


# =============================================================================
print("\n--- T-6 whoever accepted first is given the result ---")
# =============================================================================
clear_flags()
put_item("bob", 5, "tinyhealthpotion", 3)
bob_bag_before = (client.get("/api/character?slot=0", headers=auth(BOB)).get_json() or {}).get("inventory", [])
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
post(ALICE, "/api/trade/update", {"items": [{"item_id": "ironsword", "quantity": 1}]})
post(BOB, "/api/trade/update", {"gold": 300})
bob_purse, alice_purse = purse("bob"), purse("alice")
check("bob accepts first", accept(BOB).status_code == 200)
res = accept(ALICE)
done = res.get_json() or {}
check("alice's accept runs it", res.status_code == 200 and done.get("state") == "done", done)
check("the sword is bob's now", holding("bob", "ironsword") == 1 and holding("alice", "ironsword") == 0)

flags = {(r["user_id"], r["slot"]): r["resync_trade"] for r in sql(
    "SELECT user_id, slot, resync_trade FROM saves WHERE resync_trade IS NOT NULL")}
check("BOTH characters are flagged - bob was told nothing, and alice's answer could be lost",
      flags == {(uid("alice"), 0): done.get("trade_id"), (uid("bob"), 0): done.get("trade_id")},
      flags)
check("the taxes are on the row", [tuple(r) for r in sql(
    "SELECT a_tax, b_tax FROM trades WHERE trade_id = ?", (done.get("trade_id"),))]
      == [(done["tax_paid"]["a"], done["tax_paid"]["b"])])

# THE BUG. Bob's client still shows his old bag, and he drags his potions to
# another cell - an ordinary whole-bag save of what is on his screen.
stale = list(bob_bag_before[:20])
stale[9], stale[5] = stale[5], None
res = client.put("/api/character/inventory", headers=auth(BOB),
                 json={"slot": 0, "inventory": stale})
body = res.get_json() or {}
check("a whole-bag save built before the trade is refused", res.status_code == 409,
      "%d %s" % (res.status_code, body))
check("the sword he received survives it", holding("bob", "ironsword") == 1, cells("bob"))
resync = body.get("resync") or {}
check("the refusal carries the bag the server holds",
      [c for c in resync.get("inventory", []) if c and c["item_id"] == "ironsword"] != []
      and len(resync.get("inventory", [])) == app_module.CARRY_CAPACITY, resync)
check("and the purse", resync.get("gold") == bob_purse - 300 - done["tax_paid"]["b"],
      (resync.get("gold"), bob_purse))
check("and which character it is", resync.get("slot") == 0)
record = resync.get("trade") or {}
check("and a line about the trade, from bob's side",
      record.get("with") == "alice" and record.get("got") == [{"item_id": "ironsword", "quantity": 1}]
      and record.get("gold_gave") == 300 and record.get("tax") == done["tax_paid"]["b"], record)
res = client.put("/api/character/inventory", headers=auth(BOB),
                 json={"slot": 0, "inventory": resync.get("inventory", [])})
check("a save built on the delivered bag goes through", res.status_code == 200, res.get_json())
check("and still holds the sword", holding("bob", "ironsword") == 1)
check("bob's flag is cleared by the delivery",
      sql("SELECT resync_trade FROM saves WHERE user_id = ? AND slot = 0",
          (uid("bob"),))[0]["resync_trade"] is None)

# Alice's flag is still up; her trade poll is how it reaches her.
body = trade_of(ALICE)
check("the trade poll delivers alice's", (body.get("resync") or {}).get("slot") == 0, body)
check("with her purse as the server holds it",
      (body.get("resync") or {}).get("gold") == purse("alice"), body.get("resync"))
check("delivered once", trade_of(ALICE).get("resync") is None)
check("and says how the trade ended", (trade_of(ALICE).get("last") or {}).get("state") == "done")

# The broadcast poll delivers it too - for a window that was never opened.
sql("UPDATE saves SET resync_trade = 'x-test' WHERE user_id = ? AND slot = 0", (uid("bob"),))
polled = beat(BOB, 0).get_json() or {}
check("the broadcast poll carries a pending result",
      (polled.get("trade_resync") or {}).get("slot") == 0, polled.get("trade_resync"))
check("and clears it", beat(BOB, 0).get_json().get("trade_resync") is None)
check("the field is on every poll, null when nothing is pending", "trade_resync" in polled)

# A save for a character that is NOT flagged is untouched by any of this.
res = client.put("/api/character/inventory", headers=auth(ALICE),
                 json={"slot": 1, "inventory": []})
check("a whole-bag save for an unflagged character goes straight through",
      res.status_code == 200, res.get_json())

# A trade that is REFUSED flags nobody - the rollback takes the flag with it.
clear_flags()
put_item("carol", 0, "amethystsword", 1)
beat(CAROL, 0)
# Bob's purse emptied - in the ledger too, so the invariant still holds.
emptied = purse("bob")
sql("UPDATE saves SET gold = 0 WHERE user_id = ? AND slot = 0", (uid("bob"),))
sql("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail)"
    " VALUES (0, ?, 0, ?, 'test_seed', 'emptied for a refusal')", (uid("bob"), -emptied))
beat(BOB, 0)
post(CAROL, "/api/trade/offer", {"slot": 0, "username": "bob"})
post(CAROL, "/api/trade/update", {"items": [{"item_id": "amethystsword", "quantity": 1}]})
accept(CAROL)
res = accept(BOB)
check("a trade bob cannot pay the tax on is refused", res.status_code == 400, res.get_json())
check("and flags nobody",
      sql("SELECT COUNT(*) AS n FROM saves WHERE resync_trade IS NOT NULL")[0]["n"] == 0)
cancel_all(CAROL)
give_gold("bob", 5000)
check("the supply invariant holds through all of it", balanced())


# =============================================================================
print("\n--- T-7 the bag before the keys ---")
# =============================================================================
clear_flags()
sql("DELETE FROM carry_items WHERE user_id = ? AND slot = 0", (uid("alice"),))
put_item("alice", 3, "tinyhealthpotion", 5)
key = app_module.INVENTORY_CAPACITY + 1          # key 2
put_item("alice", key, "tinyhealthpotion", 5)
beat(BOB, 0)
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
post(ALICE, "/api/trade/update", {"items": [{"item_id": "tinyhealthpotion", "quantity": 3}]})
accept(BOB)
accept(ALICE)
left = cells("alice")
check("three potions came out of the bag", left.get(3) == ("tinyhealthpotion", 2), left)
check("and the key is untouched", left.get(key) == ("tinyhealthpotion", 5), left)

post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
post(ALICE, "/api/trade/update", {"items": [{"item_id": "tinyhealthpotion", "quantity": 4}]})
accept(BOB)
accept(ALICE)
left = cells("alice")
check("more than the bag holds empties the bag first", 3 not in left, left)
check("then takes the rest from the key", left.get(key) == ("tinyhealthpotion", 3), left)

# The other takers keep their own order: keys_last is the trade's choice.
with app_module.app.app_context():
    db = app_module.get_db()
    app_module._take_from_backpack(uid("alice"), 0, "tinyhealthpotion", 1)
    db.commit()
check("a take that does not ask for keys_last still goes highest first",
      cells("alice").get(key) == ("tinyhealthpotion", 2), cells("alice"))
clear_flags()


# =============================================================================
print("\n--- T-8 the broadcast poll says a trade is waiting ---")
# =============================================================================
check("no trade, no summary", beat(BOB, 0).get_json().get("trade") is None)
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
summary = beat(BOB, 0).get_json().get("trade") or {}
check("bob's poll says alice opened a trade with him",
      summary.get("with") == "alice" and summary.get("from_them") is True, summary)
check("nobody has accepted", summary.get("they_accepted") is False
      and summary.get("you_accepted") is False, summary)
mine = beat(ALICE, 0).get_json().get("trade") or {}
check("alice's says she opened it", mine.get("with") == "bob" and mine.get("from_them") is False, mine)
accept(ALICE)
summary = beat(BOB, 0).get_json().get("trade") or {}
check("once she accepts, bob's poll says she is waiting on him",
      summary.get("they_accepted") is True and summary.get("you_accepted") is False, summary)
check("carol's poll says nothing about a trade she is not in",
      beat(CAROL, 0).get_json().get("trade") is None)
cancel_all(BOB)
check("and it is gone once called off", beat(BOB, 0).get_json().get("trade") is None)
check("the trade poll says how it ended", (trade_of(ALICE).get("last") or {}).get("state") == "cancelled")


# =============================================================================
print("\n--- T-9 history ---")
# =============================================================================
history = (client.get("/api/trade/history", headers=auth(ALICE)).get_json() or {}).get("trades")
check("history is a list", isinstance(history, list), history)
stamp = (client.get("/api/trade/history", headers=auth(ALICE)).get_json() or {}).get("now", 0)
check("and carries the server's clock to age it against", abs(int(stamp) - int(time.time())) <= 5, stamp)
check("finished trades only - the cancelled ones are not in it",
      history and all(t["state"] == "done" for t in history), history)
check("newest first", history and [t["at"] for t in history] == sorted([t["at"] for t in history], reverse=True))
ids = [t["trade_id"] for t in history]
done_ids = [r["trade_id"] for r in sql(
    "SELECT trade_id FROM trades WHERE state = 'done' AND (a_user = ? OR b_user = ?)"
    " ORDER BY updated_at DESC", (uid("alice"), uid("alice")))]
check("exactly alice's finished trades", sorted(ids) == sorted(done_ids), (ids, done_ids))
sword = next((t for t in history if t["gave"] == [{"item_id": "ironsword", "quantity": 1}]), None)
check("worded from her side: what she gave, what she got, her cut",
      sword is not None and sword["with"] == "bob" and sword["gold_got"] == 300
      and sword["with_name"] == "Bram", sword)
burned = -sum(int(r["delta"]) for r in sql(
    "SELECT delta FROM gold_ledger WHERE reason = 'kingdom_tax' AND user_id = ?", (uid("alice"),)))
check("the cuts in her history add up to what the ledger burned from her",
      sum(t["tax"] for t in history) == burned, (sum(t["tax"] for t in history), burned))
carol_history = (client.get("/api/trade/history", headers=auth(CAROL)).get_json() or {}).get("trades")
check("carol's history holds none of alice's trades",
      not set(t["trade_id"] for t in carol_history or []) & set(ids), carol_history)
check("history needs a login", client.get("/api/trade/history").status_code == 401)

# Bounded, newest kept.
now = int(time.time())
for n in range(app_module.TRADE_HISTORY_SHOWN + 3):
    sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
        " VALUES (?, ?, 0, ?, 0, 'done', ?, ?)",
        ("bulk-%02d" % n, uid("carol"), uid("bob"), now + 100 + n, now + 100 + n))
carol_history = (client.get("/api/trade/history", headers=auth(CAROL)).get_json() or {}).get("trades")
check("at most TRADE_HISTORY_SHOWN come back", len(carol_history) == app_module.TRADE_HISTORY_SHOWN,
      len(carol_history))
check("and they are the newest", carol_history[0]["trade_id"] == "bulk-%02d"
      % (app_module.TRADE_HISTORY_SHOWN + 2), carol_history[0]["trade_id"])
bob_history = (client.get("/api/trade/history", headers=auth(BOB)).get_json() or {}).get("trades")
check("the same trades read from the other side say 'b' things",
      bool(bob_history) and bob_history[0]["with"] == "carol", bob_history[:1])

# THE TIE. updated_at is whole seconds, so a trade that finished and the next
# one called off in the same second share it - and "how did my last trade end"
# must still answer with the later one. Built deliberately rather than hoped
# for: two rows, one stamp, inserted in order.
DAVE = register("dave")
save(DAVE, 0, "tank", "Dunn")
tie = now + 10_000
for trade_id, state in (("tie-1-done", "done"), ("tie-2-cancel", "cancelled"),
                        ("tie-3-done", "done")):
    sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
        " VALUES (?, ?, 0, ?, 0, ?, ?, ?)", (trade_id, uid("dave"), uid("carol"), state, tie, tie))
last = trade_of(DAVE).get("last") or {}
check("two trades ending in the same second: the later one is 'last'",
      last.get("trade_id") == "tie-3-done", last)
dave_history = (client.get("/api/trade/history", headers=auth(DAVE)).get_json() or {}).get("trades") or []
check("and history orders a same-second pair newest first",
      [t["trade_id"] for t in dave_history] == ["tie-3-done", "tie-1-done"],
      [t["trade_id"] for t in dave_history])
sql("DELETE FROM trades WHERE trade_id LIKE 'tie-%'")


# =============================================================================
print("\n--- T-10 old cancelled trades are pruned ---")
# =============================================================================
old = now - app_module.TRADE_CANCELLED_KEPT_SECONDS - 60
sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
    " VALUES ('old-cancel', ?, 0, ?, 0, 'cancelled', ?, ?)", (uid("carol"), uid("bob"), old, old))
sql("INSERT INTO trade_items (trade_id, side, item_id, quantity) VALUES ('old-cancel', 'a', 'ironsword', 1)")
sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
    " VALUES ('old-done', ?, 0, ?, 0, 'done', ?, ?)", (uid("carol"), uid("bob"), old, old))
sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
    " VALUES ('new-cancel', ?, 0, ?, 0, 'cancelled', ?, ?)", (uid("carol"), uid("bob"), now, now))
beat(BOB, 0)
post(ALICE, "/api/trade/offer", {"slot": 0, "username": "bob"})
cancel_all(ALICE)
left = {r["trade_id"] for r in sql("SELECT trade_id FROM trades WHERE trade_id IN"
                                   " ('old-cancel', 'old-done', 'new-cancel')")}
check("a cancelled trade past the window is gone", "old-cancel" not in left, left)
check("with its items", sql("SELECT COUNT(*) AS n FROM trade_items WHERE trade_id = 'old-cancel'")[0]["n"] == 0)
check("a finished one of the same age is kept", "old-done" in left, left)
check("a recent cancelled one is kept", "new-cancel" in left, left)


# =============================================================================
print("\n--- T-11 the schema and the indexes ---")
# =============================================================================
columns = lambda table: {r["name"] for r in sql("PRAGMA table_info(%s)" % table)}
check("trades has revision and both taxes", {"revision", "a_tax", "b_tax"} <= columns("trades"))
check("saves has resync_trade", "resync_trade" in columns("saves"))
check("sessions has playing_slot", "playing_slot" in columns("sessions"))
indexes = {r["name"] for r in sql("SELECT name FROM sqlite_master WHERE type = 'index'")}
check("the history indexes exist", {"idx_trades_a_recent", "idx_trades_b_recent"} <= indexes, indexes)
check("the prune index exists", "idx_trades_cancelled" in indexes)
check("the indexes they replace are gone", not {"idx_trades_a", "idx_trades_b"} & indexes)


def plan(statement, args):
    return " | ".join(str(tuple(r)) for r in sql("EXPLAIN QUERY PLAN " + statement, args))


# THE QUERY THE SERVER RUNS, not a copy of it typed here. A copy would go on
# passing after _trade_find_recent changed shape; the trace is whatever the
# function actually sent, with its values bound in.
with app_module.app.app_context():
    traced = []
    db = app_module.get_db()
    db.set_trace_callback(traced.append)
    app_module._trade_find_recent(db, uid("alice"), 10)
    app_module._trade_find_recent(db, uid("alice"), 1, ("done", "cancelled"))
    db.set_trace_callback(None)
check("the history query was captured", len(traced) == 2, traced)
for label, statement in zip(("history", "the last trade's outcome"), traced):
    shown = plan(statement, ())
    check("%s reads idx_trades_a_recent" % label, "idx_trades_a_recent" in shown, shown)
    check("%s reads idx_trades_b_recent" % label, "idx_trades_b_recent" in shown, shown)
# BOUNDED, which is the point of the two-branch shape. Each side's read must
# come off its index already in order, so its LIMIT stops it after ten rows;
# a sort INSIDE a side would mean reading every trade that player ever made
# to find the newest. The merge above them sorts at most 2 x limit rows, which
# is fine - so the check is on where a sort sits, not whether there is one.
rows = [tuple(r) for r in sql("EXPLAIN QUERY PLAN " + traced[0])]
parents = {r[0]: r for r in rows}
inner_sorts = [r for r in rows if "TEMP B-TREE" in r[3]
               and "CO-ROUTINE" in parents.get(r[1], (0, 0, 0, ""))[3]]
check("each side reads its newest straight off the index, with no sort of its own",
      inner_sorts == [] and sum("CO-ROUTINE" in r[3] for r in rows) == 2, rows)
shown = plan("DELETE FROM trades WHERE state = 'cancelled' AND updated_at < ?", (0,))
check("the prune reads idx_trades_cancelled", "idx_trades_cancelled" in shown, shown)
shown = plan("SELECT * FROM trades WHERE state = 'open' AND (a_user = ? OR b_user = ?)", (1, 1))
check("finding the open trade still uses an index for both sides",
      "idx_trades_a_recent" in shown and "idx_trades_b_recent" in shown, shown)

# An old database, from before any of this: the migration must bring it up and
# boot. A trades table without revision, sessions without playing_slot, and
# the two old indexes.
legacy = os.path.join(tempfile.gettempdir(), "elusion_trades_legacy.db")
if os.path.exists(legacy):
    os.remove(legacy)
conn = sqlite3.connect(legacy)
conn.executescript("""
    CREATE TABLE trades (trade_id TEXT PRIMARY KEY, a_user INTEGER NOT NULL, a_slot INTEGER NOT NULL,
        a_gold INTEGER NOT NULL DEFAULT 0, a_confirmed INTEGER NOT NULL DEFAULT 0,
        b_user INTEGER NOT NULL, b_slot INTEGER NOT NULL, b_gold INTEGER NOT NULL DEFAULT 0,
        b_confirmed INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'open',
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
    CREATE INDEX idx_trades_a ON trades(a_user, state);
    CREATE INDEX idx_trades_b ON trades(b_user, state);
    INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, created_at, updated_at)
        VALUES ('legacy', 1, 0, 2, 0, 0, 0);
""")
conn.commit()
conn.close()
saved_path = app_module.DB_PATH
app_module.DB_PATH = legacy
try:
    app_module.init_db()
    booted = True
except Exception as error:          # the boot is the thing under test
    booted = repr(error)
finally:
    app_module.DB_PATH = saved_path
check("a database from before revisions boots", booted is True, booted)
conn = sqlite3.connect(legacy)
conn.row_factory = sqlite3.Row
legacy_cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)")}
legacy_row = conn.execute("SELECT revision, a_tax, b_tax FROM trades WHERE trade_id = 'legacy'").fetchone()
legacy_idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
conn.close()
check("and its trades gain the new columns", {"revision", "a_tax", "b_tax"} <= legacy_cols, legacy_cols)
check("an open trade from before starts at revision 0 with no tax recorded",
      legacy_row is not None and tuple(legacy_row) == (0, 0, 0), legacy_row and tuple(legacy_row))
check("the old indexes are replaced", not {"idx_trades_a", "idx_trades_b"} & legacy_idx
      and {"idx_trades_a_recent", "idx_trades_b_recent"} <= legacy_idx, legacy_idx)


# =============================================================================
print("\n--- T-12 staff can read an account's trades, under reach ---")
# =============================================================================
MIA = register("mia")
MOE = register("moe")
DAN = register("dan")
sql("UPDATE users SET role = 'mod' WHERE username IN ('mia', 'moe')")
sql("UPDATE users SET role = 'dev' WHERE username = 'dan'")


def staff_trades(token, **query):
    return client.get("/api/staff/trades", headers=auth(token), query_string=query)


res = staff_trades(ALICE, username="bob")
check("a player cannot read anybody's trades - the same 404 as any staff route",
      res.status_code == 404, res.status_code)
check("a mod must name somebody", staff_trades(MIA).status_code == 400)
check("an unknown account is a 404", staff_trades(MIA, username="nobody_at_all").status_code == 404)
check("so is one above the mod's reach, indistinguishably",
      staff_trades(MIA, username="moe").status_code == 404
      and staff_trades(MIA, username="dan").status_code == 404
      and (staff_trades(MIA, username="moe").get_json() or {}).get("message")
          == (staff_trades(MIA, username="nobody_at_all").get_json() or {}).get("message"))
check("a dev reaches a mod", staff_trades(DAN, username="mia").status_code == 200)

page = staff_trades(MIA, username="alice").get_json() or {}
states = {t["state"] for t in page.get("trades", [])}
check("a mod reads alice's trades", page.get("username") == "alice" and len(page.get("trades", [])) > 0,
      page)
check("finished and called-off ones both - a scam is often reported mid-attempt",
      {"done", "cancelled"} <= states, states)
counted = {}
for r in sql("SELECT state, COUNT(*) AS n FROM trades WHERE a_user = ? OR b_user = ? GROUP BY state",
             (uid("alice"), uid("alice"))):
    counted[r["state"]] = int(r["n"])
check("the tally by state matches the table",
      all(int(page.get("summary", {}).get(k, 0)) == int(counted.get(k, 0)) for k in ("open", "done", "cancelled")),
      (page.get("summary"), counted))
# BOB WAS ASKED, NEVER THE ASKER - side b of every trade here. A tally that
# counted one side only would read him as a man who never traded.
bob_page = staff_trades(MIA, username="bob").get_json() or {}
bob_counted = {}
for r in sql("SELECT state, COUNT(*) AS n FROM trades WHERE a_user = ? OR b_user = ? GROUP BY state",
             (uid("bob"), uid("bob"))):
    bob_counted[r["state"]] = int(r["n"])
check("and for somebody who was only ever asked",
      sum(bob_counted.values()) > 0
      and all(int(bob_page.get("summary", {}).get(k, 0)) == int(bob_counted.get(k, 0))
              for k in ("open", "done", "cancelled")),
      (bob_page.get("summary"), bob_counted))
sword = next((t for t in page.get("trades", []) if t["gave"] == [{"item_id": "ironsword", "quantity": 1}]), None)
check("each worded from alice's side, with her character and when it was opened",
      sword is not None and sword["with"] == "bob" and sword["character"] == "Aldra"
      and sword["opened_at"] > 0 and sword["gold_got"] == 300, sword)
history = (client.get("/api/trade/history", headers=auth(ALICE)).get_json() or {}).get("trades") or []
check("the player's own history names their character too",
      bool(history) and all(t.get("character") == "Aldra" for t in history), history[:1])

# ---- paging, keyset, ties across a page boundary ----
same = now + 50_000
for n in range(5):
    sql("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
        " VALUES (?, ?, 0, ?, 0, 'done', ?, ?)", ("tie-%d" % n, uid("carol"), uid("bob"), same, same))
want = [r["trade_id"] for r in sql(
    "SELECT trade_id FROM trades WHERE a_user = ? OR b_user = ? ORDER BY updated_at DESC, rowid DESC",
    (uid("carol"), uid("carol")))]
got, cursor, pages = [], None, 0
while pages < 20:
    query = {"username": "carol", "limit": 4}
    if cursor:
        query.update(before_at=cursor["at"], before_seq=cursor["seq"])
    body = staff_trades(MIA, **query).get_json() or {}
    got += [t["trade_id"] for t in body.get("trades", [])]
    pages += 1
    cursor = body.get("next_before")
    if not body.get("more"):
        break
check("paging four at a time walks every one of carol's trades, in order, once",
      got == want, (len(got), len(want), got[:6], want[:6]))
check("including five that ended in the same second, split across pages",
      [t for t in got if t.startswith("tie-")] == ["tie-4", "tie-3", "tie-2", "tie-1", "tie-0"], got[:8])
check("and the last page says there is no more", cursor is None)
check("half a cursor is refused",
      staff_trades(MIA, username="carol", before_at=same).status_code == 400
      and staff_trades(MIA, username="carol", before_seq=3).status_code == 400)
check("a limit out of range is refused",
      staff_trades(MIA, username="carol", limit=0).status_code == 400
      and staff_trades(MIA, username="carol", limit=999).status_code == 400)

# ---- each branch of a page is bounded ----
with app_module.app.app_context():
    traced = []
    db = app_module.get_db()
    db.set_trace_callback(traced.append)
    app_module._trade_find_page(db, uid("carol"), 5, app_module.STAFF_TRADE_STATES, (same, 999999))
    db.set_trace_callback(None)
rows = [tuple(r) for r in sql("EXPLAIN QUERY PLAN " + traced[0])] if traced else []
parents = {r[0]: r for r in rows}
inner_sorts = [r for r in rows if "TEMP B-TREE" in r[3]
               and "CO-ROUTINE" in parents.get(r[1], (0, 0, 0, ""))[3]]
check("a staff page is six index reads - two sides by three states - none of them sorted",
      sum("CO-ROUTINE" in r[3] for r in rows) == 6 and inner_sorts == [], rows)
sql("DELETE FROM trades WHERE trade_id LIKE 'tie-%'")


# =============================================================================
print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed:")
    for label in failures:
        print("  - %s" % label)
sys.exit(1 if failed else 0)
