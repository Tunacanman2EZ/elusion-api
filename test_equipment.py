"""
The client half of the equipment system, exercised against the real catalogue,
and then the whole round trip against the real server.

Run with:  python3 test_equipment.py
"""

import json
import os
import sys
import tempfile

import sim_client as C

# THIS SUITE CANNOT RUN AGAINST AN UNEXPORTED CATALOGUE, and it used to say so
# by dying on `KeyError: 'equip_slot'` forty lines in - which reads like a
# broken test rather than the finding it actually is.
#
# The finding: gamedata.json carries no equipment data, so the server cannot
# tell a helmet from a sword and /api/save accepts any item in any slot.
# wsgi.py now refuses to start in that state. This says the same thing to
# whoever runs the tests, in words, before the first assertion.
if not any("equip_slot" in item for item in C.ITEMS.values()):
    print("\n  STOPPED - gamedata.json predates the equipment export.\n")
    print("  Every check in this file reads equip_slot from the exported")
    print("  catalogue, and none of them can mean anything without it.")
    print("  More importantly, a server on this gamedata.json has NO")
    print("  server-side equip validation: {\"helm\": \"embersword\"} is a")
    print("  legal save. wsgi.py refuses to start for that reason.\n")
    print("  Fix: re-run src/tools/exportgamedata.gd in the Godot editor,")
    print("  then copy the new gamedata.json beside app.py.\n")
    sys.exit(1)

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


PLATE = ["ironsword", "ironhelm", "ironchest", "ironlegs", "ironboots",
         "ironshield", "ironring", "ironamulet"]
CLOTH = ["ironstaff", "ironhood", "ironrobe", "irontrousers", "ironslippers",
         "ironring", "ironamulet"]


# =============================================================================
section("THE SLOT VOCABULARY")
# =============================================================================

check("eight slots, NONE excluded", C.slot_names() == [
    "weapon", "helm", "chest", "legs", "boots", "shield", "ring", "amulet"],
    C.slot_names())
check("NONE has no name", C.slot_name(0) == "", C.slot_name(0))
check("a value the enum has no member for has no name either",
      C.slot_name(99) == "", C.slot_name(99))

# THE BUG THIS WHOLE PASS EXISTS FOR. The cloth line was authored one slot too
# high - a hood was a chest piece, a robe was leg armour, slippers were a
# shield - and the server's hand-typed list had a "robe" slot to receive it.
# Both halves of the mistake agreed with each other, which is why it survived.
for item_id, expected in [("ironhood", "helm"), ("ironrobe", "chest"),
                          ("irontrousers", "legs"), ("ironslippers", "boots"),
                          ("ironhelm", "helm"), ("ironchest", "chest"),
                          ("ironlegs", "legs"), ("ironboots", "boots"),
                          ("ironshield", "shield")]:
    got = C.slot_name(C.ITEMS[item_id]["equip_slot"])
    check("%s is worn on the %s" % (item_id, expected), got == expected, got)

check("nothing is worn in a slot called 'robe'",
      not any(C.slot_name(i["equip_slot"]) == "robe" for i in C.ITEMS.values()))


# =============================================================================
section("WHO MAY WEAR WHAT")
# =============================================================================

warrior = C.Player("warrior", 1)
check("a warrior wears the whole tier 1 plate kit",
      all(warrior.equip(i) for i in PLATE), warrior.equipped)
check("eight pieces, one per slot", len(warrior.equipped) == 8, warrior.equipped)

check("and not a mage's robe", warrior.equip("ironrobe") is False)
check("the refusal says why",
      warrior.equip_check("ironrobe")["reason"] == "class",
      warrior.equip_check("ironrobe"))

mage = C.Player("mage", 1)
check("a mage wears the cloth kit", all(mage.equip(i) for i in CLOTH),
      mage.equipped)
check("seven pieces - a staff is held in both hands, so no shield",
      len(mage.equipped) == 7 and "shield" not in mage.equipped, mage.equipped)
check("and not a warrior's cuirass", mage.equip("ironchest") is False)

check("a hood and a helm compete for the same slot",
      C.slot_name(C.ITEMS["ironhood"]["equip_slot"])
      == C.slot_name(C.ITEMS["ironhelm"]["equip_slot"]))

# Jewellery has no class gate at all. Reading an empty required_classes as
# "nobody" rather than "anyone" would take every ring in the game away from
# every class, which is the sort of inversion that looks correct in review.
for class_id in ("warrior", "mage", "tank", "healer"):
    check("a %s may wear a plain ring" % class_id,
          C.Player(class_id, 1).equip("ironring") is True)


# =============================================================================
section("THE LEVEL GATE")
# =============================================================================

low = C.Player("warrior", 1)
check("a level 1 warrior cannot lift an ember sword",
      low.equip("embersword") is False)
check("the refusal names the level it wants",
      low.equip_check("embersword")["needs"] == 22,
      low.equip_check("embersword"))
check("and nothing was equipped by the attempt", low.equipped == {}, low.equipped)

check("at 22 it goes on", C.Player("warrior", 22).equip("embersword") is True)
check("one level short is still short",
      C.Player("warrior", 21).equip("embersword") is False)

check("a potion is not equipment",
      low.equip_check("tinyhealthpotion")["reason"] == "notgear")
check("neither is a pet",
      low.equip_check("petpoisonslimesmall")["reason"] == "notgear")
