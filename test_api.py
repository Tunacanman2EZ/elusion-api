"""
test_api.py - end-to-end checks for the Elusion API.

    venv\\Scripts\\python.exe test_api.py

Runs every endpoint against a THROWAWAY database in your temp folder. It never
touches elusion.db, so it is safe to run against a live install.

WHY THIS FILE EXISTS: nearly every bug found in this project so far was in code
that was written correctly and then never exercised against the case it
existed for - a pet-drop gate that could not open, a save sanitiser that
rewrote every field, melee hitboxes on the wrong physics layer, a UI layer that
was never added to its own group. Validation code is the easiest thing in the
world to write and the easiest thing in the world to never test. So the checks
below deliberately spend most of their effort on the failure paths: the 400s,
the 401s, the 404s, and the arithmetic that must not drift.

Exits 0 if everything passes, 1 if anything fails.
"""

import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_test.db")

# Point the app at a scratch database BEFORE importing it - app.py calls
# init_db() at import time, and we do not want that touching the real one.
os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()


# =============================================================================
# TINY TEST HARNESS
# =============================================================================

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


def status(label, response, expected):
    body = response.get_json()
    check(
        "%s -> %d" % (label, expected),
        response.status_code == expected,
        "got %d: %s" % (response.status_code, json.dumps(body)[:120]),
    )
    return body


def section(title):
    print("\n" + title)
    print("-" * len(title))


# =============================================================================
# AUTH
# =============================================================================

section("AUTH")

body = status("register", client.post("/api/auth/register",
              json={"username": "checker", "password": "password123"}), 201)
TOKEN = body["token"]
H = {"Authorization": "Bearer " + TOKEN}

check("token is 43 chars (token_urlsafe(32))", len(TOKEN) == 43, "got %d" % len(TOKEN))
check("register does not echo a password hash",
      not any("hash" in k for k in body), json.dumps(body)[:120])

status("register duplicate username", client.post("/api/auth/register",
       json={"username": "checker", "password": "password123"}), 409)
status("register short password", client.post("/api/auth/register",
       json={"username": "checker2", "password": "short"}), 400)
status("register bad username chars", client.post("/api/auth/register",
       json={"username": "bad name!", "password": "password123"}), 400)

wrong = status("login wrong password", client.post("/api/auth/login",
               json={"username": "checker", "password": "wrongpassword"}), 401)
missing = status("login unknown username", client.post("/api/auth/login",
                 json={"username": "nobodyhere", "password": "password123"}), 401)
check("wrong password and unknown user are INDISTINGUISHABLE",
      wrong == missing, "%s vs %s" % (wrong, missing))

status("session with good token", client.get("/api/auth/session", headers=H), 200)
status("session with no token", client.get("/api/auth/session"), 401)
status("session with junk token", client.get("/api/auth/session",
       headers={"Authorization": "Bearer nonsense"}), 401)
status("session with token but no Bearer prefix", client.get("/api/auth/session",
       headers={"Authorization": TOKEN}), 401)


# =============================================================================
# SAVE
# =============================================================================

section("SAVE")

body = status("empty account", client.get("/api/save", headers=H), 200)
check("empty account returns an empty list, not padding", body["slots"] == [], body)

status("write slot 0", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker", "level": 12}), 200)
status("write slot 0 again (upsert)", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker", "level": 13}), 200)

body = status("list after two writes to one slot", client.get("/api/save", headers=H), 200)
check("upsert did not create a duplicate row", len(body["slots"]) == 1, body["slots"])
check("upsert kept the newer level", body["slots"][0]["level"] == 13, body["slots"][0])

status("write slot 9 (out of range)", client.put("/api/save", headers=H,
       json={"slot": 9, "class_id": "mage", "name": "X"}), 400)
status("write unknown class", client.put("/api/save", headers=H,
       json={"slot": 1, "class_id": "necromancer", "name": "X"}), 400)
status("write empty name", client.put("/api/save", headers=H,
       json={"slot": 1, "class_id": "mage", "name": "   "}), 400)
status("write with no token", client.put("/api/save",
       json={"slot": 1, "class_id": "mage", "name": "X"}), 401)


# --- active_pet_id ----------------------------------------------------------
# A pet is a 1-in-216 drop, so "which pet is out" is the single most expensive
# field in this table to lose. These checks exist because the obvious
# implementation - treat a missing key as an empty value - would silently
# unequip it on the next save from any caller that doesn't send it.

check("a fresh slot has no pet", body["slots"][0]["active_pet_id"] == "",
      body["slots"][0])

status("equip a pet", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker", "level": 13,
             "active_pet_id": "petpoisonslimesmall"}), 200)
