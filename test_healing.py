"""
E-9: does the healing check stay quiet across honest play?

Run with:  python3 test_healing.py

WHAT THIS IS FOR. _reconcile_heals() has never refused anything. It
compares each rise in hp/mana/stamina against what regeneration could have
produced and whether an authorised consume covers the rest, and then it logs.
Flipping it to refuse is a one-line change and the reason it has not happened
is written into the function: shipping a refusal before the comparison has
proven itself breaks honest saves for real players. E-1 was staged the same
way, and the interim skill bound under E-2 was the counter-example - a
threshold picked without data that did not survive contact with the client.

So this file is the data. Every scenario below is something a real player does,
and the assertion is that the check says nothing about it. The cheat section
then asserts the opposite, because a check that never fires is quiet for the
wrong reason.

IT DRIVES THE REAL ENDPOINT, not the function. PUT /api/player/status is where
the comparison actually runs, against the stored row, and the merge/clamp logic
above it can change what `after` even looks like - a level-up rewrites the
maxima in the same request. Calling the function directly would test a version
of the code that no client can reach.

WHY IT WOULD HAVE BEEN WORTHLESS UNTIL TODAY. The allowance is
elapsed x gamedata.regen_rate_for(max) x margin, and the grant lookup needs a
target pool and an amount. Against a gamedata.json with no regen constants and
no restore_amount, the rates fall back to app.py's literals and every grant
reads as an unknown quantity - which makes the function return early and
explain anything. A quiet log measured under those conditions is not evidence.
See E-13. test_catalogue.py is what keeps that true.
"""

import gc
import importlib.util
import logging
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# THE SCRATCH DATABASE GOES IN THE TEMP DIRECTORY, like the other ten suites.
# It used to sit beside this file, which puts it under whatever watches the
# project folder: a real-time virus scanner or the search indexer takes a
# transient handle on a file the moment it is written there, and on Windows a
# file with ANY open handle cannot be deleted. That is what turned a fully
# passing run into a PermissionError at teardown.
#
# HERE is still needed - for sys.path and for locating app.py. It was never
# needed for a throwaway database.
DB_PATH = os.path.join(tempfile.gettempdir(), "elusion_healing_test.db")

os.environ["ELUSION_DB"] = DB_PATH
os.environ["ELUSION_OWNER"] = "NOT_A_TEST_ACCOUNT"
os.environ.pop("ELUSION_GAMEDATA", None)   # the real catalogue, deliberately
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
    if ok:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        print("  FAIL %s   %s" % (label, detail))


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


# =============================================================================
# CATCHING WHAT THE CHECK SAYS
# =============================================================================
# A handler on app.logger rather than caplog or a monkeypatch: the thing under
# test is "would an operator see a line about this player", and the honest way
# to ask that is to listen where the operator listens.

