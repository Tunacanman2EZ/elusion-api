"""
gamedata.py - loads gamedata.json and rolls kill rewards on the server.

This is the Python half of the split that exportgamedata.gd makes: Godot
authors the data, this reads it. Nothing in here restates a number that lives
in the game; if a value is not in the JSON, that is a bug in the exporter, not
something to paper over with a default.

WHY THE SERVER ROLLS AT ALL
---------------------------
Every reward in Elusion used to be decided on the player's own machine.
BaseEnemy._die() granted the XP, rolled the loot, and spawned the bag, all
client-side. A modified client could hand itself every pet in the game. Moving
the roll here is the fix, and it only works if the client never learns the
outcome before the server has committed it.

FAITHFULNESS TO THE GDSCRIPT
----------------------------
Each roll below is a direct port, with the Godot call it replaces named in a
comment. The equivalences that matter:

    Godot                          Python
    randf()                        rng.random()          both [0.0, 1.0)
    randi_range(a, b)              rng.randint(a, b)     both INCLUSIVE
    randi() % n                    rng.randrange(n)      both [0, n)

randint being inclusive at both ends is the one people get wrong, and getting
it wrong here would silently shift every gold drop and the pet odds by one.
"""

import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
GAMEDATA_PATH = os.environ.get("ELUSION_GAMEDATA", os.path.join(HERE, "gamedata.json"))

# Bump in lockstep with SCHEMA_VERSION in exportgamedata.gd. A mismatch means
# the JSON was produced by a different version of the exporter than this file
# was written against - refuse it rather than read a field whose meaning has
# quietly changed.
EXPECTED_SCHEMA = 1

# NOT random.random(). The module-level functions use a Mersenne Twister, and
# a Mersenne Twister's entire future output can be reconstructed from 624
# observed values. Every loot roll in this file is an observed value. Someone
# farming slimes and logging the drops could, in principle, predict exactly
# when the 1-in-1296 pet lands and be standing there for it.
#
# SystemRandom reads the OS entropy pool and has no recoverable internal state.
# It is slower, which is irrelevant at one roll per kill.
_rng = random.SystemRandom()


# =============================================================================
# LOADING
# =============================================================================

class GameDataError(RuntimeError):
    """Raised when gamedata.json is missing, malformed, or the wrong schema."""


def _load():
    if not os.path.exists(GAMEDATA_PATH):
        raise GameDataError(
            "gamedata.json not found at %s. Run src/tools/exportgamedata.gd "
            "from the Godot editor (Ctrl+Shift+X) and copy the result here, or "
            "point ELUSION_GAMEDATA at it." % GAMEDATA_PATH
        )

    with open(GAMEDATA_PATH, "r", encoding="utf-8") as handle:
        raw = json.load(handle)

    schema = raw.get("schema")
    if schema != EXPECTED_SCHEMA:
        raise GameDataError(
            "gamedata.json is schema %r, this server expects %r. Re-run the "
            "exporter, or update gamedata.py to match." % (schema, EXPECTED_SCHEMA)
        )

    items = {row["item_id"]: row for row in raw.get("items", [])}
    enemies = {row["enemy_id"]: row for row in raw.get("enemies", [])}
    classes = {row["class_id"]: row for row in raw.get("classes", [])}

    if not items:
        raise GameDataError("gamedata.json contains no items.")
    if not enemies:
        raise GameDataError("gamedata.json contains no enemies.")

    # Classes may be absent in a gamedata.json produced before they were
    # exported. Not fatal: max_stats_for() falls back to trusting the client,
    # which is where the server already was. An empty roster with no warning
    # would be worse - it would look like the check was running.
    if not classes:
        print("gamedata: no classes in %s - max_hp/max_mana/max_stamina cannot "
              "be verified. Re-run exportgamedata.gd." % GAMEDATA_PATH)

    return raw.get("constants", {}), items, enemies, classes


# Loaded once at import. Failing here takes the whole server down on start,
# which is correct: a server that cannot roll loot should not accept kills and
# quietly grant nothing.
CONSTANTS, ITEMS, ENEMIES, CLASSES = _load()