check("an item that does not exist says so",
      low.equip_check("sombrero")["reason"] == "unknown")

# THE BUSH AMULET WAS THE ONE ITEM TYPED AS ARMOR WITH NO SLOT, and this pair
# of checks asserted it stayed that way. It is a boss drop at tier 5 whose
# description reads "Fantastic Trophy Good Job!", so "ownable, never wearable"
# was a defensible reading - and it is the one the export tool warned about
# every run, because an ARMOR with no slot is indistinguishable from a piece of
# gear somebody forgot to finish.
#
# It is gear now: the AMULET slot, armour 7, required level 22. The level comes
# from where it drops rather than from its price - a tier 5 item comes off a
# boss, and you fight bosses at 22, same as the ember piece. Armour 7 sits
# above amethyst's 6 and below ember's 9, so it is a real upgrade without
# displacing the top of the ladder, and its 200 gold stays deliberately low:
# a trophy you keep rather than vendor.
check("the bush amulet is wearable at the rung it drops from",
      low.equip_check("bushamulet")["reason"] == "level",
      "a level 1 character is refused for LEVEL now, not for being ungear")
check("and it goes on at 22", C.Player("warrior", 22).equip("bushamulet") is True)
check("in the amulet square and nowhere else",
      C.can_drop("amulet", "bushamulet", C.Player("warrior", 22))
      and not C.can_drop("chest", "bushamulet", C.Player("warrior", 22)))
check("it beats amethyst and loses to ember, as a trophy should",
      C.ITEMS["amethystamulet"]["armor_value"]
      < C.ITEMS["bushamulet"]["armor_value"]
      < C.ITEMS["emberamulet"]["armor_value"],
      [C.ITEMS[i]["armor_value"] for i in
       ("amethystamulet", "bushamulet", "emberamulet")])
check("no item is left typed as gear with nowhere to wear it",
      [i["item_id"] for i in C.ITEMS.values()
       if i["type_name"] in ("WEAPON", "ARMOR")
       and str(i.get("equip_slot_name", "NONE")).upper() == "NONE"] == [],
      "the export warns about these every run; the list should stay empty")


# =============================================================================
section("A SLOT POINTS AT A BAG ITEM")
# =============================================================================

p = C.Player("warrior", 1)
for item_id in PLATE:
    p.equip(item_id)
held = C.bag(*PLATE)

check("nothing is pruned while you still hold it",
      C.prune_equipment(p.equipped, held) == p.equipped)

# SELLING THE SWORD YOU ARE SWINGING. Nothing throws; the slot simply points
# at something you no longer have, and once combat reads equipment it would
# quietly pay out a weapon that is not there.
sold = [e for e in held if e["item_id"] != "ironsword"]
after = C.prune_equipment(p.equipped, sold)
check("selling the sword empties the weapon slot", "weapon" not in after, after)
check("and leaves the other seven alone", len(after) == 7, after)

check("an empty bag undresses you completely",
      C.prune_equipment(p.equipped, []) == {})
check("pruning does not mutate what it was given",
      len(p.equipped) == 8, p.equipped)

# SWAPPING MOVES NOTHING. This is the whole argument for the shape: the old
# sword was never anywhere but the bag, so there is no second container for it
# to be lost in or duplicated into.
swapper = C.Player("warrior", 22)
both = C.bag("ironsword", "embersword")
swapper.equip("ironsword")
swapper.equip("embersword")
check("the better sword replaces the worse one",
      swapper.equipped["weapon"] == "embersword", swapper.equipped)
check("and the worse one is still in the bag",
      C.prune_equipment(swapper.equipped, both) == {"weapon": "embersword"})
check("taking it off returns what came off",
      swapper.unequip("weapon") == "embersword")
check("and the slot is gone, not blank",
      "weapon" not in swapper.equipped, swapper.equipped)
check("taking off an empty slot is not an error",
      swapper.unequip("weapon") == "")

# A HAND-EDITED OR OUT-OF-DATE SAVE.
nonsense = {
    "helm": "ironsword",        # a sword worn on the head
    "robe": "ironrobe",         # a slot that has never existed
    "chest": "sombrero",        # an item that has never existed
    "boots": "",                # a blank where a key should not be
    "legs": "ironlegs",         # the one honest entry
}
cleaned = C.prune_equipment(nonsense, C.bag("ironsword", "ironrobe", "ironlegs"))
check("a save full of nonsense keeps only what is real",
      cleaned == {"legs": "ironlegs"}, cleaned)
check("equipment that is not a dictionary prunes to nothing",
      C.prune_equipment(["ironsword"], held) == {})
check("a bag that is not an array undresses rather than crashes",
      C.prune_equipment(p.equipped, None) == {})
check("a bag with holes in it is read past, not tripped over",
      C.prune_equipment({"legs": "ironlegs"},
                        [None, {"item_id": "ironlegs", "quantity": 1}, None])
      == {"legs": "ironlegs"})


# =============================================================================
section("WHAT COMBAT WILL READ")
# =============================================================================

# Nothing reads these yet. They are checked now because the numbers they return
# are the ones that decide whether tier 1 reproduces today's balance - see
# warrior.gd:21, which has described this hole since before gear existed.
kitted = C.Player("warrior", 1)
for item_id in PLATE:
    kitted.equip(item_id)
