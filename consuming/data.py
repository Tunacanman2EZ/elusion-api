"""consuming/data.py - the SQL. saves, carry_items and consume_grants are
app.py's tables (init_db()). Every write to consume_grants is here; the
healing reconciler (app.py's status write) and the combat books
(presence.py) read it. The potion itself leaves the bag through app.py's
backpack grant, handed in. No commits: the route makes them."""


def level_of(db, user_id, slot):
    """The character's level, or None for no such character."""
    row = db.execute(
        "SELECT level FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return None if row is None else int(row["level"])


def carried(db, user_id, slot, item_id):
    """How many of `item_id` this character carries, bag and hotbar."""
    return int(db.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM carry_items "
        "WHERE user_id = ? AND slot = ? AND item_id = ?",
        (user_id, slot, item_id),
    ).fetchone()[0])


def grant(db, user_id, slot, item_id, target, amount, now):
    """WHAT THIS AUTHORISED, not merely that it happened. The healing
    reconciler reads these rows, and with only a timestamp to go on the
    cheapest potion in the game explained a rise of any size in any pool."""
    db.execute(
        "INSERT INTO consume_grants (user_id, slot, item_id, target, amount, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, slot, item_id, target, int(amount), now),
    )
