"""Security-hardening tests for the Elusion API.

Focused proof of the server-authority work recorded in SECURITY_NOTES.md:
  E-1  the backpack rejects items a regular client never earned
  E-2  skills are capped for regular clients
  E-4  debug is off unless ELUSION_DEBUG asks for it
  E-5  login locks out after repeated failures

Runs against a THROWAWAY database in the temp folder, exactly like test_api.py,
so it never touches elusion.db. Run: python test_security.py
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_security_test.db")

os.environ["ELUSION_DB"] = DB_PATH
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
# The owner is the one account that bypasses server authority (a mod+ can already
# grant itself anything), so the suite needs one to prove the bypass.
os.environ["ELUSION_OWNER"] = "boss"

sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("elusion_app", os.path.join(HERE, "app.py"))
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
client = app_module.app.test_client()

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


def register(username, password="password123"):
    r = client.post("/api/auth/register", json={"username": username, "password": password})
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def make_char(headers, slot=0, class_id="warrior", name="Hero"):
    return client.put("/api/save", headers=headers,
                      json={"slot": slot, "class_id": class_id, "name": name})


def carried(headers, slot=0):
    """item_id -> total quantity the server reports in the backpack."""
    inv = client.get("/api/character?slot=%d" % slot, headers=headers).get_json()["inventory"]
    totals = {}
    for cell in inv:
        if cell:
            totals[cell["item_id"]] = totals.get(cell["item_id"], 0) + cell["quantity"]
    return totals


def skill_level(headers, skill, slot=0):
    skills = client.get("/api/character?slot=%d" % slot, headers=headers).get_json()["skills"]
    return skills.get(skill, {}).get("level")


def grant_directly(username, slot, entries):
    """Write carry_items straight into the DB, standing in for a server grant
    (loot/take, bank withdraw, staff grant all write these rows the same way)."""
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (uid, slot))
    conn.executemany(
        "INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
        [(uid, slot, i, iid, q) for i, (iid, q) in enumerate(entries)],
    )
    conn.commit()
    conn.close()


def put_inventory(headers, cells, slot=0):
    return client.put("/api/character/inventory", headers=headers,
                      json={"slot": slot, "inventory": cells})


def put_skills(headers, skills, slot=0):
    return client.put("/api/character/skills", headers=headers,
                      json={"slot": slot, "skills": skills})


print("\n=== E-1  BACKPACK IS SERVER-AUTHORITATIVE OVER GAINS ===\n")

player = register("mallory")
make_char(player)

# The core exploit: claim items never granted. Accepted as a request (200) but
# trimmed to nothing, because the server recorded no such items.
r = put_inventory(player, [{"item_id": "embersword", "quantity": 99}])
check("a fabricated backpack is accepted as a request", r.status_code == 200, r.status_code)
check("but the fabricated items are trimmed to nothing", carried(player) == {}, carried(player))

# A server-granted item survives a sync unchanged.
grant_directly("mallory", 0, [("ironsword", 1)])
put_inventory(player, [{"item_id": "ironsword", "quantity": 1}])
check("a server-granted item survives the sync", carried(player) == {"ironsword": 1}, carried(player))

# The granted item stays; fabricated excess alongside it is trimmed.
put_inventory(player, [{"item_id": "ironsword", "quantity": 1},
                       {"item_id": "embersword", "quantity": 50}])
check("granted item kept, fabricated excess trimmed",
      carried(player) == {"ironsword": 1}, carried(player))

# Claiming MORE of a granted item than was granted is trimmed to the granted amount.
put_inventory(player, [{"item_id": "ironsword", "quantity": 99}])
check("over-claiming a granted item clamps to the granted amount",
      carried(player) == {"ironsword": 1}, carried(player))

# Reductions and drops are always honest and pass through.
put_inventory(player, [])
check("dropping/using items (a reduction) is allowed", carried(player) == {}, carried(player))

# Rearranging a granted item to another cell is not a gain.
grant_directly("mallory", 0, [("smallhealthpotion", 5)])
put_inventory(player, [None, None, {"item_id": "smallhealthpotion", "quantity": 5}])
check("rearranging a granted item is not a gain", carried(player) == {"smallhealthpotion": 5},
      carried(player))


print("\n=== E-1  STAFF BYPASS (a mod+ can already self-grant) ===\n")

owner = register("boss")            # matches ELUSION_OWNER
make_char(owner)
put_inventory(owner, [{"item_id": "embersword", "quantity": 99}])
check("the owner may set an arbitrary backpack", carried(owner) == {"embersword": 99},
      carried(owner))


print("\n=== E-2  SKILLS ARE CAPPED FOR A REGULAR CLIENT ===\n")

r = put_skills(player, {"attack": {"level": 999, "xp": 0}})
check("an over-cap skill is accepted as a request", r.status_code == 200, r.status_code)
check("but the level is clamped to the ceiling", skill_level(player, "attack") == app_module.MAX_SKILL_LEVEL,
      skill_level(player, "attack"))

put_skills(player, {"attack": {"level": 40, "xp": 0}})
check("a legitimate level is stored unchanged", skill_level(player, "attack") == 40,
      skill_level(player, "attack"))

put_skills(owner, {"attack": {"level": 999, "xp": 0}})
check("the owner bypasses the skill cap", skill_level(owner, "attack") == 999,
      skill_level(owner, "attack"))


print("\n=== E-5  LOGIN LOCKS OUT AFTER REPEATED FAILURES ===\n")

register("lockme", "correct-horse")
for i in range(app_module.LOGIN_MAX_ATTEMPTS):
    r = client.post("/api/auth/login", json={"username": "lockme", "password": "wrongpass123"})
    check("failed attempt %d returns 401" % (i + 1), r.status_code == 401, r.status_code)

# Now locked: even the CORRECT password is refused with 429 until the cooldown.
r = client.post("/api/auth/login", json={"username": "lockme", "password": "correct-horse"})
check("after the cap, even the correct password is locked out (429)", r.status_code == 429,
      r.status_code)

# A different account is unaffected - the lockout is per-account.
r = client.post("/api/auth/login", json={"username": "mallory", "password": "password123"})
check("a different account is not affected by the lockout", r.status_code == 200, r.status_code)

# A success within the cap resets the streak (so honest fat-fingering never locks).
register("resets", "rightpass")
for _ in range(app_module.LOGIN_MAX_ATTEMPTS - 1):
    client.post("/api/auth/login", json={"username": "resets", "password": "nopenopenope"})
r = client.post("/api/auth/login", json={"username": "resets", "password": "rightpass"})
check("a correct password just under the cap succeeds", r.status_code == 200, r.status_code)
for _ in range(app_module.LOGIN_MAX_ATTEMPTS - 1):
    client.post("/api/auth/login", json={"username": "resets", "password": "nopenopenope"})
r = client.post("/api/auth/login", json={"username": "resets", "password": "rightpass"})
check("and the streak reset, so it is not locked", r.status_code == 200, r.status_code)


print("\n=== E-4  DEBUG DEFAULTS OFF ===\n")
# app.run(debug=...) can't be observed through the test client, but the flag it
# reads can: with ELUSION_DEBUG unset, the resolved value must be falsey.
_dbg = os.environ.get("ELUSION_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
check("debug is off when ELUSION_DEBUG is unset", _dbg is False, _dbg)


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("  failing: " + ", ".join(failures))
print("=" * 60)
sys.exit(1 if failed else 0)
