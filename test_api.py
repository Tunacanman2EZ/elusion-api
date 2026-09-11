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
# BANK - ITEMS
# =============================================================================

section("BANK - ITEMS")

body = status("empty bank", client.get("/api/bank?slot=0", headers=H), 200)
check("empty bank has no items", body["items"] == [], body)

status("deposit 5 potions", client.post("/api/bank", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "healthpotion", "quantity": 5}), 200)
body = status("deposit 7 more of the same", client.post("/api/bank", headers=H,
              json={"slot": 0, "op": "deposit", "item_id": "healthpotion", "quantity": 7}), 200)
check("same item merged into ONE row", len(body["items"]) == 1, body["items"])
check("quantities summed to 12", body["items"][0]["quantity"] == 12, body["items"])

status("withdraw more than stored", client.post("/api/bank", headers=H,
       json={"slot": 0, "op": "withdraw", "item_id": "healthpotion", "quantity": 99}), 400)
body = status("bank unchanged after refusal", client.get("/api/bank?slot=0", headers=H), 200)
check("still 12 after the refused withdrawal", body["items"][0]["quantity"] == 12, body["items"])

body = status("withdraw the exact stack", client.post("/api/bank", headers=H,
              json={"slot": 0, "op": "withdraw", "item_id": "healthpotion", "quantity": 12}), 200)
check("emptied stack is deleted, not left at zero", body["items"] == [], body["items"])

status("quantity zero", client.post("/api/bank", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "x", "quantity": 0}), 400)
status("quantity boolean", client.post("/api/bank", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "x", "quantity": True}), 400)
status("unknown op", client.post("/api/bank", headers=H,
       json={"slot": 0, "op": "steal", "item_id": "x", "quantity": 1}), 400)
status("empty item_id", client.post("/api/bank", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "", "quantity": 1}), 400)


# =============================================================================
# BANK - GOLD
# =============================================================================

section("BANK - GOLD")

body = status("bank reports both sides", client.get("/api/bank?slot=0", headers=H), 200)
TOTAL = body["gold"] + body["carried_gold"]
check("carried gold matches the status endpoint", body["carried_gold"] == 500, body)

status("deposit more than carried", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "deposit", "amount": 900}), 400)
body = status("bank unchanged after refusal", client.get("/api/bank?slot=0", headers=H), 200)
check("total conserved after refusal", body["gold"] + body["carried_gold"] == TOTAL, body)

body = status("deposit 300", client.post("/api/bank/gold", headers=H,
              json={"slot": 0, "op": "deposit", "amount": 300}), 200)
check("banked 300", body["gold"] == 300, body)
check("carried dropped to 200", body["carried_gold"] == 200, body)
check("total conserved across deposit", body["gold"] + body["carried_gold"] == TOTAL, body)

status("withdraw more than banked", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "withdraw", "amount": 999}), 400)
body = status("withdraw 100", client.post("/api/bank/gold", headers=H,
              json={"slot": 0, "op": "withdraw", "amount": 100}), 200)
check("total conserved across withdrawal", body["gold"] + body["carried_gold"] == TOTAL, body)

body = status("status endpoint agrees with bank", client.get("/api/player/status?slot=0", headers=H), 200)
check("status gold == bank carried_gold", body["gold"] == 300, body)

status("amount zero", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "deposit", "amount": 0}), 400)
status("amount negative", client.post("/api/bank/gold", headers=H,
       json={"slot": 0, "op": "deposit", "amount": -50}), 400)
status("gold move on empty slot", client.post("/api/bank/gold", headers=H,
       json={"slot": 2, "op": "deposit", "amount": 1}), 404)


# =============================================================================
# ACCOUNT ISOLATION
# =============================================================================

section("ACCOUNT ISOLATION")

other = client.post("/api/auth/register",
                    json={"username": "stranger", "password": "password123"}).get_json()
H2 = {"Authorization": "Bearer " + other["token"]}

body = status("stranger sees no saves", client.get("/api/save", headers=H2), 200)
check("stranger's save list is empty", body["slots"] == [], body)

body = status("stranger sees an empty bank", client.get("/api/bank?slot=0", headers=H2), 200)
check("stranger sees no items", body["items"] == [], body)
check("stranger sees no gold", body["gold"] == 0 and body["carried_gold"] == 0, body)

status("stranger cannot read our status", client.get("/api/player/status?slot=0", headers=H2), 404)
status("stranger cannot write our status", client.put("/api/player/status", headers=H2,
       json={"slot": 0, "gold": 999999}), 404)

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
