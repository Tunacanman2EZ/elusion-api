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

# Also BEFORE the import: OWNER_USERNAME is read at import time, the same way
# DB_PATH is. Deliberately a different case from the account registered below
# ("checker"), because users.username is COLLATE NOCASE and an owner check that
# disagreed with the database about case would lock the owner out.
os.environ["ELUSION_OWNER"] = "CHECKER"

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
# THE OWNER
# =============================================================================
#
# The owner is named by an environment variable, not stored in the database. If
# it were a column, whatever endpoint sets that column could set it, and the
# first bug in that endpoint would be a total compromise. It would also travel
# in a stolen backup.

section("THE OWNER")

import sqlite3 as _owner_sq

body = status("the owner's session", client.get("/api/auth/session", headers=H), 200)
check("the configured owner is flagged", body.get("is_owner") is True, body)
check("and their rank reads as owner", body.get("role") == "owner", body)

# The point of the previous check: they are the owner WITHOUT a stored rank.
# No UPDATE to this table can lock the owner out of their own server.
stored = _owner_sq.connect(DB_PATH).execute(
    "SELECT role FROM users WHERE username = 'checker'").fetchone()[0]
check("while users.role still says player", stored == "player", stored)

# THE KEY IS GONE, NOT FALSE. is_admin was a compatibility alias the Godot
# client used to read; there is no admin rank and now no key pretending there
# might be. A client reading body["is_admin"] should fail loudly rather than
# get False forever.
check("no is_admin key survives in a session", "is_admin" not in body, body)

body = status("someone else registers", client.post("/api/auth/register",
              json={"username": "notowner", "password": "password123"}), 201)
check("they are not the owner", body.get("is_owner") is False, body)
check("and register does not leak an is_admin key either",
      "is_admin" not in body, body)

# CASE-INSENSITIVE both ways. ELUSION_OWNER is "CHECKER", the account is
# "checker", and COLLATE NOCASE says those are one account.
check("owner match ignores case", app_module.is_owner("checker") is True, "checker")
check("and in the other direction", app_module.is_owner("CHECKER") is True, "CHECKER")
check("a different name is not the owner", app_module.is_owner("notowner") is False, "notowner")

# FAILS CLOSED. A server with no configured owner has no owner, rather than
# everyone being one - which is the failure that would actually matter.
_saved_owner = app_module.OWNER_USERNAME
app_module.OWNER_USERNAME = ""
check("no configured owner means nobody is the owner",
      app_module.is_owner("checker") is False, "unset")
check("and empty input matches nothing either",
      app_module.is_owner("") is False and app_module.is_owner(None) is False, "empty")
app_module.OWNER_USERNAME = _saved_owner
check("owner restored for the rest of the suite",
      app_module.is_owner("checker") is True, app_module.OWNER_USERNAME)


# =============================================================================
# RANKS
# =============================================================================
#
# player < mod < dev < owner, as an ordered column rather than a pile of
# booleans. Two booleans is four states; four is sixteen, and most of those are
# nonsense - someone who is a mod but not a player, a dev who is somehow not
# a mod.
#
# 'owner' is deliberately NOT storable. The CHECK on users.role refuses it and
# role_for() supplies it from the environment, so there is no write that
# produces the top rank.

section("RANKS")

body = status("a new account's rank", client.post("/api/auth/register",
              json={"username": "ranktest", "password": "password123"}), 201)
check("a fresh account is a player", body.get("role") == "player", body)
RANK_H = {"Authorization": "Bearer " + body["token"]}

check("the ordering is player < mod < dev < owner",
      app_module.ROLES == ("player", "mod", "dev", "owner"), app_module.ROLES)

def _set_rank(username, role):
    conn = _owner_sq.connect(DB_PATH)
    conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, username))
    conn.commit(); conn.close()

_set_rank("ranktest", "mod")
body = status("after promotion to mod", client.get("/api/auth/session", headers=RANK_H), 200)
check("the session reports mod", body.get("role") == "mod", body)
check("and not the owner", body.get("is_owner") is False, body)

_set_rank("ranktest", "dev")
body = status("after promotion to dev", client.get("/api/auth/session", headers=RANK_H), 200)
check("the session reports dev", body.get("role") == "dev", body)

# DEMOTION HAS TO WORK, which is the thing a boolean made awkward.
_set_rank("ranktest", "player")
body = status("after demotion", client.get("/api/auth/session", headers=RANK_H), 200)
check("rank dropped back to player", body.get("role") == "player", body)

# THE TOP RANK IS NOT STORABLE. The CHECK refuses it outright.
_rank_conn = _owner_sq.connect(DB_PATH)
try:
    _rank_conn.execute("UPDATE users SET role = 'owner' WHERE username = 'ranktest'")
    _rank_conn.commit()
    _refused = False
except Exception:
    _refused = True
_rank_conn.close()
check("the database refuses role = 'owner'", _refused is True, "CHECK constraint")

# An unrecognised value reads as the LEAST privilege, not the most. A row
# written by an older build, or by hand, must not fail open.
_set_rank("ranktest", "player")
_fake = {"username": "ranktest", "role": "superuser"}
check("an unknown rank reads as player",
      app_module.role_for(_fake) == "player", app_module.role_for(_fake))

# The owner outranks whatever the column says, in both directions.
check("the owner is owner regardless of the column",
      app_module.role_for({"username": "checker", "role": "player"}) == "owner",
      "checker is ELUSION_OWNER")
check("owner is at least dev",
      app_module.role_at_least({"username": "checker", "role": "player"}, "dev") is True, "")
check("an unknown minimum denies rather than raising",
      app_module.role_at_least({"username": "checker", "role": "player"}, "admin") is False,
      "removing a rank must not turn every surviving call into a 500")
check("a mod is at least mod",
      app_module.role_at_least({"username": "ranktest", "role": "mod"}, "mod") is True, "")
check("a mod is not at least admin",
      app_module.role_at_least({"username": "ranktest", "role": "mod"}, "admin") is False, "")
check("a player is not at least mod",
      app_module.role_at_least({"username": "ranktest", "role": "player"}, "mod") is False, "")

# The chain of command is owner -> dev -> mod -> player.
#
# THE WORD "admin" APPEARS BELOW ON PURPOSE AND SHOULD STAY. These are the
# checks that keep it from coming back: a rank nobody defined must read as the
# LOWEST privilege, and a requirement nobody defined must DENY rather than
# raise. When the admin rank was removed, every surviving role_at_least(...,
# "admin") call became a 500 until role_at_least was made to fail closed.
check("a dev outranks a mod",
      app_module.role_at_least({"username": "ranktest", "role": "dev"}, "mod") is True, "")
check("a mod does not outrank a dev",
      app_module.role_at_least({"username": "ranktest", "role": "mod"}, "dev") is False, "")
check("'admin' is not a rank and reads as player",
      app_module.role_for({"username": "ranktest", "role": "admin"}) == "player",
      app_module.role_for({"username": "ranktest", "role": "admin"}))
check("a dev is not the owner",
      app_module.role_for({"username": "ranktest", "role": "dev"}) == "dev", "")

