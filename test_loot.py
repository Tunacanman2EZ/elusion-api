"""Reachability tests: can a player actually get the things that exist?

THE QUESTION THIS ASKS is not "does the loot roll work" - test_economy.py
covers the gold side and the server's own roll functions are short enough to
read. It is the one a catalogue cannot answer about itself: IS EVERY FINISHED
THING OBTAINABLE, and would it still be if the world were rebuilt tomorrow?

WHY IT IS A SEPARATE FILE. Reachability is a property of the DATA, not of the
world. An enemy that is not placed in any scene yet is not a bug - it is
content waiting on level design - but an enemy that is placed and still cannot
pay out its pet IS one, and the two are indistinguishable by looking at the
scene tree. This file checks the half that must hold no matter where anything
is placed, so that placing a boss later is a level-design job and not a
debugging one.

THE FAILURE IT EXISTS FOR, stated plainly because it shipped: all seven bosses
carried pet odds (1/72, 1/54) and no pet_drop_id. roll_pet() reads
pet_drop_id FIRST and returns False on an empty one, before it ever looks at
the odds - so the rate was decorative and the Crowned Companion could not drop
from anything, while petboss.tres sat finished in data/items/pets/. Nothing
errored. The export printed "pet 1/72" as though it meant something.

Runs against gamedata.json and the server's own roll functions - no database,
no client, no Godot. Run: python test_loot.py
"""

import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import gamedata

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


def section(title):
    print("\n=== %s ===\n" % title)


ITEMS = gamedata.ITEMS
ENEMIES = gamedata.ENEMIES
REWARDING = {eid: e for eid, e in ENEMIES.items()
             if e.get("grants_rewards", True)}


# =============================================================================
section("EVERY PET ID NAMES A REAL ITEM")
# =============================================================================
# The silent one. roll_pet() calls has_item() and returns False when it misses,
# so a typo or a rename does not throw, does not warn and does not drop - it
# just removes a companion from the game permanently. "petpoisonslimesmall"
# against "petpoisonsmall" is five characters and an entire collectable.

for eid, enemy in sorted(ENEMIES.items()):
    for field in ("pet_drop_id", "rare_pet_drop_id"):
        pet = str(enemy.get(field, "") or "")
        if pet == "":
            continue                      # no pet is a normal, valid state
        check("%s's %s ('%s') exists" % (eid, field, pet),
              gamedata.has_item(pet),
              "roll_pet() will return False forever and nothing will say why")


# =============================================================================
section("NO ENEMY ADVERTISES ODDS IT CANNOT PAY")
# =============================================================================
# pet_odds is derived from the loot tier, so EVERY enemy carries one whether or
# not a pet was ever authored for it. Carrying odds with no pet is not broken -
# it is "the art does not exist yet" - but it is indistinguishable in the export
# from an enemy that drops properly, which is exactly how seven bosses shipped
# advertising a rate that could not come up.

decorative = sorted(
    eid for eid, e in REWARDING.items()
    if int(e.get("pet_odds", 0)) > 0 and not str(e.get("pet_drop_id", "") or "")
)
check("no rewarding enemy carries pet odds with no pet behind them",
      decorative == [],
      "%s - the odds are decorative and roll_pet() returns before reading them"
      % ", ".join(decorative))


# =============================================================================
section("EVERY PET IN THE CATALOGUE IS DROPPED BY SOMETHING")
# =============================================================================
# The other direction, and the one that catches a finished item nobody wired
# up. A pet is not sold, not crafted and not granted - the drop is the only way
# in, so a pet no enemy names is a file that exists and content that does not.

dropped_by = defaultdict(list)
for eid, enemy in ENEMIES.items():
    for field in ("pet_drop_id", "rare_pet_drop_id"):
        pet = str(enemy.get(field, "") or "")
        if pet:
            dropped_by[pet].append(eid)

pets = sorted(i["item_id"] for i in ITEMS.values() if i.get("type_name") == "PET")
check("the catalogue has pets at all", len(pets) > 0, len(pets))
for pet in pets:
    check("%s is dropped by something" % pet,
          len(dropped_by[pet]) > 0,
          "no enemy names it, so nothing can ever give it to a player")


# =============================================================================
section("A KILL ACTUALLY PAYS OUT")
# =============================================================================
# THE END-TO-END ONE, through the server's own roll_kill_rewards() rather than
# by re-reading the fields it reads. Every assertion above could pass on a
# server whose roll never fires; this one cannot.
#
# Sampled rather than reasoned about: pet rates run to 1 in 864, so the sample
# has to be large enough that a working drop is overwhelmingly likely and a
# broken one is not merely unlucky. At 1/864 over 200k kills the expected count
# is 231 and the chance of seeing zero is about e^-231, which is zero for every
# practical purpose.