def max_stats_for(class_id, level):
    """
    What a character of this class SHOULD have at this level.

    Port of Player._recompute_max_stats():

        max_hp = hp_base + (level - 1) * hp_per_lvl

    Level 1 gets exactly the base, which is what the -1 is for. Both
    implementations must agree on that or every character is one level out.

    Returns None for an unknown class, meaning "no opinion" - the caller then
    leaves the client's values alone rather than zeroing a character it cannot
    describe.
    """
    curve = CLASSES.get(class_id)
    if curve is None:
        return None

    steps = max(int(level) - 1, 0)
    return {
        "max_hp": int(curve["hp_base"]) + steps * int(curve["hp_per_lvl"]),
        "max_mana": int(curve["mana_base"]) + steps * int(curve["mana_per_lvl"]),
        "max_stamina": int(curve["stam_base"]) + steps * int(curve["stam_per_lvl"]),
    }


def has_item(item_id):
    """Mirrors ItemRegistry.has_item()."""
    return item_id in ITEMS


# Item types the loot roll never picks, by NAME rather than by the integer the
# enum happens to have. ItemData.Type is an ordered enum in GDScript; inserting
# a new type at the top would renumber every one below it, and a server
# filtering on `type == 5` would start excluding the wrong thing without a
# single error anywhere.
EXCLUDED_FROM_LOOT = {"PET", "QUEST", "CURRENCY", "FISH"}


# =============================================================================
# XP
# =============================================================================

def xp_needed_for_level(level):
    """
    Port of GameConstants.xp_needed_for_level().

    The base and growth come from the JSON, not from constants here - see the
    note in exportgamedata.gd about this formula having already drifted once
    between two copies and corrupted live saves.
    """
    base = float(CONSTANTS.get("xp_base", 100.0))
    growth = float(CONSTANTS.get("xp_growth", 1.15))
    return int(base * (growth ** max(level - 1, 0)))


def apply_xp(level, xp, xp_to_next, gained):
    """
    Port of Player.gain_xp()'s level-up loop. Pure - it computes, it does not
    write. Returns (level, xp, xp_to_next, levels_gained).

    The loop is `while`, not `if`, because a single kill can cross more than
    one level boundary at low levels, and an `if` would silently bank the
    excess XP against a level the player never reached.
    """
    levels_gained = 0
    xp += gained

    while xp >= xp_to_next:
        xp -= xp_to_next
        level += 1
        levels_gained += 1
        xp_to_next = xp_needed_for_level(level)

        # A guard the client does not need and the server does. The client's
        # loop is bounded by the XP a real kill can award; this one is bounded
        # by whatever arrives over HTTP. A corrupted xp_to_next of 0 would spin
        # here forever and take the worker with it.
        if levels_gained > 200:
            break

    return level, xp, xp_to_next, levels_gained


# =============================================================================
# LOOT
# =============================================================================

def pick_weighted_item_id(max_tier):
    """
    Port of BaseEnemy._pick_weighted_item_id().

    Weight is 2^(max_tier - item.tier), so an item exactly at the enemy's tier
    is the rarest thing it can drop and each tier below it is twice as likely.
    Returns "" when nothing qualifies, same as the GDScript.
    """
    candidates = []
    weights = []
    total_weight = 0

    for item in ITEMS.values():
        if item["tier"] > max_tier:
            continue
        if item["type_name"] in EXCLUDED_FROM_LOOT:
            continue

        # PER-ITEM OPT-OUT, independent of tier. A cooked fish is a
        # Type.CONSUMABLE exactly like a potion, so the type exclusion above
        # cannot reach it - and a slime dropping cooked mudfish takes the point
        # out of the fishing skill. See ItemData.droppable in the Godot project
        # for the full reasoning. .get() with a default because a gamedata.json
        # exported before the field existed simply will not have it, and the
        # server refusing to boot over an additive field would be worse than
        # treating an old catalogue as all-droppable.
        if not item.get("droppable", True):
            continue

        weight = 2 ** max(max_tier - item["tier"], 0)
        if weight < 1:
            weight = 1

        candidates.append(item["item_id"])
        weights.append(weight)
        total_weight += weight

    if not candidates or total_weight <= 0:
        return ""

    roll = _rng.randrange(total_weight)          # Godot: randi() % total_weight
    cumulative = 0
    for index, candidate in enumerate(candidates):
        cumulative += weights[index]
        if roll < cumulative:
            return candidate

    return ""


