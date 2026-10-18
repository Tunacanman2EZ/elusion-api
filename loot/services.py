"""loot/services.py - the rules: how long a bag lasts, how many cells it has,
and the answers. Pure: no database, no request."""

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
