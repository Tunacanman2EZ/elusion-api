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
import math
import os
import random
import re

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
    shops = {row["shop_id"]: row for row in raw.get("shops", [])}

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

    # Shops may be absent in a gamedata.json exported before they existed, and
    # a project with no vendors is legitimate. Silent rather than warned for
    # that reason - unlike classes, nothing degrades to trusting the client
    # when there are no shops; /api/shop/buy simply has nothing to sell.
    #
    # Areas and combat (0.10.0) feed presence.py's books on every monster and
    # nothing else. A catalogue from before them is not an error: the books
    # say they cannot judge, and every route here works as it did.
    areas = raw.get("areas") if isinstance(raw.get("areas"), dict) else {}
    combat = raw.get("combat") if isinstance(raw.get("combat"), dict) else {}
    return raw.get("constants", {}), items, enemies, classes, shops, areas, combat


# Loaded once at import. Failing here takes the whole server down on start,
# which is correct: a server that cannot roll loot should not accept kills and
# quietly grant nothing.
CONSTANTS, ITEMS, ENEMIES, CLASSES, SHOPS, AREAS, COMBAT = _load()


# =============================================================================
# QUALITY ROLLS
# =============================================================================
# A DROPPED PIECE OF GEAR ROLLS EACH OF ITS STATS. The owner, 5 Oct: "random
# stats on all items because it gives loot a better value if a rare max roll".
# Every stat a piece has - damage, armour, max health, max mana, the damage
# bonus - rolls on its own, from QUALITY_LOW to QUALITY_HIGH percent of the
# catalogue number, most often near 100. One piece in QUALITY_PERFECT_ODDS is
# Perfect instead: every stat at QUALITY_PERFECT. A piece bought from the shop
# rolls the same way, at the till (the owner, 5 Oct: "item stats say ? and are
# revealed upon buying in shop only") - the shelf lists the catalogue piece and
# its price, and shop_buy hands over a roll of it.
#
# THE ROLL TRAVELS IN THE ITEM ID: "jadechest~a104h96" is a jade cuirass with
# armour at 104% and health at 96%. That is the whole design, and the reason
# is everything that did not have to change. A carry cell, a bank cell, a loot
# bag entry, the equipment map, a trade row and a staff whole-bag write all
# hold an item id and nothing else, and every route that moves one takes "the
# item the game saw in that cell" - so a rolled piece moves, is equipped,
# banked, traded and sold by the paths that already exist, and the cell check
# that guards each of them checks the roll as well. A column beside the id
# would have had to be carried by every one of those, and the one that forgot
# would turn a Perfect piece back into a plain one without an error.
#
# WHAT HAD TO CHANGE IS EVERY QUESTION ABOUT WHAT AN ID IS, and they all go
# through item_row() now. "ironsword~d107" is not a key of ITEMS, so a bare
# ITEMS.get() reads a rolled piece as an item nobody has heard of: unlimited
# stacking, "No such item" on a trade, no slot to wear it in. ITEMS itself is
# still only the catalogue - iterate it for the shelf or the loot pool, never
# index it with an id that came out of a row or a request. test_quality.py
# scans app.py for that.
#
# THE LETTERS AND THE RANGE ARE THE GAME'S (GameConstants.QUALITY_*, through
# gamedata.json), like every other balance number here; the fallbacks below are
# what the game holds today, for a gamedata.json exported before them.
#
# A ROLL CHANGES WHAT A PIECE DOES, NOT WHAT IT COSTS. value stays the
# catalogue's, so the shop pays the same for a 90% sword as a 110% one and the
# trade tax is 5% of the same number. A Perfect piece is worth more to players,
# and players set that price in a trade.
QUALITY_FIELDS = tuple(
    (str(pair[0]), str(pair[1]))
    for pair in (CONSTANTS.get("quality_fields") or (
        ("d", "damage"), ("a", "armor_value"), ("h", "bonus_max_hp"),
        ("m", "bonus_max_mana"), ("p", "bonus_damage_percent"),
    ))
)
QUALITY_LOW = int(CONSTANTS.get("quality_low", 85))
QUALITY_HIGH = int(CONSTANTS.get("quality_high", 115))
QUALITY_PERFECT = int(CONSTANTS.get("quality_perfect", 120))
QUALITY_PERFECT_ODDS = int(CONSTANTS.get("quality_perfect_odds", 100))
VARIANT_MARK = "~"

_FIELD_FOR_LETTER = {letter: field for letter, field in QUALITY_FIELDS}
# [0-9], NOT \d, AND fullmatch, NOT $. Python's \d is every Unicode digit and
# its $ matches before a final newline, so "ironsword~d\u0661\u0660\u0667" and
# "ironsword~d107\n" were both read as the 107 roll - three spellings of one
# piece, where the game (ItemRegistry, [0-9] and an anchored pattern) reads
# the other two as nothing. Found adding resistances, 7 Oct.
_VARIANT_PART = re.compile(r"([a-z])([0-9]{2,3})")
_VARIANT_SUFFIX = re.compile(r"(?:[a-z][0-9]{2,3})+")

# ELEMENT RESISTANCE (0.11.0). The owner, 7 Oct: "add resistance to armor with
# a ? random roll also in the shop", "i want elements to do something". Every
# piece of ARMOUR - anything worn that is not a weapon - rolls one of the seven
# lands' elements and a percent from its tier's row, wherever its stats roll
# (a drop, the till, the owner's "roll" and "perfect" grants), and the roll is
# the LAST part of the id: "jadechest~a104h96r605" resists fire (the game's
# Element.Type 6) by 5%. The letter, one digit of element, two of percent. A
# Perfect piece resists at the top of its range. A piece from before
# resistances has none and is still read.
#
# THE SERVER ONLY CARRIES IT. What a resistance does - matching pieces added
# up to RESIST_CAP, taken off a hit of that element - happens in the game's
# Player.take_damage(), on damage the server never sees. It changes nothing a
# player can be paid for, so nothing here reads it but the parser and the roll.
#
# NEVER ON A WEAPON. The owner, 7 Oct: "the main goal should be to keep players
# damage consistent with attack level and gear". The numbers are the game's
# (GameConstants.RESIST_*, through gamedata.json); the fallbacks are what it
# holds today.
RESIST_LETTER = str(CONSTANTS.get("resist_letter") or "r")
RESIST_ELEMENTS = tuple(int(e) for e in (CONSTANTS.get("resist_elements") or (1, 2, 3, 4, 5, 6, 7)))
RESIST_RANGES = tuple(
    (int(row[0]), int(row[1]))
    for row in (CONSTANTS.get("resist_ranges") or ((0, 0), (2, 5), (3, 7), (4, 9), (5, 11), (6, 13)))
)
RESIST_CAP = int(CONSTANTS.get("resist_cap", 50))


def rolled_fields(definition):
    """[(letter, field)] for every stat this catalogue row has above zero, in
    QUALITY_FIELDS order. A potion, a rod or a coin has none and never rolls."""
    out = []
    for letter, field in QUALITY_FIELDS:
        try:
            if int(definition.get(field, 0) or 0) > 0:
                out.append((letter, field))
        except (TypeError, ValueError):
            continue
    return out


def resist_range(definition):
    """(low, high), the percent a piece of this catalogue row may resist, or
    (0, 0) for one that never resists: anything but armour that is worn, or a
    tier with no range. Past the table, its last row - the game's
    GameConstants.resist_range() and ItemData.resists_when_rolled()."""
    if not isinstance(definition, dict) or str(definition.get("type_name")) != "ARMOR":
        return (0, 0)
    try:
        slot = int(definition.get("equip_slot") or 0)
        tier = int(definition.get("tier") or 0)
    except (TypeError, ValueError):
        return (0, 0)
    if slot == 0 or tier < 1 or not RESIST_RANGES:
        return (0, 0)
    low, high = RESIST_RANGES[min(tier, len(RESIST_RANGES) - 1)]
    return (low, high) if high > 0 else (0, 0)


def _read_resist(definition, digits):
    """(element, percent) from a resistance's three digits, or None when they
    are not one this piece could have rolled."""
    low, high = resist_range(definition)
    if high <= 0 or len(digits) != 3:
        return None
    element, percent = int(digits[0]), int(digits[1:])
    if element not in RESIST_ELEMENTS or not low <= percent <= high:
        return None
    return (element, percent)


