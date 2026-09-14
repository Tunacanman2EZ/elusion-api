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
import time

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
       json={"slot": 0, "class_id": "warrior", "name": "Checker"}), 200)
status("write slot 0 again (upsert)", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Renamed"}), 200)

body = status("list after two writes to one slot", client.get("/api/save", headers=H), 200)
check("upsert did not create a duplicate row", len(body["slots"]) == 1, body["slots"])
check("upsert applied the newer name", body["slots"][0]["name"] == "Renamed", body["slots"][0])

# CHANGED: this used to push a level through /api/save and assert it stuck.
# level is server-owned now - see SERVER-OWNED STATS below - so a new character
# starts at 1 and only a kill moves it.
check("a new character starts at level 1", body["slots"][0]["level"] == 1, body["slots"][0])

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
       json={"slot": 0, "class_id": "warrior", "name": "Petless"}), 200)
body = status("read back after a pet-less save", client.get("/api/save", headers=H), 200)
check("omitting the key LEAVES the pet equipped",
      body["slots"][0]["active_pet_id"] == "petpoisonslimesmall", body["slots"][0])
check("the rest of that save still applied",
      body["slots"][0]["name"] == "Petless", body["slots"][0])

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
# CHANGED: this used to assert the schema defaults of 10. A new character is
# now seeded from its class curve at creation - a level 1 warrior has 180 hp,
# full. The old expectation was only ever right because nothing computed the
# real value, and the client would have overwritten the 10 on its first save.
check("a new warrior starts at its class maximum, full",
      body["hp"] == 180 and body["max_hp"] == 180, body)
check("and with no gold", body["gold"] == 0, body)

status("read empty slot", client.get("/api/player/status?slot=2", headers=H), 404)
status("read bad slot", client.get("/api/player/status?slot=9", headers=H), 400)
status("read missing slot param", client.get("/api/player/status", headers=H), 400)

body = status("partial write: gold only", client.put("/api/player/status", headers=H,
              json={"slot": 0, "gold": 500}), 200)
check("partial write left hp untouched", body["hp"] == 180, body)
check("partial write applied gold", body["gold"] == 500, body)

# REWRITTEN. These used to raise max_hp and then hp underneath it, and assert
# that mismatched pairs returned 400. max_hp is derived now - see DERIVED STATS
# below - so it cannot be raised at all, and hp is clamped to the real ceiling
# rather than refused. Refusing would make a character unsaveable after a
# downward rebalance of its class curve.
body = status("hp below the class maximum", client.put("/api/player/status", headers=H,
              json={"slot": 0, "hp": 50}), 200)
check("applies as sent", body["hp"] == 50, body)

body = status("hp above it, with a forged max_hp", client.put("/api/player/status", headers=H,
              json={"slot": 0, "hp": 999, "max_hp": 999}), 200)
check("clamped to the curve's answer, not the client's",
      body["hp"] == 180 and body["max_hp"] == 180, body)

body = status("same pair, keys reversed", client.put("/api/player/status", headers=H,
              json={"max_hp": 999, "hp": 999, "slot": 0}), 200)
check("key order does not change the outcome",
      body["hp"] == 180 and body["max_hp"] == 180, body)
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
# SERVER-OWNED STATS
# =============================================================================
#
# The half that makes the loot work mean anything. Moving the roll to the server
# closed "give myself a pet"; while /api/player/status accepted a level, a
# modified client could just declare itself level 60 and skip the game.

section("SERVER-OWNED STATS")

body = status("level, xp and xp_to_next are ignored, not refused",
              client.put("/api/player/status", headers=H,
                         json={"slot": 0, "level": 60, "xp": 999999, "xp_to_next": 1, "hp": 40}), 200)
check("the request still succeeds", body["level"] != 60, body)
check("the server reports what it disregarded",
      sorted(body.get("ignored", [])) == ["level", "xp", "xp_to_next"], body.get("ignored"))
check("a field the client DOES own still wrote", body["hp"] == 40, body)

body = status("read it back", client.get("/api/player/status?slot=0", headers=H), 200)
check("level did not move", body["level"] != 60, body)
check("xp did not move", body["xp"] != 999999, body)

# The same door on the other route. /api/save carried a level too.
status("PUT /api/save with a level", client.put("/api/save", headers=H,
       json={"slot": 0, "class_id": "warrior", "name": "Cheat", "level": 99}), 200)
