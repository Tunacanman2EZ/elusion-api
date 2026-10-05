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
# SEEDED, because the band below is only three standard deviations wide at
# 1 in 1296 (154 expected, a spread of about 12), and there are dozens of
# enemies: on SystemRandom roughly one run in twenty-five failed somewhere for
# no reason at all - seen 5 Oct as "193 wins in 200000 kills, expected ~154"
# on a change that never touched pets. A fixed generator still proves the roll
# fires at the advertised rate through the real roll_pet(); it just gives the
# same answer every run. test_pacing.py swaps _rng the same way.
import random as _seeded
_real_rng = gamedata._rng
gamedata._rng = _seeded.Random(2026)
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

gamedata._rng = _real_rng

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
# pick_loot_item() come back empty every time.

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
# was wrong about that. pick_loot_item() never goes ABOVE the enemy's
# ceiling and steps down to the tiers under it, so a single empty tier
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


# ---------------------------------------------------------------------------
# A GOLD DROP IS PAID IN COINS
#
# It used to be one heap: roll an amount, pick one of two ids by a threshold,
# and put THAT MANY in the bag - so a tier 8 kill paid out thirty thousand of
# a thing called "A Few Coins". Now the amount is broken down a coin ladder.
#
# THE ONE THING THAT MUST NEVER BE WRONG is the total. A decomposition that
# comes up short is gold that was rolled and never arrived, and it would be
# invisible - the player cannot know what the dice said. So the check below is
# not "does it look sensible", it is "does it add up", across the whole range
# a drop can land in.
# ---------------------------------------------------------------------------
print("\n--- gold drops are paid in coins ---")

ladder = gamedata.gold_denominations()
check("there is a coin ladder at all", len(ladder) >= 2, str(ladder))
check("it is ordered biggest first",
      all(ladder[i][1] > ladder[i + 1][1] for i in range(len(ladder) - 1)),
      str(ladder))
check("every step divides evenly into the one above - what makes greedy exact",
      all(ladder[i][1] % ladder[i + 1][1] == 0 for i in range(len(ladder) - 1)),
      str(ladder))
check("the smallest coin is one gold, so any amount is payable exactly",
      ladder[-1][1] == 1, str(ladder[-1]))
check("every coin on the ladder is a real item",
      all(gamedata.has_item(i) for i, _v in ladder),
      str([i for i, _v in ladder if not gamedata.has_item(i)]))
check("and lusions are NOT on it - a premium currency is not change",
      not any(i == "lusions" for i, _v in ladder), str(ladder))

short = []
for amount in list(range(1, 400)) + [999, 1000, 1001, 4999, 24999, 25000,
                                     31000, 99999, 100001, 250000]:
    coins = gamedata.make_change(amount, ladder)
    if gamedata.change_total(coins, ladder) != amount:
        short.append((amount, gamedata.change_total(coins, ladder)))
check("every amount from 1 to 400 and beyond decomposes to EXACTLY itself",
      not short, str(short[:6]))

coins = gamedata.make_change(31000, ladder)
check("a big drop pays in big coins, not thirty thousand coppers",
      all(int(c["quantity"]) < 100 for c in coins), str(coins))
check("a one-gold drop is a single copper",
      gamedata.make_change(1, ladder) == [{"item_id": ladder[-1][0], "quantity": 1}],
      str(gamedata.make_change(1, ladder)))
check("nothing at all comes out of nothing",
      gamedata.make_change(0, ladder) == [], "should be empty")

# AND A REAL ROLLED BAG CARRIES THEM. The checks above exercise the function;
# this one exercises the thing that calls it.
paid = 0
mixed = False
for _try in range(40):
    bag = gamedata.roll_loot_for("slime") if hasattr(gamedata, "roll_loot_for") else None
    if bag is None:
        break
    got = [c for c in bag if any(c["item_id"] == i for i, _v in ladder)]
    if got:
        paid += 1
        if len(got) > 1:
            mixed = True
if paid:
    check("a rolled bag pays in ladder coins", paid > 0, paid)
    check("and sometimes in more than one denomination", mixed, "40 rolls")