body = status("read back the equipped pet", client.get("/api/save", headers=H), 200)
check("the pet was stored",
      body["slots"][0]["active_pet_id"] == "petpoisonslimesmall", body["slots"][0])

status("save WITHOUT active_pet_id", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker", "level": 14}), 200)
body = status("read back after a pet-less save", client.get("/api/save", headers=H), 200)
check("omitting the key LEAVES the pet equipped",
      body["slots"][0]["active_pet_id"] == "petpoisonslimesmall", body["slots"][0])
check("the rest of that save still applied",
      body["slots"][0]["level"] == 14, body["slots"][0])

status("swap pets", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker", "level": 14,
             "active_pet_id": "petpoisonslimelarge"}), 200)
body = status("read back after a swap", client.get("/api/save", headers=H), 200)
check("swapping replaces rather than accumulating",
      body["slots"][0]["active_pet_id"] == "petpoisonslimelarge", body["slots"][0])

status("clear the pet with an empty string", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker", "level": 14,
             "active_pet_id": ""}), 200)
body = status("read back after clearing", client.get("/api/save", headers=H), 200)
check("an explicit empty string DOES unequip",
      body["slots"][0]["active_pet_id"] == "", body["slots"][0])

status("pet id over 64 characters", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker",
             "active_pet_id": "p" * 65}), 400)
status("pet id that isn't a string", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker",
             "active_pet_id": {"nope": 1}}), 400)

# The server has no item registry, so an unknown pet id is accepted on purpose
# - see the schema comment. This check pins that decision down rather than
# leaving it to be "fixed" later by someone who assumes it was an oversight.
status("unknown pet id is accepted by design", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Checker",
             "active_pet_id": "petfromafutureupdate"}), 200)


# =============================================================================
# PLAYER STATUS
# =============================================================================

section("PLAYER STATUS")

body = status("read slot 0", client.get("/api/player/status?slot=0", headers=H), 200)
check("fresh character starts at schema defaults",
      body["hp"] == 10 and body["max_hp"] == 10 and body["gold"] == 0, body)

status("read empty slot", client.get("/api/player/status?slot=2", headers=H), 404)
status("read bad slot", client.get("/api/player/status?slot=9", headers=H), 400)
status("read missing slot param", client.get("/api/player/status", headers=H), 400)

body = status("partial write: gold only", client.put("/api/player/status", headers=H,
              json={"slot": 0, "gold": 500}), 200)
check("partial write left hp untouched", body["hp"] == 10, body)
check("partial write applied gold", body["gold"] == 500, body)

status("hp above stored max_hp", client.put("/api/player/status", headers=H,
       json={"slot": 0, "hp": 50}), 400)
body = status("hp and max_hp raised together", client.put("/api/player/status", headers=H,
              json={"slot": 0, "hp": 50, "max_hp": 120}), 200)
check("both applied", body["hp"] == 50 and body["max_hp"] == 120, body)

body = status("same pair, keys reversed", client.put("/api/player/status", headers=H,
              json={"max_hp": 200, "hp": 150, "slot": 0}), 200)
check("key order does not change legality", body["hp"] == 150 and body["max_hp"] == 200, body)

status("lower max_hp below current hp", client.put("/api/player/status", headers=H,
       json={"slot": 0, "max_hp": 10}), 400)
status("negative value", client.put("/api/player/status", headers=H,
       json={"slot": 0, "gold": -1}), 400)
status("boolean instead of int", client.put("/api/player/status", headers=H,
       json={"slot": 0, "gold": True}), 400)
status("value past the ceiling", client.put("/api/player/status", headers=H,
       json={"slot": 0, "gold": 10 ** 12}), 400)
status("only unknown fields", client.put("/api/player/status", headers=H,
       json={"slot": 0, "is_admin": 1}), 400)
status("write to empty slot", client.put("/api/player/status", headers=H,
       json={"slot": 2, "gold": 1}), 404)