def _parse_variant(item_id):
    """
    (base_id, {field: percent}, (element, percent) or None) for a rolled id,
    (item_id, {}, None) for a plain one, and (None, None, None) for anything
    that only looks like a roll.

    STRICT, BECAUSE THE ID IS THE IDENTITY. Two spellings of one roll would be
    two different items to every cell check, so there is exactly one: every
    stat the base piece has, each once, in QUALITY_FIELDS order, and either all
    at QUALITY_PERFECT or each inside QUALITY_LOW..QUALITY_HIGH; then at most
    one resistance, on armour only, inside its tier's range - at the top of it
    on a Perfect piece. A stat the piece does not have, a missing one, a 300%,
    a "d07", a resistance on a sword or a 2% one spelled "r62" is not a roll.
    ItemRegistry.split_roll() is the game's copy of these rules.
    """
    item_id = str(item_id)
    if VARIANT_MARK not in item_id:
        return item_id, {}, None
    base, _, suffix = item_id.partition(VARIANT_MARK)
    definition = ITEMS.get(base)
    if definition is None or not _VARIANT_SUFFIX.fullmatch(suffix):
        return None, None, None
    parts = _VARIANT_PART.findall(suffix)
    resist = None
    if parts and parts[-1][0] == RESIST_LETTER:
        resist = _read_resist(definition, parts[-1][1])
        if resist is None:
            return None, None, None
        parts = parts[:-1]
    expected = rolled_fields(definition)
    if not parts or [letter for letter, _ in parts] != [letter for letter, _ in expected]:
        return None, None, None
    rolls = {}
    for letter, digits in parts:
        if digits.startswith("0"):
            return None, None, None
        rolls[_FIELD_FOR_LETTER[letter]] = int(digits)
    values = set(rolls.values())
    if values != {QUALITY_PERFECT} and not all(QUALITY_LOW <= v <= QUALITY_HIGH for v in values):
        return None, None, None
    if values == {QUALITY_PERFECT} and resist is not None and resist[1] != resist_range(definition)[1]:
        return None, None, None
    return base, rolls, resist


def split_variant(item_id):
    """
    (base_id, {field: percent}) for a rolled id, (item_id, {}) for a plain one,
    and (None, None) for anything that only looks like a roll - _parse_variant()
    without the resistance, which item_row() reads.
    """
    base, rolls, _resist = _parse_variant(item_id)
    return base, rolls


def scale_stat(number, percent):
    """A catalogue number at this percent, to the nearest whole point, halves
    up. INTEGER ARITHMETIC ON PURPOSE: ItemRegistry does the same sum in
    GDScript, and the server derives max health from it, so a float rounding
    differently on one side would put the two a point apart forever."""
    number = int(number)
    if number <= 0:
        return number
    return (number * int(percent) + 50) // 100


def item_row(item_id):
    """
    THE ONE WAY TO ASK WHAT AN ID IS. The catalogue row for a plain id; for a
    rolled one, a copy of its base row with each rolled stat scaled, plus
    "base_id", "rolls", "perfect", "resist_element" and "resist_percent" (0 and
    0 for none), and "Perfect " in front of a Perfect piece's name. None for an
    unknown id or a malformed roll.
    """
    item_id = str(item_id)
    if VARIANT_MARK not in item_id:
        return ITEMS.get(item_id)
    base, rolls, resist = _parse_variant(item_id)
    if base is None:
        return None
    row = dict(ITEMS[base])
    for field, percent in rolls.items():
        row[field] = scale_stat(row.get(field, 0) or 0, percent)
    perfect = set(rolls.values()) == {QUALITY_PERFECT}
    row["item_id"] = item_id
    row["base_id"] = base
    row["rolls"] = dict(rolls)
    row["perfect"] = perfect
    row["resist_element"] = resist[0] if resist else 0
    row["resist_percent"] = resist[1] if resist else 0
    if perfect:
        row["display_name"] = "Perfect " + str(row.get("display_name") or base)
    return row


def perfect_id(item_id, resist=False):
    """The Perfect roll of a catalogue piece - every stat at QUALITY_PERFECT -
    or item_id unchanged for anything with nothing to roll. resist=True gives
    armour its resistance too, a random element at the top of its range - what
    a Perfect drop is; without it the answer is the same every time, which is
    what a test comparing ids wants."""
    definition = ITEMS.get(str(item_id))
    fields = rolled_fields(definition) if definition is not None else []
    if not fields:
        return item_id
    return item_id + VARIANT_MARK + "".join(
        "%s%d" % (letter, QUALITY_PERFECT) for letter, _ in fields) \
        + (_resist_part(definition, top=True) if resist else "")


def _resist_part(definition, top=False):
    """The resistance a piece of armour rolls, "r605", or "" for anything that
    never resists: an element from RESIST_ELEMENTS, evenly, and a percent
    evenly across its tier's range - or the top of it (top=True, a Perfect)."""
    low, high = resist_range(definition)
    if high <= 0 or not RESIST_ELEMENTS:
        return ""
    element = RESIST_ELEMENTS[_rng.randrange(len(RESIST_ELEMENTS))]
    percent = high if top else _rng.randint(low, high)
    return "%s%d%02d" % (RESIST_LETTER, element, percent)


def base_id(item_id):
    """The catalogue id under a roll: "ironsword" for "ironsword~d107"."""
    return str(item_id).partition(VARIANT_MARK)[0]


def is_perfect(item_id):
    row = item_row(item_id)
    return bool(row and row.get("perfect"))


def roll_quality(item_id):
    """
    A dropped or bought piece's roll: item_id with a roll on every stat it has, or
    item_id unchanged for anything with nothing to roll (potions, rods, coins,
    pets). Each stat is its own triangle from QUALITY_LOW to QUALITY_HIGH,
    peaked at 100, so most pieces sit near the catalogue and the top of the
    range is rare; one in QUALITY_PERFECT_ODDS is Perfect, every stat at
    QUALITY_PERFECT. Armour also rolls its resistance (_resist_part(), the
    top of the range on a Perfect). Reads _rng when called, so a test's seeded
    generator reaches it.
    """
    definition = ITEMS.get(str(item_id))
    if definition is None:
        return item_id
    fields = rolled_fields(definition)
    if not fields:
        return item_id
    if QUALITY_PERFECT_ODDS > 0 and _rng.randrange(QUALITY_PERFECT_ODDS) == 0:
        return perfect_id(item_id, resist=True)
    parts = []
    for letter, _ in fields:
        percent = int(round(_rng.triangular(QUALITY_LOW, QUALITY_HIGH, 100)))
        parts.append("%s%d" % (letter, min(QUALITY_HIGH, max(QUALITY_LOW, percent))))
    return item_id + VARIANT_MARK + "".join(parts) + _resist_part(definition)



def shop_price(shop_id, item_id):
    """
    What this vendor charges for one of these, or None when it does not stock
    it or the item does not exist.

    THE PRICE IS NOT STORED IN THE SHOP. It is ItemData.value times the shop's
    multiplier, computed here from the server's own catalogue. A shop that
    carried its own numbers would be a second price ladder to keep in step with
    the first, and the whole point of the tier curve on value is that there is
    exactly one.

    ROUNDED UP, AND FLOORED AT 1. Rounding down would let a markup below 1.0
    price a cheap item at zero, and a vendor handing out free stock is a faucet
    wearing a sink's clothes. Up also keeps the arithmetic on the house's side,
    which is the correct direction for something whose job is to destroy gold.
    """
    shop = SHOPS.get(shop_id)
    if shop is None:
        return None
    if item_id not in shop.get("stock", []):
        return None
    definition = item_row(item_id)
    if definition is None:
        return None

    multiplier = float(shop.get("price_multiplier", 1.0))
    return max(1, int(math.ceil(int(definition.get("value", 0)) * multiplier)))


# WHAT A VENDOR WILL BUY. Gear, potions and food, fish and the fishing kit:
# the things that drop, get caught or get bought. Not money (a pile is cashed,
# not sold), not pets (a second copy already turns into lusions), and not
# quest items, which cannot leave the bag at all.
SELLABLE_TYPES = ("WEAPON", "ARMOR", "CONSUMABLE", "FISH", "MATERIAL")