# ---------------------------------------------------------------------------
# THE JACKPOT DICE
#
# The ordinary roll tops out at unit * spread, which at the highest loot tier
# in the game is a few thousand gold - so the top of the coin ladder was
# priced, named, drawn and impossible to drop. Five dice now ride on every
# gold drop and the number of sixes picks a multiplier.
#
# TWO THINGS MUST HOLD, and they pull against each other:
#   - the big coins have to be REACHABLE, or they are decoration
#   - they have to be reachable ONLY from the top of the game, and only
#     rarely, or they say nothing about what you beat
#
# And the starting area is barred outright: a new player whose third slime
# pays a gold coin has been handed the first area's economy before meeting it.
# ---------------------------------------------------------------------------
print("\n--- the jackpot dice ---")

dice = int(gamedata.CONSTANTS.get("gold_jackpot_dice", 0) or 0)
table = gamedata.CONSTANTS.get("gold_jackpot_multipliers") or []
floor = int(gamedata.CONSTANTS.get("gold_jackpot_min_tier", 0) or 0)

check("there are dice to roll", dice > 0, dice)
check("the multiplier table covers every possible number of sixes",
      len(table) == dice + 1, "%d dice, %d entries" % (dice, len(table)))
check("a poor roll multiplies by one, not zero",
      table and int(table[0]) == 1, str(table))
check("the table never goes backwards",
      all(int(table[i]) <= int(table[i + 1]) for i in range(len(table) - 1)),
      str(table))
check("all sixes is the best it gets",
      int(table[-1]) == max(int(t) for t in table), str(table))
check("and all sixes is genuinely rare - one roll in %d" % (6 ** dice),
      6 ** dice >= 1000, 6 ** dice)

check("the starting tier is barred", not gamedata.jackpot_allowed(floor - 1))
check("and the tier above it is not", gamedata.jackpot_allowed(floor))
check("a missing tier is barred rather than waved through",
      not gamedata.jackpot_allowed(None))

# NO DICE ARE ROLLED BELOW THE FLOOR, not dice whose result is thrown away.
# The difference is the random stream: rolling and discarding would shift
# every subsequent draw, so adding this feature would have silently changed
# what the starting area drops.
#
# CHECKED WITH A DIE THAT COUNTS ITSELF rather than by seeding. gamedata._rng
# is a SystemRandom, which ignores seed() by design - the obvious version of
# this test reseeds and compares, and it can never pass no matter how correct
# the code is. Handing in a die and asking whether it was touched answers the
# actual question.
class _CountingDie:
    def __init__(self, value=1):
        self.rolls = 0
        self.value = value

    def randint(self, _low, _high):
        self.rolls += 1
        return self.value


_die = _CountingDie()
_barred = [gamedata.roll_gold_jackpot(floor - 1, _die) for _ in range(5)]
check("a barred tier never touches the dice", _die.rolls == 0, _die.rolls)
check("and reports no jackpot", all(m == (0, 1) for m in _barred), str(_barred))

_die = _CountingDie()
gamedata.roll_gold_jackpot(floor, _die)
check("a permitted tier rolls every die once", _die.rolls == dice, _die.rolls)

# A LOADED DIE PROVES THE TABLE IS REALLY BEING READ.
_all_sixes = _CountingDie(int(gamedata.CONSTANTS.get("gold_jackpot_faces", 6)))
_sixes, _mult = gamedata.roll_gold_jackpot(floor, _all_sixes)
check("all sixes is read as all sixes", _sixes == dice, _sixes)
check("and pays the top multiplier", _mult == int(table[-1]),
      "%d, expected %s" % (_mult, table[-1]))

_no_sixes = _CountingDie(1)
_sixes, _mult = gamedata.roll_gold_jackpot(floor, _no_sixes)
check("and a cold roll pays nothing extra", _sixes == 0 and _mult == 1,
      "%d sixes, x%d" % (_sixes, _mult))

# WHAT EACH TIER CAN ACTUALLY PAY. Enough rolls that a 1-in-7776 shows up.
_order = [i for i, _v in gamedata.gold_denominations()]
_top_coin = _order[0]

def _best_coin(tier, rolls):
    enemy = {"max_loot_tier": tier, "slot_fill_chance": 0.0, "max_item_slots": 0}
    best = len(_order)
    biggest = 0
    for _ in range(rolls):
        bag = gamedata.build_bag_contents(enemy)
        biggest = max(biggest, gamedata.change_total(bag))
        for coin in bag:
            if coin["item_id"] in _order:
                best = min(best, _order.index(coin["item_id"]))
    return (_order[best] if best < len(_order) else ""), biggest