check("an iron sword reads back its authored damage",
      kitted.equipped_weapon_damage() == 20, kitted.equipped_weapon_damage())
check("the full iron kit is worth 28 armour",
      kitted.equipped_armor_value() == 28, kitted.equipped_armor_value())

# RECORDED, NOT ASSERTED AWAY. itemdata.gd's note on `damage` says a tier 1
# sword carrying damage = 20 "reproduces today's balance exactly", because
# warrior.gd's base_melee_damage was exported at 20 when that was written. It
# is 24 now. Nothing is broken - nothing reads either number in the same breath
# yet - but the day #169 substitutes one for the other, a tier 1 warrior loses
# 17% of their swing, and this is the check that will say so first.
check("the tier 1 sword and warrior.gd's base have drifted apart (20 vs 24)",
      kitted.equipped_weapon_damage() == 20,
      "warrior.gd:130 base_melee_damage = 24")
check("an empty character has no weapon damage",
      C.Player("warrior", 1).equipped_weapon_damage() == 0)

ember = C.Player("warrior", 22)
for item_id in ["embersword", "emberhelm", "emberchest", "emberlegs",
                "emberboots", "embershield", "emberring", "emberamulet"]:
    ember.equip(item_id)
check("and the ladder climbs from there",
      ember.equipped_weapon_damage() > kitted.equipped_weapon_damage()
      and ember.equipped_armor_value() > kitted.equipped_armor_value(),
      [ember.equipped_weapon_damage(), ember.equipped_armor_value()])


# =============================================================================
section("THE LADDER, AND WHY IT IS NOT FLAT")
# =============================================================================
# A weapon ADDS to the class's own damage rather than replacing it, so a flat
# ladder is not a fair one. The healer fires ten shots a second and the tank
# ticks four times a second, which is why their base damage is 2 and 4 where
# the warrior's swing is 24. Authored flat, an ember scepter carrying the
# sword's 100 would have given the healer 1,080 dps against the warrior's 124.
#
# THE RULE IS ABOUT DPS, NOT ABOUT DAMAGE, and this section used to say
# otherwise. It asserted that each family adds the same proportion of its
# class's BASE DAMAGE - which sounds like the same statement and is not, because
# it ignores attack period. Under that rule the mage's weapon was scaled up by
# its base and then multiplied again by casting twice as fast, and the mage sat
# at 2.3x the warrior at every tier, identically, with the ladder carrying the
# imbalance forward instead of correcting it. mage.gd's "NINE, NOT TWENTY-FIVE"
# comment is the retune that followed.
#
# The rule the game implements now, and the one asserted below:
#
#     weapon damage = round(sword damage x attack period x class dps ratio)
#
# i.e. every class's weapon adds the same proportion of ITS OWN DPS that the
# sword adds to the warrior's. All fifteen non-sword weapons in the game are
# the correctly-rounded value of that expression.

BASE = dict(C.BASE_DAMAGE)          # read from the sim, not re-typed here:
PERIOD = dict(C.ATTACK_PERIOD)      # two copies of these drifted apart once.
FAMILY = {"warrior": "sword", "mage": "staff", "healer": "scepter",
          "tank": "maul"}
TIERS = ["iron", "jade", "cobalt", "amethyst", "ember"]


def base_dps(class_id):
    return BASE[class_id] / PERIOD[class_id]


def dps(class_id, tier):
    weapon = 0 if tier is None else C.ITEMS[tier + FAMILY[class_id]]["damage"]
    return (BASE[class_id] + weapon) / PERIOD[class_id]


# THE FOUR NUMBERS THE WHOLE LADDER HANGS OFF. Asserted first: if a class's
# unarmed dps has moved, every weapon below it is scaled to a figure that no
# longer exists, and the ladder checks would be measuring the wrong thing
# while passing. This is the drift that actually happened - the sim held mage
# 25, healer 3 and tank 9 through the entire retune that replaced them.
check("the warrior's unarmed dps is the reference, at 24/s",
      abs(base_dps("warrior") - 24.0) < 0.001, base_dps("warrior"))
check("the mage sits below it, not above - 0.85x was the target",
      abs(base_dps("mage") / base_dps("warrior") - 0.8333) < 0.01,
      base_dps("mage") / base_dps("warrior"))
check("so does the healer, at ten shots a second",
      abs(base_dps("healer") / base_dps("warrior") - 0.8333) < 0.01,
      base_dps("healer") / base_dps("warrior"))
check("and the tank lowest, because its tick hits everything in range",
      abs(base_dps("tank") / base_dps("warrior") - 0.6667) < 0.01,
      base_dps("tank") / base_dps("warrior"))

# THE LADDER ITSELF. Tolerance is exactly 0.5 because that IS the rule -
# "correctly rounded to a whole number of damage" - not a slack figure picked
# to make anything pass. The epsilon is float comparison, nothing else: the
# mage's iron and ember weapons land on .5 exactly.
for tier in TIERS:
    sword = C.ITEMS[tier + "sword"]["damage"]
    for class_id in ("mage", "healer", "tank"):
        item_id = tier + FAMILY[class_id]
        got = C.ITEMS[item_id]["damage"]
        ideal = sword * PERIOD[class_id] * (base_dps(class_id) / base_dps("warrior"))
        check("%s adds the same share of its class's dps as the sword does"
              % item_id, abs(got - ideal) <= 0.5 + 1e-9,
              [got, round(ideal, 3)])