SAMPLE = 200_000
for eid in sorted(REWARDING):
    enemy = REWARDING[eid]
    pet = str(enemy.get("pet_drop_id", "") or "")
    if not pet:
        continue
    odds = int(enemy.get("pet_odds", 0))
    wins = sum(1 for _ in range(SAMPLE) if gamedata.roll_pet(enemy))
    expected = SAMPLE / odds if odds else 0
    # Wide band on purpose. This is a "does it fire at all, at roughly the
    # advertised rate" check, not a test of the RNG - a tight band would fail
    # on ordinary variance and teach everyone to ignore it.
    check("%s pays out its pet at about 1/%d" % (eid, odds),
          expected * 0.75 <= wins <= expected * 1.25,
          "%d wins in %d kills, expected ~%.0f" % (wins, SAMPLE, expected))

# And the pet that comes out is the one advertised. pick_pet_id() can return
# the RARE pet instead, so this asserts membership rather than equality.
for eid in sorted(REWARDING):
    enemy = REWARDING[eid]
    if not str(enemy.get("pet_drop_id", "") or ""):
        continue
    allowed = {str(enemy.get("pet_drop_id", "")), str(enemy.get("rare_pet_drop_id", "") or "")}
    allowed.discard("")
    got = {gamedata.pick_pet_id(enemy) for _ in range(2000)}
    check("%s hands over only pets it names" % eid,
          got <= allowed, sorted(got - allowed))


# =============================================================================
section("BAGS CONTAIN SOMETHING")
# =============================================================================
# An enemy with a bag chance, item slots and a fill chance that never produces
# a single item is a kill that looks rewarded and is not. Checked per enemy
# because max_loot_tier differs: a tier the catalogue has no items for makes
# pick_weighted_item_id() come back empty every time.

for eid in sorted(REWARDING):
    enemy = REWARDING[eid]
    if float(enemy.get("bag_drop_chance", 0)) <= 0:
        continue
    if int(enemy.get("max_item_slots", 0)) <= 0:
        continue
    filled = 0
    for _ in range(2000):
        if gamedata.build_bag_contents(enemy):
            filled += 1
    check("%s (tier %s) can fill a bag" % (eid, enemy.get("max_loot_tier")),
          filled > 0,
          "2000 bags, all empty - no item sits at or below its loot tier")


# =============================================================================
section("EVERY LOOT TIER HAS SOMETHING IN IT")
# =============================================================================
# THE ASSERTION IS CUMULATIVE, NOT PER TIER, and the first version of this
# was wrong about that. pick_weighted_item_id() skips anything ABOVE the
# enemy's ceiling and keeps everything at or below it, so a single empty tier
# costs an enemy its rarest bracket and nothing else - the bag still fills from
# the tiers underneath. Asserting per tier failed on tier 6 and claimed "every
# bag rolling it comes back short", which is simply not what the function does.
#
# What must hold is that each ceiling has SOMETHING at or below it. An empty
# tier is reported instead, because it is worth knowing and is not a fault:
# it means that ceiling behaves exactly like the highest non-empty one beneath
# it, so two enemies with different max_loot_tier drop from the same pool.

ceiling = max(int(e.get("max_loot_tier", 1)) for e in ENEMIES.values())


def rollable_at_or_below(tier):
    return [i for i in ITEMS.values()
            if int(i.get("tier", 1)) <= tier
            and i.get("droppable", True)
            and i.get("type_name") not in gamedata.EXCLUDED_FROM_LOOT]


for tier in sorted({int(e.get("max_loot_tier", 1)) for e in ENEMIES.values()}):
    check("a tier %d ceiling has items to roll" % tier,
          len(rollable_at_or_below(tier)) > 0,
          "nothing droppable at or below this tier - every bag comes back empty")

empty = []
for tier in range(1, ceiling + 1):
    exact = [i for i in ITEMS.values()
             if int(i.get("tier", 1)) == tier
             and i.get("droppable", True)
             and i.get("type_name") not in gamedata.EXCLUDED_FROM_LOOT]
    if not exact:
        empty.append(tier)
if empty:
    users = sorted(eid for eid, e in ENEMIES.items()
                   if int(e.get("max_loot_tier", 1)) in empty)
    print("  note: tier(s) %s hold nothing droppable, so the ceiling on %s"
          % (", ".join(str(t) for t in empty), ", ".join(users) or "nothing"))
    print("        buys them no item a tier lower would not also give.")


# =============================================================================
section("WHAT IS REACHABLE FROM WHERE")
# =============================================================================
# Not an assertion - a headcount, printed so that placing a boss later is an
# informed move. If a pet's only source is an enemy that is not in any scene,
# the data above is perfectly healthy and the player still cannot get it; the
# export tool checks that half, because only Godot can see the scene tree.

print("  pets and the enemies that carry them:")
for pet in pets:
    print("    %-24s %d source(s): %s"
          % (pet, len(dropped_by[pet]), ", ".join(sorted(dropped_by[pet]))))


print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("  failing: %s" % ", ".join(failures[:6]))
print("=" * 60)
sys.exit(1 if failed else 0)