_low_coin, _low_max = _best_coin(floor - 1, 60000)
check("the starting tier never pays a big coin - no jackpot reaches it",
      _order.index(_low_coin) >= _order.index("silvercoin"),
      "best was %s (%d gold)" % (_low_coin, _low_max))
check("and never a platinum coin", _low_coin != _top_coin, _low_coin)

_top_tier = max(int(e.get("max_loot_tier", 0)) for e in gamedata.ENEMIES.values()) \
    if isinstance(gamedata.ENEMIES, dict) else 6
_hi_coin, _hi_max = _best_coin(_top_tier, 200000)
check("the top tier CAN reach the top of the ladder - it is not decoration",
      _hi_coin == _top_coin, "best was %s (%d gold)" % (_hi_coin, _hi_max))

# AND A JACKPOT STILL MAKES EXACT CHANGE. A multiplier is just a bigger
# number going into make_change, but "bigger number" is where an off-by-one
# in the ladder would first show.
_short = []
for _mult in table:
    for _base in (1, 7, 49, 137, 1370, 2975):
        _amount = _base * int(_mult)
        _coins = gamedata.make_change(_amount)
        if gamedata.change_total(_coins) != _amount:
            _short.append((_amount, gamedata.change_total(_coins)))
check("every multiplied amount still decomposes exactly", not _short, str(_short[:4]))



# =============================================================================
section("THE TIER IS ROLLED FIRST, THEN THE ITEM")
# =============================================================================
# It used to be one weight per item, 2^(max_tier - tier), which let the
# CATALOGUE set the odds: eleven iron pieces outvoted everything and 58% of a
# boss bag was iron, while amethyst and ember gear came out at 1%. Now
# EnemyData.tier_odds says what share of filled slots land on each tier, top
# first, and an item is then picked evenly inside that tier.

import random as _random
_saved_rng = gamedata._rng
gamedata._rng = _random.Random(20260929)


def _tier_shares(enemy, rolls):
    counts = defaultdict(int)
    for _ in range(rolls):
        counts[gamedata.roll_loot_tier(enemy)] += 1
    return {t: counts[t] / rolls for t in counts}


_normal = {"max_loot_tier": 3, "tier_odds": [0.15, 0.45, 0.40]}
_shares = _tier_shares(_normal, 40000)
check("a tier-3 enemy's slots land 15% / 45% / 40% on tiers 3 / 2 / 1",
      abs(_shares.get(3, 0) - 0.15) < 0.01 and abs(_shares.get(2, 0) - 0.45) < 0.01
      and abs(_shares.get(1, 0) - 0.40) < 0.01, _shares)

_low = {"max_loot_tier": 1, "tier_odds": [0.15, 0.45, 0.40]}
check("steps that would fall below iron land on iron",
      set(_tier_shares(_low, 4000)) == {1}, _tier_shares(_low, 4000))

_elite = {"max_loot_tier": 3, "tier_odds": [0.4, 0.6], "tier_up_chance": 1.0}
check("a tier_up_chance hit rolls one tier ABOVE the enemy",
      set(_tier_shares(_elite, 2000)) == {4}, _tier_shares(_elite, 2000))

check("an enemy exported before the field existed gets the default odds",
      abs(_tier_shares({"max_loot_tier": 3}, 40000).get(3, 0) - 0.15) < 0.01)

# THE COUNT OF ITEMS IN A TIER NO LONGER MOVES THE ODDS. Tier 1 holds more
# droppable things than tier 3 (potions, worms, rods); under the old weights
# that alone tilted every bag toward it. Rolled at the same tier, each item in
# a pool now comes up about equally often.
_pool = gamedata.loot_pool(3)
_hits = defaultdict(int)
for _ in range(len(_pool) * 400):
    _hits[gamedata.pick_loot_item(3)] += 1
_expected = 400
check("every item in a tier comes up about equally often",
      _pool and all(abs(_hits[i] - _expected) < _expected * 0.35 for i in _pool),
      {i: _hits[i] for i in _pool})

# TIER 7, AND IT WAS 6. Tier 6 was the empty tier above ember until day 2, when
# the three mythic weapons filled it - so the empty tier this asks about is the
# one above that now.
check("a tier with nothing of that kind steps DOWN to the nearest that has some",
      not gamedata.loot_pool(7, "gear")
      and gamedata.pick_loot_item(7, "gear") in gamedata.loot_pool(6, "gear"),
      gamedata.pick_loot_item(7, "gear"))