def shop_sell_price(shop_id, item_id):
    """
    What this vendor pays for one of these, or None when it will not buy it.

    ItemData.value times the shop's sell_multiplier, ROUNDED DOWN - the house's
    side again, as shop_price() rounds up - and None when that comes to zero:
    an item worth nothing to the shop is one it does not take, rather than a
    sale for 0 gold that reads as a bug.

    NEVER AT OR ABOVE WHAT THE SAME SHOP CHARGES. A vendor that pays what it
    asks is a loop: buy, sell, repeat, and the gold comes from nowhere. The
    multiplier the game exports is far below the markup today (0.05 against
    1.0), so this cap only ever bites on a mistake - which is when it matters.
    """
    shop = SHOPS.get(shop_id)
    if shop is None:
        return None
    definition = item_row(item_id)
    if definition is None:
        return None
    if str(definition.get("type_name", "")) not in SELLABLE_TYPES:
        return None

    multiplier = float(shop.get("sell_multiplier", 0.0) or 0.0)
    price = int(math.floor(int(definition.get("value", 0)) * multiplier))
    # A ROLLED PIECE IS PRICED AS ITS BASE, and capped by what the shop
    # charges for the base: the shelf never holds a roll, so asking about
    # "ironsword~d107" there would find nothing and skip the cap.
    charged = shop_price(shop_id, base_id(item_id))
    if charged is not None:
        price = min(price, charged - 1)
    return price if price > 0 else None


def stack_value(item_id, quantity):
    """
    What a stack is worth to the kingdom's assessor, or None for an item the
    catalogue does not know.

    ItemData.value, times the count. The same number the vendor prices from, on
    purpose: a second valuation ladder for tax would let players find the gap
    between the two and trade across it.
    """
    definition = item_row(item_id)
    if definition is None:
        return None
    return int(definition.get("value", 0)) * int(quantity)


def trade_tax(taxable_value):
    """
    The tax on receiving `taxable_value` worth of goods and gold.

    ROUNDED UP, WITH A FLOOR, AND THE FLOOR IS THE POINT. At 5%, rounding down
    makes every trade under 20 gold of value free - and a free trade is a
    laundering channel, because 10,000 gold moved as a thousand dribbles pays
    nothing and the sink never fires. Rounding up with a minimum of 1 makes
    splitting strictly WORSE than not splitting: one trade worth 100 pays 5; the
    same 100 as ten trades of 10 pays 10. The cheapest route is the honest one,
    which is the only way a rule like this survives contact with players.

    Nothing taxable means no tax. A trade of two worthless items is not a
    loophole - there is nothing in it to launder.

    THE RATE COMES FROM THE GAME, NOT FROM HERE. GameConstants.KINGDOM_TAX_RATE
    is a balance decision and lives with the other balance decisions; this
    module restates no number that lives in the game.
    """
    taxable_value = int(taxable_value)
    if taxable_value <= 0:
        return 0

    rate = float(CONSTANTS.get("kingdom_tax_rate", 0.05))
    minimum = int(CONSTANTS.get("kingdom_tax_minimum", 1))
    return max(minimum, int(math.ceil(taxable_value * rate)))


def revive_gold_cost(total_gold):
    """
    What reviving costs in gold, given everything the account holds - carry and
    bank together. Mirrors GameConstants.REVIVE_GOLD_RATE and
    GameConstants.REVIVE_GOLD_MINIMUM.

    A SHARE RATHER THAN A PRICE, and the consequences are the point. A flat
    figure is unaffordable at level 3 and pocket change at level 30, so death
    would sting at exactly one point in the game. Eighty percent of everything
    always hurts - and, being a share of what you HAVE, it scales with the
    player rather than with the calendar.

    ROUNDED UP, like the trade tax and for the same reason: rounding down would
    make a very small balance free.

    AND A FLOOR UNDER IT, which is new and which deliberately gives up the old
    guarantee that this could always be paid.

    A pure share has a hole at the bottom. Eighty percent of the thirty gold a
    fresh character is carrying is twenty-four gold, and twenty-four gold is not
    a death - it is a toll. Worse, it got cheaper the less you had, so the
    correct play after dying broke was to die again rather than walk. The floor
    makes the first deaths cost something real and leaves the curve above it
    exactly as it was: at 5,000 gold the share is 4,000 and the floor is not
    even in the conversation.

    WHERE THE FLOOR ACTUALLY BITES, in three bands - and they are three, not
    two, which is worth writing down because the obvious reading gets it wrong:

        under 100    the price exceeds the balance   -> the gold route is refused
        100 to 123   the floor is above the share    -> you pay 100, not the share
        124 and up   the share is above the floor    -> nothing changed at all

    The share drops below the floor at 125, but the route only CLOSES at 100:
    between the two you can still pay, you simply pay the floor. Refusing is
    the honest answer below that - the caller turns the unaffordable cost into
    a 402 naming it, and the lusion route and the walk both still exist. That
    is the trade being made on purpose: one of the three ways back is closed to
    a player with almost nothing, and the other two are not.

    Zero in, zero out, still: an account holding nothing is not quoted a price
    it could never have paid.
    """
    total = int(total_gold)
    if total <= 0:
        return 0
    rate = float(CONSTANTS.get("revive_gold_rate", 0.80))
    floor = int(CONSTANTS.get("revive_gold_minimum", 0) or 0)
    return max(floor, int(math.ceil(total * rate)))


# WHAT WEARING SOMETHING ADDS, beyond armour and a weapon's hit. Same three
# names as ItemData's exports; an item that does not carry one has 0.
GEAR_BONUS_FIELDS = ("bonus_max_hp", "bonus_max_mana", "bonus_damage_percent")


def gear_bonuses(equipment):
    """
    Every bonus field summed over what is worn: {"bonus_max_hp": 60, ...}.

    COUNTED ONLY WHERE IT BELONGS. A piece in a slot it is not worn in adds
    nothing - an amulet stored under "weapon" is not being worn as an amulet.
    The equip endpoint already refuses that, so this only matters for a row
    written some other way, and there the safe answer is no bonus, not a free
    one. Unknown ids, non-string ids and a non-dict map all add nothing.

    Negatives are floored at 0 for the same reason the exporter refuses them:
    a bad number in the catalogue must never take a character's maximum below
    its class curve.
    """
    totals = {field: 0 for field in GEAR_BONUS_FIELDS}
    if not isinstance(equipment, dict):
        return totals
    for slot_name, item_id in equipment.items():
        if not isinstance(item_id, str) or not item_id:
            continue
        # item_row(), so a rolled amulet adds its rolled health.
        item = item_row(item_id)
        if item is None or equip_slot_for(item_id) != str(slot_name).strip().lower():
            continue
        for field in GEAR_BONUS_FIELDS:
            try:
                totals[field] += max(0, int(item.get(field, 0) or 0))
            except (TypeError, ValueError):
                continue
    return totals


def max_stats_for(class_id, level, equipment=None):
    """
    What a character of this class SHOULD have at this level, wearing this.

    Port of Player._recompute_max_stats():

        max_hp = hp_base + (level - 1) * hp_per_lvl + worn bonus_max_hp

    Level 1 gets exactly the base, which is what the -1 is for. Both
    implementations must agree on that or every character is one level out.

    THE GEAR HALF IS NOT OPTIONAL IN PRACTICE, even though the argument is.
    The status route clamps hp to this maximum, so a caller that forgets the
    equipment hands a character in a Vitality amulet a lower ceiling than their
    client shows, and the next save quietly takes the difference away. app.py
    reaches this only through _derived_stats(row), which reads the row's own
    equipment; test_gearbonus.py scans for any other call.

    Returns None for an unknown class, meaning "no opinion" - the caller then
    leaves the client's values alone rather than zeroing a character it cannot
    describe.
    """
    curve = CLASSES.get(class_id)
    if curve is None:
        return None

    worn = gear_bonuses(equipment)
    steps = max(int(level) - 1, 0)
    return {
        "max_hp": int(curve["hp_base"]) + steps * int(curve["hp_per_lvl"]) + worn["bonus_max_hp"],
        "max_mana": int(curve["mana_base"]) + steps * int(curve["mana_per_lvl"]) + worn["bonus_max_mana"],
        "max_stamina": int(curve["stam_base"]) + steps * int(curve["stam_per_lvl"]),
    }


def has_item(item_id):
    """Mirrors ItemRegistry.has_item(). A well-formed roll of a known piece is
    an item; a malformed one is not."""
    return item_row(item_id) is not None


def item_value(item_id):
    """What one of these is worth in gold. 0 for anything unpriced."""
    record = item_row(item_id) or {}
    try:
        return max(0, int(record.get("value", 0) or 0))
    except (TypeError, ValueError):
        return 0


