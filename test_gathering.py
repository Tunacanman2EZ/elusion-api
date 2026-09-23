# test_gathering.py - fishing and cooking, the two newest endpoints that
# CREATE and DESTROY items.
#
# WHY THIS FILE EXISTS. Every other item-moving route in the app carries ten to
# sixteen assertions across the other suites. `/api/fishing/catch` and
# `/api/cooking/cook` carried ZERO - while being the two endpoints that mint a
# new item out of nothing and consume one permanently. They are also the pair
# the README points at as the worked example of server authority, which is the
# worst possible thing to have untested.
#
# WHAT IT ASSERTS. Not "does it return 200". The question from CLAUDE.md's wire
# rule: **could a malicious client profit by lying in this request?** So every
# section below sends the lie a modified client would send, and checks the
# server decides rather than believes.
#
# Run it the same way as the others:
#   .\venv\Scripts\python.exe test_gathering.py

import os
import sys
import json
import sqlite3
import tempfile

DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_gathering_test.db")
os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "gatherowner"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as app_module                                    # noqa: E402
import gamedata                                             # noqa: E402

client = app_module.app.test_client()

passed = 0
failed = 0


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print("  pass  %s" % label)
    else:
        failed += 1
        print("  FAIL  %s   %s" % (label, detail))


def section(title):
    print("\n=== %s ===\n" % title)


def register(username, password="password123"):
    r = client.post("/api/auth/register", json={"username": username, "password": password})
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def make_char(headers, slot=0):
    return client.put("/api/save", headers=headers,
                      json={"slot": slot, "class_id": "warrior", "name": "Anglr"})


def carried(headers, slot=0):
    inv = client.get("/api/character?slot=%d" % slot, headers=headers).get_json()["inventory"]
    totals = {}
    for cell in inv:
        if cell:
            totals[cell["item_id"]] = totals.get(cell["item_id"], 0) + cell["quantity"]
    return totals


def skill(headers, name, slot=0):
    s = client.get("/api/character?slot=%d" % slot, headers=headers).get_json()["skills"]
    return s.get(name, {"level": 1, "xp": 0})


def grant(username, entries, slot=0):
    """Write carry_items directly - standing in for a real server grant."""
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (uid, slot))
    conn.executemany(
        "INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
        [(uid, slot, i, iid, q) for i, (iid, q) in enumerate(entries)],
    )
    conn.commit()
    conn.close()


def set_skill(username, name, level, slot=0):
    """Set a skill server-side. Deliberately NOT through the API: fishing and
    cooking are server-owned, so the API refuses to set them - which is the
    point of the test further down."""
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute(
        "INSERT INTO skills (user_id, slot, skill_id, level, xp) VALUES (?,?,?,?,0) "
        "ON CONFLICT(user_id, slot, skill_id) DO UPDATE SET level = excluded.level",
        (uid, slot, name, level),
    )
    conn.commit()
    conn.close()


def refill_casts(username, slot=0):
    """Top the cast bucket back up. The rate limit is asserted in its own
    section; everywhere else it is noise, and a test that has to sleep 2.5
    seconds per cast is a test nobody runs."""
    conn = sqlite3.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("UPDATE saves SET cast_tokens = ? WHERE user_id = ? AND slot = ?",
                 (app_module.CAST_BUCKET_CAPACITY, uid, slot))
    conn.commit()
    conn.close()


def cast(headers, slot=0):
    return client.post("/api/fishing/catch", headers=headers, json={"slot": slot})


def cook(headers, item_id, slot=0):
    return client.post("/api/cooking/cook", headers=headers,
                       json={"slot": slot, "item_id": item_id})


BAIT = app_module.FISHING_BAIT_ID
ITEMS = {i["item_id"]: i for i in gamedata.ITEMS.values()} if hasattr(gamedata, "ITEMS") \
        else {i["item_id"]: i for i in json.load(open(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "gamedata.json")))["items"]}
FISH = {k: v for k, v in ITEMS.items() if v.get("type_name") == "FISH"}


section("SETUP")
angler = register("angler")
make_char(angler)
check("bait id matches the catalogue", BAIT in ITEMS, BAIT)
check("there are fish to catch", len(FISH) > 0, len(FISH))


section("FISHING  -  THE GATES")

# NO ROD. The client also checks this and shows the message early, but that copy
# is a courtesy; a modified client simply skips it.
grant("angler", [(BAIT, 10)])
r = cast(angler)
check("no rod -> 403", r.status_code == 403, (r.status_code, r.get_json()))
check("and no bait was consumed", carried(angler).get(BAIT) == 10, carried(angler))