body = status("read it back", client.get("/api/player/status?slot=0", headers=H), 200)
check("creating or updating a save cannot set a level", body["level"] != 99, body)

# A kill is the ONLY way level and xp move.
before = status("before a kill", client.get("/api/player/status?slot=0", headers=H), 200)
body = status("report a kill", client.post("/api/combat/kill", headers=H,
              json={"slot": 0, "enemy_id": "poisonslimesmall"}), 200)
check("the server grants xp itself", body["xp_gained"] > 0, body)
after = status("after a kill", client.get("/api/player/status?slot=0", headers=H), 200)
check("and only a kill moves it", after["xp"] != before["xp"] or after["level"] > before["level"],
      [before["xp"], after["xp"]])

# THE BURST A REAL FIGHT PRODUCES MUST GO THROUGH.
#
# The first version of this limit was a minimum 0.35s gap between kills, and it
# refused legitimate kills within an hour: a large poison slime splits into four
# smalls, and an area attack kills several enemies in the same frame. The player
# lost those rewards. A bucket allows the burst and still caps the sustained
# rate.
burst_ok = 0
for _ in range(10):
    if client.post("/api/combat/kill", headers=H,
                   json={"slot": 0, "enemy_id": "poisonslimesmall"}).status_code == 200:
        burst_ok += 1
check("ten kills in one frame all pay out", burst_ok == 10, burst_ok)

# ...but an unbounded flood does not. The bucket holds 50, and ten are already
# spent above.
flood_refused = 0
for _ in range(120):
    if client.post("/api/combat/kill", headers=H,
                   json={"slot": 0, "enemy_id": "poisonslimesmall"}).status_code == 429:
        flood_refused += 1
check("a flood past the bucket is refused", flood_refused > 0, flood_refused)

body = status("the refusal explains itself", client.post("/api/combat/kill", headers=H,
              json={"slot": 0, "enemy_id": "poisonslimesmall"}), 429)
check("and says it is a rate, not a malformed request",
      "faster than" in body.get("message", ""), body)

status("an unknown enemy", client.post("/api/combat/kill", headers=H,
       json={"slot": 0, "enemy_id": "dragon"}), 400)
status("an enemy that awards nothing", client.post("/api/combat/kill", headers=H,
       json={"slot": 0, "enemy_id": "poisonslimelarge"}), 400)
status("a kill on an empty slot", client.post("/api/combat/kill", headers=H,
       json={"slot": 2, "enemy_id": "poisonslimesmall"}), 404)


# =============================================================================
# DERIVED STATS
# =============================================================================
#
# max_hp was the last thing the client got to assert about a character. The
# curve lived as literals inside warrior.gd's _set_stat_curve(), so the server
# knew your level and your class and still could not work out your maximum
# health. ClassData moved it into data and the exporter carries it here.

section("DERIVED STATS")

# EXPECTED VALUES ARE COMPUTED, NOT HARDCODED.
#
# These asserted a flat 180 until the kill-burst test above started levelling
# the warrior up, and then failed - correctly. A magic number only tests the
# formula while nothing else in the suite moves the inputs. Deriving it here
# tests the rule instead: warrior is 180 base, +12 a level.
WARRIOR_HP_BASE, WARRIOR_HP_PER_LVL = 180, 12

def warrior_max_hp(level):
    return WARRIOR_HP_BASE + (level - 1) * WARRIOR_HP_PER_LVL

body = status("the warrior's maxima", client.get("/api/player/status?slot=0", headers=H), 200)
EXPECTED_HP = warrior_max_hp(body["level"])
check("max_hp comes from the curve, not the client",
      body["max_hp"] == EXPECTED_HP, [body["level"], body["max_hp"], EXPECTED_HP])

body = status("claiming a million health", client.put("/api/player/status", headers=H,
              json={"slot": 0, "max_hp": 999999, "max_mana": 999999, "max_stamina": 999999}), 200)
check("the request succeeds", "max_hp" in body, body)
check("max_hp is still the curve's answer", body["max_hp"] == EXPECTED_HP, body)
check("the server says it disregarded them",
      set(["max_hp", "max_mana", "max_stamina"]).issubset(set(body.get("ignored", []))),
      body.get("ignored"))

# The pairing check has to run against the SERVER's ceiling, not the client's -
# otherwise hp=999999 alongside max_hp=999999 passes on its own say-so.
body = status("hp above the real maximum", client.put("/api/player/status", headers=H,
              json={"slot": 0, "hp": 999999, "max_hp": 999999}), 200)