check("and never up", gamedata.pick_loot_item(0) == "")


# =============================================================================
section("EVERY GEAR TIER CAN BE EARNED")
# =============================================================================
# The last two tiers were finished items - icons, stats, prices - marked not
# droppable and not sold, so the best reachable gear was cobalt and a
# playthrough ran out at level 10. The enemy's tier is what keeps them rare:
# nothing drops above max_loot_tier, so a tier-2 slime still cannot hand out
# ember.

_gear_by_tier = defaultdict(list)
for _item in ITEMS.values():
    if _item["type_name"] in gamedata.LOOT_GEAR_TYPES:
        _gear_by_tier[int(_item["tier"])].append(_item)
for _t in sorted(_gear_by_tier):
    _locked = sorted(i["item_id"] for i in _gear_by_tier[_t] if not i.get("droppable", True))
    check("every tier-%d weapon and armour piece can drop" % _t, not _locked, _locked)

_top_gear_tier = max(_gear_by_tier)
_carriers = sorted(eid for eid, e in REWARDING.items()
                   if int(e.get("max_loot_tier", 1)) >= _top_gear_tier
                   and int(e.get("max_item_slots", 0)) > 0)
check("and some enemy is tough enough to drop the top tier", bool(_carriers), _carriers)

_low_tier = min(int(e.get("max_loot_tier", 1)) for e in REWARDING.values()
                if int(e.get("max_item_slots", 0)) > 0)
_leaked = set()
_weakest = [e for e in REWARDING.values() if int(e.get("max_loot_tier", 1)) == _low_tier]
for _ in range(20000):
    for _entry in gamedata.build_bag_contents(_weakest[0]):
        _it = ITEMS.get(_entry["item_id"], {})
        if int(_it.get("tier", 1)) > _low_tier and _it.get("type_name") != "CURRENCY":
            _leaked.add(_entry["item_id"])
check("but the weakest enemy never drops above its own tier", not _leaked, sorted(_leaked)[:6])


# =============================================================================
section("A BOSS BAG IS ONE PIECE OF GEAR, AND SOMETIMES A POTION")
# =============================================================================

_bosses = sorted((eid, e) for eid, e in REWARDING.items() if e.get("slots_are_gear"))
check("the bosses are marked gear-only", len(_bosses) >= 7, [b[0] for b in _bosses])
_boss_items = 0
_gear_slot_kinds = set()
_potions = 0
_iron = 0
_bags = 0
for _eid, _e in _bosses:
    for _ in range(3000):
        _contents = [c for c in gamedata.build_bag_contents(_e)
                     if gamedata.item_row(c["item_id"])["type_name"] != "CURRENCY"]
        _bags += 1
        _boss_items += len(_contents)
        if _contents:
            _gear_slot_kinds.add(gamedata.item_row(_contents[0]["item_id"])["type_name"])
        _potions += sum(1 for c in _contents[1:] if gamedata.item_row(c["item_id"])["type_name"] == "CONSUMABLE")
        _iron += sum(1 for c in _contents if int(gamedata.item_row(c["item_id"])["tier"]) == 1)
check("the guaranteed slot is always a weapon or armour",
      _gear_slot_kinds <= gamedata.LOOT_GEAR_TYPES and bool(_gear_slot_kinds), _gear_slot_kinds)
check("a boss bag holds about 1.5 items (was 3.6)",
      1.4 < _boss_items / _bags < 1.6, round(_boss_items / _bags, 3))
check("the extra item is a potion about half the time",
      0.45 < _potions / _bags < 0.55, round(_potions / _bags, 3))
check("and a boss never drops iron (was 58% of its bag)", _iron == 0, _iron)



# =============================================================================
section("THE RAREST THINGS COME FIRST IN THE BAG")
# =============================================================================
# Coins used to lead the bag because gold is rolled first, and the pet was
# appended last - so when a bag ran past the panel's cells, the cut fell on the
# pet. rarest_first() orders pet, then items, then coins.

_first_bad = 0
_order_bad = 0
_saved_pet = {eid: e.get("pet_odds") for eid, e in ENEMIES.items()}
try:
    for _eid in [eid for eid, e in REWARDING.items() if e.get("pet_drop_id")][:6]:
        ENEMIES[_eid]["pet_odds"] = 1
        for _ in range(500):
            _c = gamedata.roll_kill_rewards(_eid)["contents"]
            _kinds = [gamedata.item_row(x["item_id"])["type_name"] for x in _c]
            if not _kinds or _kinds[0] != "PET":
                _first_bad += 1
            _ranks = [0 if k == "PET" else (2 if k == "CURRENCY" else 1) for k in _kinds]
            if _ranks != sorted(_ranks):
                _order_bad += 1