# =============================================================================
# GOLD DENOMINATIONS
# =============================================================================
# A gold drop used to be one heap: roll an amount, then pick one of two item
# ids by a threshold and put THAT MANY of it in the bag. A tier 8 kill paid
# out thirty thousand of a thing called "A Few Coins".
#
# Now the amount is broken into coins. The ladder is ordered highest first and
# every step divides evenly into the one above it, which is what makes greedy
# change-making exact - and exact is the whole requirement here, because a
# remainder is gold that was rolled and never arrived.
#
# NAMED EXPLICITLY, WITH A FALLBACK THAT CANNOT GO STALE. The list comes from
# baseenemy.gd through gamedata.json, so the game and the server cannot hold
# two different ladders. If that key is missing - an older export, or a
# regenerated file from a build that predates it - the ladder is derived from
# the items themselves: every CURRENCY item with a price, biggest first. That
# keeps working rather than silently paying nothing.

def gold_denominations():
    """[(item_id, value)] highest value first. Never includes lusions."""
    named = CONSTANTS.get("gold_denomination_ids") or []
    out = []
    for item_id in named:
        value = item_value(item_id)
        if has_item(item_id) and value > 0:
            out.append((item_id, value))

    if not out:
        premium = CONSTANTS.get("lusions_item_id", "lusions")
        for item_id, record in ITEMS.items():
            if item_id == premium:
                continue
            if str(record.get("type_name", "")) != "CURRENCY":
                continue
            value = item_value(item_id)
            if value > 0:
                out.append((item_id, value))

    out.sort(key=lambda pair: pair[1], reverse=True)
    return out


# =============================================================================
# THE JACKPOT DICE
# =============================================================================
# THE PROBLEM THIS SOLVES. The ordinary roll tops out at unit * spread, and at
# the highest loot tier in the game that is a few thousand gold. The top three
# rungs of the coin ladder - a stack, a pile, a platinum coin - were therefore
# unreachable: they existed, they had prices, and nothing could ever drop one.
# A denomination nobody can get is just a file.
#
# FIVE DICE, AND YOU WANT SIXES. Every gold drop rolls GOLD_JACKPOT_DICE of
# them; the number that come up on the top face picks a multiplier. Three
# sixes happens now and then, four is rare, and five is one roll in 7776.
#
# WHY DICE RATHER THAN A FLAT PERCENTAGE. Because the shape is the point. A
# 0.013% chance of a big multiplier and five-sixes-in-a-row are the same
# number, but only one of them has near misses - and four sixes landing a few
# times an evening is what makes the fifth feel like it is coming. The player
# never sees the dice; they see a platinum coin, which is unmistakable.
#
# ALIGNED WITH WHAT KILLED YOU. The multiplier scales the tier's own roll
# rather than replacing it, so a jackpot off a slime is a pleasant surprise
# and a jackpot off a boss is the only way a platinum coin enters the game.

def gold_multiplier_for(sixes):
    """The multiplier for that many top-face dice. 1 for an ordinary roll."""
    table = CONSTANTS.get("gold_jackpot_multipliers") or []
    if not table:
        return 1
    index = max(0, min(int(sixes), len(table) - 1))
    try:
        return max(1, int(table[index]))
    except (TypeError, ValueError):
        return 1


def jackpot_allowed(max_tier):
    """Whether a drop at this loot tier may roll the dice at all.

    THE STARTING AREA IS EXCLUDED ON PURPOSE. A windfall is only a windfall
    against a baseline, and a new player whose third slime pays out a gold
    coin has had the rest of the first area's economy handed to them before
    they have met it. It also puts the top of the ladder where the top of the
    game is: if the easiest thing in the world can roll a platinum coin, the
    coin says nothing about what you beat.
    """
    floor = int(CONSTANTS.get("gold_jackpot_min_tier", 0) or 0)
    if floor <= 0:
        return True
    try:
        return int(max_tier) >= floor
    except (TypeError, ValueError):
        return False


def roll_gold_jackpot(max_tier=None, rng=None):
    """(sixes, multiplier) for one drop's dice. (0, 1) when the tier is barred."""
    if rng is None:
        rng = _rng
    if max_tier is not None and not jackpot_allowed(max_tier):
        # NO DICE ROLLED AT ALL, rather than dice whose result is discarded.
        # Same outcome, but it keeps the rng stream identical to what it was
        # before the jackpot existed for every low-tier drop - so nothing in
        # the starting area shifts its loot because this feature was added.
        return 0, 1
    dice = int(CONSTANTS.get("gold_jackpot_dice", 0) or 0)
    faces = max(2, int(CONSTANTS.get("gold_jackpot_faces", 6) or 6))
    if dice <= 0:
        return 0, 1
    sixes = sum(1 for _ in range(dice) if rng.randint(1, faces) == faces)
    return sixes, gold_multiplier_for(sixes)


def make_change(amount, ladder=None):
    """[{"item_id", "quantity"}] adding up to EXACTLY `amount`.

    GREEDY, WHICH IS ONLY CORRECT BECAUSE OF THE LADDER'S SHAPE. Each
    denomination divides evenly into the next, so taking as many of the
    biggest as fit and moving down can never strand a remainder it cannot
    pay. A ladder of 7s and 13s would need real change-making and could still
    come up short - and coming up short here means gold quietly going missing
    between the roll and the bag.
    """
    amount = max(0, int(amount))
    if amount <= 0:
        return []

    if ladder is None:
        ladder = gold_denominations()
    if not ladder:
        return []

    coins = []
    left = amount
    for item_id, value in ladder:
        if value <= 0 or left < value:
            continue
        count = left // value
        left -= count * value
        coins.append({"item_id": item_id, "quantity": int(count)})

    # THE REMAINDER IS NOT ROUNDED AWAY. If the smallest coin is worth more
    # than one gold there can be a few left over, and dropping them would
    # mean the amount rolled and the amount paid are different numbers. One
    # more of the smallest coin is worth slightly more than was rolled, which
    # is the error worth having: generous beats missing, and it is visible.
    if left > 0:
        smallest_id, smallest_value = ladder[-1]
        if coins and coins[-1]["item_id"] == smallest_id:
            coins[-1]["quantity"] += 1
        else:
            coins.append({"item_id": smallest_id, "quantity": 1})

    return coins


def change_total(coins, ladder=None):
    """What a decomposition is actually worth - the check on make_change."""
    if ladder is None:
        ladder = gold_denominations()
    prices = dict(ladder)
    return sum(int(c["quantity"]) * int(prices.get(c["item_id"], 0)) for c in coins)


def restore_for(item_id):
    """
    What using one of these restores: (target_name, amount).

    ('', 0) for an item that restores nothing, an item the catalogue does not
    know, or - importantly - a gamedata.json exported before these fields
    existed. Every caller has to treat that triple the same way, so it is
    returned rather than raised.

    BY NAME, NEVER BY THE ENUM'S INTEGER. ItemData.RestoreTarget is ordered,
    and a value inserted at the top would renumber every one below it while a
    server comparing `== 1` carried on silently applying a heal to the wrong
    pool. exportgamedata.gd writes the name beside the integer for exactly this
    reason; see RESTORE_TARGET_NAMES there.
    """
    item = item_row(item_id)
    if item is None:
        return "", 0
    target = str(item.get("restore_target_name", ""))
    if target in ("", "NONE"):
        return "", 0
    return target, int(item.get("restore_amount", 0))


# =============================================================================
# EQUIPMENT
# =============================================================================
# THE SLOT VOCABULARY IS THE GAME'S, NOT THIS SERVER'S.
#
# app.py used to hold its own tuple of slot names, typed from memory, and it
# was wrong in both directions at once: it had a "robe" slot ItemData.EquipSlot
# has never defined, and no "boots" slot, which it does. Nothing errored.
# Nothing could have - a server holding a private copy of a vocabulary has no
# way to find out it disagrees. It would simply have refused every pair of
# boots in the game and stored a slot no client would ever send.
#
# So the names come out of gamedata.json now, gathered from the items
# themselves. exportgamedata.gd writes equip_slot_name from EQUIP_SLOT_NAMES,
# which sits beside the enum and fails the export if it gains a member that
# list does not know - the same chain type_name and restore_target_name ride.
#
# LOWERCASED HERE. The export writes "HELM", matching the enum's own spelling;
# the wire and the saves.equipment column say "helm". One translation, in one
# place, rather than two spellings drifting apart.
EQUIP_EXPORTED = any("equip_slot_name" in item for item in ITEMS.values())