_set_rank("ranktest", "dev")
body = status("a dev's session", client.get("/api/auth/session", headers=RANK_H), 200)
check("the session reports dev", body.get("role") == "dev", body)
check("and still no is_admin key at any rank", "is_admin" not in body, body)
_set_rank("ranktest", "player")

# THE CHECK CONSTRAINT IS NOT THE GUARANTEE, and this is the part that would
# otherwise be a fresh-database-only truth. A migrated users table gets its
# role column via ALTER and therefore carries no constraint, so 'owner' IS
# storable there. role_for() is what actually refuses it, on both shapes.
check("a column reading 'owner' still grants nothing",
      app_module.role_for({"username": "ranktest", "role": "owner"}) == "player",
      app_module.role_for({"username": "ranktest", "role": "owner"}))
check("and does not sneak past role_at_least either",
      app_module.role_at_least({"username": "ranktest", "role": "owner"}, "mod") is False, "")


# =============================================================================
# WHO MAY ACT ON WHOM
# =============================================================================
#
# One comparison - strictly above, not at-or-above - is the whole moderation
# hierarchy. Every case below is a consequence of it rather than a separate
# rule, which is the reason it is one comparison.
#
# The ban endpoint does not exist yet. The predicate does, and it is tested
# here so that whatever calls it later inherits a rule that was already right.

section("WHO MAY ACT ON WHOM")

def _u(name, role):
    return {"username": name, "role": role}

OWNER  = _u("checker", "player")   # checker is ELUSION_OWNER; the column is ignored
DEV    = _u("adev", "dev")
DEV2   = _u("anotherdev", "dev")
MOD    = _u("amod", "mod")
MOD2   = _u("anothermod", "mod")
PLAYER = _u("someone", "player")

act = app_module.can_act_on

check("a mod may not ban another mod", act(MOD, MOD2) is False, "equal ranks")
check("a mod may ban a player", act(MOD, PLAYER) is True, "")
check("a mod may not ban a dev", act(MOD, DEV) is False, "")
check("a mod may not ban the owner", act(MOD, OWNER) is False, "")

check("a dev may ban a mod", act(DEV, MOD) is True, "")
check("a dev may ban a player", act(DEV, PLAYER) is True, "")
check("a dev may not ban another dev", act(DEV, DEV2) is False, "equal ranks")
check("a dev may not ban the owner", act(DEV, OWNER) is False, "")

check("the owner may ban a dev", act(OWNER, DEV) is True, "")
check("the owner may ban a mod", act(OWNER, MOD) is True, "")
check("the owner may ban a player", act(OWNER, PLAYER) is True, "")

check("a player may not ban anyone", act(PLAYER, PLAYER) is False, "")
check("nor a mod", act(PLAYER, MOD) is False, "")

# Falls out of the same comparison rather than needing its own guard.
check("nobody may act on themselves", act(MOD, MOD) is False, "own rank is never below itself")
check("not even the owner", act(OWNER, OWNER) is False, "")

# The owner is decided before the column is read, in BOTH directions - so a
# row claiming a rank cannot be used to shield someone from the owner, or to
# reach past one.
check("the owner outranks a column claiming 'owner'",
      act(OWNER, _u("liar", "owner")) is True, "role_for reads it as player")
check("and a column claiming 'owner' reaches nobody",
      act(_u("liar", "owner"), MOD) is False, "")


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

# GOLD IS SERVER-OWNED NOW, AND THIS BLOCK USED TO PROVE THE OPPOSITE.
#
# It asserted that PUT /api/player/status applied a gold figure the client sent.
# It passed, and that was the bug: one request set any balance an authenticated
# player liked, and the supply invariant broke the instant it did. `gold` was in
# STATUS_FIELDS and had never been added to SERVER_OWNED_STATS, so PUT /api/save
# refused a client's gold while this endpoint took it.
#
# The partial-write half is still worth proving, so it is kept - on a field the
# client may legitimately write.
body = status("partial write: one field only", client.put("/api/player/status", headers=H,
              json={"slot": 0, "hp": 170}), 200)
check("partial write left gold untouched", body["gold"] == 0, body)
check("partial write applied hp", body["hp"] == 170, body)

body = status("a client pushing its own gold", client.put("/api/player/status", headers=H,
              json={"slot": 0, "gold": 1_000_000}), 200)
check("gold is ignored, not stored", body["gold"] == 0, body)
check("and the server names it as disregarded",
      "gold" in body.get("ignored", []), body)

# REWRITTEN. These used to raise max_hp and then hp underneath it, and assert
# that mismatched pairs returned 400. max_hp is derived now - see DERIVED STATS
# below - so it cannot be raised at all, and hp is clamped to the real ceiling
# rather than refused. Refusing would make a character unsaveable after a
# downward rebalance of its class curve.
body = status("hp below the class maximum", client.put("/api/player/status", headers=H,
              json={"slot": 0, "hp": 50}), 200)
check("applies as sent", body["hp"] == 50, body)

# TWO CEILINGS ON hp NOW, and this pair used to only know about one.
#
# max_hp is derived, so a forged one is replaced by the curve's answer. That is
# what these two checks are for and it is unchanged. What changed is that the
# heal reconciler is no longer in shadow mode: 50 -> 180 in no elapsed time is
# also a rise regeneration cannot explain, so hp lands at the TIGHTER of the
# two ceilings rather than at max_hp. Asserting 180 here would now be asserting
# that the heal clamp did not run.
body = status("hp above it, with a forged max_hp", client.put("/api/player/status", headers=H,
              json={"slot": 0, "hp": 999, "max_hp": 999}), 200)
check("max_hp is the curve's answer, not the client's", body["max_hp"] == 180, body)
# READS THE FLAG rather than hardcoding a mode. The heal reconciler enforces or
# only reports depending on HEAL_CLAMP_ENFORCED, and a suite that asserted one
# of those would break every time the flag moved - which it already has once,
# in both directions, inside an hour.
if app_module.HEAL_CLAMP_ENFORCED:
    check("and hp is bounded by both ceilings, landing at the tighter one",
          50 <= body["hp"] < 180, body)
else:
    check("and hp reaches max_hp, because the heal clamp only reports today",
          body["hp"] == 180, body)

body = status("same pair, keys reversed", client.put("/api/player/status", headers=H,
              json={"max_hp": 999, "hp": 999, "slot": 0}), 200)
check("key order does not change the outcome",
      body["max_hp"] == 180
      and (body["hp"] < 180 if app_module.HEAL_CLAMP_ENFORCED else body["hp"] == 180),
      body)
CLAMPED_HP = body["hp"]
# ON hp RATHER THAN gold, because a server-owned field is dropped BEFORE it is
# parsed - a malformed gold value would now return 200 and prove nothing about
# parse_stat(). These have to ride on a field the client may still write, or
# they quietly stop testing the validator.
status("negative value", client.put("/api/player/status", headers=H,
       json={"slot": 0, "hp": -1}), 400)
status("boolean instead of int", client.put("/api/player/status", headers=H,
       json={"slot": 0, "hp": True}), 400)
status("value past the ceiling", client.put("/api/player/status", headers=H,
       json={"slot": 0, "hp": 10 ** 12}), 400)