# THE CONSEQUENCE, stated as the thing anyone would actually check: gear lifts
# every class's dps by the same multiple, so whatever the relationship between
# the four classes is today, it is the same afterwards. Gear is a ladder
# everyone climbs at one rate, not a rebalance in disguise.
#
# THE TOLERANCE IS DERIVED, NOT PICKED. Damage is an integer, so the ideal can
# be half a point away - and half a point of damage is a different amount of
# DPS for each class, because each divides by its own period. Half a point at
# the healer's 0.1s is five dps; at the warrior's 1.0s it is half of one. That
# is why the healer's ladder is the coarsest in the game, and why this is
# written as a derivation rather than as a flat percentage that would have been
# chosen to make the healer pass.
for class_id in BASE:
    slack_dps = 0.5 / PERIOD[class_id]
    for tier in TIERS:
        ratio = dps(class_id, tier) / base_dps(class_id)
        want = dps("warrior", tier) / base_dps("warrior")
        slack = slack_dps / base_dps(class_id)
        check("%s in %s gains what a warrior gains (x%.2f)"
              % (class_id, tier, want), abs(ratio - want) <= slack + 1e-9,
              [round(ratio, 3), round(want, 3), round(slack, 3)])

check("and the healer is the one whose ladder rounds coarsest",
      max(BASE, key=lambda c: (0.5 / PERIOD[c]) / base_dps(c)) == "healer",
      {c: round((0.5 / PERIOD[c]) / base_dps(c), 3) for c in BASE})

# THE GAP THAT USED TO BE RECORDED HERE IS CLOSED. This line said the mage did
# roughly 2.3x the warrior and always had. It does not any more - mage.gd's
# damage_per_magic went 25 -> 9, healer.gd's 3 -> 2, tank.gd's aura 9 -> 4 -
# and the figure is still printed rather than only asserted, because a number
# someone reads every run is how the next drift gets noticed early.
print("      (class dps in ember, relative to the warrior: %s)" % ", ".join(
    "%s %.2fx" % (c, dps(c, "ember") / dps("warrior", "ember")) for c in BASE))

# AND IT STAYS CLOSED. The print above is a courtesy; this is the guard. No
# class may out-damage the warrior on a single target at any tier, and none may
# fall below two thirds of it.
for tier in TIERS:
    spread = {c: dps(c, tier) / dps("warrior", tier) for c in BASE}
    check("no class out-damages the warrior single-target in %s" % tier,
          max(spread.values()) <= 1.0 + 1e-9, spread)
    check("and none is left behind in %s" % tier,
          min(spread.values()) >= 0.63, spread)

# A spread of 0 would make every hit identical, which is the thing the field
# exists to prevent. Checked on the whole catalogue rather than one weapon,
# because the default is what every .tres relies on.
weapons = [i for i in C.ITEMS.values() if i["damage"] > 0]
check("every weapon in the game rolls rather than repeats",
      all(i.get("damage_spread", 0) > 0 for i in weapons), len(weapons))
check("and there are twenty of them", len(weapons) == 20, len(weapons))

