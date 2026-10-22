"""gathering/services.py - the rules: the bait, the rod, the cast bucket, and
the answers. Pure: no database, no request."""

# What a fishing spot takes as bait, one per fish landed.
#
# NAMED HERE RATHER THAN SENT BY THE CLIENT. fishingspot.gd has its own
# bait_item_id export so a spot can want something else, but that is a display
# and early-refusal concern - a client that named its own bait would name the
# cheapest thing it had.
FISHING_BAIT_ID = "fishingworm"

# Rods are matched on this suffix, matching fishingspot.gd's _best_rod_tier().
FISHING_ROD_SUFFIX = "fishingrod"

# Cast rate limit, the same token bucket /api/combat/kill uses.
#
# SIZED FOR THE REAL ACTIVITY. A cast is a 2-6.5s wait plus a bite window, so an
# honest player lands well under one fish every three seconds and never sees
# this. It exists so a patched client cannot turn the endpoint into a printing
# press by removing the wait.
CAST_BUCKET_CAPACITY = 6.0
CAST_TOKENS_PER_SECOND = 0.4


def best_rod_tier(item_ids, item_row):
    """Highest fishing rod tier among these carried item_ids, or 0 for none.
    item_row is the catalogue's lookup (gamedata.item_row): an id -> its row,
    or None for an item it does not know."""
    best = 0
    for item_id in item_ids:
        if not item_id.endswith(FISHING_ROD_SUFFIX):
            continue
        item = item_row(item_id)
        if item is None:
            continue
        best = max(best, int(item["tier"]))
    return best


def rolls_of(item_id, item_row):
    """How many times a cast with this bait rolls the catch: the catalogue's
    bait_rolls (ItemData, 0.74.0), and 0 for anything that is not bait.

    THE WORM IS ALWAYS BAIT, AT ONE ROLL AT LEAST. It was the only bait for
    seventy releases and the game sets its bait_rolls to 1; a catalogue from
    before the field says nothing about it, and the worm must not stop being
    bait because a number was missing."""
    row = item_row(item_id) or {}
    try:
        rolls = max(0, int(row.get("bait_rolls", 0) or 0))
    except (TypeError, ValueError):
        rolls = 0
    if item_id == FISHING_BAIT_ID:
        rolls = max(rolls, 1)
    return rolls


def best_bait(item_ids, item_row):
    """(item_id, rolls) of the best bait among these carried ids - the most
    rolls, a tie to the first id in order - or ("", 0) for none.

    THE SERVER CHOOSES, NOT THE CLIENT (0.74.0): a client that named its own
    bait would name the cheapest thing it had and say it was the best. Best
    first because bait is bought to be used: a glowgrub in the bag is a player
    who wants three rolls, and the worms underneath are the fallback."""
    best = ("", 0)
    for item_id in sorted(set(item_ids)):
        rolls = rolls_of(item_id, item_row)
        if rolls > best[1]:
            best = (item_id, rolls)
    return best


def best_catch(catches, item_row):
    """The catch kept from a cast that rolled more than once: the highest-tier
    fish, a tie to the first rolled. None only when nothing was rolled."""
    best, best_tier = None, None
    for catch in catches:
        if catch is None:
            continue
        try:
            tier = int((item_row(catch["item_id"]) or {}).get("tier", 0) or 0)
        except (TypeError, ValueError):
            tier = 0
        if best is None or tier > best_tier:
            best, best_tier = catch, tier
    return best


def refilled(tokens, last_cast_at_ms, now_ms):
    """The cast bucket now: what it held at the last cast, and what has
    dripped in since, never past CAST_BUCKET_CAPACITY. Refill then spend,
    exactly as /api/combat/kill does."""
    elapsed_seconds = max(int(now_ms) - int(last_cast_at_ms), 0) / 1000.0
    return min(
        CAST_BUCKET_CAPACITY,
        float(tokens) + elapsed_seconds * CAST_TOKENS_PER_SECOND,
    )


def caught(catch, written, levels_gained, level, status, inventory, skills, bait=FISHING_BAIT_ID):
    """The answer to a landed fish: what it was, the bait it took (0.74.0), the
    cells it went into, the skill, and the totals."""
    return {
        "item_id": catch["item_id"],
        "quantity": catch["quantity"],
        "xp": catch["xp"],
        "bait": bait,
        "carry_positions": written,
        "levelled_up": levels_gained > 0,
        "fishing_level": level,
        "status": status,
        "inventory": inventory,
        "skills": skills,
    }


def cooked(item_id, outcome, written, levels_gained, level, status, inventory, skills):
    """The answer to a fish on the fire: burnt or not, what came of it, the
    skill, and the totals. A meal's answer (larder/, 0.74.0) is this one with
    what it took added."""
    return {
        "item_id": item_id,
        "burnt": outcome["burnt"],
        "output": outcome["output"],
        "xp": outcome["xp"],
        "carry_positions": written,
        "levelled_up": levels_gained > 0,
        "cooking_level": level,
        "status": status,
        "inventory": inventory,
        "skills": skills,
    }