status("only unknown fields", client.put("/api/player/status", headers=H,
       json={"slot": 0, "wingspan": 1}), 400)
status("write to empty slot", client.put("/api/player/status", headers=H,
       json={"slot": 2, "hp": 1}), 404)

body = status("rejections left state intact", client.get("/api/player/status?slot=0", headers=H), 200)
# AGAINST WHAT THE LAST ACCEPTED WRITE ACTUALLY STORED, not a literal. This
# used to read 180, because the clamping cases above ended with hp pinned to
# the class maximum. They now end at the heal reconciler's ceiling instead, and
# hardcoding either number tests the clamp rather than the thing this check is
# about - that a REFUSED write disturbs nothing.
check("hp unchanged after four refused writes", body["hp"] == CLAMPED_HP,
      "wanted %s, got %s" % (CLAMPED_HP, body["hp"]))
check("an unknown field never becomes part of the record",
      "wingspan" not in body, body)


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


def fill_backpack(username, slot, entries):
    """Seed carry_items directly, as if the server had already granted these.

    The backpack is under server authority now: PUT /api/character/inventory
    reconciles a non-staff account against what the server recorded rather than
    setting arbitrary items. A fixture that needs a regular player to HOLD items
    therefore establishes them at the record level, the same way plant_bag
    plants a loot bag. Positional from cell 0."""
    conn = _sq.connect(DB_PATH)
    conn.row_factory = _sq.Row
    user_id = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()["id"]
    conn.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))
    conn.executemany(
        "INSERT INTO carry_items (user_id, slot, position, item_id, quantity)"
        " VALUES (?, ?, ?, ?, ?)",
        [(user_id, slot, i, item_id, qty) for i, (item_id, qty) in enumerate(entries)],
    )
    conn.commit()
    conn.close()


def fill_bank(username, entries):
    """Seed bank_items directly, as if the server had already banked these.

    THE BANK'S fill_backpack, AND FOR THE SAME REASON. PUT /api/account/bank now
    reconciles a non-staff account against what the server recorded, so it can
    no longer be used to PLACE items - only to reorder ones already there. A
    fixture that needs a player to HOLD something in the bank establishes it at
    the record level, the way fill_backpack and plant_bag do.

    Account-wide, so no slot: the bank is shared between a player's characters.
    Positional from cell 0."""
    conn = _sq.connect(DB_PATH)
    conn.row_factory = _sq.Row
    user_id = conn.execute(
        "SELECT id FROM users WHERE username = ?", (username,)
    ).fetchone()["id"]
    conn.execute("DELETE FROM bank_items WHERE user_id = ?", (user_id,))
    conn.executemany(
        "INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
        [(user_id, i, item_id, qty) for i, (item_id, qty) in enumerate(entries)],
    )
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
# Seeded at the record level rather than asserted through PUT /api/account/bank.
# That endpoint used to accept any array; it now reconciles against what the
# server banked, so using it to PLACE a pet would be testing the trim, not the
# duplicate-pet rule this section is about.
fill_bank("bagtester", [("petpoisonslimesmall", 1)])
BAG = plant_bag("bagtester", 0, [("petpoisonslimesmall", 1)])
body = status("a pet held only in the bank", client.post("/api/loot/take", headers=H4,
              json={"bag_id": BAG, "position": 0}), 200)
check("still counts as owned", body.get("duplicate_pet") is True, body)


# ---- A FULL BACKPACK --------------------------------------------------------
#
# 409, and the item STAYS IN THE BAG. Refusing beats dropping it on the floor:
# the player can make room and ask again.

clear_backpack("bagtester", 0)
# Server authority: a regular player cannot PUT items it never earned, so this
# full pack is established at the record level (see fill_backpack).
fill_backpack("bagtester", 0, [("ironsword", 1)] * 20)

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
fill_backpack("bagtester", 0,
              [("ironsword", 1)] * 19 + [("smallhealthpotion", 18)])
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
# EQUIPMENT AND HOTBAR SURVIVE A RE-LOGIN
# =============================================================================
# Both are strings the client cannot keep for itself. hotbar_assignments used
# to live only in the local slot dictionary, so it survived a scene change and
# not a re-login - the pet came back, because active_pet_id has a column, and
# the hotbar did not, because it had none. Equipment would have inherited that
# exact hole.

section("SAVE - EQUIPMENT AND HOTBAR")

def save_slot(**fields):
    body = {"slot": 0, "class_id": "warrior", "name": "checker"}
    body.update(fields)
    return client.put("/api/save", headers=H, json=body)


def slot_zero():
    body = client.get("/api/save", headers=H).get_json()
    rows = body if isinstance(body, list) else (body.get("saves") or body.get("slots") or [])
    for row in rows:
        if row.get("slot") == 0:
            return row
    return {}


# THE CHARACTER HAS TO BE OLD ENOUGH TO WEAR THIS, and until the equipment
# export existed it did not have to be. parse_equipment() skips
# gamedata.equip_check() entirely on a gamedata.json that predates
# equip_slot_name, so this block spent its whole life dressing a LEVEL 1
# warrior in level 22 ember gear and getting a 200 for it. The first run after
# the export turned that into five failures - which is the gate reporting for
# duty, not a regression.
#
# Raised where the server keeps it rather than claimed over the wire: level is
# in SERVER_OWNED_STATS precisely so a client cannot buy its way past this
# check by asserting a number, and a test that set it through the payload
# would be testing a door the server does not have.
save_slot(area="field")          # make sure slot 0 exists before updating it
_lvl = _owner_sq.connect(DB_PATH)
_lvl.execute("UPDATE saves SET level = 22 WHERE user_id = "
             "(SELECT id FROM users WHERE username = 'checker') AND slot = 0")
_lvl.commit()
_lvl.close()

# EQUIPMENT NO LONGER TRAVELS THROUGH /api/save, AND THAT IS THE CHANGE.
#
# This block used to send a full set of gear here and assert the server stored
# it, then assert it refused a mage's robe on a warrior. Both were correct
# while a save was how you got dressed. Equipping is a MOVE now -
# /api/character/equip takes the item out of the backpack, and that take is
# the ownership check - so a save that asserts equipment is asserting
# something it no longer has any business knowing.
#
# Ignored rather than refused, exactly like gold: an un-updated client still
# sends the key every save, and 400-ing an otherwise honest sync over a field
# it may no longer set would break saving for anyone who had not restarted.
body = status("a save that still sends gear is accepted",
              save_slot(equipment={"chest": "ironrobe"}), 200)
check("and says the field was ignored",
      "equipment" in (body.get("ignored") or []), body)

# THE GATE IS STILL REAL, it just lives at the endpoint now. Asserted rather
# than assumed, for the reason the old comment gave: without this the block
# passes just as happily on a server that skipped equip_check() altogether,
# which is the state it actually shipped in.
check("a warrior still cannot wear a mage's robe",
      client.post("/api/character/equip", headers=H,
                  json={"slot": 0, "item_id": "ironrobe"}).status_code in (403, 404),
      "wrong class should be refused before ownership is even reached")
