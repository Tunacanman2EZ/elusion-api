"""loot/data.py - the SQL. loot_bags and loot_bag_items are app.py's tables
(init_db()). Everything that reads or writes them is here, but for one
delete: app.py's character_delete() clears a character's bags with the rest of
it. No commits: the take's commit is its route's, a new bag's is the kill's."""


def store_bag(db, bag_id, user_id, slot, enemy_id, stored, now, prune_before):
    """A rolled bag, already stamped(), into the two tables - and every bag
    made before prune_before out of them.

    The cleanup is opportunistic. Bags are small and expire on their own
    terms, so it runs here rather than on a schedule - a player who is killing
    things is exactly the player generating rows worth clearing."""
    db.execute(
        "INSERT INTO loot_bags (bag_id, user_id, slot, enemy_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (bag_id, user_id, slot, enemy_id, now),
    )
    db.executemany(
        "INSERT INTO loot_bag_items (bag_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
        [(bag_id, e["position"], e["item_id"], e["quantity"]) for e in stored],
    )
    db.execute("DELETE FROM loot_bags WHERE created_at < ?", (prune_before,))


def bag_of(db, bag_id, user_id):
    """The bag's (slot, enemy_id, created_at) - or None, for a bag that does
    not exist AND for one that is somebody else's.

    SCOPED TO user_id IN THE QUERY ITSELF, not checked afterwards: the two
    answer exactly the same, and there is nothing to learn from asking."""
    return db.execute(
        "SELECT slot, enemy_id, created_at FROM loot_bags WHERE bag_id = ? AND user_id = ?",
        (bag_id, user_id),
    ).fetchone()


def drop_bag(db, bag_id):
    db.execute("DELETE FROM loot_bags WHERE bag_id = ?", (bag_id,))


def cell(db, bag_id, position):
    """(item_id, quantity) in that cell of the bag, or None."""
    return db.execute(
        "SELECT item_id, quantity FROM loot_bag_items WHERE bag_id = ? AND position = ?",
        (bag_id, position),
    ).fetchone()


def empty_cell(db, bag_id, position):
    db.execute(
        "DELETE FROM loot_bag_items WHERE bag_id = ? AND position = ?", (bag_id, position)
    )


def left_in(db, bag_id):
    """How many cells of the bag still hold something."""
    return db.execute(
        "SELECT COUNT(*) FROM loot_bag_items WHERE bag_id = ?", (bag_id,)
    ).fetchone()[0]


def contents(db, bag_id):
    """What is still in the bag, cell by cell."""
    return db.execute(
        "SELECT position, item_id, quantity FROM loot_bag_items WHERE bag_id = ? ORDER BY position",
        (bag_id,),
    ).fetchall()