check("hp is clamped to the derived maximum", body["hp"] == EXPECTED_HP, body)

# Every class gets its own answer, and the curve is level-scaled.
status("a mage in slot 3", client.put("/api/save", headers=H,
       json={"slot": 3, "class_id": "mage", "name": "Mage"}), 200)
body = status("the mage's maxima", client.get("/api/player/status?slot=3", headers=H), 200)
check("a level 1 mage has 110 hp and 250 mana",
      body["max_hp"] == 110 and body["max_mana"] == 250, body)
check("and differs from the warrior", body["max_hp"] != 180, body)


# =============================================================================
# LOOT BAGS
# =============================================================================
#
# The last thing the client could assert. /api/combat/kill already decided what
# dropped, but the bag existed only in the client's world - it picked things up
# and the next save simply told the server what it was now carrying. Which is
# why `gold` sat on the client-asserted list.

section("LOOT BAGS")

# REFILL THE KILL BUCKET FIRST.
#
# The SERVER-OWNED STATS section above deliberately floods it to prove the limit
# works, and tokens refill at five a second in wall-clock time - which a test
# suite does not wait for. Without this every kill below returns 429, nothing
# drops, and the whole section fails for a reason that has nothing to do with
# loot.
#
# Reaching into the database rather than sleeping: the alternative is a ten
# second pause in a suite that otherwise runs instantly.
import sqlite3 as _sq
_conn = _sq.connect(DB_PATH)
_conn.execute("UPDATE saves SET kill_tokens = 50.0, last_kill_at = 0")
_conn.commit()
_conn.close()

# Kill until something actually drops. bag_drop_chance is 0.30, so this is
# overwhelmingly likely inside a few dozen attempts and deterministic enough not
# to make the suite flaky.
BAG_ID = ""
for _ in range(60):
    body = client.post("/api/combat/kill", headers=H,
                       json={"slot": 0, "enemy_id": "poisonslimesmall"}).get_json()
    if body and body.get("bag_id"):
        BAG_ID = body["bag_id"]
        break

check("a kill that drops something returns a bag id", BAG_ID != "", BAG_ID)

body = status("read the bag", client.get("/api/loot/bag?bag_id=" + BAG_ID, headers=H), 200)
# .get() rather than [] on purpose: if the request above failed, `body` is an
# error dict, and indexing it would crash the whole suite instead of reporting
# one failed check. A test file that dies halfway tells you far less than one
# that fails a line and keeps going.
contents = body.get("contents", [])
check("the bag has contents", len(contents) > 0, body)
FIRST = contents[0] if contents else {"position": 0, "item_id": "", "quantity": 0}

before = status("gold and backpack before", client.get("/api/player/status?slot=0", headers=H), 200)
GOLD_BEFORE = before["gold"]

body = status("take the first item", client.post("/api/loot/take", headers=H,
              json={"bag_id": BAG_ID, "position": FIRST["position"]}), 200)
check("it says what it did with it", body.get("credited") in ("gold", "lusions", "inventory"), body)

# Every bag contains gold, and gold resolves into the balance rather than a
# backpack cell - which is the whole point of this endpoint existing.
if body.get("credited") == "gold":
    check("gold went into the purse, not a bag slot",
          body["status"]["gold"] == GOLD_BEFORE + FIRST["quantity"],
          [GOLD_BEFORE, FIRST["quantity"], body["status"]["gold"]])
else:
    check("a non-currency item landed in the backpack",
          bool(body.get("carry_positions"))
          and body["inventory"][body["carry_positions"][0]]["item_id"] == FIRST["item_id"], body)

# THE DUPLICATION TEST. A client that sends the same take twice - lag, a double
# click, or deliberately - must not be paid twice.
status("take the same slot again", client.post("/api/loot/take", headers=H,
       json={"bag_id": BAG_ID, "position": FIRST["position"]}), 404)
after = status("gold after the repeat", client.get("/api/player/status?slot=0", headers=H), 200)
GOLD_AFTER_TAKE = body.get("status", {}).get("gold")
check("a repeated take credits nothing",
      GOLD_AFTER_TAKE is not None and after["gold"] == GOLD_AFTER_TAKE,
      [GOLD_AFTER_TAKE, after["gold"]])

status("take from a bag that does not exist", client.post("/api/loot/take", headers=H,
       json={"bag_id": "nosuchbag", "position": 0}), 404)