check("nor can gear be forced into the wrong slot",
      app_module.gamedata.equip_slot_for("embersword") == "weapon",
      app_module.gamedata.equip_slot_for("embersword"))

check("the save stored no equipment at all",
      slot_zero().get("equipment", {}) == {}, slot_zero().get("equipment"))

status("a save with a hotbar", save_slot(
    hotbar=["tinyhealthpotion", "", "embersword"]), 200)
body = slot_zero()
check("the hotbar came back", body.get("hotbar", [])[0] == "tinyhealthpotion",
      body.get("hotbar"))
check("padded to nine, so the client always draws nine",
      len(body.get("hotbar", [])) == 9, body.get("hotbar"))

# THE RULE THAT MATTERS MOST: a save that says nothing about gear must not
# undress you. Several callers write this slot and not all of them know what
# the player is wearing - the same reasoning active_pet_id carries.
status("a save that mentions neither", save_slot(area="field"), 200)
body = slot_zero()
check("the hotbar is untouched by a save that does not mention it",
      body.get("hotbar", [])[0] == "tinyhealthpotion", body.get("hotbar"))

# EQUIPMENT REFUSALS MOVED WITH THE FEATURE. These used to assert that /api/save
# 400s on a slot the catalogue does not have, an item it has never heard of, and
# a list where a map belongs - all correct while a save was how you got dressed.
# The field is ignored here now, so there is nothing left to refuse: the shapes
# below are not stored, they are not looked at.
#
# THE RULES THEMSELVES ARE NOT GONE, and test_equipmove.py is where they live.
# An unknown item is a 400 at /api/character/equip, an unknown slot is a 400 at
# /unequip, and the wrong class or level is a 403 - checked against the server's
# own class and level rather than the body's.
status("gear sent to /api/save is accepted and dropped",
       save_slot(equipment={"hat": "emberhelm"}), 200)
status("even when the item does not exist",
       save_slot(equipment={"helm": "sombrero"}), 200)
status("even when it is not a map at all",
       save_slot(equipment=["emberhelm"]), 200)
check("and none of it reached the column",
      slot_zero().get("equipment", {}) == {}, slot_zero().get("equipment"))

status("a hotbar item that does not exist", save_slot(hotbar=["sombrero"]), 400)
status("a hotbar sent as a string", save_slot(hotbar="potion"), 400)

body = slot_zero()
check("and not one refusal disturbed the hotbar",
      body.get("hotbar", [])[0] == "tinyhealthpotion", body.get("hotbar"))

# A short hotbar is an older client, not a liar.
status("a hotbar shorter than nine", save_slot(hotbar=["tinyhealthpotion"]), 200)
check("is padded rather than refused", len(slot_zero().get("hotbar", [])) == 9,
      slot_zero().get("hotbar"))


# =============================================================================
# ACCOUNT - LUSIONS
# =============================================================================

section("ACCOUNT - LUSIONS")

# LUSIONS ARE SERVER-OWNED NOW, and this block used to prove the opposite - it
# asserted that a figure the client sent was stored, and it passed.
#
# Lusions have exactly one sink: reviving after death. So the balance IS the
# death penalty, and a client able to write it had removed the cost of dying
# from the game. Created by the server when a duplicate pet converts, spent by
# the server at /api/character/revive, and written by nobody else.
#
# The validation cases went with it. A field that is dropped before it is parsed
# cannot return 400 for being malformed, and keeping cases that expect one would
# only prove the endpoint still reads something it must not.
body = status("a client pushing its own lusions",
              client.put("/api/account/lusions", headers=H, json={"lusions": 40}), 200)
check("the figure is ignored, not stored", body["lusions"] == 0, body)
check("and the server names it as disregarded",
      "lusions" in body.get("ignored", []), body)

body = status("a nonsense figure is ignored just the same",
              client.put("/api/account/lusions", headers=H, json={"lusions": -1}), 200)
check("still nothing", body["lusions"] == 0, body)

body = status("reading it back", client.get("/api/account", headers=H), 200)
check("the balance never moved", body["lusions"] == 0, body)


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


def fund_gold(username, slot, amount):
    """Put gold in a purse the way the world does, for a fixture.

    THE LEDGER ROW IS NOT OPTIONAL. This used to be a PUT to
    /api/player/status with a gold figure, which worked because gold was not
    server-owned - the hole these tests now assert is closed. Writing the
    balance alone would leave SUM(ledger.delta) behind SUM(purses) and break
    the invariant test_economy.py exists to hold, so the row goes in beside it,
    exactly as gold_delta() would. This stands in for a loot drop, which is the
    only place gold is really created.
    """
    conn = _owner_sq.connect(DB_PATH)
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
    conn.execute("UPDATE saves SET gold = gold + ? WHERE user_id = ? AND slot = ?",
                 (amount, uid, slot))
    conn.execute(
        "INSERT INTO gold_ledger (at, user_id, slot, delta, reason, detail) "
        "VALUES (?, ?, ?, ?, 'test_fixture', 'bank section funding')",
        (int(time.time()), uid, slot, amount),
    )
    conn.commit()
    conn.close()


fund_gold("checker", 0, 500)

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
# BANK - ITEMS
# =============================================================================
#
# A TRANSFER, NOT TWO ASSERTIONS. PUT /api/account/bank takes a whole bank array
# on trust and PUT /api/character/inventory takes a whole backpack array on
# trust, and nothing checks that what left one equals what arrived at the other:
# two arrays that do not add up look exactly like two that do.
#
# POST /api/bank/items is the one place the server can conserve items, because
# it owns both sides. The checks that matter here are the ones about the total
# being preserved and about a refused transfer leaving the item where it started.

section("BANK - ITEMS")


def _carried(headers, slot=0):
    body = client.get("/api/character?slot=%d" % slot, headers=headers).get_json()
    return sum(c["quantity"] for c in body["inventory"] if c and c["item_id"] == "tinyhealthpotion")


def _banked(headers):
    body = client.get("/api/account", headers=headers).get_json()
    return sum(c["quantity"] for c in body["bank_inventory"]
               if c and c["item_id"] == "tinyhealthpotion")


# THIS SECTION MUTATES SHARED STATE AND PUTS IT BACK. The suite runs against one
# database in order, and a later isolation check asserts an exact quantity in
# bank cell 0 - which the full-bank rollback test below would otherwise leave
# holding an iron sword. Capture first, restore at the end.
_BANK_SNAPSHOT = client.get("/api/account", headers=H).get_json()["bank_inventory"]
_CARRY_SNAPSHOT = client.get("/api/character?slot=0", headers=H).get_json()["inventory"]