def _equip_slots():
    """Every slot name the catalogue actually uses, lowercased.

    DERIVED, NOT LISTED, so a slot added in Godot and worn by a new piece of
    gear needs no edit here. NONE never appears - it is how an item says it is
    not equipment, not a place to put one."""
    found = set()
    for item in ITEMS.values():
        name = str(item.get("equip_slot_name", "NONE")).strip().upper()
        if name and name != "NONE":
            found.add(name.lower())
    return frozenset(found)


EQUIP_SLOTS = _equip_slots()


# HOW MANY OF EACH ENEMY THE WORLD ACTUALLY CONTAINS.
#
# WHAT IT IS FOR. /api/combat/kill cannot see the fight - that is E-3 - so the
# only honest question left is "could this many kills physically have happened
# by now". A rate limit answers that with a number somebody picked. This
# answers it with the world: an enemy that exists once, on a respawner, can be
# killed once per respawn, and no client saying otherwise changes that.
#
# THE REASON IT IS A COUNT AND NOT A dps BUDGET. The obvious alternative is to
# bound HP destroyed by the player's own damage over time. It was measured and
# it is worse: the tank's aura damages EVERY enemy in range and the mage's cast
# explodes, so the budget needs roughly 8x headroom to avoid refusing honest
# AoE - and a cheater inherits the whole allowance, landing looser than the
# token bucket it replaced. Bounding by what EXISTS has no such slack, because
# a tank killing six at once is simply six spawn points that cannot pay out
# again until they respawn.
#
# EXPORTED, NOT COUNTED HERE. Only Godot can see a scene tree.
# exportgamedata.gd already walks every .tscn to report unplaced enemies; it
# writes the tally it computes on the way.
SPAWNS_EXPORTED = any("placed_count" in enemy for enemy in ENEMIES.values())


def spawn_count_for(enemy_id):
    """How many of this enemy are placed in the world, or 0 when unknown.

    ZERO MEANS "DO NOT JUDGE", NOT "IMPOSSIBLE", and the distinction is the
    whole safety of the check that reads this.

    An enemy can legitimately be placed nowhere and still be killed: the large
    poison slime splits into smalls through a path in its own script, so the
    smalls exist at runtime and appear in no scene. Treating 0 as "this can
    never be killed" would refuse every one of those kills and break the
    fight. A caller that gets 0 must fall through and allow the kill.

    The cost of that is an exemption for runtime-spawned enemies, which is
    worth stating plainly: today that is poisonslimesmall, at 25 xp, the
    lowest-value enemy in the game. The loophole is real and it is bounded by
    being the worst thing to farm.
    """
    enemy = ENEMIES.get(enemy_id)
    if enemy is None:
        return 0
    try:
        return max(0, int(enemy.get("placed_count", 0)))
    except (TypeError, ValueError):
        return 0


def equip_slot_for(item_id):
    """Which slot this item is worn in, lowercased.

    "" for anything that is not equipment, for an item the catalogue does not
    know, and for a gamedata.json exported before equip_slot_name existed.
    Callers that need to tell those apart check EQUIP_EXPORTED first."""
    item = item_row(item_id)
    if item is None:
        return ""
    name = str(item.get("equip_slot_name", "NONE")).strip().upper()
    if name in ("", "NONE"):
        return ""
    return name.lower()


def equip_classes_for(item_id):
    """Which classes may wear this, as class_ids.

    AN EMPTY LIST MEANS ANYONE - rings and amulets are shared by everybody - so
    a caller has to test for emptiness before it tests for membership, or it
    will lock every class out of the jewellery."""
    item = item_row(item_id)
    if item is None:
        return []
    raw = item.get("required_classes", [])
    if not isinstance(raw, list):
        return []
    return [str(entry) for entry in raw]


def gear_bonus_for(item_id):
    """(damage, armor_value) for a piece of equipment, (0, 0) for anything
    else. The two arrive together because combat will want both from one
    lookup, and because the exporter already refuses to write a weapon whose
    damage is zero."""
    item = item_row(item_id)
    if item is None:
        return 0, 0
    return int(item.get("damage", 0)), int(item.get("armor_value", 0))


def equip_check(item_id, slot_name, class_id, character_level):
    """
    May this character wear this item in this slot? Same dict shape as
    consume_check():

        {"ok": False, "reason": "unknown"}                  no such item
        {"ok": False, "reason": "notgear"}                  not equipment
        {"ok": False, "reason": "slot",  "belongs": NAME}   wrong slot
        {"ok": False, "reason": "class", "allowed": [...]}  wrong class
        {"ok": False, "reason": "level", "needs": N}        character level
        {"ok": True}

    THE SLOT CHECK IS THE ONE THAT WAS MISSING. Without it {"helm":
    "embersword"} was a perfectly legal save and the server would store it - a
    sword worn on the head, in the column combat is about to start reading.

    THE CLASS CHECK IS THE ONE THAT MATTERS MOST. required_classes is the only
    thing stopping a warrior wearing a mage's robe for its armour, and a gate
    the client alone enforces is a suggestion. An EMPTY list means anyone, so
    emptiness is tested before membership - rings and amulets are shared, and
    reading an empty list as "nobody" would lock the jewellery away from
    everybody.

    WHY THE LEVEL GATE IS HERE NOW WHEN IT WAS NOT BEFORE. This function's
    predecessor deliberately skipped it, on the grounds that equipping is a
    reference to a bag item rather than a grant of anything - the server was
    recording a preference and had no reason to police it. That stops being
    true the moment an equipped weapon decides what a hit is worth, which is
    the next thing to be built. `character_level` must be the level the SERVER
    holds, never one out of the request body, or the gate checks a number the
    player chose.

    A gamedata.json exported before equip_slot_name existed answers "" for
    every slot. That cannot be told apart from "this is not equipment", so the
    caller checks EQUIP_EXPORTED and skips this entirely rather than refusing
    every honest save on an un-re-exported server.
    """
    item = item_row(item_id)
    if item is None:
        return {"ok": False, "reason": "unknown"}

    belongs = equip_slot_for(item_id)
    if belongs == "":
        return {"ok": False, "reason": "notgear"}
    if belongs != str(slot_name).strip().lower():
        return {"ok": False, "reason": "slot", "belongs": belongs}

    allowed = equip_classes_for(item_id)
    if allowed and str(class_id).strip().lower() not in allowed:
        return {"ok": False, "reason": "class", "allowed": allowed}

    needs_level = int(item.get("required_level", 1))
    if int(character_level) < needs_level:
        return {"ok": False, "reason": "level", "needs": needs_level}

    return {"ok": True}


# =============================================================================
# COMBAT BOUNDS
# =============================================================================
# THE MOST ONE CHARACTER CAN DO TO ONE MONSTER: its biggest single hit, how
# much damage a second it can put into one target, and how fast it can walk.
# presence.py's books on every monster (combatbook.py) hold every hit a game
# reports to these, with room to spare (combatbook.SLACK), and write down what
# does not fit. E3_SCOPE.md, option C, step 1: the server watches, and nothing
# in play changes.
#
# FROM WHAT THE CHARACTER HOLDS, NOT ONLY WHAT IT WEARS. The ticket the socket
# reads (app.py, presence_ticket) names every piece worn and every piece of
# gear in the character's bag. A character that swaps to a better sword in the
# middle of a fight could always hit that hard - it was carrying the sword - so
# the weapon is the best one held and each slot's damage bonus the best held
# for that slot. An honest character never hits above this, whatever it puts on.
#
# THE ARITHMETIC IS THE GAME'S, from the four class scripts:
#   unit    (class base + the weapon's top roll) x skills x worn damage percent;
#           roundi((own_base + weapon_damage_roll()) * get_damage_multiplier())
#   speed   1 + 1% a level of agility, never more than 2
#           (PlayerStats.attack_speed_multiplier); cooldowns divide by it.
#   warrior a swing and its slash wave (wave_ratio of a swing) reach one target
#           once a swing, and a swing lasts its animation (swing_seconds), not
#           the 1.0 s lock - the lock is released when the clip ends. With the
#           Double Axe: a full swing as it passes out and back, and a swing a
#           second (the attack period) while it spins.
#   mage    one circle a cast; the Meteorite casts twice one time in ten, so
#           two a cast is the most.
#   healer  one shot a cooldown.
#   tank    the aura ticks every cooldown; with Dynamite, a stick is a second
#           of ticks (dynamite_cooldown / cooldown), and two one time in ten.
#   pet     half the character's multiplier on its own damage (pet_share),
#           half the agility bonus on its cooldown (pet_speed_share); the boss
#           pet's puddles tick for puddle_damage.
# A catalogue from before the combat block has no bounds (None), and the books
# then judge no hit at all rather than guess.