status("read a bag that does not exist", client.get("/api/loot/bag?bag_id=nosuchbag", headers=H), 404)
status("a position past the backpack", client.post("/api/loot/take", headers=H,
       json={"bag_id": BAG_ID, "position": 999}), 400)

# ANOTHER ACCOUNT MUST NOT BE ABLE TO REACH IT. The bag id is unguessable, but
# the query scopes on user_id anyway - "not yours" and "does not exist" return
# the same 404, so there is nothing to learn by asking.
stranger = client.post("/api/auth/register",
                       json={"username": "bagthief", "password": "password123"}).get_json()
H3 = {"Authorization": "Bearer " + stranger["token"]}
status("a stranger reading our bag", client.get("/api/loot/bag?bag_id=" + BAG_ID, headers=H3), 404)
status("a stranger taking from our bag", client.post("/api/loot/take", headers=H3,
       json={"bag_id": BAG_ID, "position": 0}), 404)


# =============================================================================
# LOOT BAGS - PLANTED
# =============================================================================
#
# Everything above rides on a real roll, which is the right way to prove the
# happy path works end to end and the wrong way to prove anything about a
# SPECIFIC item. A pet is a 1-in-1296 drop; waiting for one to test what happens
# when you already own it is not a test, it is a lottery ticket.
#
# So these plant a bag directly in the database and then go through the endpoint
# exactly like the client does. The roll is not what is under test here - the
# grant is.

section("LOOT BAGS - PLANTED")

# ON ITS OWN ACCOUNT, DELIBERATELY.
#
# These tests fill a backpack, bank a pet and collect lusions. Doing that to
# `checker` left the sections below asserting "bank starts empty" against a bank
# that had a slime in it - a failure that says nothing about the bank and
# everything about test order. An account costs one request.
body = status("an account just for these", client.post("/api/auth/register",
              json={"username": "bagtester", "password": "password123"}), 201)
H4 = {"Authorization": "Bearer " + body["token"]}
status("give it a character", client.put("/api/save", headers=H4,
       json={"slot": 0, "class_id": "warrior", "name": "Bagtester"}), 200)


def plant_bag(username, slot, entries):
    """Write a loot bag straight into the test database and return its id."""
    conn = _sq.connect(DB_PATH)
    conn.row_factory = _sq.Row
    user_id = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()["id"]

    bag_id = "planted" + os.urandom(6).hex()
    conn.execute(
        "INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at) VALUES (?,?,?,?,?)",
        (bag_id, user_id, slot, "poisonslimesmall", int(time.time())),
    )
    for position, (item_id, quantity) in enumerate(entries):
        conn.execute(
            "INSERT INTO loot_bag_items (bag_id, position, item_id, quantity) VALUES (?,?,?,?)",
            (bag_id, position, item_id, quantity),
        )
    conn.commit()
    conn.close()
    return bag_id


def clear_backpack(username, slot):
    conn = _sq.connect(DB_PATH)
    conn.row_factory = _sq.Row
    user_id = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()["id"]
    conn.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))
    conn.commit()
    conn.close()


# ---- STACKING ---------------------------------------------------------------
#
# THE REASON THIS MATTERS IS THE RESPONSE, NOT THE DATABASE.
#
# /api/loot/take hands `inventory` back as the authoritative layout and the
# client applies it. The server used to drop every pickup into the lowest free
# cell while InventoryContainer.add_stack_partial() merges onto a part-used
# stack - so the same two potions would land in two cells here and one there,
# and the player would watch their bag split on every loot.

clear_backpack("bagtester", 0)
BAG = plant_bag("bagtester", 0, [("smallhealthpotion", 5), ("smallhealthpotion", 5)])

body = status("take the first potion", client.post("/api/loot/take", headers=H4,
              json={"bag_id": BAG, "position": 0}), 200)
check("it went to the backpack", body.get("credited") == "inventory", body)
FIRST_CELL = body["carry_positions"][0]

body = status("take the second potion", client.post("/api/loot/take", headers=H4,
              json={"bag_id": BAG, "position": 1}), 200)
check("the second stack merged into the first cell",
      body["carry_positions"] == [FIRST_CELL], body)
check("the merged cell holds all ten",
      body["inventory"][FIRST_CELL] == {"item_id": "smallhealthpotion", "quantity": 10},
      body["inventory"][FIRST_CELL])
check("and nothing spilled into a second cell",
      sum(1 for cell in body["inventory"] if cell) == 1,
      [c for c in body["inventory"] if c])
