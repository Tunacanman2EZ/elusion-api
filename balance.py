"""
Time-to-kill, measured before anything is changed.

Every formula here is transcribed from the class scripts. The point is not
precision to the frame - it is that a proposed weapon number can be argued
against a fight length rather than against a feeling.

    warrior   int(base_melee_damage * mult)      one swing per attack_lock_duration
    mage      int(damage_per_magic * mult)       one cast per spell_cooldown
    healer    int(damage_per_magic * mult)       one shot per shot_cooldown
    tank      int(aura_damage * mult)            one tick per aura_tick

    mult = 1 + (attack-1)*0.01 + (magic-1)*0.01     PlayerStats.damage_multiplier
"""

import json
import math

GD = json.load(open("gamedata.json"))
ENEMIES = {e["enemy_id"]: e for e in GD["enemies"]}
ITEMS = {i["item_id"]: i for i in GD["items"]}

# base damage, seconds per hit, whether it hits more than one thing
CLASSES = {
    "warrior": {"base": 24, "period": 1.00, "aoe": True,  "weapon": "sword"},
    "mage":    {"base": 25, "period": 0.45, "aoe": True,  "weapon": "staff"},
    "healer":  {"base": 3,  "period": 0.10, "aoe": False, "weapon": "scepter"},
    "tank":    {"base": 9,  "period": 0.25, "aoe": True,  "weapon": "maul"},
}

TIERS = [("iron", 1), ("jade", 5), ("cobalt", 10), ("amethyst", 16),
         ("ember", 22)]

# What you are actually fighting while wearing each tier, read off
# max_loot_tier: a mob that can drop tier N gear is the mob you farm in it.
CONTENT = {
    "iron":     ["lightslime", "firesprite", "bushsniper"],
    "jade":     ["firesprite", "bushmage", "icebushmage"],
    "cobalt":   ["darkslime", "darksprite", "darkbushmage"],
    "amethyst": ["lightboss", "boss", "iceboss"],
    "ember":    ["boss", "earthboss", "fireboss"],
}


def mult(skill_level):
    # attack and magic both climb; modelled as equal, which is generous to
    # nobody in particular and keeps one number on the axis.
    return 1.0 + (skill_level - 1) * 0.01 + (skill_level - 1) * 0.01


def dps(class_id, weapon_damage, skill_level):
    spec = CLASSES[class_id]
    per_hit = int((spec["base"] + weapon_damage) * mult(skill_level))
    return per_hit / spec["period"], per_hit


def ttk(class_id, enemy_id, weapon_damage, skill_level):
    rate, _ = dps(class_id, weapon_damage, skill_level)
    return ENEMIES[enemy_id]["max_hp"] / rate


def weapon_damage(tier, class_id, ladder=None):
    if ladder is not None:
        return ladder[tier][class_id]
    return ITEMS[tier + CLASSES[class_id]["weapon"]]["damage"]


def table(title, skill_of_level, ladder=None, with_weapon=True):
    print("\n" + title)
    print("-" * len(title))
    head = "%-9s %-4s %-8s" % ("tier", "lvl", "class")
    print(head + " %6s %8s   %s" % ("hit", "dps", "seconds to kill"))
    for tier, req_level in TIERS:
        for class_id in ("warrior", "mage", "healer", "tank"):
            wd = weapon_damage(tier, class_id, ladder) if with_weapon else 0
            skill = skill_of_level(req_level)
            rate, per_hit = dps(class_id, wd, skill)
            times = []
            for enemy_id in CONTENT[tier]:
                times.append("%s %.0fs" % (enemy_id[:12],
                                           ttk(class_id, enemy_id, wd, skill)))
            print("%-9s %-4d %-8s %6d %8.1f   %s"
                  % (tier, req_level, class_id, per_hit, rate, "  ".join(times)))


# A character's attack/magic skill at a given character level. Skills climb
# from use rather than from levelling, so this is an estimate - but a
# consistent one, and the shape of the answer does not depend on it.
def skills_at(level):
    return 1 + level * 2


print("=" * 78)
print("BASELINE - what the game does today, with no gear read at all")
print("=" * 78)
table("no weapon (today)", skills_at, with_weapon=False)

print()
print("=" * 78)
print("ADDITIVE, AT THE LADDER AS AUTHORED")
print("=" * 78)
table("base + authored weapon damage", skills_at)

