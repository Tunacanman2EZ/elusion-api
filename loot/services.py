"""loot/services.py - the rules: how long a bag lasts, how many cells it has,
a minigame's chest and how a win goes into it, and the answers. Pure: no
database, no request."""

# How long the server will honour a bag after it is created.
#
# Deliberately far longer than the client's LOOT_BAG_DESPAWN_SECONDS (45). The
# client despawning the node is a display decision; if the two disagree the
# player should lose the bag to the ANIMATION, never to a 410 from a server that
# expired it a moment early. This exists to stop bags accumulating forever, not
# to enforce the despawn.
LOOT_BAG_TTL_SECONDS = 600

# The loot panel's grid, in cells. lootbaginventory.gd's LOOT_SIZE.
#
# The position IS the grid cell on both sides - that is what lets the client
# send "take cell 2" and mean the thing the player is looking at. A bag rolled
# with more entries than the panel can show would put loot behind a cell that
# does not exist, and the player would never be able to ask for it.
#
# NINE, AND IT WAS SIX. The note here said rolls were capped at five - one gold
# pile, three item slots, one pet - and that stopped being true when gold began
# dropping as coins: one drop's gold is up to four entries on its own. Measured
# on the old rules, 76% of boss bags ran past six and the cut fell on the pet,
# appended last: 573 of 616 boss pet wins never reached the player. Now the
# roll puts coins last (gamedata.rarest_first) and there are nine cells, so a
# cut, when there is one, costs the smallest coin.
LOOT_BAG_CAPACITY = 9


def stamped(contents):
    """A roll as it is stored: cut to LOOT_BAG_CAPACITY, each entry with the
    POSITION it is taken by - the only thing /api/loot/take accepts."""
    return [
        {"position": position, "item_id": entry["item_id"], "quantity": int(entry["quantity"])}
        for position, entry in enumerate(contents[:LOOT_BAG_CAPACITY])
    ]


def expired(created_at, now):
    """Whether a bag made at created_at is past honouring at now."""
    return int(created_at) < int(now) - LOOT_BAG_TTL_SECONDS


def contents_answer(bag_id, bag, rows):
    """GET /api/loot/bag's answer: the bag, and what is left in it."""
    return {
        "bag_id": bag_id,
        "slot": int(bag["slot"]),
        "enemy_id": bag["enemy_id"],
        "contents": [
            {"position": int(r["position"]), "item_id": r["item_id"], "quantity": int(r["quantity"])}
            for r in rows
        ],
    }


# =============================================================================
# A MINIGAME'S CHEST (game 0.93.4)
# =============================================================================
# The Endless pays into a chest in its corridor, not a bag on the floor: the
# door is one way, so a bag in the arena is lost to anybody who falls, and to
# everybody when the arena empties. One chest a character and minigame, kept
# until taken or the account's next Go. It is taken from like a bag, through
# /api/loot/take, by an id that names it: "chest:<game>:<slot>" - scoped to the
# account in every query, so naming somebody else's finds nothing.
#
# THIRTY-SIX CELLS, six by six: lootbaginventory.gd draws a chest at
# CHEST_COLUMNS wide. A run's coins, materials and potions fold onto the cells
# already holding them, so the cells go on gear - about one in nine elites.
# When it is full, a new find takes the place of the least of what is there if
# it is rarer (chest_after()); what loses is said back as "cut".

CHEST_CELLS = 36
CHEST_COLUMNS = 6
CHEST_PREFIX = "chest:"


def chest_id(game, slot):
    """The id a chest is taken from by: "chest:endless:0"."""
    return "%s%s:%d" % (CHEST_PREFIX, game, int(slot))


def parse_chest_id(bag_id):
    """(game, slot) for a chest's id, or None for anything else - a bag's id
    included (token_urlsafe never makes a colon)."""
    if not str(bag_id).startswith(CHEST_PREFIX):
        return None
    parts = str(bag_id)[len(CHEST_PREFIX):].split(":")
    if len(parts) != 2 or not parts[0] or not parts[1].isdigit():
        return None
    return parts[0], int(parts[1])


def _stack_size(row):
    """How many of an item one cell holds: 1 for anything that does not stack."""
    if not row.get("stackable"):
        return 1
    try:
        return max(1, int(row.get("max_stack", 1) or 1))
    except (TypeError, ValueError):
        return 1


def worth(item_row, rank, item_id):
    """How much a cell is kept when the chest is full: a smaller rank first
    (the pet and a mythic, then gear and the rest, then coins - the bag's own
    rarest_first()), then the more valuable."""
    row = item_row(item_id) or {}
    try:
        value = int(row.get("value", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return (-int(rank(item_id)), value)


def chest_after(cells, entries, capacity, item_row, rank):
    """
    The chest with a win added: (cells, cut).

    cells    {position: [item_id, quantity]} - the chest now; changed in place
    entries  [{"item_id", "quantity"}] - the roll
    rank     item_id -> 0 the rarest (pet, mythic), 1 the rest, 2 coins

    Each entry folds onto cells already holding it while they have room, then
    takes the lowest free cell. With none free it takes the place of the least
    kept cell (worth()) when it is worth more, and whichever loses is cut.
    """
    cut = []
    for entry in entries:
        item_id = str(entry.get("item_id", ""))
        quantity = int(entry.get("quantity", 0) or 0)
        if not item_id or quantity <= 0:
            continue
        most = _stack_size(item_row(item_id) or {})
        if most > 1:
            for cell in sorted(cells):
                if quantity <= 0:
                    break
                held = cells[cell]
                if held[0] == item_id and held[1] < most:
                    added = min(most - held[1], quantity)
                    held[1] += added
                    quantity -= added
        while quantity > 0:
            amount = min(quantity, most)
            quantity -= amount
            free = next((cell for cell in range(capacity) if cell not in cells), None)
            if free is not None:
                cells[free] = [item_id, amount]
                continue
            least = min(cells, key=lambda cell: worth(item_row, rank, cells[cell][0]))
            if worth(item_row, rank, item_id) > worth(item_row, rank, cells[least][0]):
                cut.append({"item_id": cells[least][0], "quantity": cells[least][1]})
                cells[least] = [item_id, amount]
            else:
                cut.append({"item_id": item_id, "quantity": amount})
    return cells, cut


def chest_answer(game, slot, rows):
    """GET /api/loot/chest's answer: the chest's id, its size and what is in it."""
    return {
        "bag_id": chest_id(game, slot),
        "game": game,
        "slot": int(slot),
        "cells": CHEST_CELLS,
        "columns": CHEST_COLUMNS,
        "contents": [
            {"position": int(r["position"]), "item_id": r["item_id"], "quantity": int(r["quantity"])}
            for r in rows
        ],
    }
