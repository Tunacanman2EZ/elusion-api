"""
The catalogue that actually ships.

Run with:  python3 test_catalogue.py

WHY THIS SUITE EXISTS, AND WHY THE OTHER NINE DID NOT CATCH IT
---------------------------------------------------------------
Every other suite builds its own gamedata.json fixture and points
ELUSION_GAMEDATA at it. That is correct - a test of the spawn ceiling should
control how many of an enemy exist, not inherit whatever the export happened
to produce. But it means all nine suites test the code's behaviour GIVEN a
catalogue, and nothing tested the catalogue.

So gamedata.json in this repo sat 46 hours behind the Godot export, and four
protections were failing open the whole time:

    EQUIP_EXPORTED      False   ->  parse_equipment() skipped equip_check(),
                                    which is the state that let
                                    {"helm": "embersword"} through
    SPAWNS_EXPORTED     False   ->  the spawn ceiling was disabled entirely
    REGEN_EXPORTED      False   ->  the healing check measured against app.py's
                                    fallback literals
    restore_for()       ('', 0) ->  a grant with an unknown amount makes
                                    _report_unexplained_heals() return early,
                                    so any potion explained a rise of any size

1,515 green checks, four dead protections, no error anywhere. Each of those
fails open BY DESIGN - a half-upgraded server must not refuse every kill - and
fail-open is the right call. The bug is that it was silent. This suite is the
noise.

WHAT IT ASSERTS, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------
It does NOT assert a date, a byte count, or a hash. Those go stale the moment
anyone legitimately re-exports, and a test that cries wolf on every export is
a test people delete.

It asserts that each protection is ARMED: the flag the server gates on is
true, and the accessor behind it returns a real value rather than a degenerate
one. A flag can be true while every value under it is zero, which is armed in
name only - so both are checked.

Adding a protection that depends on an exported field means adding a row to
REQUIRED below. That is the whole maintenance burden, and it is deliberate:
the alternative is remembering to write a test, which is what did not happen
the last four times.
"""

import json
import os
import sys

# THE REAL FILE, NOT A FIXTURE. Cleared explicitly rather than merely left
# unset, because the other suites set this and a developer running the whole
# directory in one shell would otherwise test whichever fixture ran last.
os.environ.pop("ELUSION_GAMEDATA", None)

import gamedata

passed = failed = 0


def check(label, ok, detail=None):
    global passed, failed
    if ok:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        print("  FAIL %s   %r" % (label, detail))


def section(title):
    print("\n%s\n%s" % (title, "-" * len(title)))


FIX = ("re-run src/tools/exportgamedata.gd in Godot, then copy "
       "Elusion_RPG/data/gamedata.json over this folder's gamedata.json")


# =============================================================================
# THE MANIFEST
# =============================================================================
#
# One row per exported field a server-side protection gates on.
#
#   field    what exportgamedata.gd writes
#   flag     the module-level boolean app.py branches on
#   probe    returns a truthy value when the data behind the flag is real,
#            or a falsy one when the field is present but degenerate
#   breaks   what silently stops protecting when this is missing
#
# `probe` is the half that matters most. EQUIP_EXPORTED is
# `any("equip_slot_name" in item ...)`, so ONE item carrying the field turns it
# true for all 123 - and the other 122 then fall through the equip check with
# the flag reading green.

REQUIRED = [
    dict(
        field="equip_slot_name",
        flag=lambda: gamedata.EQUIP_EXPORTED,
        probe=lambda: sum(
            1 for i in gamedata.ITEMS.values() if i.get("equip_slot_name")),
        expect=lambda n: n > 0,
        breaks="equipment saves fall back to the loose slot list, and nothing "
               "verifies an item belongs in the slot it was sent for",
    ),
    dict(
        field="placed_count",
        flag=lambda: gamedata.SPAWNS_EXPORTED,
        probe=lambda: sum(
            1 for e in gamedata.ENEMIES.values() if e.get("placed_count")),
        expect=lambda n: n > 0,
        breaks="the spawn ceiling is disabled entirely - kill claims are "
               "bounded only by the token bucket",
    ),
    dict(
        field="regen_percent_per_second",
        flag=lambda: gamedata.REGEN_EXPORTED,
        probe=lambda: gamedata.regen_rate_for(1000),
        expect=lambda r: r > 0.0,
        breaks="the healing check measures against app.py's fallback copies "
               "rather than against PlayerStats",
    ),
    dict(
        field="restore_amount",
        flag=lambda: any(
            "restore_amount" in i for i in gamedata.ITEMS.values()),
        probe=lambda: sum(
            1 for i in gamedata.ITEMS.values() if i.get("restore_amount")),
        expect=lambda n: n > 0,
        breaks="a consume grant carries no amount, and an unknown amount makes "
               "_report_unexplained_heals() return early - so one cheap potion "
               "explains a heal of any size",
    ),
]