status("start with a known backpack", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "tinyhealthpotion", "quantity": 10}]}), 200)
status("and an empty bank", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": []}), 200)

_before = _carried(H) + _banked(H)
check("10 potions to start with", _before == 10, _before)

body = status("deposit 4", client.post("/api/bank/items", headers=H,
              json={"slot": 0, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": 4}), 200)
check("6 left carried", _carried(H) == 6, _carried(H))
check("4 now banked", _banked(H) == 4, _banked(H))
check("NOTHING WAS CREATED OR LOST", _carried(H) + _banked(H) == _before,
      "%d + %d" % (_carried(H), _banked(H)))
check("the response carries both grids",
      "inventory" in body and "bank_inventory" in body, sorted(body.keys()))

status("withdraw 3", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "withdraw", "item_id": "tinyhealthpotion", "quantity": 3}), 200)
check("9 carried again", _carried(H) == 9, _carried(H))
check("1 still banked", _banked(H) == 1, _banked(H))
check("still conserved", _carried(H) + _banked(H) == _before,
      "%d + %d" % (_carried(H), _banked(H)))

# A DEPOSIT MERGES onto the stack already there rather than opening a new cell,
# the same way _add_to_backpack does. If it opened a cell instead, a player who
# banked potions ten at a time would find the bank full of part-used stacks.
status("deposit onto the existing banked stack", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": 5}), 200)
_bank_cells = [c for c in client.get("/api/account", headers=H).get_json()["bank_inventory"]
               if c and c["item_id"] == "tinyhealthpotion"]
check("one banked stack, not two", len(_bank_cells) == 1, _bank_cells)
check("holding all 6", _bank_cells[0]["quantity"] == 6, _bank_cells)

# ---- refusals leave both sides untouched -----------------------------------
status("deposit more than carried", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": 999}), 400)
check("and changed nothing", _carried(H) + _banked(H) == _before,
      "%d + %d" % (_carried(H), _banked(H)))

status("withdraw more than banked", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "withdraw", "item_id": "tinyhealthpotion", "quantity": 999}), 400)
check("and changed nothing either", _carried(H) + _banked(H) == _before,
      "%d + %d" % (_carried(H), _banked(H)))

status("an item you hold none of", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "ironsword", "quantity": 1}), 400)

status("unknown op", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "launder", "item_id": "tinyhealthpotion", "quantity": 1}), 400)
status("quantity zero", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": 0}), 400)
status("quantity negative", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": -3}), 400)
status("quantity as a boolean", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": True}), 400)
status("empty item id", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "", "quantity": 1}), 400)
status("a slot with no character", client.post("/api/bank/items", headers=H,
       json={"slot": 2, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": 1}), 404)

check("after every refusal, the total is still 10",
      _carried(H) + _banked(H) == _before, "%d + %d" % (_carried(H), _banked(H)))

# ---- THE ROLLBACK, which is the one that would lose items ------------------
#
# A deposit removes from the backpack and THEN tries to add to the bank. If the
# bank is full the add fails, and without a rollback the potions would be gone
# from both sides - the exact bug the "writes nothing unless it all fits" rule
# in _add_to_bank exists to prevent, except one level up.
_full = [{"item_id": "ironsword", "quantity": 1} for _ in range(50)]
status("fill every bank cell", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": _full}), 200)

_carried_before_full = _carried(H)
status("depositing into a full bank is refused", client.post("/api/bank/items", headers=H,
       json={"slot": 0, "op": "deposit", "item_id": "tinyhealthpotion", "quantity": 1}), 409)
check("AND THE POTION IS STILL IN THE BACKPACK",
      _carried(H) == _carried_before_full,
      "had %d, now %d" % (_carried_before_full, _carried(H)))

# Put back exactly what this section found, so the sections after it see the
# database they were written against.
status("restore the bank", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": _BANK_SNAPSHOT}), 200)
status("restore the backpack", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": _CARRY_SNAPSHOT}), 200)


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
# STACK CEILING
# =============================================================================
#
# "A positive integer" is not a bound. Before this, the backpack and the bank
# both accepted a cell holding a billion potions - the table only checks
# quantity > 0 - and max_stack was enforced in exactly one place, the loot
# path, which is the one place the server hands you the item rather than being
# told about it.
#
# It matters more than it looks: items have a `value`, so an unbounded stack is
# an unbounded gold printer the day a shop exists.

section("STACK CEILING")

status("a stack past the item's max_stack", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "smallhealthpotion", "quantity": 21}]}), 400)
status("exactly max_stack is fine", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "smallhealthpotion", "quantity": 20}]}), 200)

# A sword does not stack at all, so its ceiling is 1 regardless of max_stack.
status("two of an unstackable item in one cell", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "ironsword", "quantity": 2}]}), 400)
status("one of it is fine", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "ironsword", "quantity": 1}]}), 200)

status("the billion-potion cell", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "smallhealthpotion", "quantity": 999999999}]}), 400)

# THE BANK GOES THROUGH THE SAME VALIDATOR, which is the point of there being
# one. This check is here because there used to be two copies of it and only
# one would have got the ceiling.
status("the bank is bound by the same rule", client.put("/api/account/bank", headers=H,
       json={"bank_inventory": [{"item_id": "smallhealthpotion", "quantity": 500}]}), 400)

# An item the server has never heard of still has a bound, but not max_stack -
# ids are deliberately not whitelisted, so that the game can add an item
# without a matching server deploy.
status("an unknown item is capped at the ceiling", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "notathing", "quantity": 10000}]}), 400)
status("and allowed below it", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "notathing", "quantity": 9999}]}), 200)

# Nothing may be half-written. A legal cell followed by an illegal one must
# leave the whole array refused, not the first cell stored.
status("clear the bag", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": []}), 200)
status("a good cell followed by a bad one", client.put("/api/character/inventory", headers=H,
       json={"slot": 0, "inventory": [{"item_id": "smallhealthpotion", "quantity": 5},
                                      {"item_id": "smallhealthpotion", "quantity": 99}]}), 400)
body = status("nothing was written", client.get("/api/character?slot=0", headers=H), 200)
check("the refused array left the bag empty",
      all(c is None for c in body["inventory"]), [c for c in body["inventory"] if c])


# =============================================================================
# CHARACTER - SKILLS
# =============================================================================

section("CHARACTER - SKILLS")

body = status("write skills is accepted", client.put("/api/character/skills", headers=H,
              json={"slot": 0, "skills": {
                  "defense": {"level": 12, "xp": 340},
                  "magic":  {"level": 3,  "xp": 9},
              }}), 200)
# EVERY skill is server-owned now (E-2 close): defense, agility and magic joined
# attack/fishing/cooking, so the client can no longer write ANY of them. The
# request is still accepted - an un-updated client's save must not 400 over a
# field it may no longer set - but the claim is DROPPED. A round-trip must NOT
# return the level the client named; it comes from /api/skill/train instead.
check("a skill claim does not round-trip (server-owned)",
      body["skills"].get("defense", {}).get("level") != 12, body["skills"])

status("an unknown skill", client.put("/api/character/skills", headers=H,
       json={"slot": 0, "skills": {"swimming": {"level": 1, "xp": 0}}}), 400)
status("skill level 0", client.put("/api/character/skills", headers=H,
       json={"slot": 0, "skills": {"defense": {"level": 0, "xp": 0}}}), 400)
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
# A stack of 99 potions rather than 99 swords: a sword has max_stack 1, and
# the stack ceiling now refuses that. This check is about ACCOUNT ISOLATION -
# the quantity is incidental and should not be the thing that fails it.
status("stranger cannot write our bank", client.put("/api/account/bank", headers=H2,
       json={"bank_inventory": [{"item_id": "smallhealthpotion", "quantity": 20}]}), 200)
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
       json={"slot": 0, "skills": {"defense": {"level": 99, "xp": 0}}}), 404)

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
# MODERATION
# =============================================================================
#
# can_act_on() is tested above as a predicate. This is the machinery around it:
# who the endpoints let through, what a ban actually does to a live session,
# and whether the audit log records it.