body = status("rejections left state intact", client.get("/api/player/status?slot=0", headers=H), 200)
check("gold still 500 after four refused writes", body["gold"] == 500, body)
check("is_admin was never writable via status", "is_admin" not in body, body)


# =============================================================================
# ACCOUNT - BANK (SHARED ACROSS CHARACTERS)
# =============================================================================

section("ACCOUNT - BANK")

body = status("empty account", client.get("/api/account", headers=H), 200)
check("bank starts empty", all(c is None for c in body["bank_inventory"]), body["bank_inventory"][:3])
check("lusions start at 0", body["lusions"] == 0, body)
check("bank gold starts at 0", body["bank_gold"] == 0, body)
CAPACITY = body["capacity"]

body = status("write a positional bank", client.put("/api/account/bank", headers=H,
              json={"bank_inventory": [
                  {"item_id": "ironsword", "quantity": 1},
                  None,
                  {"item_id": "smallhealthpotion", "quantity": 12},
              ]}), 200)
bank = body["bank_inventory"]
check("comes back at full capacity", len(bank) == CAPACITY, len(bank))
check("empty cells stay empty", bank[1] is None, bank[:3])
check("an item stays in the cell it was put in",
      bank[2] is not None and bank[2]["quantity"] == 12, bank[2])

# The bank used to be keyed on (user_id, slot, item_id), which merged stacks
# and hid a warrior's deposits from a mage. Both halves of that are tested here.
body = status("two stacks of one item", client.put("/api/account/bank", headers=H,
              json={"bank_inventory": [
                  {"item_id": "smallhealthpotion", "quantity": 4},
                  {"item_id": "smallhealthpotion", "quantity": 6},
              ]}), 200)
bank = body["bank_inventory"]
check("are NOT merged", bank[0]["quantity"] == 4 and bank[1]["quantity"] == 6, bank[:2])

status("more entries than capacity", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": [None] * (CAPACITY + 1)}), 400)
status("quantity zero", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": [{"item_id": "ironsword", "quantity": 0}]}), 400)
status("quantity boolean", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": [{"item_id": "ironsword", "quantity": True}]}), 400)
status("empty item_id", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": [{"item_id": "", "quantity": 1}]}), 400)

body = status("bank is unchanged by every refusal", client.get("/api/account", headers=H), 200)
check("still the two unmerged stacks",
      body["bank_inventory"][0]["quantity"] == 4, body["bank_inventory"][:2])

# THE POINT OF THE REWRITE: the bank is reached without naming a character, so
# there is no slot for it to be trapped in.
body = status("a SECOND character", client.put("/api/save", headers=H,
              json={"slot": 1, "class_id": "mage", "name": "Second", "level": 1}), 200)
body = status("reads the same bank", client.get("/api/account", headers=H), 200)
check("what one character banked, another can see",
      body["bank_inventory"][0]["quantity"] == 4, body["bank_inventory"][:2])


# =============================================================================
# ACCOUNT - LUSIONS
# =============================================================================

section("ACCOUNT - LUSIONS")

body = status("set lusions", client.put("/api/account/lusions", headers=H, json={"lusions": 40}), 200)
check("stored", body["lusions"] == 40, body)
status("negative lusions", client.put("/api/account/lusions", headers=H, json={"lusions": -1}), 400)
status("lusions as a string", client.put("/api/account/lusions", headers=H, json={"lusions": "lots"}), 400)
body = status("unchanged after refusals", client.get("/api/account", headers=H), 200)
check("still 40", body["lusions"] == 40, body)


# =============================================================================
# BANK - GOLD
# =============================================================================

section("BANK - GOLD")

body = status("account and carried gold", client.get("/api/player/status?slot=0", headers=H), 200)
CARRIED = body["gold"]
check("carried gold matches the status endpoint", CARRIED == 500, body)
TOTAL = CARRIED + status("account", client.get("/api/account", headers=H), 200)["bank_gold"]

status("deposit more than carried", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "deposit", "amount": 900}), 400)
body = status("unchanged after refusal", client.get("/api/account", headers=H), 200)
check("total conserved after refusal",
      body["bank_gold"] + client.get("/api/player/status?slot=0", headers=H).get_json()["gold"] == TOTAL, body)

body = status("deposit 300", client.post("/api/bank/gold", headers=H,
              json={"slot": 0, "op": "deposit", "amount": 300}), 200)
check("banked 300", body["bank_gold"] == 300, body)
check("carried dropped to 200", body["carried_gold"] == 200, body)
check("total conserved across deposit", body["bank_gold"] + body["carried_gold"] == TOTAL, body)

status("withdraw more than banked", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "withdraw", "amount": 999}), 400)
body = status("withdraw 100", client.post("/api/bank/gold", headers=H,
              json={"slot": 0, "op": "withdraw", "amount": 100}), 200)