# ROD BUT NO BAIT.
grant("angler", [("ironfishingrod", 1)])
r = cast(angler)
check("rod but no bait -> 403", r.status_code == 403, (r.status_code, r.get_json()))
check("the rod is not consumed", carried(angler).get("ironfishingrod") == 1, carried(angler))

# BOTH.
grant("angler", [("ironfishingrod", 1), (BAIT, 5)])
refill_casts("angler")
r = cast(angler)
check("rod and bait -> 200", r.status_code == 200, (r.status_code, r.get_json()))
body = r.get_json()
check("the server names the fish, the client never asked", "item_id" in body, body.keys())
check("what it returned is a real FISH item", body["item_id"] in FISH, body["item_id"])
check("exactly one bait was spent", carried(angler).get(BAIT) == 4, carried(angler))
check("the fish is in the backpack", carried(angler).get(body["item_id"], 0) >= 1, carried(angler))
check("the rod survives the cast", carried(angler).get("ironfishingrod") == 1, carried(angler))


section("FISHING  -  XP IS GRANTED BY THE SERVER, NOT CLAIMED")

before = skill(angler, "fishing")
refill_casts("angler")
r = cast(angler)
after = skill(angler, "fishing")
gained = (after["level"] - before["level"]) * 100000 + (after["xp"] - before["xp"])
check("a catch moves the fishing skill", after != before, (before, after))
check("the XP awarded matches the fish's own value",
      r.get_json()["xp"] == FISH[r.get_json()["item_id"]]["fishing_xp"],
      (r.get_json()["xp"], r.get_json()["item_id"]))

# THE CLIENT CANNOT WRITE ANY SKILL BACK NOW. A normal client still syncs all
# six on save; if that overwrote fishing, every grant would survive until the
# next routine save and no further. With the E-2 close, defense/agility/magic
# joined fishing/cooking/attack as server-owned, so the sync drops ALL of them -
# fishing here, and defense right beside it.
level_now = skill(angler, "fishing")["level"]
client.put("/api/character/skills", headers=angler,
           json={"slot": 0, "skills": {"fishing": {"level": 99, "xp": 0},
                                       "defense": {"level": 5, "xp": 0}}})
check("a client claiming fishing 99 is ignored",
      skill(angler, "fishing")["level"] == level_now, skill(angler, "fishing"))
check("and a claim of defense - now server-owned too - is dropped, not synced",
      skill(angler, "defense")["level"] == 1, skill(angler, "defense"))


section("FISHING  -  THE ROD TIER IS A CEILING, NOT A SUGGESTION")

# A tier-1 rod at level 1 must not land a tier-8 fish. Rolled many times rather
# than once: this is a probabilistic endpoint and one sample proves nothing.
grant("angler", [("ironfishingrod", 1), (BAIT, 60)])
set_skill("angler", "fishing", 1)
tiers = []
for _ in range(40):
    refill_casts("angler")
    r = cast(angler)
    if r.status_code == 200:
        tiers.append(ITEMS[r.get_json()["item_id"]]["tier"])
        grant("angler", [("ironfishingrod", 1), (BAIT, 60)])   # keep space and bait
ceiling = gamedata.fish_ceiling(1, 1)
check("40 casts on a tier-1 rod produced catches", len(tiers) > 0, len(tiers))
check("none of them exceeded the rod's ceiling",
      all(t <= ceiling for t in tiers), (sorted(set(tiers)), "ceiling %d" % ceiling))


section("FISHING  -  THE CAST RATE LIMIT")

grant("angler", [("ironfishingrod", 1), (BAIT, 99)])
refill_casts("angler")
codes = []
for _ in range(int(app_module.CAST_BUCKET_CAPACITY) + 3):
    codes.append(cast(angler).status_code)
    grant("angler", [("ironfishingrod", 1), (BAIT, 99)])
check("a full bucket allows a burst", codes[0] == 200, codes)
check("and the bucket runs dry", 429 in codes, codes)
check("a drained bucket refuses rather than erroring",
      all(c in (200, 429) for c in codes), codes)


section("COOKING  -  YOU CANNOT COOK WHAT YOU DO NOT HAVE")

chef = register("chef")
make_char(chef)
grant("chef", [])
r = cook(chef, "rawmudfish")
check("cooking an item you are not carrying -> 404", r.status_code == 404,
      (r.status_code, r.get_json()))