lo = int(20 * 0.75)
hi = -(-int(20 * 1.25) // 1)
check("an iron sword lands somewhere in 15-25",
      (lo, hi) == (15, 25), (lo, hi))


# =============================================================================
section("ARMOUR, AND A BOSS THAT IS STILL A FIGHT")
# =============================================================================
HALF_POINT = 200.0


def armour_reduction(value):
    return 0.0 if value <= 0 else value / (value + HALF_POINT)


# NAMED FOR SLOTS, not for the item list called PLATE at the top of this
# file. The two were briefly the same name, and the round-trip section
# below then equipped seven items called "helm", "chest" and so on - none
# of which exist - and reported that the server had stored nothing.
PLATE_SLOTS = {"helm", "chest", "legs", "boots", "shield", "ring", "amulet"}
kit = {}
for tier in TIERS:
    worn = [i for i in C.ITEMS.values()
            if i["item_id"].startswith(tier)
            and C.slot_name(i["equip_slot"]) in PLATE_SLOTS
            and ("warrior" in i["required_classes"]
                 or not i["required_classes"])]
    kit[tier] = sum(i["armor_value"] for i in worn)

check("wearing nothing takes nothing off", armour_reduction(0) == 0.0)
check("a full iron kit is 28 armour", kit["iron"] == 28, kit["iron"])
check("a full ember kit is 167", kit["ember"] == 167, kit["ember"])
check("iron takes off about an eighth",
      0.11 < armour_reduction(kit["iron"]) < 0.14,
      round(armour_reduction(kit["iron"]), 3))
check("ember takes off about 45%",
      0.43 < armour_reduction(kit["ember"]) < 0.47,
      round(armour_reduction(kit["ember"]), 3))
# The reason it is a curve and not a subtraction: no amount of gear may ever
# reach zero, because an immortal character is only fixable by inflating enemy
# damage until an under-geared one is deleted by the same attack.
check("no amount of armour ever reaches zero",
      armour_reduction(100000) < 1.0, armour_reduction(100000))
check("and stacked with the best defense tier it still is not immunity",
      (1 - 0.50) * (1 - armour_reduction(kit["ember"])) > 0.25,
      (1 - 0.50) * (1 - armour_reduction(kit["ember"])))

# A BOSS IS THE ONE FIGHT LONG ENOUGH FOR GEAR TO SHOW, which makes it the one
# that gear can erase. Ungeared, these took 29-46 seconds; the weapon ladder
# alone would have cut that to 9-13. Boss health is x4 so the fight lands back
# where it was FOR A PLAYER WHO BROUGHT THE GEAR - and is genuinely out of
# reach for one who did not, which is the point of a boss.
BOSS_FIGHT = [("lightboss", "cobalt"), ("boss", "cobalt"),
              ("iceboss", "amethyst"), ("earthboss", "amethyst"),
              ("fireboss", "ember")]
LEVEL = {"iron": 1, "jade": 5, "cobalt": 10, "amethyst": 16, "ember": 22}

for boss, tier in BOSS_FIGHT:
    weapon = C.ITEMS[tier + "sword"]["damage"]
    mult = 1 + (1 + LEVEL[tier] * 2 - 1) * 0.02
    rate = int((BASE["warrior"] + weapon) * mult) / PERIOD["warrior"]
    seconds = C.ENEMIES[boss]["max_hp"] / rate if boss in C.ENEMIES else 0
    check("%s in %s is a %d-second fight, not a formality"
          % (boss, tier, seconds), 25 <= seconds <= 60, round(seconds, 1))

# Trash is meant to die fast. The check is that it does not become INSTANT,
# which is what would happen if boss health were scaled and nothing else were
# looked at.
for mob, tier in [("lightslime", "iron"), ("bushsniper", "iron"),
                  ("bushmage", "jade"), ("darkbushmage", "cobalt")]:
    weapon = C.ITEMS[tier + "sword"]["damage"]
    mult = 1 + (1 + LEVEL[tier] * 2 - 1) * 0.02
    rate = int((BASE["warrior"] + weapon) * mult) / PERIOD["warrior"]
    seconds = C.ENEMIES[mob]["max_hp"] / rate
    check("%s still takes more than one swing" % mob, seconds >= 1.0,
          round(seconds, 1))


# =============================================================================
section("THE PAPER DOLL")
# =============================================================================
# Eight squares that accept a drag from the backpack. The square decides
# whether the cursor lights up; CharacterData decides whether it sticks; the
# player decides whether it is allowed at all. These check the first of those,
# because it is the one a player experiences as the panel working or not.

DOLL = ["weapon", "helm", "chest", "legs", "boots", "shield", "ring", "amulet"]

check("the doll has a square for every slot the enum defines",
      sorted(DOLL) == sorted(C.slot_names()), sorted(DOLL))

w = C.Player("warrior", 22)

# THE SQUARE, NOT ANY SQUARE. A helm dropped on the chest is refused rather
# than redirected: the cursor going dead is how a player learns where a piece
# goes, and guessing for them looks like a bug the first time it guesses wrong.
check("a helm is accepted by the helm square",
      C.can_drop("helm", "ironhelm", w) is True)
check("and refused by the chest square",
      C.can_drop("chest", "ironhelm", w) is False)
check("a sword goes in the weapon square only",
      C.can_drop("weapon", "ironsword", w)
      and not C.can_drop("shield", "ironsword", w))
check("boots have a square of their own now",
      C.can_drop("boots", "ironboots", w) is True)

# The three rules the square asks the character about, each refused at the
# cursor rather than after the click.
check("a warrior's chest square refuses a mage's robe",
      C.can_drop("chest", "ironrobe", w) is False)
check("but a mage's does not",
      C.can_drop("chest", "ironrobe", C.Player("mage", 22)) is True)
check("a level 1 warrior cannot drop an ember sword in",
      C.can_drop("weapon", "embersword", C.Player("warrior", 1)) is False)
check("at 22 the same drop is accepted",
      C.can_drop("weapon", "embersword", w) is True)
check("nothing that is not equipment is accepted anywhere",
      not any(C.can_drop(s, "tinyhealthpotion", w) for s in DOLL))
# The bush amulet used to be the second half of that check - typed as gear,
# wearable nowhere, so every square refused it. It has a slot now, so the
# assertion is the opposite one: exactly ONE square takes it, which is the
# property that actually matters and the one a missing slot would break.
check("a boss trophy goes in exactly one square",
      [s for s in DOLL if C.can_drop(s, "bushamulet", w)] == ["amulet"],
      [s for s in DOLL if C.can_drop(s, "bushamulet", w)])

# A square that already holds something still takes a replacement - the old
# piece was never anywhere but the bag, so there is nothing to make room for.
w.equip("ironsword")
check("a full square still accepts a better sword",
      C.can_drop("weapon", "embersword", w) is True)
check("and the swap leaves the old one in the bag",
      C.prune_equipment({"weapon": "ironsword"}, C.bag("ironsword")) ==
      {"weapon": "ironsword"})


# =============================================================================
section("WHAT THE DOLL PRINTS UNDERNEATH")
# =============================================================================
# Four lines: what you hit for, what that is per second, what you are wearing,
# and what it takes off an incoming hit. The last one is the only one that
# means anything on its own - "Armour: 28" is a position on a ladder nobody has
# seen, and a percentage is what the player feels.

bare = C.Player("warrior", 22)
check("an unarmed warrior still hits for something",
      C.summary(bare)["Damage"] == "24 - 24", C.summary(bare))
check("and takes no less damage than anyone",
      C.summary(bare)["Damage taken"] == "0% less", C.summary(bare))

kitted = C.Player("warrior", 22)
for piece in ["ironsword", "ironhelm", "ironchest", "ironlegs", "ironboots",
              "ironshield", "ironring", "ironamulet"]:
    kitted.equip(piece)
s = C.summary(kitted)
check("a full iron kit widens the damage into a band",
      s["Damage"] == "39 - 49", s)
check("prints the armour it adds up to", s["Armour"] == "28", s)
check("and what that is actually worth", s["Damage taken"] == "12% less", s)

ember = C.Player("warrior", 22)
for piece in ["embersword", "emberhelm", "emberchest", "emberlegs",
              "emberboots", "embershield", "emberring", "emberamulet"]:
    ember.equip(piece)
e = C.summary(ember)
check("ember plate roughly halves what gets through",
      e["Damage taken"] == "46% less", e)
check("and the damage band has climbed with it",
      int(e["Damage"].split(" - ")[1]) > int(s["Damage"].split(" - ")[1]),
      [e["Damage"], s["Damage"]])

# THE LINE THE WHOLE LADDER RESCALE EXISTS FOR. Four classes, one tier, and
# the summary should show them all gaining the same multiple - the per-hit
# numbers differ wildly and the dps does not.
tiered = {}
for class_id, weapon in [("warrior", "embersword"), ("mage", "emberstaff"),
                         ("healer", "emberscepter"), ("tank", "embermaul")]:
    p = C.Player(class_id, 22)
    p.equip(weapon)
    bare_p = C.Player(class_id, 22)
    tiered[class_id] = (int(C.summary(p)["Damage per second"])
                        / int(C.summary(bare_p)["Damage per second"]))
# THE BOUND IS DERIVED PER CLASS, NOT ONE FLAT NUMBER FOR ALL FOUR.
#
# It was 0.25 for everybody, and the real spread is exactly 0.25 - healer 5.00
# against tank 5.25 - so this failed on a ladder that is correctly authored.
# A flat bound cannot be right here: the multiple is 1 + weapon/base, so a half
# point of rounding on the weapon moves it by 0.5/base, and base runs from 24
# down to 2. The same half point is 0.02 to the warrior and 0.25 to the healer.
#
# Two sources, both counted:
#   0.5 / BASE[c]        the weapon damage is a whole number
#   2.0 / base_dps(c)    the panel prints dps as a whole number, and this
#                        ratio is built from two of those figures
warrior_gain = tiered["warrior"]
for class_id, gain in sorted(tiered.items()):
    slack = 0.5 / BASE[class_id] + 2.0 / base_dps(class_id)
    check("the %s gains what the warrior gains from an ember weapon" % class_id,
          abs(gain - warrior_gain) <= slack + 1e-9,
          [round(gain, 3), round(warrior_gain, 3), round(slack, 3)])

check("and it is a little over five times",
      4.7 < min(tiered.values()) < 5.6,
      {k: round(v, 2) for k, v in tiered.items()})


# =============================================================================
section("WHAT THE TOOLTIP SAYS")
# =============================================================================
# itemtooltip.tscn has had a stats panel since it was first drawn - three rows
# reading "Value: 100", "Tier: 1", "Required Level: 1". The script never
# touched any of them. Those are the placeholder strings typed into the scene,
# and every item in the game showed the same three numbers because nothing ever
# overwrote them. It survived because it does not look like a bug: the panel is
# there, it is styled, the numbers are plausible. You have to hover two items
# and notice they did not change.
import tooltip_sim as T   # noqa: E402

WARRIOR = dict(class_id="warrior", level=22, skills={"cooking": 12},
               equipped={"weapon": "ironsword", "chest": "ironchest"})


def rows(item_id, quantity=1, **over):
    args = dict(WARRIOR)
    args.update(over)
    return {label: (value, colour) for label, value, colour
            in T.rows_for(item_id, quantity, args["class_id"], args["level"],
                          args["skills"], args["equipped"])}


r = rows("embersword")
check("a weapon names its slot", r["Slot"][0] == "Weapon", r.get("Slot"))
check("and shows the band it rolls in, not the midpoint",
      r["Damage"][0] == "75 - 125", r.get("Damage"))
check("and what that is worth per second",
      r["Damage per second"][0] == "100", r.get("Damage per second"))
check("and what it is worth in gold", "gold" in r["Value"][0], r.get("Value"))

# THE ROW THE WHOLE EXERCISE IS FOR. An ember scepter is 13 damage and an ember
# sword is 100, for the same gold, because one fires ten times a second. Per-hit
# numbers make that pair look like a swindle and a bargain.
sword = rows("embersword")
scepter = rows("emberscepter", class_id="healer",
               equipped={"weapon": "ironscepter"})
check("a scepter's per-hit damage looks tiny beside a sword's",
      int(scepter["Damage"][0].split(" - ")[1]) * 5
      < int(sword["Damage"][0].split(" - ")[1]),
      [scepter["Damage"][0], sword["Damage"][0]])
check("and its damage per second does not",
      abs(int(scepter["Damage per second"][0])
          - int(sword["Damage per second"][0]))
      < int(sword["Damage per second"][0]),
      [scepter["Damage per second"][0], sword["Damage per second"][0]])

# The comparison against what you have on - the question a player is actually
# asking when they hover something on the ground.
check("an upgrade is green and signed",
      rows("embersword")["vs Iron Sword"] == ("+80", "GOOD"),
      rows("embersword").get("vs Iron Sword"))
check("the piece you are already wearing compares to nothing",
      "vs Iron Sword" not in rows("ironsword"), rows("ironsword"))
check("and neither does one for a slot you have empty",
      not any(k.startswith("vs ") for k in rows("ironhelm")), rows("ironhelm"))

# ACROSS CLASSES IT SAYS NOTHING, and the first version of this did not. An
# ember scepter hovered by a warrior read "vs Iron Sword: -7" - both numbers
# real, the subtraction correct, the row nonsense.
cross = rows("emberscepter")
check("a healer's scepter is not compared to a warrior's sword",
      not any(k.startswith("vs ") for k in cross), cross)
check("nor given a dps only a healer would get",
      "Damage per second" not in cross, cross)
check("but it does say whose it is, in red",
      cross["Class"] == ("Healer", "BAD"), cross.get("Class"))

check("a robe tells a warrior it is not theirs",
      rows("ironrobe")["Class"][1] == "BAD", rows("ironrobe").get("Class"))
check("a ring belongs to nobody in particular, so says nothing about class",
      "Class" not in rows("ironring"), rows("ironring"))

# Requirements, shown before the click rather than after it.
low = rows("jadesword", level=4, equipped={"weapon": "ironsword"})
check("a level you have not reached is red",
      low["Required level"] == ("5", "BAD"), low.get("Required level"))
check("but the upgrade above it is still green",
      low["vs Iron Sword"][1] == "GOOD", low.get("vs Iron Sword"))
met = rows("jadesword", level=22, equipped={})
check("a level you have reached is not",
      met["Required level"][1] == "neutral", met.get("Required level"))

fish = rows("cookedreefclown")
check("a skill gate names the skill",
      "Required Cooking" in fish, sorted(fish))
check("and is red at cooking 12 against a gate of 70",
      fish["Required Cooking"][1] == "BAD", fish.get("Required Cooking"))

# Rows an item does not have do not appear, which is the difference between a
# stats panel and a form.
potion = rows("tinyhealthpotion")
check("a potion says what it restores",
      potion["Restores"] == ("20 HP", "GOOD"), potion.get("Restores"))
check("and has no slot, damage or armour row",
      not {"Slot", "Damage", "Armour"} & set(potion), sorted(potion))
check("a stack prices the whole stack",
      rows("tinyhealthpotion", 16)["Value"][0] == "50 gold  (800)",
      rows("tinyhealthpotion", 16).get("Value"))
check("a single one does not",
      potion["Value"][0] == "50 gold", potion.get("Value"))

trophy = rows("bushamulet")
check("the bush amulet now prints a slot like any other gear",
      "Slot" in trophy and "Armour" in trophy and "gold" in trophy["Value"][0],
      sorted(trophy))
check("and says what it takes to wear",
      trophy.get("Required level", [""])[0].startswith("22"),
      trophy.get("Required level"))

# EVERY ITEM IN THE CATALOGUE, not just the interesting ones: the failure this
# replaces was a panel that rendered the same three rows for all 123.
seen = set()
for item_id in C.ITEMS:
    got = rows(item_id)
    check_rows = len(got)
    seen.add(tuple(sorted(got)))
    if check_rows == 0 and C.ITEMS[item_id]["value"] > 0:
        check("%s has at least one row" % item_id, False, got)
check("the 123 items do not all render the same rows", len(seen) > 8, len(seen))


# =============================================================================
section("THE SAVE BODY")
# =============================================================================

slot = {"character": "warrior", "level": 1}
check("a slot that has never had gear says nothing about it",
      "equipment" not in C.save_body(0, slot)
      and "hotbar" not in C.save_body(0, slot), C.save_body(0, slot))

# The distinction that matters: `{}` is "take everything off", an absent key is
# "leave it alone". A save file written before equipment existed has no key,
# and the first save after upgrading must not undress a character.
slot["equipment"] = {"weapon": "ironsword"}
slot["hotbar_assignments"] = ["tinyhealthpotion"]
body = C.save_body(0, slot)
check("once it does, it is sent", body["equipment"] == {"weapon": "ironsword"},
      body)
check("a short hotbar is padded to nine on the way out",
      body["hotbar"] == ["tinyhealthpotion"] + [""] * 8, body["hotbar"])

slot["equipment"] = {}
check("and taking everything off is sent explicitly",
      C.save_body(0, slot)["equipment"] == {})

back = C.slot_from_server({
    "class_id": "warrior",
    "equipment": {"weapon": "ironsword"},
    "hotbar": ["tinyhealthpotion", "", "ironsword"],
    "status": {"level": 4},
})
check("what comes back lands under the client's own key names",
      back["equipment"] == {"weapon": "ironsword"}
      and back["hotbar_assignments"][2] == "ironsword", back)
check("a hotbar of the wrong length is still nine on the way in",
      len(C.slot_from_server({"hotbar": ["a"] * 40})["hotbar_assignments"]) == 9)
check("and a missing one is nine empty keys",
      C.slot_from_server({})["hotbar_assignments"] == [""] * 9)


# =============================================================================
section("THE ROUND TRIP, AGAINST THE REAL SERVER")
# =============================================================================

os.environ.setdefault("ELUSION_OWNER", "simowner")
_fd, _db = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["ELUSION_DB"] = _db

import app  # noqa: E402  - imported here so the env above is in place

app.app.config["TESTING"] = True
client = app.app.test_client()

registered = client.post("/api/auth/register",
                         json={"username": "gearcheck",
                               "password": "Sufficiently-Long-1"})
H = {"Authorization": "Bearer %s" % registered.get_json()["token"]}


def server_slot(index=0):
    body = client.get("/api/save", headers=H).get_json()
    rows = body if isinstance(body, list) else (body.get("saves")
                                               or body.get("slots") or [])
    for row in rows:
        if row.get("slot") == index:
            return row
    return {}


# The client's own _save_body(), posted as the client would post it.
player = C.Player("warrior", 1)
for item_id in PLATE:
    player.equip(item_id)
local = {"character": "warrior", "level": 1, "equipment": dict(player.equipped),
         "hotbar_assignments": ["tinyhealthpotion", "", "ironsword"]}

res = client.put("/api/save", headers=H, json=C.save_body(0, local))
check("the body ServerStorage builds is one the server accepts",
      res.status_code == 200, res.get_json())

# EQUIPMENT NO LONGER ARRIVES THIS WAY, and the save above proves only that a
# client still sending it is not broken by that. Getting dressed is
# /api/character/equip, which takes each piece OUT of the backpack - the take
# being the ownership check - so the fixture has to own the gear first.
import sqlite3 as _eqsq
_eq = _eqsq.connect(_db)
_uid = _eq.execute("SELECT id FROM users WHERE username = 'gearcheck'").fetchone()[0]
for _pos, _item in enumerate(PLATE):
    _eq.execute("INSERT OR REPLACE INTO carry_items (user_id, slot, position,"
                " item_id, quantity) VALUES (?, 0, ?, ?, 1)", (_uid, _pos, _item))
_eq.commit(); _eq.close()

_worn_ok = True
for _item in PLATE:
    if client.post("/api/character/equip", headers=H,
                   json={"slot": 0, "item_id": _item}).status_code != 200:
        _worn_ok = False
check("every piece equips through the endpoint", _worn_ok)

row = server_slot()
check("all eight pieces came back", len(row.get("equipment", {})) == 8,
      row.get("equipment"))
check("and the hotbar with them",
      row.get("hotbar", [])[2] == "ironsword", row.get("hotbar"))

# The full circle: server -> _slot_from_server() -> CharacterData ->
# load_character_state() -> the player. This is the path a re-login takes, and
# the one the hotbar never used to survive.
restored = C.slot_from_server({
    "class_id": row.get("class_id", ""),
    "equipment": row.get("equipment", {}),
    "hotbar": row.get("hotbar", []),
    "status": {"level": row.get("level", 1)},
})
reloaded = C.Player("warrior", 1)
reloaded.equipped = C.prune_equipment(restored["equipment"], C.bag(*PLATE))
check("a re-login puts the same eight pieces back on",
      reloaded.equipped == player.equipped, reloaded.equipped)
check("and the hotbar survives it too",
      restored["hotbar_assignments"][2] == "ironsword")

# A save that says nothing about gear must not undress anyone - and the body
# the client builds for a gear-less slot is exactly that save.
res = client.put("/api/save", headers=H,
                 json=C.save_body(0, {"character": "warrior", "level": 1}))
check("a save from a slot with no gear key is accepted", res.status_code == 200)
check("and leaves the eight pieces exactly where they were",
      len(server_slot().get("equipment", {})) == 8,
      server_slot().get("equipment"))

# What the client refuses, the server refuses too. Same three questions, same
# order - the client greys the slot out, the server makes it the rule.
for label, equipment in [
    ("a sword worn on the head", {"helm": "ironsword"}),
    ("a warrior in a mage's robe", {"chest": "ironrobe"}),
    ("a level 1 character in a level 22 sword", {"weapon": "embersword"}),
    ("a slot that has never existed", {"robe": "ironrobe"}),
]:
    # AGAINST THE ENDPOINT, not /api/save. The save ignores equipment now, so
    # comparing a client refusal to a 400 there would be comparing it to a
    # door that no longer exists. The three questions are unchanged; only the
    # place that asks them moved.
    _item = list(equipment.values())[0]
    res = client.post("/api/character/equip", headers=H,
                      json={"slot": 0, "item_id": _item})
    client_verdict = C.Player("warrior", 1).equip_check(_item)
    check("%s: refused by both" % label,
          res.status_code in (400, 403, 404)
          and (not client_verdict["ok"]
               or client_verdict["slot"] != list(equipment)[0]),
          [res.status_code, client_verdict])

check("and not one refusal disturbed what was stored",
      len(server_slot().get("equipment", {})) == 8,
      server_slot().get("equipment"))

# Verdict first, housekeeping after, and housekeeping cannot change the verdict
# - see the note at the end of test_healing.py, which is where deleting the
# scratch file before printing the result turned thirty passing checks into a
# failed suite with no summary line at all.
print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)

import gc  # noqa: E402  - wanted only for the teardown below
gc.collect()
try:
    os.unlink(_db)
except OSError as exc:
    print("  note: could not remove %s (%s)" % (_db, exc))

raise SystemExit(1 if failed else 0)
