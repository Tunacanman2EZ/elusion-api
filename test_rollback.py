"""
Save history, rollback, and giving a player an item. Run: python3 test_rollback.py

The owner, 6 Oct: "roll back and give player item might be useful". The server
keeps each character as it was, a few times over (save_snapshots); the owner
can put a character back to one of them, and can put an item in another
player's bag. SAVE SNAPSHOTS AND ROLLBACK in app.py.

  RB-1  OWNER ONLY. A player, a mod and a dev get the same bare 404 from
        both routes and from a grant naming somebody else.
  RB-2  WHEN A SNAPSHOT IS TAKEN. A save takes one; another save inside
        SNAPSHOT_EVERY_SECONDS takes none; a character that has not changed
        takes none however long it has been.
  RB-3  THE NEWEST SNAPSHOTS_KEPT PER CHARACTER, and a character's history is
        its own - another character's saves do not push it out.
  RB-4  A ROLLBACK PUTS THE CHARACTER BACK: bag, hotbar, skills, level, xp,
        what is worn, the pet - and nothing of the account's: the bank, the
        lusions, the other characters, the area and the map stay.
  RB-5  THE GOLD GOES THROUGH THE LEDGER, up and down, and the supply
        invariant holds.
  RB-6  THEIR GAME RELOADS: every session of theirs ends. The owner's does not,
        unless it is the owner's own character.
  RB-7  A ROLLBACK CAN BE UNDONE: the snapshot taken before it is returned,
        and restoring it puts everything back.
  RB-8  REFUSALS: somebody else's snapshot, an unknown account, a bad id, an
        open trade, a slot that holds another class now, a deleted character.
  RB-9  THE POOLS ONLY COME DOWN, and a pet no longer held is not put back out.
  RB-10 LOGGED: a "rollback" line about the player, saying what changed.
  GV-1  GIVE TO A PLAYER: into the character they are playing, and their game
        is handed the new bag and who gave what on its next poll - once.
  GV-2  OFFLINE: their last character, and the answer says they are offline.
  GV-3  A FULL BAG IS A 409 AND NOTHING ELSE: no item, no flag, no line, no
        snapshot.
  GV-4  WHO: your own name is yourself; an unknown name, or an account with no
        character, is 404.
  GV-5  A GIFT CAN BE TAKEN BACK: a snapshot is taken first, and it is logged
        as "give", about the player.
  GV-6  A TRADE'S RESULT WINS THE FLAG, and the gift list is short.
"""
import importlib.util, json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_rollback_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "boss"
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
        passed += 1; print("  ok   %s" % label)
    else:
        failed += 1; failures.append(label); print("  FAIL %s   %s" % (label, detail))
def section(t): print("\n%s\n%s" % (t, "-" * len(t)))