grant("chef", [("ironsword", 1)])
r = cook(chef, "ironsword")
# 409, NOT 400, AND THAT IS RIGHT. The request is well-formed and the item is
# genuinely held - what fails is the state, not the syntax. It matches the shape
# fishing already uses for "there is nothing to catch here".
check("cooking something with no recipe -> 409", r.status_code == 409,
      (r.status_code, r.get_json()))
check("and it is not consumed", carried(chef).get("ironsword") == 1, carried(chef))

r = cook(chef, "notarealitem")
check("cooking an invented item id -> 404", r.status_code == 404, r.status_code)


section("COOKING  -  THE LEVEL GATE")

# rawsilverfin needs cooking 20. A level-1 chef must be refused - and this is
# the check a modified client would most like to skip, since the reward scales
# with the fish.
grant("chef", [("rawsilverfin", 1)])
set_skill("chef", "cooking", 1)
r = cook(chef, "rawsilverfin")
check("a fish above your cooking level -> 403", r.status_code == 403,
      (r.status_code, r.get_json()))
check("the fish is not consumed by a refusal",
      carried(chef).get("rawsilverfin") == 1, carried(chef))

set_skill("chef", "cooking", ITEMS["rawsilverfin"]["cook_level"])
r = cook(chef, "rawsilverfin")
check("at exactly the required level it is allowed", r.status_code == 200,
      (r.status_code, r.get_json()))


section("COOKING  -  THE TRADE IS REAL BOTH WAYS")

# Mastery means it can never burn, which makes the success path deterministic
# and therefore assertable.
mud = ITEMS["rawmudfish"]
set_skill("chef", "cooking", mud["cook_mastery_level"])
grant("chef", [("rawmudfish", 3)])
before = skill(chef, "cooking")
r = cook(chef, "rawmudfish")
body = r.get_json()
check("cooking at mastery -> 200", r.status_code == 200, body)
check("and never burns at mastery", body["burnt"] is False, body)
check("one raw fish was consumed", carried(chef).get("rawmudfish") == 2, carried(chef))
check("the cooked fish appeared", carried(chef).get(mud["cooks_into"]) == 1, carried(chef))
check("the cooked item is the one the recipe names",
      mud["cooks_into"] in carried(chef), carried(chef))
check("cooking XP was granted", skill(chef, "cooking") != before,
      (before, skill(chef, "cooking")))
check("the XP matches the fish's cook_xp", body["xp"] == mud["cook_xp"], (body["xp"], mud["cook_xp"]))

# THE OTHER HALF OF THE BARGAIN: burning costs the fish and pays nothing. At the
# unlock level the burn chance is at its worst, so this is rolled until one
# burns rather than assumed.
set_skill("chef", "cooking", mud["cook_level"])
burnt_seen = False
for _ in range(120):
    grant("chef", [("rawmudfish", 1)])
    r = cook(chef, "rawmudfish")
    if r.status_code == 200 and r.get_json()["burnt"]:
        burnt_seen = True
        check("a burn consumes the raw fish", carried(chef).get("rawmudfish") is None, carried(chef))
        check("a burn produces no cooked fish",
              carried(chef).get(mud["cooks_into"]) is None, carried(chef))
        check("a burn grants no XP", r.get_json()["xp"] == 0, r.get_json())
        break
check("burning happens at the unlock level", burnt_seen,
      "120 cooks at cook_level never burned - check burn_chance()")


section("COOKING  -  THE SKILL IS SERVER-OWNED TOO")

level_now = skill(chef, "cooking")["level"]
client.put("/api/character/skills", headers=chef,
           json={"slot": 0, "skills": {"cooking": {"level": 99, "xp": 0}}})
check("a client claiming cooking 99 is ignored",
      skill(chef, "cooking")["level"] == level_now, skill(chef, "cooking"))


section("BOTH  -  AUTH AND SLOT")

check("fishing without a token -> 401",
      client.post("/api/fishing/catch", json={"slot": 0}).status_code == 401)
check("cooking without a token -> 401",
      client.post("/api/cooking/cook", json={"slot": 0, "item_id": "rawmudfish"}).status_code == 401)
check("fishing an out-of-range slot -> 400", cast(angler, slot=99).status_code == 400)
check("fishing an empty slot -> 404", cast(angler, slot=3).status_code == 404)


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
sys.exit(1 if failed else 0)