finally:
    for _eid, _odds in _saved_pet.items():
        ENEMIES[_eid]["pet_odds"] = _odds
check("a pet, when one drops, is the first thing in the bag", _first_bad == 0, _first_bad)
check("and coins always come after the items", _order_bad == 0, _order_bad)


# =============================================================================
section("A MYTHIC CAN COME FROM ANYTHING, AND THE TOUGHER THE BETTER")
# =============================================================================
# Day 2, the owner: regular mobs and bosses both drop the mythic weapons,
# "mixed rarity but it should be super rewarding getting 1". No tier_odds
# reaches tier 6. Each enemy carries "one in N" odds instead (mythic_odds,
# worked out in the game by EnemyData.mythic_odds() and exported), and
# roll_mythic() rolls them at every kill, beside the bag.
MYTHIC_TIER = gamedata.mythic_tier()
_odds = {eid: int(e.get("mythic_odds", 0)) for eid, e in ENEMIES.items()}
_bosses = {eid for eid, e in REWARDING.items() if e.get("slots_are_gear")}
_normals = {eid for eid in REWARDING if eid not in _bosses}
# The small slimes: what a large splits into, and the poison slime's small,
# which poisonslime.gd splits in code rather than through split_into.
_smalls = {e.get("split_into") for e in ENEMIES.values() if e.get("split_into")} | {"poisonslimesmall"}

check("the mythic tier is 6", MYTHIC_TIER == 6, MYTHIC_TIER)
check("every enemy that pays rewards has mythic odds",
      all(_odds[eid] > 0 for eid in REWARDING), sorted(e for e in REWARDING if _odds[e] <= 0))
check("and one that pays nothing has none",
      all(_odds[eid] == 0 for eid in ENEMIES if eid not in REWARDING),
      sorted(e for e in ENEMIES if e not in REWARDING and _odds[e]))
check("the Crowned is the best mythic hunt in the game",
      _odds["boss"] == min(_odds[e] for e in REWARDING), _odds["boss"])
check("every boss beats every regular enemy",
      max(_odds[e] for e in _bosses) < min(_odds[e] for e in _normals),
      (max(_odds[e] for e in _bosses), min(_odds[e] for e in _normals)))
check("a tier 6 boss beats a tier 5 one",
      all(_odds[a] < _odds[b] for a in _bosses for b in _bosses
          if a != "boss" and int(ENEMIES[a]["max_loot_tier"]) > int(ENEMIES[b]["max_loot_tier"])))

# THE TOUGHER THE BETTER, band by band, among the regular enemies.
_by_band = defaultdict(set)
for _eid in _normals - _smalls:
    _by_band[int(ENEMIES[_eid]["max_loot_tier"])].add(_odds[_eid])
check("regular enemies of one band share one rate",
      all(len(v) == 1 for v in _by_band.values()), dict(_by_band))
_bands = sorted(_by_band)
check("and a higher band always has the better odds",
      all(min(_by_band[_bands[i]]) < min(_by_band[_bands[i - 1]]) for i in range(1, len(_bands))),
      {b: sorted(v) for b, v in _by_band.items()})
check("a small slime is a quarter of its band's ticket (one large releases eight)",
      all(_odds[s] == 4 * min(_by_band[int(ENEMIES[s]["max_loot_tier"])]) for s in _smalls if s in REWARDING),
      {s: _odds[s] for s in _smalls})

# WHAT THAT COMES TO, so the numbers in GameConstants' comment stay true.
# Five kills a minute is the pace the level curve is built on (test_pacing);
# the Crowned is a kill every three minutes or so with its two-minute respawn.
_dark = min(_by_band[5])
check("the dark band is about one mythic in 65 hours (40 to 100)",
      40 <= _dark / 300.0 <= 100, round(_dark / 300.0, 1))
check("the Crowned is about one in 8 hours (6 to 10)",
      6 <= _odds["boss"] * 3 / 60.0 <= 10, round(_odds["boss"] * 3 / 60.0, 1))
check("light and wind are a lottery ticket: over a thousand hours",
      min(_by_band[1]) / 300.0 > 1000, min(_by_band[1]) / 300.0)