section("MODERATION")

def _register(name):
    return client.post("/api/auth/register",
                       json={"username": name, "password": "password123"}).get_json()

def _rank(name, role):
    conn = _owner_sq.connect(DB_PATH)
    conn.execute("UPDATE users SET role = ? WHERE username = ?", (role, name))
    conn.commit(); conn.close()

OWNER_H = H                                   # checker is ELUSION_OWNER
_dev = _register("thedev");   _rank("thedev", "dev")
_mod = _register("themod");   _rank("themod", "mod")
_mod2 = _register("othermod"); _rank("othermod", "mod")
_victim = _register("victim")

# tokens have to be re-issued after the rank change, since the row is read per
# request but the fixtures above registered before being promoted
DEV_H = {"Authorization": "Bearer " + client.post("/api/auth/login",
         json={"username": "thedev", "password": "password123"}).get_json()["token"]}
MOD_H = {"Authorization": "Bearer " + client.post("/api/auth/login",
         json={"username": "themod", "password": "password123"}).get_json()["token"]}
PLAYER_H = {"Authorization": "Bearer " + client.post("/api/auth/login",
            json={"username": "victim", "password": "password123"}).get_json()["token"]}

# ---- who gets through the door --------------------------------------------
status("a player cannot reach the account list",
       client.get("/api/staff/users", headers=PLAYER_H), 404)
status("a mod can", client.get("/api/staff/users", headers=MOD_H), 200)

status("a player cannot ban", client.post("/api/staff/ban", headers=PLAYER_H,
       json={"username": "themod", "reason": "because"}), 404)

# ---- the hierarchy, through the endpoint rather than the predicate ---------
status("a mod cannot ban another mod", client.post("/api/staff/ban", headers=MOD_H,
       json={"username": "othermod", "reason": "turf war", "days": 1}), 404)
status("a mod cannot ban a dev", client.post("/api/staff/ban", headers=MOD_H,
       json={"username": "thedev", "reason": "nope", "days": 1}), 404)
status("a mod cannot ban the owner", client.post("/api/staff/ban", headers=MOD_H,
       json={"username": "checker", "reason": "nope", "days": 1}), 404)
status("an account that does not exist is the same 404",
       client.post("/api/staff/ban", headers=MOD_H,
       json={"username": "ghost", "reason": "nope", "days": 1}), 404)

# ---- a reason is not optional ---------------------------------------------
status("a ban with no reason", client.post("/api/staff/ban", headers=MOD_H,
       json={"username": "victim"}), 400)

# ---- permanence is a higher permission ------------------------------------
status("a mod cannot ban permanently", client.post("/api/staff/ban", headers=MOD_H,
       json={"username": "victim", "reason": "forever"}), 403)
status("nor for longer than a mod's ceiling", client.post("/api/staff/ban", headers=MOD_H,
       json={"username": "victim", "reason": "ages", "days": 90}), 403)

body = status("a mod may time someone out", client.post("/api/staff/ban", headers=MOD_H,
              json={"username": "victim", "reason": "language", "days": 3}), 200)
check("the ban is not permanent", body.get("permanent") is False, body)
check("and carries an expiry", isinstance(body.get("expires_at"), int), body)
check("and records who did it", body.get("banned_by") == "themod", body)

# ---- what a ban does to someone already inside ----------------------------
status("the banned player's live token stops working",
       client.get("/api/auth/session", headers=PLAYER_H), 401)
body = status("and they cannot log back in", client.post("/api/auth/login",
              json={"username": "victim", "password": "password123"}), 403)
check("the refusal says why", body.get("ban", {}).get("reason") == "language", body)

# ---- unban -----------------------------------------------------------------
status("a dev can lift a mod's ban", client.post("/api/staff/unban", headers=DEV_H,
       json={"username": "victim"}), 200)
body = status("and they can log in again", client.post("/api/auth/login",
              json={"username": "victim", "password": "password123"}), 200)
PLAYER_H = {"Authorization": "Bearer " + body["token"]}
status("unbanning someone who is not banned is a no-op",
       client.post("/api/staff/unban", headers=DEV_H, json={"username": "victim"}), 200)

# ---- a dev outranks a mod, and the owner outranks everyone ------------------
status("a dev can ban a mod", client.post("/api/staff/ban", headers=DEV_H,
       json={"username": "themod", "reason": "abuse of tools"}), 200)
status("the owner can ban a dev", client.post("/api/staff/ban", headers=OWNER_H,
       json={"username": "thedev", "reason": "went rogue"}), 200)
status("and lift it again", client.post("/api/staff/unban", headers=OWNER_H,
       json={"username": "thedev"}), 200)
status("and the mod's", client.post("/api/staff/unban", headers=OWNER_H,
       json={"username": "themod"}), 200)

# UNBANNING DOES NOT HAND THE SESSION BACK. The ban deleted it, and lifting the
# ban does not resurrect a token - they log in again like anyone else. Worth
# asserting rather than working around, because a test that quietly reused a
# dead token would be hiding it.
status("the unbanned dev's old token is still dead",
       client.get("/api/auth/session", headers=DEV_H), 401)
DEV_H = {"Authorization": "Bearer " + client.post("/api/auth/login",
         json={"username": "thedev", "password": "password123"}).get_json()["token"]}
MOD_H = {"Authorization": "Bearer " + client.post("/api/auth/login",
         json={"username": "themod", "password": "password123"}).get_json()["token"]}
status("but logging in again works", client.get("/api/auth/session", headers=DEV_H), 200)

# ---- granting a rank -------------------------------------------------------
status("a mod cannot make someone a mod", client.put("/api/staff/role", headers=MOD_H,
       json={"username": "victim", "role": "mod"}), 403)
status("a dev cannot make someone a dev", client.put("/api/staff/role", headers=DEV_H,
       json={"username": "victim", "role": "dev"}), 403)
body = status("a dev can make someone a mod", client.put("/api/staff/role", headers=DEV_H,
              json={"username": "victim", "role": "mod"}), 200)
check("it reports what changed", body.get("was") == "player" and body.get("role") == "mod", body)
status("the owner can make someone a dev", client.put("/api/staff/role", headers=OWNER_H,
       json={"username": "victim", "role": "dev"}), 200)
status("nobody can grant owner", client.put("/api/staff/role", headers=OWNER_H,
       json={"username": "victim", "role": "owner"}), 400)
status("and demotion works", client.put("/api/staff/role", headers=OWNER_H,
       json={"username": "victim", "role": "player"}), 200)

# ---- who is online ---------------------------------------------------------
#
# A session lasts thirty days, so "holds a session" is not "is playing". The
# staff list reads presence off the heartbeat instead - GET /api/auth/session,
# which the game calls every 15 seconds - and these pin the three states a
# kick list has to tell apart: here now, logged in but gone quiet, logged out.