check("total conserved across withdrawal", body["bank_gold"] + body["carried_gold"] == TOTAL, body)

# Gold banked by one character is spendable by another - the same property the
# item bank has, and the reason bank_gold moved off the saves row.
body = status("the second character's pocket", client.post("/api/bank/gold", headers=H,
              json={"slot": 1, "op": "withdraw", "amount": 200}), 200)
check("one character can withdraw what another banked",
      body["carried_gold"] == 200 and body["bank_gold"] == 0, body)

body = status("status endpoint agrees", client.get("/api/player/status?slot=0", headers=H), 200)
check("status gold == what the transfer reported", body["gold"] == 300, body)

status("amount zero", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "deposit", "amount": 0}), 400)
status("amount negative", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "deposit", "amount": -50}), 400)
status("unknown op", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "steal", "amount": 1}), 400)
status("gold move on empty slot", client.post("/api/bank/gold", headers=H,
       json={"slot": 2, "op": "deposit", "amount": 1}), 404)


# =============================================================================
# CHARACTER - BACKPACK
# =============================================================================

section("CHARACTER - BACKPACK")

body = status("write a positional inventory", client.put("/api/character/inventory", headers=H,
              json={"slot": 0, "inventory": [
                  {"item_id": "ironsword", "quantity": 1},
                  None,
                  None,
                  {"item_id": "smallhealthpotion", "quantity": 7},
              ]}), 200)
inv = body["inventory"]
check("comes back as 20 positional cells", len(inv) == 20, len(inv))
check("empty cells stay empty", inv[1] is None and inv[2] is None, inv[:4])
check("an item stays in the cell it was put in",
      inv[3] is not None and inv[3]["item_id"] == "smallhealthpotion", inv[3])

# The reason carry_items is keyed on position rather than item_id. Keying on
# item_id would merge these two stacks and reshuffle the bag on every login.
body = status("the same item in two cells", client.put("/api/character/inventory", headers=H,
              json={"slot": 0, "inventory": [
                  {"item_id": "smallhealthpotion", "quantity": 2},
                  {"item_id": "smallhealthpotion", "quantity": 3},
              ]}), 200)
inv = body["inventory"]
check("two stacks of one item are NOT merged",
      inv[0]["quantity"] == 2 and inv[1]["quantity"] == 3, inv[:2])

body = status("an empty array", client.put("/api/character/inventory", headers=H,
              json={"slot": 0, "inventory": []}), 200)
check("clears the bag rather than leaving it", all(c is None for c in body["inventory"]))

status("more entries than capacity", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [None] * 21}), 400)
status("quantity 0", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "ironsword", "quantity": 0}]}), 400)
status("a cell that is not an object", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": ["ironsword"]}), 400)

# The whole array is validated before any of it is written. A bad entry at the
# end must not leave the good entries before it stored - a half-saved bag with
# no error is worse than a refused write.
client.put("/api/character/inventory", headers=H,
           json={"slot": 0, "inventory": [{"item_id": "ironsword", "quantity": 1}]})
status("a write with a bad entry LATE in the array", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [
           {"item_id": "bushamulet", "quantity": 1},
           {"item_id": "", "quantity": 1},
       ]}), 400)
body = status("read it back", client.get("/api/character?slot=0", headers=H), 200)
check("a rejected write changed NOTHING",
      body["inventory"][0]["item_id"] == "ironsword", body["inventory"][0])


# =============================================================================
# CHARACTER - SKILLS
# =============================================================================

section("CHARACTER - SKILLS")

body = status("write skills", client.put("/api/character/skills", headers=H,
              json={"slot": 0, "skills": {
                  "attack": {"level": 12, "xp": 340},
                  "magic":  {"level": 3,  "xp": 9},
              }}), 200)
check("skills round-trip", body["skills"]["attack"]["level"] == 12, body["skills"])