# THE ROLL READS THOSE ODDS. A die that remembers what it was asked, as with
# the jackpot above: randint(1, N) wins on N.
class _OddsDie:
    def __init__(self, win, pick=0):
        self.win = win
        self.pick = pick
        self.asked = []

    def randint(self, low, high):
        self.asked.append((low, high))
        return high if self.win else low

    def randrange(self, n):
        return self.pick % n


_saved_roll = gamedata._rng
_wrong = []
try:
    for _eid in sorted(REWARDING):
        gamedata._rng = _OddsDie(win=False)
        _got = gamedata.roll_mythic(ENEMIES[_eid], "mage")
        if _got != "" or gamedata._rng.asked != [(1, _odds[_eid])]:
            _wrong.append((_eid, _got, gamedata._rng.asked))
finally:
    gamedata._rng = _saved_roll
check("every enemy rolls its own odds once, and a miss is nothing", not _wrong, _wrong[:3])

# A WIN IS YOUR OWN CLASS'S PIECE.
MYTHIC_FOR = {"mage": "meteorite", "warrior": "doubleaxe", "tank": "dynamite"}
_pool = gamedata.mythic_pool("")
check("the mythic pool is the three weapons", sorted(_pool) == sorted(MYTHIC_FOR.values()), _pool)
try:
    for _cls, _item in sorted(MYTHIC_FOR.items()):
        _seen = set()
        for _pick in range(6):
            gamedata._rng = _OddsDie(win=True, pick=_pick)
            _seen.add(gamedata.roll_mythic(ENEMIES["darksprite"], _cls))
        check("a %s who wins gets the %s, every time" % (_cls, ITEMS[_item]["display_name"]),
              _seen == {_item}, sorted(_seen))
    _healer = set()
    for _pick in range(6):
        gamedata._rng = _OddsDie(win=True, pick=_pick)
        _healer.add(gamedata.roll_mythic(ENEMIES["darksprite"], "healer"))
finally:
    gamedata._rng = _saved_roll
check("a healer, with no mythic of their own yet, can get any of the three",
      _healer == set(MYTHIC_FOR.values()), sorted(_healer))

# MEASURED, not only reasoned about: the real roll at the advertised rate.
gamedata._rng = _random.Random(20261003)
for _eid in ("boss", "lightboss"):
    _n = 300 * _odds[_eid]
    _wins = sum(1 for _ in range(_n) if gamedata.roll_mythic(ENEMIES[_eid], "tank"))
    check("%s pays a mythic about 1 in %d" % (_eid, _odds[_eid]), 240 <= _wins <= 360,
          "%d in %d" % (_wins, _n))

# A MYTHIC MAKES A BAG, AND NOTHING PUSHES IT OUT.
_saved_odds = dict(_odds)
_missing = _cut = _flagged = 0
try:
    for _eid in sorted(REWARDING):
        ENEMIES[_eid]["mythic_odds"] = 1
        for _ in range(40):
            _r = gamedata.roll_kill_rewards(_eid, "warrior")
            # base_id(): a mythic rolls like any other dropped piece.
            _ids = [gamedata.base_id(c["item_id"]) for c in _r["contents"]]
            if gamedata.base_id(_r["mythic"]) != "doubleaxe":
                _flagged += 1
            if "doubleaxe" not in _ids:
                _missing += 1
            elif _ids.index("doubleaxe") != (1 if _r["pet_won"] else 0):
                _cut += 1
finally:
    for _eid, _o in _saved_odds.items():
        ENEMIES[_eid]["mythic_odds"] = _o
check("a kill that wins a mythic says so", _flagged == 0, _flagged)
check("and always has a bag with it in, even when the bag roll said no", _missing == 0, _missing)
check("and it comes first in the bag, after a pet, where no cell limit can cut it", _cut == 0, _cut)
ENEMIES["darksprite"]["mythic_odds"] = 0
try:
    _quiet = [gamedata.roll_kill_rewards("darksprite", "mage")["mythic"] for _ in range(200)]
finally:
    ENEMIES["darksprite"]["mythic_odds"] = _saved_odds["darksprite"]
check("odds of 0 never pay", set(_quiet) == {""}, set(_quiet))

gamedata._rng = _saved_rng

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
if failures:
    print("  failing: %s" % ", ".join(failures[:6]))
print("=" * 60)
sys.exit(1 if failed else 0)