def _listed(name, headers=OWNER_H):
    for entry in client.get("/api/staff/users", headers=headers).get_json()["accounts"]:
        if entry["username"] == name:
            return entry
    return None

def _one_row(sql, args):
    # Opened and CLOSED per query. A connection left open holds the scratch
    # database, and on Windows that is what made teardown fail before.
    conn = _owner_sq.connect(DB_PATH)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()

def _age_heartbeats(name, seconds):
    conn = _owner_sq.connect(DB_PATH)
    conn.execute("UPDATE sessions SET last_seen_at = last_seen_at - ?"
                 " WHERE user_id = (SELECT id FROM users WHERE username = ?)",
                 (seconds, name))
    conn.commit(); conn.close()

_present = _register("present")
_PRESENT_H = {"Authorization": "Bearer " + _present["token"]}
_entry = _listed("present")
check("every listed account says whether it is online",
      _entry is not None and "online" in _entry and "last_seen_at" in _entry, _entry)
check("an account that just logged in is online", _entry and _entry["online"] is True, _entry)
_clock = client.get("/api/staff/users", headers=OWNER_H).get_json().get("now")
check("the list carries the server's clock, so 'last seen' needs no client clock",
      isinstance(_clock, int) and abs(_clock - time.time()) < 5, _clock)

_age_heartbeats("present", app_module.ONLINE_WINDOW_SECONDS + 5)
check("gone quiet past the window reads offline, though the session is alive",
      _listed("present")["online"] is False and
      client.get("/api/character", headers=_PRESENT_H).status_code != 401,
      "a thirty-day token must not keep someone on the kick list")

_expiry_before = _one_row("SELECT expires_at FROM sessions WHERE token = ?",
                          (_present["token"],))[0]
status("the heartbeat", client.get("/api/auth/session", headers=_PRESENT_H), 200)
check("brings them back online", _listed("present")["online"] is True, _listed("present"))
check("and does NOT extend the login",
      _one_row("SELECT expires_at FROM sessions WHERE token = ?",
               (_present["token"],))[0] == _expiry_before,
      "a heartbeat proves the game is running, not that the login is fresh")

# ONLY THE SESSION THAT BEAT. A second device left open elsewhere must not be
# vouched for by this one - the stamp is keyed by token, not by account.
_second = client.post("/api/auth/login",
                      json={"username": "present", "password": "password123"}).get_json()
_age_heartbeats("present", 10_000)
client.get("/api/auth/session", headers=_PRESENT_H)
_rows = {
    tok: _one_row("SELECT last_seen_at FROM sessions WHERE token = ?", (tok,))[0]
    for tok in (_present["token"], _second["token"])
}
check("the heartbeat stamps its own session and no other",
      _rows[_present["token"]] > _rows[_second["token"]], _rows)

# AN EXPIRED LOGIN IS NOT PRESENCE, however recent its last beat. Expired
# rows are only swept when their token is next used, so one can sit in the
# table looking fresh.
client.get("/api/auth/session", headers=_PRESENT_H)
_conn = _owner_sq.connect(DB_PATH)
_conn.execute("UPDATE sessions SET expires_at = 1 WHERE user_id ="
              " (SELECT id FROM users WHERE username = 'present')")
_conn.commit(); _conn.close()
check("an expired session does not count, however recent its heartbeat",
      _listed("present")["online"] is False, _listed("present"))
_PRESENT_H = {"Authorization": "Bearer " + client.post("/api/auth/login",
              json={"username": "present", "password": "password123"}).get_json()["token"]}

client.post("/api/staff/kick", headers=OWNER_H, json={"username": "present"})
_entry = _listed("present")
check("a kicked account is offline and shows no heartbeat",
      _entry["online"] is False and _entry["last_seen_at"] == 0, _entry)
status("and its heartbeat is refused - this 401 is how the game notices",
       client.get("/api/auth/session", headers=_PRESENT_H), 401)

# ---- staff item grants -----------------------------------------------------
#
# THE DEBUG KEYS, WHERE THEY CAN BE ENFORCED. F1-F7 and the pet row used to add
# items client-side and push the whole bag on the next save, with the rank check
# living in the client - so a patched build that set Api.role to "owner" got
# them back, and could have written the item straight into the array it was
# going to send anyway.
#
# A player must not be able to reach this at all. That is the check that turns
# "players must loot a pet" from a rule into something enforced.

section("STAFF GRANTS")

# The grant needs a character to put things in. Give the mod one.
status("a mod makes a character", client.put("/api/save", headers=MOD_H,
       json={"slot": 0, "class_id": "warrior", "name": "Modling"}), 200)

status("a player cannot grant themselves anything",
       client.post("/api/staff/grant", headers=PLAYER_H,
                   json={"slot": 0, "item_id": "ironsword", "quantity": 1}), 404)

body = status("a mod can", client.post("/api/staff/grant", headers=MOD_H,
              json={"slot": 0, "item_id": "ironsword", "quantity": 1}), 200)
check("the item landed in the backpack",
      any(cell and cell["item_id"] == "ironsword" for cell in body["inventory"]),
      body["inventory"])
check("and the response says what was granted",
      body["granted_item_id"] == "ironsword" and body["granted_quantity"] == 1, body)

# A PET, which is the whole point. Pets are the rarest loot in the game and the
# thing a player is supposed to earn.
body = status("a mod can grant a pet", client.post("/api/staff/grant", headers=MOD_H,
              json={"slot": 0, "item_id": "petsniper", "quantity": 1}), 200)
check("the pet is in the bag",
      any(cell and cell["item_id"] == "petsniper" for cell in body["inventory"]),
      body["inventory"])

status("a player cannot grant themselves a pet either",
       client.post("/api/staff/grant", headers=PLAYER_H,
                   json={"slot": 0, "item_id": "petsniper", "quantity": 1}), 404)

# THE SAME CEILING AS AN ORDINARY WRITE. Staff is not a reason to be allowed to
# create a corrupt row - a cell holding a billion potions is a bug wearing a
# privilege.
status("not past the stack ceiling", client.post("/api/staff/grant", headers=MOD_H,
       json={"slot": 0, "item_id": "tinyhealthpotion", "quantity": 10 ** 9}), 400)
status("nor a negative quantity", client.post("/api/staff/grant", headers=MOD_H,
       json={"slot": 0, "item_id": "ironsword", "quantity": -1}), 400)
status("nor zero", client.post("/api/staff/grant", headers=MOD_H,
       json={"slot": 0, "item_id": "ironsword", "quantity": 0}), 400)
status("nor a boolean dressed as a number", client.post("/api/staff/grant", headers=MOD_H,
       json={"slot": 0, "item_id": "ironsword", "quantity": True}), 400)
status("nor an empty item id", client.post("/api/staff/grant", headers=MOD_H,
       json={"slot": 0, "item_id": "", "quantity": 1}), 400)
status("nor into a slot with no character", client.post("/api/staff/grant", headers=MOD_H,
       json={"slot": 3, "item_id": "ironsword", "quantity": 1}), 404)