check("emptying the bag says so", body.get("bag_empty") is True, body)

# A stack past max_stack has to overflow into another cell - 20 is
# smallhealthpotion's ceiling, so 10 held + 15 taken is 20 and 5.
BAG = plant_bag("bagtester", 0, [("smallhealthpotion", 15)])
body = status("take more than the stack ceiling allows", client.post("/api/loot/take", headers=H4,
              json={"bag_id": BAG, "position": 0}), 200)
check("it filled the open stack and opened one more",
      len(body["carry_positions"]) == 2, body.get("carry_positions"))
check("the ceiling was respected",
      sorted(cell["quantity"] for cell in body["inventory"] if cell) == [5, 20],
      [c for c in body["inventory"] if c])


# ---- A PET YOU ALREADY OWN --------------------------------------------------
#
# This decision used to live in lootbaginventory.gd, which checked the local
# inventory and the local bank to decide what a pet was worth. Both are views of
# rows this process owns, and a client deciding what it is owed is the thing the
# whole endpoint exists to stop.

clear_backpack("bagtester", 0)
BAG = plant_bag("bagtester", 0, [("petpoisonslimesmall", 1)])
body = status("a pet you do not own", client.post("/api/loot/take", headers=H4,
              json={"bag_id": BAG, "position": 0}), 200)
check("lands in the backpack as the pet",
      body.get("credited") == "inventory" and body.get("granted_item_id") == "petpoisonslimesmall",
      body)
check("and is not flagged as a duplicate", "duplicate_pet" not in body, body)

LUSIONS_BEFORE = status("lusions before the duplicate",
                        client.get("/api/account", headers=H4), 200)["lusions"]

BAG = plant_bag("bagtester", 0, [("petpoisonslimesmall", 1)])
body = status("the same pet again", client.post("/api/loot/take", headers=H4,
              json={"bag_id": BAG, "position": 0}), 200)
check("is recognised as a duplicate", body.get("duplicate_pet") is True, body)
check("and pays lusions instead", body.get("credited") == "lusions", body)
check("at the rate gamedata.json sets",
      body.get("granted_quantity") == app_module.gamedata.CONSTANTS["dupe_pet_lusions"],
      [body.get("granted_quantity"), app_module.gamedata.CONSTANTS["dupe_pet_lusions"]])
check("the balance came back with the response",
      body.get("lusions") == LUSIONS_BEFORE + body["granted_quantity"],
      [LUSIONS_BEFORE, body.get("lusions")])
check("it still reports what was IN the bag",
      body.get("item_id") == "petpoisonslimesmall", body)
check("the duplicate did not also take a backpack cell",
      sum(1 for cell in body["inventory"] if cell and cell["item_id"] == "petpoisonslimesmall") == 1,
      [c for c in body["inventory"] if c])

# THE BANK COUNTS AS OWNING IT. A pet is a collectible, and putting one in the
# bank is what a collection looks like - it must not start dropping again.
clear_backpack("bagtester", 0)
status("bank the pet", client.put("/api/account/bank", headers=H4,
       json={"bank_inventory": [{"item_id": "petpoisonslimesmall", "quantity": 1}]}), 200)
BAG = plant_bag("bagtester", 0, [("petpoisonslimesmall", 1)])
body = status("a pet held only in the bank", client.post("/api/loot/take", headers=H4,
              json={"bag_id": BAG, "position": 0}), 200)
check("still counts as owned", body.get("duplicate_pet") is True, body)


# ---- A FULL BACKPACK --------------------------------------------------------
#
# 409, and the item STAYS IN THE BAG. Refusing beats dropping it on the floor:
# the player can make room and ask again.

clear_backpack("bagtester", 0)
status("fill every cell", client.put("/api/character/inventory", headers=H4,
       json={"slot": 0, "inventory": [{"item_id": "ironsword", "quantity": 1}] * 20}), 200)

BAG = plant_bag("bagtester", 0, [("bushamulet", 1)])
status("take into a full backpack", client.post("/api/loot/take", headers=H4,
       json={"bag_id": BAG, "position": 0}), 409)

body = status("the bag still has it", client.get("/api/loot/bag?bag_id=" + BAG, headers=H4), 200)
check("refusing left the item exactly where it was",
      body["contents"] == [{"position": 0, "item_id": "bushamulet", "quantity": 1}], body)

inv = status("and the backpack was not touched",
             client.get("/api/character?slot=0", headers=H4), 200)