COMBAT_EXPORTED = bool(COMBAT.get("classes"))
# What a missing skill is read as: the most it could be. A ticket from an API
# that does not send skills must not make honest hits look too big.
BOUNDS_UNKNOWN_SKILL = 99
# The boss pet's puddles: two can lie under a target at once (a cast every two
# seconds, half of them leave one, a puddle lasts three) and each ticks twice a
# second. Six ticks a second is that with room.
PUDDLE_TICKS_PER_SECOND = 6


def _skill_for_bounds(skills, name):
    if not isinstance(skills, dict):
        return BOUNDS_UNKNOWN_SKILL
    try:
        return min(BOUNDS_UNKNOWN_SKILL, max(1, int(skills.get(name, 1) or 1)))
    except (TypeError, ValueError):
        return 1


def weapon_top_roll(item):
    """The most one hit of this weapon adds - PlayerStats.weapon_damage_range()'s
    high end: damage x (1 + spread), spread at most 0.9, rounded up."""
    try:
        damage = int(item.get("damage", 0) or 0)
        spread = float(item.get("damage_spread", 0.0) or 0.0)
    except (TypeError, ValueError, AttributeError):
        return 0
    if damage <= 0:
        return 0
    band = min(max(spread, 0.0), 0.9)
    low = max(1, math.floor(damage * (1.0 - band)))
    return max(low, math.ceil(damage * (1.0 + band)))


def best_loadout(gear, class_id, level):
    """(the best weapon's top roll, the best damage percent each slot could
    wear added up, the special attacks a held weapon brings) over the pieces a
    character holds AND MAY WEAR - equip_check(), the equip routes' own rule,
    so a healer carrying a Double Axe is not bounded by it. gear None means
    nobody said: the whole catalogue."""
    if not isinstance(gear, (list, tuple)):
        gear = None
    pieces = list(ITEMS.keys()) if gear is None else [g for g in gear if isinstance(g, str) and g]
    attacks = COMBAT.get("weapon_attacks") if isinstance(COMBAT.get("weapon_attacks"), dict) else {}
    top = 0
    by_slot = {}
    specials = set()
    for item_id in pieces:
        item = item_row(item_id)
        slot = equip_slot_for(item_id)
        if item is None or not slot:
            continue
        if not equip_check(item_id, slot, class_id, level).get("ok"):
            continue
        if slot == "weapon":
            top = max(top, weapon_top_roll(item))
            special = attacks.get(base_id(item_id))
            if special:
                specials.add(str(special))
        try:
            bonus = max(0, int(item.get("bonus_damage_percent", 0) or 0))
        except (TypeError, ValueError):
            bonus = 0
        by_slot[slot] = max(by_slot.get(slot, 0), bonus)
    return top, sum(by_slot.values()), specials


def combat_bounds(identity):
    """
    {"max_hit", "dps", "speed"} for the character a presence ticket describes:
    the biggest single hit it can land, the most damage a second it can put
    into ONE monster (character and pet together), and the fastest it can
    walk. None when this catalogue has no combat block.

    identity is the ticket's: "cls", "gear" (worn and carried equipment ids),
    "skills" ({attack, magic, agility}) and "pets" (the pets it may have out).
    A missing "gear" reads as the whole catalogue and missing "skills" as the
    highest level - an older API's ticket can only make the bounds looser.
    """
    if not COMBAT_EXPORTED or not isinstance(identity, dict):
        return None
    classes = COMBAT.get("classes") or {}
    cls = str(identity.get("cls", "") or "")
    if cls not in classes:
        # A class this catalogue does not know: the loosest of the four.
        options = [combat_bounds(dict(identity, cls=name)) for name in classes]
        options = [o for o in options if o]
        if not options:
            return None
        return {key: max(o[key] for o in options) for key in ("max_hit", "dps", "speed")}
    row = classes[cls]

    skills = identity.get("skills")
    attack = _skill_for_bounds(skills, "attack")
    magic = _skill_for_bounds(skills, "magic")
    agility = _skill_for_bounds(skills, "agility")
    step = float(COMBAT.get("skill_step", 0.01))
    try:
        level = max(1, int(identity.get("lvl", 1) or 1))
    except (TypeError, ValueError):
        level = 1
    if "gear" not in identity:
        level = 10 ** 6
    top, percent, specials = best_loadout(identity.get("gear") if "gear" in identity else None, cls, level)
    mult = (1.0 + (attack - 1) * step + (magic - 1) * step) * (1.0 + percent / 100.0)
    haste = min(max(1.0 + (agility - 1) * float(COMBAT.get("agility_step", 0.01)), 1.0),
                float(COMBAT.get("agility_cap", 2.0)))

    unit = (int(row.get("base", 0)) + top) * mult
    cooldown = max(float(row.get("cooldown", 1.0) or 1.0), 0.01)
    if cls == "warrior":
        swing = max(float(row.get("swing_seconds", 0.0) or 0.0), 0.05) or cooldown
        max_hit = unit
        dps = unit * haste * (1.0 + float(row.get("wave_ratio", 0.0))) / swing
        if "SPINNING_AXE" in specials:
            dps = max(dps, unit * haste * (1.0 / cooldown + 2.0 / swing))
    elif cls == "mage":
        casts = 2.0 if "METEOR" in specials else 1.0
        max_hit = unit
        dps = unit * haste * casts / cooldown
    elif cls == "tank":
        max_hit = unit
        dps = unit * haste / cooldown
        if "DYNAMITE" in specials:
            fuse = max(float(row.get("dynamite_cooldown", 1.0) or 1.0), 0.01)
            sticks = fuse / cooldown
            max_hit = unit * sticks
            dps += unit * sticks * haste * 2.0 / fuse
    else:
        max_hit = unit
        dps = unit * haste / cooldown

    pets = COMBAT.get("pets") or {}
    claimed = identity.get("pets") if isinstance(identity.get("pets"), (list, tuple)) else []
    held = [p for p in claimed if isinstance(p, str) and p in pets]
    if held:
        share = float(COMBAT.get("pet_share", 0.5))
        pet_haste = 1.0 + (haste - 1.0) * float(COMBAT.get("pet_speed_share", 0.5))
        pet_hit = max(int(int(pets[p].get("damage", 0)) * mult * share) for p in held)
        pet_dps = max(int(int(pets[p].get("damage", 0)) * mult * share) * pet_haste
                      / max(float(pets[p].get("cooldown", 2.0) or 2.0), 0.01) for p in held)
        max_hit = max(max_hit, pet_hit)
        dps += pet_dps + float(COMBAT.get("puddle_damage", 0)) * PUDDLE_TICKS_PER_SECOND

    walk = (int(row.get("speed", 0) or 0) + (agility - 1) * int(COMBAT.get("agility_speed", 10))) \
        * float(COMBAT.get("sprint", 2.0) or 2.0)
    return {"max_hit": int(math.ceil(max_hit)), "dps": round(dps, 2), "speed": round(float(walk), 1)}


# HOW FAST A CHARACTER RECOVERS ON ITS OWN.
#
# These come from PlayerStats, through exportgamedata.gd, because the server
# has to tell an honest rise in health from an invented one and cannot do that
# without knowing the rate. The fallbacks below are what app.py used to hold as
# literals, and they are a FALLBACK rather than the source: a gamedata.json
# exported before these were added still runs, it just measures against numbers
# nobody is keeping in step. app.py says so at boot when that happens.
REGEN_EXPORTED = "regen_percent_per_second" in CONSTANTS


def regen_rate_for(stat_max):
    """Points per second a pool of this size recovers. Mirrors
    PlayerStats.regen_rate_for(), including its floor for small pools."""
    percent = float(CONSTANTS.get("regen_percent_per_second", 0.0167))
    minimum = float(CONSTANTS.get("regen_minimum_per_second", 1.0))
    return max(minimum, float(stat_max) * percent)