class Ear(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        # getMessage() ALREADY applies record.args. Doing it again raises
        # TypeError inside a logging handler, which Flask then reports as a
        # 500 on the endpoint - so a bug in the listener reads as a bug in the
        # thing being listened to.
        self.lines.append(record.getMessage())


ear = Ear()
app_module.app.logger.addHandler(ear)
app_module.app.logger.setLevel(logging.WARNING)


def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def account(username):
    client.post("/api/auth/register",
                json={"username": username, "password": "hunter2hunter2"})
    token = client.post("/api/auth/login",
                        json={"username": username, "password": "hunter2hunter2"}
                        ).get_json()["token"]
    return {"Authorization": "Bearer " + token}


H = account("healer_subject")
client.put("/api/save", headers=H, json={"slot": 0, "class_id": "warrior", "name": "Subject"})

USER_ID = db_conn().execute(
    "SELECT id FROM users WHERE username = ?", ("healer_subject",)).fetchone()["id"]

MAXES = db_conn().execute(
    "SELECT max_hp, max_mana, max_stamina FROM saves WHERE user_id = ? AND slot = 0",
    (USER_ID,)).fetchone()
MAX_HP = int(MAXES["max_hp"])
RATE = gamedata.regen_rate_for(MAX_HP)
MARGIN = app_module.HEAL_ALLOWANCE_MARGIN


def attempt(before, after, elapsed, grants=()):
    """Stage a stored row `elapsed` seconds old, PUT `after`, report the log.

    Returns the lines the check produced - empty means it said nothing.
    """
    conn = db_conn()
    conn.execute(
        "UPDATE saves SET hp = ?, mana = ?, stamina = ?, updated_at = ?"
        " WHERE user_id = ? AND slot = 0",
        (before["hp"], before.get("mana", 50), before.get("stamina", 50),
         int(time.time()) - int(elapsed), USER_ID),
    )
    conn.execute("DELETE FROM consume_grants WHERE user_id = ?", (USER_ID,))
    for item_id, target, amount in grants:
        conn.execute(
            "INSERT INTO consume_grants (user_id, slot, item_id, target, amount, at)"
            " VALUES (?, 0, ?, ?, ?, ?)",
            (USER_ID, item_id, target, amount, int(time.time())),
        )
    conn.commit()
    conn.close()

    ear.lines.clear()
    body = dict(after)
    body["slot"] = 0
    response = client.put("/api/player/status", headers=H, json=body)
    if response.status_code != 200:
        return ["HTTP %d %s" % (response.status_code, response.data[:120])]
    return [line for line in ear.lines if "unexplained heal" in line]


def silent(label, before, after, elapsed, grants=()):
    lines = attempt(before, after, elapsed, grants)
    check(label, not lines, lines[0] if lines else "")


def flagged(label, before, after, elapsed, grants=()):
    lines = attempt(before, after, elapsed, grants)
    check(label, bool(lines), "said nothing")


print("\nsubject: warrior, max_hp %d, regen %.2f/s, margin %.2fx"
      % (MAX_HP, RATE, MARGIN))
check("the catalogue under test carries real regen constants",
      gamedata.REGEN_EXPORTED,
      "REGEN_EXPORTED is False - this whole file would be measuring the "
      "fallback literals. See E-13.")


# =============================================================================
# HONEST PLAY
# =============================================================================

section("Standing still")

# The ordinary case, and the one a naive check flags: a character regenerates
# continuously, so almost every save after a fight carries a rise.
silent("30 seconds of regen after a fight",
       {"hp": 100}, {"hp": 100 + int(30 * RATE)}, 30)

silent("a full minute back to maximum",
       {"hp": 50}, {"hp": MAX_HP}, 60)

# THE CAP IS WHAT MAKES THIS SAFE, not the elapsed time: regeneration cannot
# take you past your own maximum however long you waited, so a client that
# simply saves rarely gets headroom rather than an unlimited allowance.
silent("ten minutes away, returning at full",
       {"hp": 10}, {"hp": MAX_HP}, 600)

section("Saving mid-fight")

# characterdata.gd's SAVE_MAX_DELAY_SECONDS writes at most ten seconds after
# the first unsaved change, so this is the tightest window an honest client
# produces while anything is happening.
silent("a save every 10 seconds, healing between swings",
       {"hp": 200}, {"hp": 200 + int(10 * RATE)}, 10)

# HEAL_MINIMUM_ELAPSED_SECONDS exists for this: two writes in the same second
# must not read as "healed with zero time available".
silent("two saves in the same second, no rise",
       {"hp": 180}, {"hp": 180}, 0)

section("Drinking something")

# THROUGH restore_for(), NOT BY READING THE FIELDS. The first draft of this
# filtered on item["restore_target"], which is the ENUM INTEGER - the exact
# mistake that function's own docstring warns against - and every consumable
# silently failed to match. Asking the accessor the server asks means the test
# cannot drift from it.
def pool_items(pool):
    found = []
    for item in gamedata.ITEMS.values():
        target, amount = gamedata.restore_for(item["item_id"])
        if target.lower() == pool and amount > 0:
            found.append((item["item_id"], target, amount))
    return sorted(found, key=lambda row: row[2])


potions = pool_items("hp")
check("the catalogue has an hp consumable to test with", bool(potions),
      "none found - restore_target_name/restore_amount may not be exported")

if potions:
    pid, ptarget, heal = potions[0]
    silent("a %s (+%d hp) drunk two seconds ago" % (pid, heal),
           {"hp": 100}, {"hp": 100 + heal}, 2, [(pid, ptarget, heal)])

    # A potion mid-regen: both sources at once, which is the normal case and
    # the one an allowance-only check would flag.
    silent("the same potion, plus ten seconds of regen",
           {"hp": 100}, {"hp": 100 + heal + int(10 * RATE)}, 10,
           [(pid, ptarget, heal)])

section("Levelling and dying")

# Both refill every pool to its maximum, and "as much as you had room for" is
# not a number the server can write down in advance - so both are recorded as
# special cases rather than as amounts.
silent("a level-up refills all three pools",
       {"hp": 12, "mana": 3, "stamina": 5},
       {"hp": MAX_HP}, 2,
       [(app_module.LEVELUP_GRANT_ID, "", 0)])

silent("a revive is a jump from zero to full",
       {"hp": 0}, {"hp": MAX_HP}, 2,
       [(app_module.REVIVE_GRANT_ID, "", 0)])

section("Small pools")

# regen_rate_for() has a floor, so a pool too small for the percentage to
# matter still recovers at a fixed rate. Mana and stamina are where that bites.
silent("mana recovering at the floor rate",
       {"hp": MAX_HP, "mana": 10}, {"mana": 10 + int(10 * gamedata.regen_rate_for(
           int(MAXES["max_mana"])))}, 10)


# =============================================================================
# WHAT IT MUST CATCH
# =============================================================================
# A check that never fires is quiet for the wrong reason.

section("Cheating")

flagged("full health from nothing, in two seconds",
        {"hp": 1}, {"hp": MAX_HP}, 2)

flagged("a large heal with no consume at all",
        {"hp": 50}, {"hp": 50 + int(20 * RATE)}, 2)

if potions:
    pid, ptarget, heal = potions[0]

    # THE HOLE THE PER-POOL REWRITE CLOSED. Before it, any grant inside the
    # window explained any rise, so the cheapest consumable in the game
    # explained a jump of any size in any pool.
    flagged("a small potion (+%d) claimed as a full heal" % heal,
            {"hp": 20}, {"hp": MAX_HP}, 2, [(pid, ptarget, heal)])

stam = pool_items("stamina")
check("the catalogue has a stamina consumable too", bool(stam), "none found")
if stam:
    sid, starget, samount = stam[0]
    flagged("a stamina potion does not explain a health jump",
            {"hp": 20}, {"hp": MAX_HP}, 2, [(sid, starget, samount)])


# =============================================================================
# THE CLAMP
# =============================================================================
# Two lines: HEAL_ALLOWANCE_MARGIN (1.25x) is logged, HEAL_CLAMP_MARGIN (3x) is
# enforced. The band between them is the evidence for closing the gap later, so
# these assert that it is a band and not a second enforcement line.

section("The band between the two lines")

TIGHT = app_module.HEAL_ALLOWANCE_MARGIN
LOOSE = app_module.HEAL_CLAMP_MARGIN
check("the enforced line is looser than the logged one", LOOSE > TIGHT,
      "%s vs %s" % (LOOSE, TIGHT))


def stored_hp():
    return int(db_conn().execute(
        "SELECT hp FROM saves WHERE user_id = ? AND slot = 0",
        (USER_ID,)).fetchone()["hp"])


# Two seconds of allowance is ~7hp tight, ~18hp loose. A rise of 12 sits
# squarely between: it should be LOGGED and STORED UNTOUCHED.
between = int(2 * RATE * TIGHT) + 4
attempt({"hp": 60}, {"hp": 60 + between}, 2)
check("a rise between the lines is stored as sent", stored_hp() == 60 + between,
      "wanted %d, stored %d" % (60 + between, stored_hp()))
check("...and it was logged anyway, which is the point",
      bool(attempt({"hp": 60}, {"hp": 60 + between}, 2)))

section("Past the enforced line")

lines = attempt({"hp": 20}, {"hp": MAX_HP}, 2)
clamped = stored_hp()
check("a full heal from nothing is always REPORTED", bool(lines), lines)

if app_module.HEAL_CLAMP_ENFORCED:
    check("...and trimmed", clamped < MAX_HP, "stored %d of %d" % (clamped, MAX_HP))
    check("trimmed to roughly what regen allowed, not to zero",
          20 < clamped < 20 + int(2 * RATE * TIGHT) + 5,
          "stored %d, floor was 20" % clamped)
    check("and the clamp says so in the log",
          any("heal clamped" in line for line in ear.lines), ear.lines)
else:
    # ENFORCEMENT IS OFF. It was on for an hour and the first honest save it
    # saw was a zero-to-full refill from player.gd::_ready(), which the server
    # has no record of - see HEAL_CLAMP_ENFORCED in app.py.
    check("...and stored as sent, because enforcement is off",
          clamped == MAX_HP, clamped)
    check("and nothing claims to have clamped it",
          not any("heal clamped" in line for line in ear.lines), ear.lines)

# THE SAVE MUST STILL SUCCEED. A 400 here would discard the XP, gold, position
# and inventory riding along in the same request - punishing a suspicious hp
# figure by throwing away a legitimate half-hour of play.
body = {"slot": 0, "hp": MAX_HP, "gold": 0}
before_gold = db_conn().execute(
    "SELECT gold FROM saves WHERE user_id = ? AND slot = 0", (USER_ID,)).fetchone()["gold"]
resp = client.put("/api/player/status", headers=H, json=body)
check("a clamped save still returns 200", resp.status_code == 200, resp.status_code)
check("and the rest of the save survived it",
      db_conn().execute("SELECT gold FROM saves WHERE user_id = ? AND slot = 0",
                        (USER_ID,)).fetchone()["gold"] == before_gold)

# A grant still counts toward the clamp, not just toward the log.
if potions:
    pid, ptarget, heal = potions[0]
    attempt({"hp": 20}, {"hp": MAX_HP}, 2, [(pid, ptarget, heal)])
    check("an authorised potion raises the floor by its amount",
          stored_hp() >= 20 + heal, "stored %d, potion was +%d" % (stored_hp(), heal))


# =============================================================================
# THE RESIDUAL, MEASURED
# =============================================================================
# What a cheat can still take without being seen, so the number is written down
# before anything is flipped to refuse rather than discovered afterwards.

section("How much still slips through")

lo, hi = 0, MAX_HP
while lo < hi:
    mid = (lo + hi + 1) // 2
    if attempt({"hp": 1}, {"hp": 1 + mid}, 2):
        hi = mid - 1
    else:
        lo = mid
per_save = lo

check("a single save can gain at most %d hp unseen" % per_save,
      per_save < MAX_HP, per_save)

# At the client's own save ceiling, ten seconds apart, forever.
per_hour = per_save * 360
print("\n  measured: %d hp per save unexplained and unflagged" % per_save)
print("            %d saves/hour at the 10s save ceiling" % 360)
print("            = %d hp/hour of invisible healing, against a %d hp pool"
      % (per_hour, MAX_HP))
print("  This check bounds the RATE of unexplained healing, not its existence -")
print("  the same shape as the kill bucket. It is a control, not a proof.")

# THE VERDICT COMES BEFORE THE HOUSEKEEPING, and that ordering is a fix rather
# than tidiness. This used to unlink first, so a scratch file something else was
# still holding raised PermissionError AFTER all thirty checks had passed: no
# summary line, exit 1, and a runner correctly reporting a failed suite that had
# not failed anything. Half an hour went into the healing logic before anyone
# looked at the last line of the file.
#
# Cleanup is housekeeping. It does not get a vote on whether the code under test
# is correct, so it happens after the verdict and it cannot change it.
print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)

gc.collect()   # release any sqlite3.Connection still reachable only from a cycle
try:
    os.unlink(DB_PATH)
except OSError as exc:
    print("  note: could not remove %s (%s)" % (DB_PATH, exc))
    print("        harmless - the next run deletes it before it starts")

raise SystemExit(1 if failed else 0)