check("still twenty swords and nothing else",
      sum(1 for cell in inv["inventory"] if cell) == 20, inv)

# A stackable that PARTLY fits must not half-land either: 19 swords and one
# potion stack at 18 leaves room for 2, and asking for 5 has to be all or
# nothing.
clear_backpack("bagtester", 0)
status("nineteen swords and a part-used potion stack", client.put("/api/character/inventory", headers=H4,
       json={"slot": 0,
             "inventory": [{"item_id": "ironsword", "quantity": 1}] * 19
                          + [{"item_id": "smallhealthpotion", "quantity": 18}]}), 200)
BAG = plant_bag("bagtester", 0, [("smallhealthpotion", 5)])
status("a take that only partly fits", client.post("/api/loot/take", headers=H4,
       json={"bag_id": BAG, "position": 0}), 409)
inv = status("nothing partly landed",
             client.get("/api/character?slot=0", headers=H4), 200)
check("the part-used stack is still at 18",
      inv["inventory"][19] == {"item_id": "smallhealthpotion", "quantity": 18},
      inv["inventory"][19])


# ---- POSITIONS --------------------------------------------------------------
#
# The position is the grid cell on both sides, which is what lets the client
# send "take cell 2" and mean the thing the player is looking at. It is stamped
# by the server rather than inferred from array order on each side separately.

clear_backpack("bagtester", 0)
_conn = _sq.connect(DB_PATH)
_conn.execute("UPDATE saves SET kill_tokens = 50.0, last_kill_at = 0")
_conn.commit()
_conn.close()

for _ in range(60):
    body = client.post("/api/combat/kill", headers=H4,
                       json={"slot": 0, "enemy_id": "poisonslimesmall"}).get_json()
    if body and body.get("bag_id"):
        break

check("the kill response stamps a position on every entry",
      all("position" in entry for entry in body.get("contents", [])), body.get("contents"))
read_back = client.get("/api/loot/bag?bag_id=" + body["bag_id"], headers=H4).get_json()
check("and they are the positions the bag actually holds",
      [e["position"] for e in body["contents"]] == [e["position"] for e in read_back["contents"]],
      [body["contents"], read_back["contents"]])

status("a position past the bag's own grid", client.post("/api/loot/take", headers=H4,
       json={"bag_id": body["bag_id"], "position": app_module.LOOT_BAG_CAPACITY}), 400)


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

# BALANCES ARE READ, NOT ASSUMED.
#
# These used to assert a starting purse of exactly 500, which held only while
# nothing earlier in the suite touched it. The loot-bag section above now credits
# gold from a real drop, and the numbers moved - correctly. What actually matters
# here is that the TOTAL is conserved across every transfer, so that is what is
# asserted, against whatever the balance happens to be.
body = status("account and carried gold", client.get("/api/player/status?slot=0", headers=H), 200)
CARRIED = body["gold"]
check("the character is carrying something to bank", CARRIED >= 500, body)
TOTAL = CARRIED + status("account", client.get("/api/account", headers=H), 200)["bank_gold"]

status("deposit more than carried", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "deposit", "amount": CARRIED + 1}), 400)
body = status("unchanged after refusal", client.get("/api/account", headers=H), 200)
check("total conserved after refusal",
      body["bank_gold"] + client.get("/api/player/status?slot=0", headers=H).get_json()["gold"] == TOTAL, body)

body = status("deposit 300", client.post("/api/bank/gold", headers=H,
              json={"slot": 0, "op": "deposit", "amount": 300}), 200)
check("banked 300", body["bank_gold"] == 300, body)
check("carried dropped by exactly that", body["carried_gold"] == CARRIED - 300, body)
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

SLOT0_EXPECTED = CARRIED - 300 + 100
body = status("status endpoint agrees", client.get("/api/player/status?slot=0", headers=H), 200)
check("status gold == what the transfers add up to",
      body["gold"] == SLOT0_EXPECTED, [body["gold"], SLOT0_EXPECTED])

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

GOLD_BEFORE_STRANGER = client.get("/api/player/status?slot=0", headers=H).get_json()["gold"]

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

# Read the balance BEFORE the stranger tries anything, so this asserts that
# nothing changed rather than asserting a number that earlier sections move.
body = status("our gold is untouched", client.get("/api/player/status?slot=0", headers=H), 200)
check("unchanged by the stranger's attempts", body["gold"] == GOLD_BEFORE_STRANGER,
      [GOLD_BEFORE_STRANGER, body["gold"]])


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
