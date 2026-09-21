"""
Equipping is a MOVE now, not a reference. Run: python3 test_equipmove.py

WHAT THIS GUARDS. Equipment used to be a pointer into the backpack, and the
bag - which IS reconciled against what the server granted - carried the
ownership check for free. Moving the item out of the bag removes that, so the
move had to become a server endpoint: taking from the bag IS the ownership
check, and the client never asserts the result.

The checks below are mostly about what must NOT be possible.
"""
import gc, importlib.util, json, os, sqlite3, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Temp directory, not beside this file - see the note in test_healing.py. A
# scratch database written into the project folder is a file the virus scanner
# takes a handle on, and on Windows an open handle means it cannot be deleted.
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_equipmove_test.db")
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

def account(u):
    client.post("/api/auth/register", json={"username": u, "password": "hunter2hunter2"})
    tok = client.post("/api/auth/login", json={"username": u, "password": "hunter2hunter2"}).get_json()["token"]
    return {"Authorization": "Bearer " + tok}

def give(u, item_id, qty=1, slot=0, position=None):
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (u,)).fetchone()[0]
    if position is None:
        used = [r[0] for r in conn.execute(
            "SELECT position FROM carry_items WHERE user_id=? AND slot=?", (uid, slot))]
        position = next(i for i in range(200) if i not in used)
    conn.execute("INSERT INTO carry_items (user_id, slot, position, item_id, quantity)"
                 " VALUES (?,?,?,?,?)", (uid, slot, position, item_id, qty))
    conn.commit(); conn.close()

def bag(h, slot=0):
    cells = client.get("/api/character?slot=%d" % slot, headers=h).get_json().get("inventory", [])
    return [c["item_id"] for c in cells if c]

def worn(h, slot=0):
    return client.get("/api/character?slot=%d" % slot, headers=h).get_json().get("equipment", {})

def equip(h, item_id, slot=0):
    return client.post("/api/character/equip", headers=h, json={"slot": slot, "item_id": item_id})
def unequip(h, es, slot=0):
    return client.post("/api/character/unequip", headers=h, json={"slot": slot, "equip_slot": es})

# a low-level chest piece any warrior can wear
GEAR = sorted(
    (i["item_id"] for i in gamedata.ITEMS.values()
     if gamedata.equip_slot_for(i["item_id"]) == "chest"
     and int(i.get("required_level", 1)) <= 1
     and ("warrior" in (i.get("required_classes") or []) or not i.get("required_classes"))),
)
CHEST = GEAR[0] if GEAR else ""

section("Setup")
H = account("mover")
client.put("/api/save", headers=H, json={"slot": 0, "class_id": "warrior", "name": "Mover"})
check("a wearable chest piece exists to test with", CHEST != "", GEAR)

section("Equipping moves the item out of the bag")
give("mover", CHEST)
check("it starts in the bag", CHEST in bag(H))
r = equip(H, CHEST)
check("equip -> 200", r.status_code == 200, r.data[:160])
check("it is worn", worn(H).get("chest") == CHEST, worn(H))
check("and it is NO LONGER in the bag", CHEST not in bag(H), bag(H))

section("Unequipping puts it back")
r = unequip(H, "chest")
check("unequip -> 200", r.status_code == 200, r.data[:160])
check("the slot is empty", worn(H).get("chest") in (None, ""), worn(H))
check("and it is back in the bag", CHEST in bag(H), bag(H))

section("You cannot equip what you do not hold")
ghost = account("ghost")
client.put("/api/save", headers=ghost, json={"slot": 0, "class_id": "warrior", "name": "Ghost"})
r = equip(ghost, CHEST)
check("equipping an item you were never given -> 404", r.status_code == 404, r.status_code)
check("and nothing is worn", worn(ghost) == {}, worn(ghost))

section("Refusals")
give("mover", "tinyhealthpotion", 1)
r = equip(H, "tinyhealthpotion")
check("a potion is not equipment -> 403", r.status_code == 403, r.status_code)
check("the potion is still in the bag", "tinyhealthpotion" in bag(H), bag(H))
r = equip(H, "nosuchitem")
check("an unknown id -> 400", r.status_code == 400, r.status_code)
r = unequip(H, "chest")
check("unequipping an empty slot -> 404", r.status_code == 404, r.status_code)
r = unequip(H, "hat")
check("an unknown equip_slot -> 400", r.status_code == 400, r.status_code)

section("A full bag has nowhere to put what comes off")
CAP = app_module.INVENTORY_CAPACITY
full = account("packrat")
client.put("/api/save", headers=full, json={"slot": 0, "class_id": "warrior", "name": "Packrat"})
give("packrat", CHEST)
equip(full, CHEST)
for i in range(CAP):
    give("packrat", "tinyhealthpotion", 1, position=i)
r = unequip(full, "chest")
check("unequipping into a full bag -> 409", r.status_code == 409, r.status_code)
check("and it is still worn, not lost", worn(full).get("chest") == CHEST, worn(full))

section("/api/save may no longer dress a character")

give("mover", CHEST)
equip(H, CHEST)
before = worn(H)
check("something is worn to begin with", before.get("chest") == CHEST, before)

# A CLIENT ASSERTING ITS OWN EQUIPMENT. The endpoints take the item out of the
# bag and that take is the ownership check - all of which is worth nothing if a
# save can simply declare what is worn.
r = client.put("/api/save", headers=H, json={
    "slot": 0, "class_id": "warrior", "name": "Mover",
    "equipment": {"weapon": "embersword", "helm": "embersword"}})
check("the save still succeeds", r.status_code == 200, r.status_code)
check("and says the field was ignored",
      "equipment" in (r.get_json().get("ignored") or []), r.get_json())
check("the fabricated gear was NOT stored", worn(H) == before, worn(H))

# THE REGRESSION THAT NEARLY SHIPPED: forcing equipment to {} while still
# telling the upsert the caller supplied it writes an empty map over the
# character's gear on every save - the same stripping bug the endpoints exist
# to fix, reintroduced by the fix.
r = client.put("/api/save", headers=H, json={"slot": 0, "class_id": "warrior", "name": "Mover"})
check("a save that mentions no equipment leaves it alone",
      worn(H).get("chest") == CHEST, worn(H))

# Verdict first, housekeeping after, and housekeeping cannot change the verdict
# - see the longer note at the end of test_healing.py, which is where deleting
# the scratch file before printing the result cost a green run.
print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)

gc.collect()
try:
    os.unlink(DB_PATH)
except OSError as exc:
    print("  note: could not remove %s (%s)" % (DB_PATH, exc))

raise SystemExit(1 if failed else 0)