# EVERY GRANT IS IN THE AUDIT LOG, in the same transaction as the grant itself.
# An item that appears with no line about it is the thing this endpoint exists
# to prevent.
_grants = _owner_sq.connect(DB_PATH).execute(
    "SELECT actor_name, target_name, detail FROM staff_actions WHERE action = 'grant'"
).fetchall()
check("both grants were logged", len(_grants) == 2, _grants)
check("the log names the granter and what they took",
      all(a == "themod" and t == "themod" for a, t, _ in _grants), _grants)
check("including the pet", any("petsniper" in d for _, _, d in _grants), _grants)

# A REFUSED GRANT LEAVES NO LINE. A log that records attempts as if they were
# grants is worse than no log - it would read as the mod having taken things
# they never got.
status("a refused grant", client.post("/api/staff/grant", headers=MOD_H,
       json={"slot": 0, "item_id": "ironsword", "quantity": -5}), 400)
_after = _owner_sq.connect(DB_PATH).execute(
    "SELECT COUNT(*) FROM staff_actions WHERE action = 'grant'").fetchone()[0]
check("and did not write one", _after == 2, _after)


# ---- the audit log ---------------------------------------------------------
_log = _owner_sq.connect(DB_PATH).execute(
    "SELECT actor_name, action, target_name, detail FROM staff_actions ORDER BY id").fetchall()
check("every action was recorded", len(_log) >= 10, len(_log))
check("a ban records who, what and why",
      any(a == "themod" and act == "ban" and t == "victim" and "language" in d
          for a, act, t, d in _log), _log[:4])
check("a rank change records both sides",
      any(act == "role" and "player -> mod" in d for _, act, _, d in _log), _log)

body = status("the account list shows rank and reach",
              client.get("/api/staff/users", headers=MOD_H), 200)
_by_name = {a["username"]: a for a in body["accounts"]}
check("the owner is listed as owner", _by_name["checker"]["role"] == "owner", _by_name["checker"])
check("and is not actionable by a mod", _by_name["checker"]["actionable"] is False, _by_name["checker"])
check("a player is actionable by a mod", _by_name["victim"]["actionable"] is True, _by_name["victim"])


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

def _user_cols_after(conn):
    return [row[1] for row in conn.execute("PRAGMA table_info(users)")]


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
    CREATE TABLE admin_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, actor_name TEXT NOT NULL,
                                action TEXT NOT NULL, target_id INTEGER, target_name TEXT NOT NULL,
                                detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
    CREATE INDEX idx_admin_actions_target ON admin_actions(target_name);
    """
)

# One real audit row in the old table. The rename has to carry it, not create
# an empty table beside it - a log that silently stops being the log is worse
# than no log, and an empty table looks exactly like a quiet database.
_legacy.execute("INSERT INTO admin_actions (actor_name, action, target_name, created_at)"
                " VALUES ('someone', 'ban', 'someoneelse', 1)")
_legacy.execute("INSERT INTO users VALUES (1,'legacyuser','x',0,0)")
# An is_admin=1 row in the old shape, so the rank migration has something real
# to carry across rather than only proving the column appeared. The old column
# is recreated here deliberately - it is what the migration migrates FROM, and
# a test for a migration that cannot build the old shape tests nothing.
_legacy.execute("INSERT INTO users VALUES (2,'legacyflagged','x',1,0)")
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

# THE RANK MIGRATION, against a users table that had no role column at all.
# CREATE TABLE IF NOT EXISTS would have done nothing here, which is the whole
# reason this section exists.
_user_cols = [row[1] for row in _db.execute("PRAGMA table_info(users)")]
check("users gained the role column", "role" in _user_cols, _user_cols)

# PRESENCE, against the three-column sessions table every database before it
# had. The staff list selects last_seen_at; without this migration it would
# 500 on the real elusion.db while this suite, building fresh, stayed green.
check("sessions gained the last_seen_at column",
      "last_seen_at" in [row[1] for row in _db.execute("PRAGMA table_info(sessions)")])

_ranks = dict(_db.execute("SELECT username, role FROM users").fetchall())
check("an is_admin=1 account became dev", _ranks.get("legacyflagged") == "dev", _ranks)
check("everyone else became a player", _ranks.get("legacyuser") == "player", _ranks)

# AND THEN THE OLD COLUMN GOES. Order matters and this proves it: if the drop
# ran before the carry-across, legacyflagged would be a player here.
check("users.is_admin was dropped once role had taken over",
      "is_admin" not in _user_cols_after(_db),
      "ALTER TABLE DROP COLUMN needs SQLite 3.35+; this build has %s"
      % _sqlite3.sqlite_version)

# THE AUDIT TABLE, RENAMED RATHER THAN REPLACED.
_schema = {row[0]: row[1] for row in
           _db.execute("SELECT name, type FROM sqlite_master WHERE name LIKE '%actions%'")}
check("admin_actions is gone", "admin_actions" not in _schema, _schema)
check("staff_actions is there", _schema.get("staff_actions") == "table", _schema)
check("and the audit row came with it, not an empty table",
      _db.execute("SELECT actor_name, action, target_name FROM staff_actions").fetchall()
      == [("someone", "ban", "someoneelse")],
      _db.execute("SELECT * FROM staff_actions").fetchall())

# An index follows its table through a rename but keeps its old name, so this
# is the one that would otherwise survive with 'admin' in it forever - beside
# a duplicate the schema block creates on the same column.
check("the stale index was dropped", "idx_admin_actions_target" not in _schema, _schema)
check("and the renamed one exists exactly once",
      _schema.get("idx_staff_actions_target") == "index", _schema)
_db.close()

# Booting twice must not double the gold or duplicate the rows - every server
# restart runs init_db() again.
# Demote before the second boot: the migration must not resurrect a rank that
# was deliberately taken away. It only ever promotes a row still sitting at the
# default.
_demote = _sqlite3.connect(LEGACY_DB)
_demote.execute("UPDATE users SET role = 'player' WHERE username = 'legacyflagged'")
_demote.commit(); _demote.close()

_spec2 = importlib.util.spec_from_file_location("elusion_legacy2", os.path.join(HERE, "app.py"))
_again = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(_again)
_db = _sqlite3.connect(LEGACY_DB)
check("migrating twice does not double the gold",
      _db.execute("SELECT bank_gold FROM accounts").fetchone()[0] == 550)
check("migrating twice does not duplicate rows",
      _db.execute("SELECT COUNT(*) FROM bank_items").fetchone()[0] == 2)
check("and does not re-promote someone who was demoted",
      _db.execute("SELECT role FROM users WHERE username = 'legacyflagged'").fetchone()[0] == "player",
      _db.execute("SELECT role FROM users WHERE username = 'legacyflagged'").fetchone()[0])
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

# The summary is already printed above, but the exit code is not set yet, and
# a raise here would make it 1 on a passing run. Housekeeping does not get a
# vote on the verdict - see the note at the end of test_healing.py, where the
# same line deleted a green result instead of a scratch file.
import gc  # noqa: E402  - wanted only for the teardown below
gc.collect()
try:
    os.remove(DB_PATH)
except OSError as exc:
    print("  note: could not remove %s (%s)" % (DB_PATH, exc))

sys.exit(1 if failed else 0)