# Item types the loot roll never picks, by NAME rather than by the integer the
# enum happens to have. ItemData.Type is an ordered enum in GDScript; inserting
# a new type at the top would renumber every one below it, and a server
# filtering on `type == 5` would start excluding the wrong thing without a
# single error anywhere.
EXCLUDED_FROM_LOOT = {"PET", "QUEST", "CURRENCY", "FISH"}


# The only type /api/character/consume will destroy. Same reasoning as
# EXCLUDED_FROM_LOOT: by name, never by the enum's integer.
#
# PETS ARE NOT CONSUMABLE and that is a design decision, not an oversight -
# inventoryscreen.gd::_use_pet() says so at length. A pet item is both the
# summon and the dismiss, so consuming it would make swapping pets a one-way
# door. CURRENCY is excluded for the opposite reason: a gold pile IS used up,
# but it turns into a balance, and gold that moves without a gold_ledger row is
# the one thing the economy suite exists to catch. Whoever adds that path adds
# it with a ledger write, not by widening this set.
CONSUMABLE_TYPES = {"CONSUMABLE"}


def consume_check(item_id, character_level, skill_levels, known_skills=None):
    """
    May this character use one of this item? Returns a dict, the same shape
    roll_cook() uses:

        {"ok": False, "reason": "unknown"}                no such item
        {"ok": False, "reason": "nottype", "type": NAME}  not a consumable
        {"ok": False, "reason": "level",  "needs": N}     character level
        {"ok": False, "reason": "skill",  "needs": N, "skill": "cooking"}
        {"ok": True}

    THIS IS THE RULE THE CLIENT ALREADY HAS, MOVED SOMEWHERE IT CANNOT BE
    PATCHED. inventoryscreen.gd::_meets_requirements() checks the same two
    fields and says so in its own comment: the client refusing is a courtesy,
    the server refusing is the rule.

    WHY BOTH FIELDS EXIST AND WHY THEY ARE NOT ONE FIELD. `required_level` is
    character level, which is the right axis for gear you BUY - it is the ladder
    the shop sells against. `required_skill` is a named skill, which is the
    right axis for things you MADE: a cooked shark is gated on cooking, because
    fishing and cooking grow on their own curves and a character-level bound
    would clamp a dedicated cook who has never killed anything. The same
    reasoning is written out under E-2 in SECURITY_NOTES, where it is the reason
    a character-level heuristic was rejected for skills.

    A SKILL THE GAME DOES NOT HAVE passes, loudly. It means a typo in the .tres,
    and refusing would take a working item away from every player over an editor
    slip. The client does the same thing for the same reason - and the caller
    logs it, because a gate that has quietly stopped gating is the one outcome
    worth avoiding.

    A SKILL THE CHARACTER HAS NO ROW FOR IS LEVEL 1, WHICH IS NOT THE SAME
    THING, and conflating the two is a bypass rather than a nicety. Skill rows
    are written when a skill is first trained, so a fresh character legitimately
    has none - and the first version of this function treated "cooking is not in
    your levels dict" as "cooking is not a skill", which let every brand new
    account drink a level 55 cooking item. `known_skills` is what separates the
    two questions: in the set and absent from the dict means level 1 and the
    gate holds; not in the set at all means nobody can answer and it passes.
    Found by a smoke test, not by review.
    """
    item = item_row(item_id)
    if item is None:
        return {"ok": False, "reason": "unknown"}

    type_name = str(item.get("type_name", ""))
    if type_name not in CONSUMABLE_TYPES:
        return {"ok": False, "reason": "nottype", "type": type_name}

    needs_level = int(item.get("required_level", 1))
    if int(character_level) < needs_level:
        return {"ok": False, "reason": "level", "needs": needs_level}

    skill = str(item.get("required_skill", "")).strip()
    if skill == "":
        return {"ok": True}

    needs_skill = int(item.get("required_skill_level", 1))
    if needs_skill <= 1:
        return {"ok": True}

    if known_skills is not None and skill not in known_skills:
        return {"ok": True, "unknown_skill": skill}

    # Absent from the dict but real: untrained, which is level 1.
    if int(skill_levels.get(skill, 1)) < needs_skill:
        return {"ok": False, "reason": "skill", "needs": needs_skill, "skill": skill}

    return {"ok": True}


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


def apply_xp(level, xp, xp_to_next, gained, need=None):
    """
    Port of Player.gain_xp()'s level-up loop. Pure - it computes, it does not
    write. Returns (level, xp, xp_to_next, levels_gained).

    The loop is `while`, not `if`, because a single kill can cross more than
    one level boundary at low levels, and an `if` would silently bank the
    excess XP against a level the player never reached.
    """
    levels_gained = 0
    xp += gained

    # WHICH CURVE. `need(level)` is the requirement for one level; the character
    # curve unless a caller passes its own. Skills pass theirs - and before this
    # argument existed, a skill grant that crossed two levels priced the second
    # one on the CHARACTER curve, because the loop below hardcoded it. Harmless
    # while the two curves were 100 x 1.15 and 100 x 1.18; not once the
    # character curve became 1,250 x 1.27.
    if need is None:
        need = xp_needed_for_level

    # THE REQUIREMENT IS DERIVED FROM THE LEVEL, NOT READ FROM THE ROW. It is a
    # pure function of level (the client's sanitizer already recomputes its own
    # copy on every load), and the stored figure goes stale the moment the curve
    # is retuned - which it was, for the eight hours to level 22. A row still
    # holding the old 100 would otherwise hand every new character a first level
    # twelve times cheaper than the curve. The argument stays in the signature
    # so callers do not change.
    xp_to_next = need(level)

    while xp >= xp_to_next:
        xp -= xp_to_next
        level += 1
        levels_gained += 1
        xp_to_next = need(level)

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

# The item types a boss's guaranteed slot may hold (EnemyData.slots_are_gear).
LOOT_GEAR_TYPES = {"WEAPON", "ARMOR"}

# EnemyData.tier_odds's default, for a catalogue exported before the field
# existed. Top tier first: 15% the enemy's own tier, 45% one below, 40% two.
DEFAULT_TIER_ODDS = (0.15, 0.45, 0.40)


def _droppable(item):
    """Can this item come out of a bag at all? Tier aside."""
    if item["type_name"] in EXCLUDED_FROM_LOOT:
        return False
    # PER-ITEM OPT-OUT, independent of tier. A cooked fish is a
    # Type.CONSUMABLE exactly like a potion, so the type exclusion above
    # cannot reach it - and a slime dropping cooked mudfish takes the point
    # out of the fishing skill. See ItemData.droppable in the Godot project
    # for the full reasoning. .get() with a default because a gamedata.json
    # exported before the field existed simply will not have it, and the
    # server refusing to boot over an additive field would be worse than
    # treating an old catalogue as all-droppable.
    return bool(item.get("droppable", True))


def loot_pool(tier, kind="any"):
    """
    Every item one bag slot could hold at exactly this tier, in id order so a
    seeded roll is repeatable. kind: "any", "gear" (weapons and armour) or
    "potion" (consumables).
    """
    out = []
    for item in ITEMS.values():
        if int(item["tier"]) != int(tier) or not _droppable(item):
            continue
        if kind == "gear" and item["type_name"] not in LOOT_GEAR_TYPES:
            continue
        if kind == "potion" and item["type_name"] not in CONSUMABLE_TYPES:
            continue
        out.append(item["item_id"])
    out.sort()
    return out


def roll_loot_tier(enemy):
    """
    Which tier one filled slot rolls. TIER FIRST, then the item.

    IT USED TO BE ONE WEIGHT PER ITEM, 2^(max_tier - item.tier), and that let
    the catalogue decide the odds: eleven iron pieces outvoted everything, so
    58% of what a boss dropped was iron and the amethyst and ember gear came
    out at 1%. Now EnemyData.tier_odds says what share of slots land on each
    tier, top first, however many items each tier holds.

    Never below 1. A tier_up_chance hit rolls one above the enemy's tier.
    """
    top = max(1, int(enemy.get("max_loot_tier", 1)))
    up = float(enemy.get("tier_up_chance", 0.0) or 0.0)
    if up > 0.0 and _rng.random() < up:
        return top + 1
    odds = enemy.get("tier_odds") or DEFAULT_TIER_ODDS
    roll = _rng.random()
    running = 0.0
    for step, share in enumerate(odds):
        running += float(share)
        if roll < running:
            return max(1, top - step)
    return max(1, top - (len(odds) - 1))