print()
print("=" * 78)
print("THE LADDER ITSELF")
print("=" * 78)
for family in ("sword", "maul", "staff", "scepter"):
    row = [(t, ITEMS[t + family]["damage"]) for t, _ in TIERS]
    steps = ["%.2f" % (row[i + 1][1] / row[i][1]) for i in range(len(row) - 1)]
    print("%-9s %s     steps %s   total x%.1f"
          % (family, " ".join("%-4d" % d for _, d in row), " ".join(steps),
             row[-1][1] / row[0][1]))

print()
print("enemy ladder: %d -> %d  (x%.1f)"
      % (min(e["max_hp"] for e in ENEMIES.values()),
         max(e["max_hp"] for e in ENEMIES.values()),
         max(e["max_hp"] for e in ENEMIES.values())
         / min(e["max_hp"] for e in ENEMIES.values())))


# =============================================================================
# THE PROPOSAL
# =============================================================================
# The ladder as authored is a REPLACEMENT ladder wearing an additive hat. A flat
# 20/32/48/70/100 is a sensible "this is your whole damage" number for someone
# swinging once a second. The healer fires ten times a second and the tank ticks
# four times a second - their base damage is 3 and 9 for exactly that reason -
# so adding the same flat number to their hit gives the healer 1,370 dps in
# ember and the warrior 233.
#
# So the weapon contributes a PROPORTION OF ITS OWN CLASS'S BASE, and the sword
# ladder sets what that proportion is at each tier. The sword is unchanged,
# which is what was asked for; the other three families are re-derived from it.
#
# IT PRESERVES TODAY'S CLASS BALANCE EXACTLY. Every class's dps is multiplied by
# the same number at the same tier, so whatever the warrior/mage/healer/tank
# relationship is now - tuned by playing them - it is the same afterwards. Gear
# is a ladder everyone climbs at one rate, not a rebalance in disguise.
SWORD = {"iron": 20, "jade": 32, "cobalt": 48, "amethyst": 70, "ember": 100}
MULTIPLE = {tier: SWORD[tier] / CLASSES["warrior"]["base"] for tier in SWORD}

# math.floor(x + 0.5), not round(): Python rounds halves to even, so a
# scepter at 3 x 0.8333 = 2.5 came out as 2 while the .tres says 3.
PROPOSED = {
    tier: {c: max(1, int(math.floor(CLASSES[c]["base"] * MULTIPLE[tier] + 0.5)))
           for c in CLASSES}
    for tier in SWORD
}

print()
print("=" * 78)
print("PROPOSED LADDER")
print("=" * 78)
print("%-9s %-6s  %s" % ("tier", "x base", "  ".join(
    "%-8s" % (c + "/" + CLASSES[c]["weapon"]) for c in CLASSES)))
for tier, _ in TIERS:
    print("%-9s %-6.3f  %s" % (tier, MULTIPLE[tier], "  ".join(
        "%-8d" % PROPOSED[tier][c] for c in CLASSES)))
print()
print("authored, for comparison:")
for family, class_id in [("sword", "warrior"), ("staff", "mage"),
                         ("scepter", "healer"), ("maul", "tank")]:
    was = [ITEMS[t + family]["damage"] for t, _ in TIERS]
    now = [PROPOSED[t][class_id] for t, _ in TIERS]
    print("  %-8s was %-28s now %s" % (family, " ".join("%-4d" % d for d in was),
                                       " ".join("%-4d" % d for d in now)))

table("base + proposed weapon damage", skills_at, ladder=PROPOSED)

print()
print("=" * 78)
print("WHAT THAT DOES TO A BOSS")
print("=" * 78)
print("A boss is the one fight long enough for the gear to show. These are the")
print("numbers a warrior brings to it, and what the boss would need to stay a")
print("fight rather than a formality.")
print()
print("%-10s %-9s %8s %8s %10s %10s" % (
    "boss", "in", "hp now", "ttk now", "hp for 45s", "multiple"))
for boss, tier in [("lightboss", "cobalt"), ("boss", "cobalt"),
                   ("iceboss", "amethyst"), ("earthboss", "amethyst"),
                   ("fireboss", "ember")]:
    level = dict(TIERS)[tier]
    rate, _ = dps("warrior", PROPOSED[tier]["warrior"], skills_at(level))
    hp = ENEMIES[boss]["max_hp"]
    want = rate * 45
    print("%-10s %-9s %8d %7.0fs %10.0f %9.1fx"
          % (boss, tier, hp, hp / rate, want, want / hp))