status("an unknown skill", client.put("/api/character/skills", headers=H,
       json={"slot": 0, "skills": {"swimming": {"level": 1, "xp": 0}}}), 400)
status("skill level 0", client.put("/api/character/skills", headers=H,
       json={"slot": 0, "skills": {"attack": {"level": 0, "xp": 0}}}), 400)
status("skills that are not an object", client.put("/api/character/skills", headers=H,
       json={"slot": 0, "skills": []}), 400)


# =============================================================================
# CHARACTER - ONE-CALL READ
# =============================================================================

section("CHARACTER - ONE-CALL READ")

body = status("GET /api/character", client.get("/api/character?slot=0", headers=H), 200)
check("carries identity, status, backpack and skills together",
      all(k in body for k in ("class_id", "name", "status", "inventory", "skills")),
      sorted(body))
status("an empty slot", client.get("/api/character?slot=2", headers=H), 404)
status("no token", client.get("/api/character?slot=0"), 401)


# =============================================================================
# ACCOUNT ISOLATION
# =============================================================================

section("ACCOUNT ISOLATION")

other = client.post("/api/auth/register",
                    json={"username": "stranger", "password": "password123"}).get_json()
H2 = {"Authorization": "Bearer " + other["token"]}

body = status("stranger sees no saves", client.get("/api/save", headers=H2), 200)
check("stranger's save list is empty", body["slots"] == [], body)

body = status("stranger sees an empty account", client.get("/api/account", headers=H2), 200)
check("stranger sees no bank items", all(c is None for c in body["bank_inventory"]), body["bank_inventory"][:3])
check("stranger sees no bank gold", body["bank_gold"] == 0, body)
check("stranger sees no lusions", body["lusions"] == 0, body)
status("stranger cannot write our bank", client.put("/api/account/bank", headers=H2,
       json={"bank_inventory": [{"item_id": "ironsword", "quantity": 99}]}), 200)
body = status("our bank is untouched", client.get("/api/account", headers=H), 200)
check("the stranger wrote to THEIR bank, not ours",
      body["bank_inventory"][0]["quantity"] == 4, body["bank_inventory"][:2])

status("stranger cannot read our status", client.get("/api/player/status?slot=0", headers=H2), 404)
status("stranger cannot write our status", client.put("/api/player/status", headers=H2,
       json={"slot": 0, "gold": 999999}), 404)
status("stranger cannot read our character", client.get("/api/character?slot=0", headers=H2), 404)
status("stranger cannot write our backpack", client.put("/api/character/inventory", headers=H2,
       json={"slot": 0, "inventory": [{"item_id": "ironsword", "quantity": 99}]}), 404)
status("stranger cannot write our skills", client.put("/api/character/skills", headers=H2,
       json={"slot": 0, "skills": {"attack": {"level": 99, "xp": 0}}}), 404)

body = status("our gold is untouched", client.get("/api/player/status?slot=0", headers=H), 200)
check("still 300 after the stranger's attempts", body["gold"] == 300, body)


# =============================================================================
# LOGOUT
# =============================================================================

section("LOGOUT")

status("logout", client.post("/api/auth/logout", headers=H2), 204)
status("token is dead afterwards", client.get("/api/auth/session", headers=H2), 401)
status("logging out twice", client.post("/api/auth/logout", headers=H2), 401)


# =============================================================================
# MIGRATION - AN EXISTING DATABASE
# =============================================================================
#
# THE SECTION THAT WAS MISSING, AND THE BUG IT WOULD HAVE CAUGHT.
#
# Everything above runs against a database created from scratch, where every
# CREATE TABLE statement actually fires. A real elusion.db is not that. When
# bank_items was re-keyed from (user_id, slot, item_id) to (user_id, position),
# CREATE TABLE IF NOT EXISTS did nothing to the table already sitting there, and
# every bank request failed with "no such column: position" - while this suite
# reported 151 passed.
#
# So this builds a database in the OLD shape, boots the app against it, and
# checks the migration actually ran.

section("MIGRATION - AN EXISTING DATABASE")

LEGACY_DB = os.path.join(tempfile.gettempdir(), "elusion_legacy_test.db")
if os.path.exists(LEGACY_DB):
    os.remove(LEGACY_DB)

import sqlite3 as _sqlite3