def roll_pet(enemy):
    """Port of BaseEnemy._roll_pet() - decides THAT a pet drops, not which."""
    pet_id = enemy.get("pet_drop_id", "")
    if not pet_id:
        return False

    # The large slime's pet has never been able to drop: poisonslime.gd sets
    # pet_drop_id = "petpoisonslime" and no such resource exists. Keeping this
    # check means it starts working the moment the .tres is authored, with no
    # change needed on either side.
    if not has_item(pet_id):
        return False

    odds = int(enemy.get("pet_odds", 0))
    if odds <= 0:
        return False

    return _rng.randint(1, odds) == odds          # Godot: randi_range(1, odds)


def pick_pet_id(enemy):
    """
    Port of BaseEnemy._pick_pet_id() - decides WHICH pet, once roll_pet() has
    said one drops. Deliberately one or the other, never both: two independent
    rolls would let a single kill hand over the rare pet and the common one
    together, which would make the rare one feel worthless the moment it
    happened.
    """
    common = enemy.get("pet_drop_id", "")
    rare = enemy.get("rare_pet_drop_id", "")

    if not rare:
        return common
    if not has_item(rare):
        return common
    if _rng.random() < float(enemy.get("rare_pet_chance", 0.25)):
        return rare
    return common


def build_bag_contents(enemy):
    """Port of BaseEnemy._build_bag_contents()."""
    contents = []

    max_tier = int(enemy.get("max_loot_tier", 1))

    # Gold is guaranteed on every bag and has no rarity gate. That is
    # deliberate and matches the design: currency flows freely, items are
    # scarce.
    gold_amount = _rng.randint(max_tier, max_tier * 25)
    threshold = int(CONSTANTS.get("large_gold_threshold", 100))
    gold_id = (
        CONSTANTS.get("gold_large_id", "largeamountofgold")
        if gold_amount >= threshold
        else CONSTANTS.get("gold_small_id", "smallamountofgold")
    )
    if has_item(gold_id):
        contents.append({"item_id": gold_id, "quantity": gold_amount})

    slot_fill_chance = float(enemy.get("slot_fill_chance", 0.0))
    for _ in range(int(enemy.get("max_item_slots", 0))):
        if _rng.random() <= slot_fill_chance:
            picked = pick_weighted_item_id(max_tier)
            if picked:
                contents.append({"item_id": picked, "quantity": 1})

    return contents


def xp_needed_for_skill_level(skill_id, level):
    """
    XP to go from `level` to the next, for one skill.

    Port of PlayerStats.xp_needed_for_skill(): base * factor^(level-1).

    PER SKILL, NOT ONE SHARED CURVE. player.gd uses a different factor for each
    - cooking climbs at 1.10 where attack climbs at 1.25 - so a single growth
    here would quietly re-pace every skill the server touches. The table is
    authored in GameConstants.SKILL_XP_GROWTH and shipped in the JSON.
    """
    base = int(CONSTANTS.get("skill_xp_base", 100))
    growth = CONSTANTS.get("skill_xp_growth", {})
    factor = float(growth.get(skill_id, 1.18))
    return int(base * (factor ** max(int(level) - 1, 0)))