def pick_loot_item(tier, kind="any"):
    """
    An item at this tier, evenly - or, when the tier holds nothing droppable of
    this kind, at the nearest tier BELOW that does. Returns "" only when
    nothing at or below the tier qualifies.

    Stepping down, never up: a roll at a tier with nothing in it gets the best
    piece of the tier below, not nothing and not something above it.
    """
    for t in range(int(tier), 0, -1):
        pool = loot_pool(t, kind)
        if pool:
            return pool[_rng.randrange(len(pool))]
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


def mythic_tier():
    """The tier the mythic roll pays out from. GameConstants.MYTHIC_TIER."""
    return int(CONSTANTS.get("mythic_tier", 6))


def mythic_pool(class_id=""):
    """
    The mythic pieces a character of this class can be given, in id order so a
    seeded roll is repeatable: droppable weapons and armour at mythic_tier()
    that this class may wear (required_classes names it, or is empty).

    A CLASS WITH NONE OF ITS OWN GETS ALL OF THEM. On day 2 the healer has no
    mythic yet, and a win that turned into nothing would be the worst thing
    this roll could do. The day a healer piece exists, healers get that.
    """
    tier = mythic_tier()
    every = sorted(
        item["item_id"] for item in ITEMS.values()
        if int(item.get("tier", 0)) == tier and _droppable(item)
        and item["type_name"] in LOOT_GEAR_TYPES
    )
    own = [
        item_id for item_id in every
        if not equip_classes_for(item_id) or class_id in equip_classes_for(item_id)
    ]
    return own or every


def roll_mythic(enemy, class_id=""):
    """
    Did this kill drop a mythic, and which one? "" for no.

    A ROLL OF ITS OWN, BESIDE THE BAG'S. No enemy's tier_odds reaches the
    mythic tier; each enemy carries "one in N" odds instead (mythic_odds, worked
    out by EnemyData.mythic_odds() in the game and exported). The owner's call
    on day 2: regular mobs and bosses both drop them, the tougher the better,
    "super rewarding getting 1". A win is the killer's own class's piece, so it
    is never a weapon they can only sell.
    """
    odds = int(enemy.get("mythic_odds", 0) or 0)
    if odds <= 0:
        return ""
    if _rng.randint(1, odds) != odds:
        return ""
    pool = mythic_pool(class_id)
    if not pool:
        return ""
    return pool[_rng.randrange(len(pool))]


def build_bag_contents(enemy):
    """Port of BaseEnemy._build_bag_contents()."""
    contents = []

    max_tier = int(enemy.get("max_loot_tier", 1))

    # Gold is guaranteed on every bag and has no rarity gate. That is
    # deliberate and matches the design: currency flows freely, items are
    # scarce.
    #
    # GEOMETRIC IN TIER, NOT LINEAR. This was randint(max_tier, max_tier * 25),
    # which grew income linearly while every price in the game grows
    # geometrically - so a tier-5 player was ~7x poorer in real terms than a
    # tier-1 player, and the best gold farm in the game was the starting zone.
    # See THE GOLD CURVE in baseenemy.gd for the full reasoning; the constants
    # are read from there via gamedata.json so there is only ever one 2.6.
    #
    # Tier 1 is unchanged: unit = 1, so this is still randint(1, 25).
    ratio = float(CONSTANTS.get("gold_tier_ratio", 2.6))
    base = float(CONSTANTS.get("gold_base_unit", 1.0))
    spread = int(CONSTANTS.get("gold_spread", 25))
    unit = max(1, int(round(base * (ratio ** (max_tier - 1)))))
    gold_amount = _rng.randint(unit, unit * spread)

    # THE DICE, ON TOP OF THE ORDINARY ROLL. See THE JACKPOT DICE above for
    # why this is dice and not a percentage, and why it multiplies the tier's
    # own roll instead of replacing it.
    sixes, multiplier = roll_gold_jackpot(max_tier)
    if multiplier > 1:
        gold_amount *= multiplier
    # PAID IN COINS, NOT IN ONE HEAP. See make_change() for why greedy is
    # exact here and what happens to a remainder.
    ladder = gold_denominations()
    if ladder:
        contents.extend(make_change(gold_amount, ladder))
    else:
        # NO LADDER AT ALL means a data file with no currency in it. The old
        # two-id behaviour is kept as the floor so a bag still pays out.
        threshold = int(CONSTANTS.get("large_gold_threshold", 100))
        gold_id = (
            CONSTANTS.get("gold_large_id", "largeamountofgold")
            if gold_amount >= threshold
            else CONSTANTS.get("gold_small_id", "smallamountofgold")
        )
        if has_item(gold_id):
            contents.append({"item_id": gold_id, "quantity": gold_amount})

    slot_fill_chance = float(enemy.get("slot_fill_chance", 0.0))
    slot_kind = "gear" if enemy.get("slots_are_gear", False) else "any"
    for _ in range(int(enemy.get("max_item_slots", 0))):
        if _rng.random() <= slot_fill_chance:
            picked = pick_loot_item(roll_loot_tier(enemy), slot_kind)
            if picked:
                # A dropped piece rolls its stats; a potion has none to roll.
                contents.append({"item_id": roll_quality(picked), "quantity": 1})

    # A boss's potion rides beside its gear piece rather than competing with it
    # for the one slot.
    potion_chance = float(enemy.get("bonus_potion_chance", 0.0) or 0.0)
    if potion_chance > 0.0 and _rng.random() < potion_chance:
        picked = pick_loot_item(roll_loot_tier(enemy), "potion")
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

    Weighted like the old item roll, 2^(ceiling - tier) per fish, so the
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
    item = item_row(item_id)
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


def rarest_first(contents):
    """
    The bag's entries with the pet first, then the other items, then the coins.

    THE ORDER IS WHAT A FULL BAG LOSES. The panel has a fixed number of cells
    and app._create_loot_bag() cuts anything past them. Coins used to come
    first, because gold is rolled first - and once a drop's gold was split
    into denominations it took up to four cells on its own, so the cut fell on
    the items and on the pet, which is appended last. Measured on the old
    rules, 76% of boss bags ran past six cells and 573 of 616 boss pet wins
    were cut away without a word. Coins last means a cut costs copper.
    """
    def rank(entry):
        item = item_row(entry.get("item_id", "")) or {}
        kind = item.get("type_name", "")
        # A mythic is cut by nothing, like the pet.
        if kind == "PET" or (kind in LOOT_GEAR_TYPES and int(item.get("tier", 0)) >= mythic_tier()):
            return 0
        if kind == "CURRENCY":
            return 2
        return 1
    return sorted(contents, key=rank)


def roll_kill_rewards(enemy_id, class_id=""):
    """
    The whole reward for one kill. Port of BaseEnemy._die() plus
    _roll_and_spawn_loot(), minus everything to do with spawning a node.
    class_id is the killer's, for the mythic roll (roll_mythic()).

    Returns:
        {
          "enemy_id":   str,
          "xp":         int,
          "attack_xp":  int,
          "pet_won":    bool,
          "mythic":     str,    the mythic item_id in the bag, or ""
          "contents":   [{"item_id": str, "quantity": int}, ...],
        }

    An empty `contents` is a normal outcome, not a failure - most kills drop
    nothing at all. A mythic, like a pet, makes a bag even when the bag roll
    said no.
    """
    enemy = ENEMIES.get(enemy_id)
    if enemy is None:
        raise KeyError(enemy_id)

    pet_won = roll_pet(enemy)
    bag_drops = _rng.random() <= float(enemy.get("bag_drop_chance", 0.0))
    # A mythic rolls like any other dropped piece. The answer's "mythic" is
    # the rolled id, the one in the bag.
    mythic = roll_mythic(enemy, class_id)
    if mythic:
        mythic = roll_quality(mythic)

    contents = []
    if bag_drops or pet_won or mythic:
        contents = build_bag_contents(enemy)
        if pet_won:
            contents.append({"item_id": pick_pet_id(enemy), "quantity": 1})
        if mythic:
            contents.append({"item_id": mythic, "quantity": 1})
        contents = rarest_first(contents)

    return {
        "enemy_id": enemy_id,
        "xp": int(enemy.get("xp_reward", 0)),
        "attack_xp": int(enemy.get("attack_xp_reward", 0)),
        "pet_won": pet_won,
        "mythic": mythic,
        "contents": contents,
    }