_legacy = _sqlite3.connect(LEGACY_DB)
_legacy.executescript(
    """
    CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL);
    CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL);
    CREATE TABLE saves (user_id INTEGER NOT NULL, slot INTEGER NOT NULL, class_id TEXT NOT NULL, name TEXT NOT NULL,
                        level INTEGER NOT NULL DEFAULT 1, area TEXT NOT NULL DEFAULT 'elusion',
                        hp INTEGER NOT NULL DEFAULT 10, max_hp INTEGER NOT NULL DEFAULT 10,
                        mana INTEGER NOT NULL DEFAULT 10, max_mana INTEGER NOT NULL DEFAULT 10,
                        stamina INTEGER NOT NULL DEFAULT 10, max_stamina INTEGER NOT NULL DEFAULT 10,
                        gold INTEGER NOT NULL DEFAULT 0, xp INTEGER NOT NULL DEFAULT 0,
                        xp_to_next INTEGER NOT NULL DEFAULT 100, bank_gold INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (user_id, slot));
    CREATE TABLE bank_items (user_id INTEGER NOT NULL, slot INTEGER NOT NULL, item_id TEXT NOT NULL,
                             quantity INTEGER NOT NULL CHECK (quantity > 0), PRIMARY KEY (user_id, slot, item_id));
    """
)
_legacy.execute("INSERT INTO users VALUES (1,'legacyuser','x',0,0)")
_legacy.execute("INSERT INTO saves (user_id,slot,class_id,name,bank_gold) VALUES (1,0,'warrior','Warrior',400)")
_legacy.execute("INSERT INTO saves (user_id,slot,class_id,name,bank_gold) VALUES (1,1,'mage','Mage',150)")
# The same item banked by two characters - two rows under the old key, one
# stack under the new one.
_legacy.execute("INSERT INTO bank_items VALUES (1,0,'smallhealthpotion',6)")
_legacy.execute("INSERT INTO bank_items VALUES (1,1,'smallhealthpotion',4)")
_legacy.execute("INSERT INTO bank_items VALUES (1,0,'ironsword',1)")
_legacy.commit()
_legacy.close()

os.environ["ELUSION_DB"] = LEGACY_DB
_spec = importlib.util.spec_from_file_location("elusion_legacy", os.path.join(HERE, "app.py"))
_migrated = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_migrated)
    check("an existing database boots without error", True)
except Exception as exc:
    check("an existing database boots without error", False, repr(exc))

_db = _sqlite3.connect(LEGACY_DB)
_columns = [row[1] for row in _db.execute("PRAGMA table_info(bank_items)")]
check("bank_items was re-keyed on position", "position" in _columns and "slot" not in _columns, _columns)

_rows = _db.execute("SELECT position, item_id, quantity FROM bank_items ORDER BY position").fetchall()
check("two characters' stacks of one item merged", len(_rows) == 2, _rows)
_potion = [r for r in _rows if r[1] == "smallhealthpotion"]
check("nothing was lost in the merge (6 + 4 = 10)",
      _potion and _potion[0][2] == 10, _rows)

_account = _db.execute("SELECT lusions, bank_gold FROM accounts WHERE user_id = 1").fetchone()
check("banked gold pooled onto the account (400 + 150)", _account and _account[1] == 550, _account)
check("the old per-character column was zeroed",
      _db.execute("SELECT SUM(bank_gold) FROM saves").fetchone()[0] == 0)
check("the legacy table was dropped",
      not _db.execute("SELECT 1 FROM sqlite_master WHERE name='bank_items_legacy'").fetchall())
_db.close()

# Booting twice must not double the gold or duplicate the rows - every server
# restart runs init_db() again.
_spec2 = importlib.util.spec_from_file_location("elusion_legacy2", os.path.join(HERE, "app.py"))
_again = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_again)
_db = _sqlite3.connect(LEGACY_DB)
check("migrating twice does not double the gold",
      _db.execute("SELECT bank_gold FROM accounts").fetchone()[0] == 550)
check("migrating twice does not duplicate rows",
      _db.execute("SELECT COUNT(*) FROM bank_items").fetchone()[0] == 2)
_db.close()

os.environ["ELUSION_DB"] = DB_PATH


# =============================================================================
# RESULT
# =============================================================================

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("\n  failing checks:")
    for name in failures:
        print("    - " + name)
print("=" * 60)

os.remove(DB_PATH)
sys.exit(1 if failed else 0)