def fish_ceiling(rod_tier, fishing_level):
    """
    Highest fish tier a rod of this tier, in hands of this skill, can pull up.

    THE ROD SETS THE FLOOR, THE SKILL RAISES IT. A rod alone would make fishing
    a shopping problem; skill alone would make the rods decoration. Both matter,
    and the divisor is authored in the game (GameConstants.FISHING_TIER_PER_LEVEL)
    rather than chosen here - see this module's header on restating numbers.
    """
    per = int(CONSTANTS.get("fishing_tier_per_level", 20))
    if per < 1:
        per = 1
    return max(1, int(rod_tier) + int(fishing_level) // per)


def roll_fishing_catch(rod_tier, fishing_level):
    """
    Decide what came out of the water. Returns {item_id, quantity, xp}, or None
    when the catalogue holds no fish at all.

    THE CLIENT SENDS THAT IT FISHED, NOT WHAT IT GOT. docs/inventoryauthority.md
    is explicit: "a client that names its own catch is a client that catches
    whatever it likes." Everything this needs - the rod, the level - is read by
    the caller from rows the server owns.

    Weighted exactly like pick_weighted_item_id(): 2^(ceiling - tier), so the
    best fish a player can currently reach is also the rarest, and each tier
    below it is twice as likely.
    """
    ceiling = fish_ceiling(rod_tier, fishing_level)

    candidates = []
    weights = []
    total_weight = 0

    for item in ITEMS.values():
        if item["type_name"] != "FISH":
            continue
        if item["tier"] > ceiling:
            continue

        weight = 2 ** max(ceiling - item["tier"], 0)
        if weight < 1:
            weight = 1

        candidates.append(item)
        weights.append(weight)
        total_weight += weight

    if not candidates or total_weight <= 0:
        return None

    roll = _rng.randrange(total_weight)
    cumulative = 0
    for index, candidate in enumerate(candidates):
        cumulative += weights[index]
        if roll < cumulative:
            return {
                "item_id": candidate["item_id"],
                "quantity": 1,
                "xp": int(candidate.get("fishing_xp", 0)),
            }

    return None


def burn_chance(item, cooking_level):
    """
    Probability this fish is ruined, at this cooking level.

    Worst at the fish's cook_level and zero at its cook_mastery_level, sliding
    linearly between. cookingscreen.gd shows the player this same curve off the
    same constant, so the number on the label is the number that gets rolled.
    """
    floor_level = int(item.get("cook_level", 1))
    mastery = int(item.get("cook_mastery_level", 1))
    worst = float(CONSTANTS.get("cook_burn_max", 0.40))

    if mastery <= floor_level or cooking_level >= mastery:
        return 0.0

    span = float(mastery - floor_level)
    into = float(int(cooking_level) - floor_level)
    chance = worst * (1.0 - into / span)
    return min(max(chance, 0.0), worst)


def roll_cook(item_id, cooking_level):
    """
    Cook one raw fish. Returns a dict describing what happened:

        {"ok": False, "reason": "unknown"}      no such item
        {"ok": False, "reason": "notcookable"}  nothing to cook it into
        {"ok": False, "reason": "level", "needs": N}
        {"ok": True, "burnt": True,  "output": "", "xp": 0}
        {"ok": True, "burnt": False, "output": "cooked...", "xp": N}

    BURNING GRANTS NOTHING, deliberately. If a failed cook still paid XP there
    would be no cost to cooking above your level and the burn chance would be
    decoration - a slower road to the same place rather than a reason to wait.
    """
    item = ITEMS.get(item_id)
    if item is None:
        return {"ok": False, "reason": "unknown"}

    output = str(item.get("cooks_into", ""))
    if output == "" or output not in ITEMS:
        return {"ok": False, "reason": "notcookable"}

    needs = int(item.get("cook_level", 1))
    if int(cooking_level) < needs:
        return {"ok": False, "reason": "level", "needs": needs}

    if _rng.random() < burn_chance(item, cooking_level):
        return {"ok": True, "burnt": True, "output": "", "xp": 0}

    return {
        "ok": True,
        "burnt": False,
        "output": output,
        "xp": int(item.get("cook_xp", 0)),
    }


def roll_kill_rewards(enemy_id):
    """
    The whole reward for one kill. Port of BaseEnemy._die() plus
    _roll_and_spawn_loot(), minus everything to do with spawning a node.

    Returns:
        {
          "enemy_id":   str,
          "xp":         int,
          "attack_xp":  int,
          "pet_won":    bool,
          "contents":   [{"item_id": str, "quantity": int}, ...],
        }

    An empty `contents` is a normal outcome, not a failure - most kills drop
    nothing at all.
    """
    enemy = ENEMIES.get(enemy_id)
    if enemy is None:
        raise KeyError(enemy_id)

    pet_won = roll_pet(enemy)
    bag_drops = _rng.random() <= float(enemy.get("bag_drop_chance", 0.0))

    contents = []
    if bag_drops or pet_won:
        contents = build_bag_contents(enemy)
        if pet_won:
            contents.append({"item_id": pick_pet_id(enemy), "quantity": 1})

    return {
        "enemy_id": enemy_id,
        "xp": int(enemy.get("xp_reward", 0)),
        "attack_xp": int(enemy.get("attack_xp_reward", 0)),
        "pet_won": pet_won,
        "contents": contents,
    }