section("Every protection that depends on an export is armed")

for row in REQUIRED:
    armed = bool(row["flag"]())
    check("%s is present  (without it: %s)" % (row["field"], row["breaks"]),
          armed, "MISSING - %s" % FIX)

    if not armed:
        # The probe would only restate the same failure in a more confusing
        # way. Skipped rather than reported twice.
        continue

    value = row["probe"]()
    check("  ...and carries real values, not just the key", row["expect"](value),
          "%s present but degenerate (%r) - %s" % (row["field"], value, FIX))


# =============================================================================
# THE ACCESSORS THE ENDPOINTS ACTUALLY CALL
# =============================================================================
#
# The flags above are what app.py branches on. These are what it calls once it
# has branched, and they can disagree: gamedata.restore_for() reads a field
# name the exporter could rename without any flag noticing.

section("The accessors behind the flags return usable answers")

consumables = [
    i for i in gamedata.ITEMS.values()
    if str(i.get("type_name", "")).upper() == "CONSUMABLE"
]
check("the catalogue contains consumables at all", len(consumables) > 0,
      len(consumables))

if consumables:
    sample = consumables[0]
    target, amount = gamedata.restore_for(sample["item_id"])
    check("restore_for('%s') names a pool" % sample["item_id"],
          str(target).lower() in ("hp", "mana", "stamina"), target)
    check("restore_for('%s') gives an amount" % sample["item_id"],
          int(amount) > 0, amount)

placed = [e for e in gamedata.ENEMIES.values() if e.get("placed_count")]
if placed:
    sample = placed[0]
    check("spawn_count_for('%s') is non-zero" % sample["enemy_id"],
          gamedata.spawn_count_for(sample["enemy_id"]) > 0,
          gamedata.spawn_count_for(sample["enemy_id"]))

# ZERO IS A LEGITIMATE ANSWER and must stay one. The poison slime's smalls are
# spawned by its own script and appear in no scene, so the ceiling exempts them
# on purpose. Asserted here so that nobody "fixes" the exporter into claiming a
# placement it cannot see.
check("an unplaced enemy still answers 0 rather than raising",
      gamedata.spawn_count_for("__no_such_enemy__") == 0,
      gamedata.spawn_count_for("__no_such_enemy__"))


# =============================================================================
# DRIFT FROM THE GODOT REPO  (optional)
# =============================================================================
#
# The checks above catch a catalogue that disarms a protection. They do NOT
# catch a catalogue that is merely OLD - add an enemy in Godot, forget to copy,
# and every flag stays green while placed_count is wrong for the new one.
#
# Comparing against the source of truth catches that directly, but only when
# this machine has the Godot repo. Set ELUSION_GODOT_DATA to its
# data/gamedata.json to enable it; skipped, loudly, when unset.

section("Drift from the Godot export")

godot_path = os.environ.get("ELUSION_GODOT_DATA", "")
if not godot_path:
    print("  skip ELUSION_GODOT_DATA not set - cannot compare against source")
    print("       set it to Elusion_RPG/data/gamedata.json to enable")
elif not os.path.exists(godot_path):
    check("ELUSION_GODOT_DATA points at a file", False, godot_path)
else:
    with open(godot_path, "r", encoding="utf-8") as handle:
        source = json.load(handle)
    with open(gamedata.GAMEDATA_PATH, "r", encoding="utf-8") as handle:
        shipped = json.load(handle)

    # generated_at ALONE, not the whole document. Two exports of identical data
    # differ in that one field, and a byte comparison would fail on a re-export
    # that changed nothing - which is exactly the crying-wolf test this suite
    # is trying not to be.
    src_at = int(source.get("generated_at", 0))
    ship_at = int(shipped.get("generated_at", 0))
    check("this repo's copy is not older than the Godot export",
          ship_at >= src_at,
          "shipped %d, source %d - %d seconds behind. %s"
          % (ship_at, src_at, src_at - ship_at, FIX))

    for key in ("enemies", "items"):
        check("the same number of %s in both copies" % key,
              len(source.get(key, [])) == len(shipped.get(key, [])),
              "source %d, shipped %d - %s"
              % (len(source.get(key, [])), len(shipped.get(key, [])), FIX))


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failed:
    print("\n  %s" % FIX)
print("=" * 60)
raise SystemExit(1 if failed else 0)