PW = "hunter2hunter2"
_ip = [20]
def register(name):
    _ip[0] += 1
    r = client.post("/api/auth/register", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["token"]

def login(name):
    """A rollback ends every session of the account, so a test that goes on
    acting as that player signs in again, like their game would."""
    _ip[0] += 1
    r = client.post("/api/auth/login", json={"username": name, "password": PW},
                    environ_base={"REMOTE_ADDR": "198.51.100.%d" % _ip[0]})
    assert r.status_code == 200, r.get_json()
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

def supply():
    conn = sqlite3.connect(DB_PATH)
    try:
        return app_module.gold_supply(conn)
    finally:
        conn.close()

def make(token, slot, class_id):
    r = client.put("/api/save", headers=bearer(token),
                   json={"slot": slot, "class_id": class_id, "name": class_id})
    assert r.status_code == 200, r.get_json()

def save(token, slot, class_id, **extra):
    body = {"slot": slot, "class_id": class_id, "name": class_id}
    body.update(extra)
    return client.put("/api/save", headers=bearer(token), json=body)

def mint(user_id, slot, amount):
    """Gold the way the server mints it, so the invariant holds before the test."""
    raw("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?", (amount, user_id, slot))
    raw("INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) VALUES (1, ?, ?, ?, 'test', '')",
        (user_id, slot, amount))

def put_cell(user_id, slot, position, item_id, quantity):
    raw("INSERT OR REPLACE INTO carry_items (user_id, slot, position, item_id, quantity)"
        " VALUES (?, ?, ?, ?, ?)", (user_id, slot, position, item_id, quantity))

def bag(user_id, slot):
    return [(r["position"], r["item_id"], r["quantity"]) for r in raw(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?"
        " ORDER BY position", (user_id, slot))]

def skills(user_id, slot):
    return {r["skill_id"]: (r["level"], r["xp"]) for r in raw(
        "SELECT skill_id, level, xp FROM skills WHERE user_id = ? AND slot = ?", (user_id, slot))}

def snapshots_of(user_id, slot):
    return raw("SELECT * FROM save_snapshots WHERE user_id = ? AND slot = ? ORDER BY taken_at DESC, id DESC",
               (user_id, slot))

def age_snapshots(user_id, slot, seconds):
    raw("UPDATE save_snapshots SET taken_at = taken_at - ? WHERE user_id = ? AND slot = ?",
        (seconds, user_id, slot))

def history(token, username, slot=None):
    path = "/api/staff/snapshots?username=%s" % username
    if slot is not None:
        path += "&slot=%s" % slot
    return client.get(path, headers=bearer(token))

def rollback(token, username, snapshot_id):
    return client.post("/api/staff/rollback", headers=bearer(token),
                       json={"username": username, "snapshot_id": snapshot_id})

def give(token, username, item_id, quantity=1, **extra):
    body = {"username": username, "item_id": item_id, "quantity": quantity}
    body.update(extra)
    return client.post("/api/staff/grant", headers=bearer(token), json=body)

def poll(token, slot=None):
    path = "/api/server/broadcasts" if slot is None else "/api/server/broadcasts?slot=%d" % slot
    return client.get(path, headers=bearer(token))

def msg(res):
    return (res.get_json(silent=True) or {}).get("message", "")

POTION = "largehealthpotion"
SWORD = "ironmaul" if "ironmaul" in gamedata.ITEMS else sorted(gamedata.ITEMS)[0]
AMULET = "amethystamulet"
PET = "petmage"

boss = register("boss")
make(boss, 0, "warrior")
alice = register("alice")
make(alice, 0, "warrior")
make(alice, 1, "mage")
me = uid("alice")
mod_t = register("modder"); raw("UPDATE users SET role = 'mod' WHERE username = 'modder'")
dev_t = register("devver"); raw("UPDATE users SET role = 'dev' WHERE username = 'devver'")
plain_t = register("plainy")
make(mod_t, 0, "warrior"); make(dev_t, 0, "warrior"); make(plain_t, 0, "warrior")


# =============================================================================
section("RB-2  when a snapshot is taken")
# =============================================================================
check("a brand new character has no history: there is nothing to go back to",
      len(snapshots_of(me, 0)) == 0, len(snapshots_of(me, 0)))
put_cell(me, 0, 0, POTION, 3)
save(alice, 0, "warrior")
first = snapshots_of(me, 0)
check("a save of a character that exists takes one", len(first) == 1, len(first))
check("  of reason 'save', with its level, gold and how many cells are carried",
      first and first[0]["reason"] == "save" and first[0]["level"] == 1
      and first[0]["gold"] == 0 and first[0]["items"] == 1, first and dict(first[0]))
put_cell(me, 0, 1, POTION, 2)
save(alice, 0, "warrior")
check("another save inside SNAPSHOT_EVERY_SECONDS takes none",
      len(snapshots_of(me, 0)) == 1, len(snapshots_of(me, 0)))
age_snapshots(me, 0, app_module.SNAPSHOT_EVERY_SECONDS + 1)
save(alice, 0, "warrior")
check("after the window, a changed character takes one",
      len(snapshots_of(me, 0)) == 2, len(snapshots_of(me, 0)))
age_snapshots(me, 0, app_module.SNAPSHOT_EVERY_SECONDS * 5)
save(alice, 0, "warrior")
save(alice, 0, "warrior")
check("an unchanged character takes none, however long it has been",
      len(snapshots_of(me, 0)) == 2, len(snapshots_of(me, 0)))
check("a snapshot is the character, not a whole save row: no area, no map, no pools",
      set(json.loads(snapshots_of(me, 0)[0]["snapshot"])) ==
      {"class_id", "name", "level", "xp", "xp_to_next", "gold", "active_pet_id",
       "equipment", "carry", "skills"},
      sorted(json.loads(snapshots_of(me, 0)[0]["snapshot"])))


# =============================================================================
section("RB-1  owner only")
# =============================================================================
some_id = snapshots_of(me, 0)[0]["id"]
refused_all = []
for who, token in (("a player", plain_t), ("a mod", mod_t), ("a dev", dev_t)):
    r = history(token, "alice")
    check("%s reading the save history gets the bare 404" % who,
          r.status_code == 404 and msg(r) == "Not found.", (r.status_code, r.get_json()))
    refused_all.append(r.status_code == 404 and msg(r) == "Not found.")
    r = rollback(token, "alice", some_id)
    check("%s rolling back gets the same" % who,
          r.status_code == 404 and msg(r) == "Not found.", (r.status_code, r.get_json()))
    refused_all.append(r.status_code == 404 and msg(r) == "Not found.")
    r = give(token, "alice", POTION)
    check("%s giving a player an item gets the same" % who,
          r.status_code == 404 and msg(r) == "Not found.", (r.status_code, r.get_json()))
    refused_all.append(r.status_code == 404 and msg(r) == "Not found.")
check("nobody below the owner reads, restores or gives - the same bare 404 as no route at all",
      len(refused_all) == 9 and all(refused_all), refused_all)
check("  and none of it changed alice's bag", bag(me, 0) == [(0, POTION, 3), (1, POTION, 2)], bag(me, 0))
r = client.get("/api/staff/snapshots?username=alice")
check("no token at all is a 401, like every route", r.status_code == 401, r.status_code)
ladder = {rank["rank"]: [x["path"] for x in rank["routes"]] for rank in
          client.get("/api/staff/powers", headers=bearer(boss)).get_json()["ladder"]}
# Listed under the co-owner since 0.21.0: the lowest rank require_owner lets in,
# with the owner above it.
check("both routes are on the owner's list of powers, read from the decorators",
      "/api/staff/snapshots" in ladder["coowner"] and "/api/staff/rollback" in ladder["coowner"],
      ladder["coowner"])


# =============================================================================
section("RB-3  the newest SNAPSHOTS_KEPT per character")
# =============================================================================
kept = app_module.SNAPSHOTS_KEPT
for n in range(kept + 5):
    put_cell(me, 1, 0, POTION, n + 1)
    age_snapshots(me, 1, app_module.SNAPSHOT_EVERY_SECONDS + 1)
    save(alice, 1, "mage")
mage_hist = snapshots_of(me, 1)
check("a character keeps the newest %d" % kept, len(mage_hist) == kept, len(mage_hist))
check("  the oldest are the ones that went",
      json.loads(mage_hist[-1]["snapshot"])["carry"][0][2] == 6,
      json.loads(mage_hist[-1]["snapshot"])["carry"])
check("another character's history is its own and untouched", len(snapshots_of(me, 0)) == 2,
      len(snapshots_of(me, 0)))

r = history(boss, "alice", 1)
body = r.get_json() or {}
check("the owner reads it: newest first, with the character as it is now",
      r.status_code == 200 and len(body.get("snapshots", [])) == kept
      and body["snapshots"][0]["taken_at"] >= body["snapshots"][-1]["taken_at"]
      and body.get("now", {}).get("class_id") == "mage", (r.status_code, body.get("now")))
check("  and every character on the account, to choose between",
      [c["slot"] for c in body.get("characters", [])] == [0, 1], body.get("characters"))
check("  and no snapshot's contents, only what the list shows",
      all(set(s) == {"id", "taken_at", "reason", "level", "gold", "items"} for s in body.get("snapshots", [])))
check("  with how many are kept and how often, for the window to say",
      body.get("kept") == kept and body.get("every_seconds") == app_module.SNAPSHOT_EVERY_SECONDS)
r = history(boss, "alice", 3)
check("a slot with no character there is a 404", r.status_code == 404, r.status_code)
r = history(boss, "alice", "x")
check("a slot that is not a number is a 400", r.status_code == 400, r.status_code)
r = history(boss, "")
check("no name is a 400", r.status_code == 400, r.status_code)
poll(alice, 1)
r = history(boss, "alice")
check("no slot reads the character they are playing", (r.get_json() or {}).get("slot") == 1,
      (r.get_json() or {}).get("slot"))


# =============================================================================
section("RB-4  a rollback puts the character back, and nothing of the account's")
# =============================================================================
carol = register("carol")
make(carol, 0, "warrior")
make(carol, 1, "mage")
cid = uid("carol")
mint(cid, 0, 500)
put_cell(cid, 0, 0, POTION, 4)
put_cell(cid, 0, 5, SWORD, 1)
put_cell(cid, 0, 21, POTION, 2)          # a hotbar key
put_cell(cid, 0, 7, PET, 1)
raw("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 0, 'attack', 9, 120)", (cid,))
raw("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 0, 'fishing', 4, 30)", (cid,))
raw("UPDATE saves SET level = 16, xp = 777, equipment = ? , active_pet_id = ? WHERE user_id = ? AND slot = 0",
    (json.dumps({"amulet": AMULET}), PET, cid))
save(carol, 0, "warrior", area="field", explored={})
then = snapshots_of(cid, 0)
check("carol's character is kept as she is", len(then) == 1, len(then))
then_id = then[0]["id"]
then_bag = bag(cid, 0)

# Then things happen to her.
client.get("/api/account", headers=bearer(carol))
raw("INSERT OR IGNORE INTO accounts (user_id) VALUES (?)", (cid,))
raw("UPDATE accounts SET lusions = 7 WHERE user_id = ?", (cid,))
raw("INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, 0, ?, 1)", (cid, SWORD))
raw("DELETE FROM carry_items WHERE user_id = ? AND slot = 0", (cid,))
put_cell(cid, 0, 2, "ironhelm", 1)
put_cell(cid, 1, 0, POTION, 9)
raw("UPDATE skills SET level = 12 WHERE user_id = ? AND slot = 0 AND skill_id = 'attack'", (cid,))
raw("INSERT OR REPLACE INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, 0, 'cooking', 3, 5)", (cid,))
raw("UPDATE saves SET level = 18, xp = 5, equipment = '{}', area = 'cave', explored = ? WHERE user_id = ? AND slot = 0",
    (json.dumps({"cave": {"w": 1, "h": 1, "ox": 0, "oy": 0, "bits": "AQ=="}}), cid))
mage_bag = bag(cid, 1)

r = rollback(boss, "carol", then_id)
out = r.get_json() or {}
check("the owner rolls her back", r.status_code == 200, (r.status_code, out))
check("  the bag, cell for cell, the hotbar key included", bag(cid, 0) == then_bag, (bag(cid, 0), then_bag))
check("  the skills, including one learned since going", skills(cid, 0) == {"attack": (9, 120), "fishing": (4, 30)},
      skills(cid, 0))
row = raw("SELECT * FROM saves WHERE user_id = ? AND slot = 0", (cid,))[0]
check("  the level and xp", row["level"] == 16 and row["xp"] == 777, (row["level"], row["xp"]))
check("  and xp_to_next is the curve's for that level",
      row["xp_to_next"] == gamedata.xp_needed_for_level(16), row["xp_to_next"])
check("  what was worn", json.loads(row["equipment"]) == {"amulet": AMULET}, row["equipment"])
check("  and the maxima follow what is worn at that level",
      row["max_hp"] == gamedata.max_stats_for("warrior", 16, {"amulet": AMULET})["max_hp"], row["max_hp"])
check("  the pet that was out, which is still in the bag", row["active_pet_id"] == PET, row["active_pet_id"])
check("the area and the map are where she is now, not where she was",
      row["area"] == "cave" and "cave" in json.loads(row["explored"]), (row["area"], row["explored"]))
check("the bank is the account's and untouched",
      [(r2["item_id"]) for r2 in raw("SELECT item_id FROM bank_items WHERE user_id = ?", (cid,))] == [SWORD])
check("  and so are the lusions", one("SELECT lusions FROM accounts WHERE user_id = ?", (cid,)) == 7)
check("  and her other character", bag(cid, 1) == mage_bag, bag(cid, 1))
check("the answer says what it was and what it is", out.get("was", {}).get("level") == 18
      and out.get("now", {}).get("level") == 16 and out.get("restored") == then_id, out)


# =============================================================================
section("RB-5  the gold goes through the ledger")
# =============================================================================
check("carol's purse is back to 500", row["gold"] == 500, row["gold"])
check("the supply balances after a rollback", supply()["balanced"], supply())
dave = register("dave")
make(dave, 0, "warrior")
did = uid("dave")
mint(did, 0, 100)
put_cell(did, 0, 0, POTION, 1)
save(dave, 0, "warrior")
poor_id = snapshots_of(did, 0)[0]["id"]
mint(did, 0, 900)
check("dave has 1000 now and the supply balances", one("SELECT gold FROM saves WHERE user_id = ?", (did,)) == 1000
      and supply()["balanced"])
r = rollback(boss, "dave", poor_id)
check("rolled back to 100", r.status_code == 200 and one("SELECT gold FROM saves WHERE user_id = ?", (did,)) == 100,
      (r.status_code, r.get_json()))
burns = raw("SELECT delta, reason FROM gold_ledger WHERE user_id = ? AND reason = ?", (did, app_module.ROLLBACK_REASON))
check("  the 900 left through a burn row, not a bare write", [b["delta"] for b in burns] == [-900], [dict(b) for b in burns])
check("  and the supply still balances", supply()["balanced"], supply())
undo = (r.get_json() or {}).get("undo_snapshot")
r = rollback(boss, "dave", undo)
mints = raw("SELECT delta FROM gold_ledger WHERE user_id = ? AND reason = ?", (did, app_module.ROLLBACK_REASON))
check("and back up again, minted the same way", one("SELECT gold FROM saves WHERE user_id = ?", (did,)) == 1000
      and [m["delta"] for m in mints] == [-900, 900], [m["delta"] for m in mints])
check("  balanced", supply()["balanced"], supply())


# =============================================================================
section("RB-6  their game reloads")
# =============================================================================
erin = register("erin")
make(erin, 0, "warrior")
eid = uid("erin")
put_cell(eid, 0, 0, POTION, 2)
save(erin, 0, "warrior")
erin_snap = snapshots_of(eid, 0)[0]["id"]
check("erin is signed in", poll(erin).status_code == 200)
r = rollback(boss, "erin", erin_snap)
check("the answer counts the sessions it ended", (r.get_json() or {}).get("sessions_ended") == 1, r.get_json())
check("  and her game's next poll is the 401 that sends it to the login screen",
      poll(erin).status_code == 401, poll(erin).status_code)
check("the owner, rolling back somebody else, is still signed in", poll(boss).status_code == 200)
r = client.post("/api/auth/login", json={"username": "erin", "password": PW},
                environ_base={"REMOTE_ADDR": "198.51.100.200"})
erin = (r.get_json() or {}).get("token", "")
check("she signs straight back in - a reload, not a ban", r.status_code == 200 and erin, r.status_code)
boss_id = uid("boss")
put_cell(boss_id, 0, 0, POTION, 1)
save(boss, 0, "warrior")
own_snap = snapshots_of(boss_id, 0)[0]["id"]
r = rollback(boss, "boss", own_snap)
check("the owner may roll back their own character", r.status_code == 200, (r.status_code, r.get_json()))
check("  and is signed out like anybody else, so their game reloads it", poll(boss).status_code == 401)
r = client.post("/api/auth/login", json={"username": "boss", "password": PW},
                environ_base={"REMOTE_ADDR": "198.51.100.201"})
boss = (r.get_json() or {}).get("token", "")
check("  and signs back in", r.status_code == 200 and boss, (r.status_code, r.get_json()))


# =============================================================================
section("RB-7  a rollback can be undone")
# =============================================================================
before_undo = bag(cid, 0)
put_cell(cid, 0, 9, "ironhelm", 1)
raw("UPDATE saves SET level = 19 WHERE user_id = ? AND slot = 0", (cid,))
r = rollback(boss, "carol", then_id)
undo_id = (r.get_json() or {}).get("undo_snapshot")
undo_row = raw("SELECT * FROM save_snapshots WHERE id = ?", (undo_id,))
check("the snapshot taken before a rollback is returned", undo_id and undo_row
      and undo_row[0]["reason"] == "before-rollback", (undo_id, undo_row and dict(undo_row[0])))
check("  it holds what the rollback replaced", undo_row and undo_row[0]["level"] == 19)
r = rollback(boss, "carol", undo_id)
check("restoring it puts everything back", r.status_code == 200
      and (9, "ironhelm", 1) in bag(cid, 0) and one("SELECT level FROM saves WHERE user_id = ? AND slot = 0", (cid,)) == 19,
      (r.status_code, bag(cid, 0)))
r = rollback(boss, "carol", undo_id)
again = (r.get_json() or {}).get("undo_snapshot")
count = len(snapshots_of(cid, 0))
bag_now = bag(cid, 0)
r = rollback(boss, "carol", undo_id)
check("restoring the state it is already in changes nothing, and adds no snapshot: the newest already holds it",
      r.status_code == 200 and (r.get_json() or {}).get("undo_snapshot") == again
      and len(snapshots_of(cid, 0)) == count and bag(cid, 0) == bag_now, (again, r.get_json()))


# =============================================================================
section("RB-8  refusals")
# =============================================================================
r = rollback(boss, "dave", then_id)
check("carol's snapshot named with dave's name is a 404 - the two must agree",
      r.status_code == 404, (r.status_code, r.get_json()))
check("  and dave is untouched", one("SELECT gold FROM saves WHERE user_id = ?", (did,)) == 1000)
r = rollback(boss, "nobody_here", then_id)
check("an unknown account is a 404", r.status_code == 404, r.status_code)
for bad in ("x", None, True, 1.5, [1]):
    r = rollback(boss, "carol", bad)
    check("a snapshot_id of %r is a 400" % (bad,), r.status_code == 400, (r.status_code, r.get_json()))
r = rollback(boss, "carol", 999999)
check("an id that is nobody's is a 404", r.status_code == 404, r.status_code)

frank = register("frank")
make(frank, 0, "warrior")
fid = uid("frank")
put_cell(fid, 0, 0, POTION, 1)
save(frank, 0, "warrior")
frank_snap = snapshots_of(fid, 0)[0]["id"]
raw("INSERT INTO trades (trade_id, a_user, a_slot, b_user, b_slot, state, created_at, updated_at)"
    " VALUES ('t-open', ?, 0, ?, 0, 'open', strftime('%s','now'), strftime('%s','now'))", (fid, did))
put_cell(fid, 0, 3, SWORD, 1)
r = rollback(boss, "frank", frank_snap)
check("a character in an open trade is a 409", r.status_code == 409 and "trade" in msg(r), (r.status_code, msg(r)))
check("  and nothing changed", (3, SWORD, 1) in bag(fid, 0), bag(fid, 0))
raw("UPDATE trades SET state = 'cancelled' WHERE trade_id = 't-open'")

state = json.loads(raw("SELECT snapshot FROM save_snapshots WHERE id = ?", (frank_snap,))[0]["snapshot"])
state["class_id"] = "mage"
raw("UPDATE save_snapshots SET snapshot = ? WHERE id = ?", (json.dumps(state), frank_snap))
r = rollback(boss, "frank", frank_snap)
check("a snapshot of another class than the slot holds now is a 409", r.status_code == 409, (r.status_code, msg(r)))
check("  and nothing changed", (3, SWORD, 1) in bag(fid, 0), bag(fid, 0))

carol = login("carol")
save(carol, 1, "mage")
check("carol's mage has a history of its own", len(snapshots_of(cid, 1)) > 0, len(snapshots_of(cid, 1)))
r = client.post("/api/character/delete", headers=bearer(carol), json={"slot": 1, "confirm": "mage"})
check("deleting a character", r.status_code == 200, r.get_json())
check("  takes its save history with it", len(snapshots_of(cid, 1)) == 0)
check("  and leaves the other character's", len(snapshots_of(cid, 0)) > 0)


# =============================================================================
section("RB-9  the pools only come down, and a pet not held is not put out")
# =============================================================================
gina = register("gina")
make(gina, 0, "warrior")
gid = uid("gina")
put_cell(gid, 0, 0, PET, 1)
raw("UPDATE saves SET level = 3, active_pet_id = ? WHERE user_id = ? AND slot = 0", (PET, gid))
save(gina, 0, "warrior")
low_snap = snapshots_of(gid, 0)[0]["id"]
derived20 = gamedata.max_stats_for("warrior", 20, {})
raw("UPDATE saves SET level = 20, max_hp = ?, hp = ?, mana = 5, max_mana = ? WHERE user_id = ? AND slot = 0",
    (derived20["max_hp"], derived20["max_hp"], derived20["max_mana"], gid))
raw("DELETE FROM carry_items WHERE user_id = ? AND slot = 0", (gid,))
r = rollback(boss, "gina", low_snap)
row = raw("SELECT * FROM saves WHERE user_id = ? AND slot = 0", (gid,))[0]
derived3 = gamedata.max_stats_for("warrior", 3, {})
check("level 20 at full health, rolled back to level 3: hp comes down to the old maximum",
      row["hp"] == derived3["max_hp"] and row["max_hp"] == derived3["max_hp"], (row["hp"], row["max_hp"]))
check("  and mana that was low stays low - a rollback is not a heal", row["mana"] == 5, row["mana"])
check("the pet is back in the bag, so it is out again", row["active_pet_id"] == PET, row["active_pet_id"])

# The same snapshot, but with the pet's cell gone from it: after the restore the
# pet is in neither the bag nor the bank, so it is not put out.
state = json.loads(raw("SELECT snapshot FROM save_snapshots WHERE id = ?", (low_snap,))[0]["snapshot"])
state["carry"] = []
raw("UPDATE save_snapshots SET snapshot = ? WHERE id = ?", (json.dumps(state), low_snap))
r = rollback(boss, "gina", low_snap)
row = raw("SELECT active_pet_id FROM saves WHERE user_id = ? AND slot = 0", (gid,))[0]
check("a snapshot whose pet is held nowhere now puts no pet out",
      row["active_pet_id"] == "" and (r.get_json() or {}).get("pet_cleared") is True,
      (row["active_pet_id"], r.get_json()))
raw("INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, 0, ?, 1)", (gid, PET))
r = rollback(boss, "gina", low_snap)
row = raw("SELECT active_pet_id FROM saves WHERE user_id = ? AND slot = 0", (gid,))[0]
check("  but one in the account's bank is held, so it is", row["active_pet_id"] == PET, row["active_pet_id"])


# =============================================================================
section("RB-10  logged")
# =============================================================================
lines = raw("SELECT * FROM staff_actions WHERE action = 'rollback' AND target_name = 'carol' ORDER BY id")
# RB-4 rolled her back once and RB-7 four times; the refusals in RB-8 changed
# nothing, so they wrote nothing.
check("every rollback of carol is a line about carol, and no refusal is", len(lines) == 5, len(lines))
first = lines[0] if lines else {}
check("  by the owner", lines and first["actor_name"] == "boss" and first["target_id"] == cid)
check("  saying which snapshot, and what changed",
      lines and ("snapshot #%d" % then_id) in first["detail"] and "level 18 -> 16" in first["detail"]
      and "undo is #" in first["detail"], lines and first["detail"])
r = client.get("/api/staff/actions?action=moderation&player=carol", headers=bearer(boss))
kinds = [a.get("action") for a in (r.get_json() or {}).get("actions", [])]
check("  and it is on her moderation record, where a mod reading her will see it",
      "rollback" in kinds, (r.status_code, kinds))
check("rollback and give are kinds the log knows",
      "rollback" in app_module.STAFF_ACTION_KINDS and "give" in app_module.STAFF_ACTION_KINDS)


# =============================================================================
section("GV-1  give to a player: their playing character, and their game is told")
# =============================================================================
hank = register("hank")
make(hank, 0, "warrior")
make(hank, 2, "tank")
hid = uid("hank")
check("hank is playing his tank", poll(hank, 2).status_code == 200)
supply_before = supply()
r = give(boss, "hank", POTION, 3)
out = r.get_json() or {}
check("the owner gives hank three potions", r.status_code == 200, (r.status_code, out))
check("  into the character he is playing, not his first", bag(hid, 2) == [(0, POTION, 3)] and bag(hid, 0) == [],
      (bag(hid, 2), bag(hid, 0)))
check("  the answer says who, which character, what and where",
      out.get("username") == "hank" and out.get("slot") == 2 and out.get("character") == "tank"
      and out.get("granted_item_id") == POTION and out.get("granted_quantity") == 3
      and out.get("carry_positions") == [0], out)
check("  and that he is online, so he will see it within a poll", out.get("online") is True, out)
check("  and not the owner's bag", "inventory" not in out)
seen = (poll(hank, 2).get_json() or {}).get("trade_resync")
check("his game's next poll hands it the bag the server holds",
      isinstance(seen, dict) and seen.get("slot") == 2
      and seen.get("inventory", [None])[0] == {"item_id": POTION, "quantity": 3}, seen)
check("  and who gave what, for the line on his screen",
      isinstance(seen, dict) and seen.get("gifts") == [{"by": "boss", "item_id": POTION, "quantity": 3}]
      and seen.get("trade") is None, seen)
check("  once: the next poll has nothing", (poll(hank, 2).get_json() or {}).get("trade_resync") is None)
check("a gift is an item, not gold: the supply did not move", supply() == supply_before)
r = give(boss, "hank", POTION, 2, slot=0)
check("a slot sent with a name is not used - the character he is playing is",
      r.status_code == 200 and bag(hid, 2) == [(0, POTION, 5)] and bag(hid, 0) == [], (bag(hid, 2), bag(hid, 0)))
r = give(boss, "hank", SWORD, 1, quality="perfect")
check("gear can be given rolled, as the owner's own grant can",
      r.status_code == 200 and "~" in (r.get_json() or {}).get("granted_item_id", ""), r.get_json())
r = give(boss, "hank", POTION, 999)
check("the stack ceiling holds, as for the owner's own grant", r.status_code == 400, (r.status_code, msg(r)))


# =============================================================================
section("GV-2  offline: their last character")
# =============================================================================
ivy = register("ivy")
make(ivy, 0, "warrior")
make(ivy, 1, "mage")
iid = uid("ivy")
raw("DELETE FROM sessions WHERE user_id = ?", (iid,))
raw("UPDATE saves SET updated_at = 1 WHERE user_id = ? AND slot = 0", (iid,))
r = give(boss, "ivy", POTION, 1)
check("an offline player gets it in the character saved last",
      r.status_code == 200 and bag(iid, 1) == [(0, POTION, 1)], (r.status_code, bag(iid, 1)))
check("  and the answer says she is offline", (r.get_json() or {}).get("online") is False, r.get_json())
r = client.post("/api/auth/login", json={"username": "ivy", "password": PW},
                environ_base={"REMOTE_ADDR": "198.51.100.202"})
ivy = (r.get_json() or {}).get("token", "")
seen = (poll(ivy).get_json() or {}).get("trade_resync")
check("  and her game reads the line when she next signs in",
      isinstance(seen, dict) and seen.get("gifts", [{}])[0].get("item_id") == POTION, seen)


# =============================================================================
section("GV-3  a full bag is a 409 and nothing else")
# =============================================================================
jack = register("jack")
make(jack, 0, "warrior")
jid = uid("jack")
for pos in range(app_module.INVENTORY_CAPACITY):
    put_cell(jid, 0, pos, SWORD, 1)
lines_before = one("SELECT COUNT(*) FROM staff_actions")
snaps_before = len(snapshots_of(jid, 0))
r = give(boss, "jack", POTION, 1)
check("jack's bag is full: 409, and it says whose", r.status_code == 409 and "jack" in msg(r), (r.status_code, msg(r)))
check("  nothing went in", all(b[1] == SWORD for b in bag(jid, 0)) and len(bag(jid, 0)) == app_module.INVENTORY_CAPACITY)
check("  no line in the log", one("SELECT COUNT(*) FROM staff_actions") == lines_before)
check("  no flag on his bag", one("SELECT resync_trade FROM saves WHERE user_id = ?", (jid,)) is None)
check("  and no snapshot left behind", len(snapshots_of(jid, 0)) == snaps_before, len(snapshots_of(jid, 0)))


# =============================================================================
section("GV-4  who")
# =============================================================================
boss_bag = bag(boss_id, 0)
r = client.post("/api/staff/grant", headers=bearer(boss),
                json={"username": "BOSS", "slot": 0, "item_id": POTION, "quantity": 1})
check("the owner's own name, in any case, is the owner's own grant",
      r.status_code == 200 and "inventory" in (r.get_json() or {}) and len(bag(boss_id, 0)) >= len(boss_bag),
      (r.status_code, r.get_json()))
r = client.post("/api/staff/grant", headers=bearer(boss), json={"username": "boss", "item_id": POTION})
check("  which still needs a slot", r.status_code == 400, (r.status_code, msg(r)))
r = give(boss, "nobody_here", POTION)
check("an unknown name is a 404", r.status_code == 404 and msg(r) == "No such account.", (r.status_code, msg(r)))
kim = register("kim")
r = give(boss, "kim", POTION)
check("an account with no character is a 404 that says so",
      r.status_code == 404 and "no character" in msg(r), (r.status_code, msg(r)))


# =============================================================================
section("GV-5  a gift can be taken back, and is logged about the player")
# =============================================================================
lena = register("lena")
make(lena, 0, "warrior")
lid = uid("lena")
put_cell(lid, 0, 0, POTION, 1)
r = give(boss, "lena", SWORD, 1)
before_give = [s for s in snapshots_of(lid, 0) if s["reason"] == "before-give"]
check("a snapshot is taken before the gift", len(before_give) == 1
      and json.loads(before_give[0]["snapshot"])["carry"] == [[0, POTION, 1]], [dict(s) for s in before_give])
line = raw("SELECT * FROM staff_actions WHERE action = 'give' AND target_name = 'lena'")
check("the gift is a 'give' line about lena, by the owner",
      len(line) == 1 and line[0]["actor_name"] == "boss" and line[0]["target_id"] == lid
      and SWORD in line[0]["detail"] and "warrior" in line[0]["detail"], [dict(x) for x in line])
r = rollback(boss, "lena", before_give[0]["id"] if before_give else 0)
check("rolling back to it takes the gift back", r.status_code == 200 and bag(lid, 0) == [(0, POTION, 1)], bag(lid, 0))
own = raw("SELECT action FROM staff_actions WHERE target_name = 'boss' AND action = 'grant'")
check("the owner's grants to themselves are still 'grant', filed with the testing tools",
      len(own) >= 1 and "grant" not in app_module.STAFF_ACTION_GROUPS["moderation"])


# =============================================================================
section("GV-6  a trade's result wins the flag, and the gift list is short")
# =============================================================================
moe = register("moe")
make(moe, 0, "warrior")
mid = uid("moe")
raw("UPDATE saves SET resync_trade = 'tradetoken123' WHERE user_id = ?", (mid,))
give(boss, "moe", POTION, 1)
check("a trade's result still waiting is not overwritten by a gift",
      one("SELECT resync_trade FROM saves WHERE user_id = ?", (mid,)) == "tradetoken123")
seen = (poll(moe).get_json() or {}).get("trade_resync")
check("  and the bag it delivers holds the gift anyway",
      isinstance(seen, dict) and {"item_id": POTION, "quantity": 1} in seen.get("inventory", []), seen)
for n in range(app_module.GIFTS_KEPT + 3):
    give(boss, "moe", POTION, 1)
marker = one("SELECT resync_trade FROM saves WHERE user_id = ?", (mid,))
check("gifts pile up in one flag, the newest %d of them" % app_module.GIFTS_KEPT,
      str(marker).startswith(app_module.GIFT_MARK)
      and len(json.loads(marker[len(app_module.GIFT_MARK):])) == app_module.GIFTS_KEPT, marker)
check("a garbled flag reads as no gifts, not a crash", app_module._gifts_in("gift:{nope") == []
      and app_module._gifts_in("gift:{}") == [] and app_module._gifts_in(None) == [])
check("the supply balanced at the end", supply()["balanced"], supply())


print("\n" + "=" * 70)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failed:")
    for f in failures:
        print("    - %s" % f)
print("=" * 70)
sys.exit(1 if failed else 0)
