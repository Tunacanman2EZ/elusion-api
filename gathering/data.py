"""gathering/data.py - the SQL. Both tables are app.py's (init_db()): the
saves row's cast bucket (last_cast_at, cast_tokens - fishing's alone), and
carry_items, which the whole carry shares. Nothing here writes an item: a
catch, a dish, a spent worm or fish all go through the grants handed in."""


def cast_bucket(db, user_id, slot):
    """This character's (last_cast_at, cast_tokens), or None for an empty
    slot."""
    return db.execute(
        "SELECT last_cast_at, cast_tokens FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def carried_ids(db, user_id, slot):
    """Every item_id this character carries, once each."""
    rows = db.execute(
        "SELECT DISTINCT item_id FROM carry_items WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchall()
    return [row["item_id"] for row in rows]


def held(db, user_id, slot, item_id):
    """How many of item_id this character carries, across every cell."""
    return int(db.execute(
        "SELECT COALESCE(SUM(quantity), 0) FROM carry_items "
        "WHERE user_id = ? AND slot = ? AND item_id = ?",
        (user_id, slot, item_id),
    ).fetchone()[0])


def spend_cast(db, user_id, slot, now_ms, tokens):
    """The bucket as this cast left it. No commit - the cast's own."""
    db.execute(
        "UPDATE saves SET last_cast_at = ?, cast_tokens = ? WHERE user_id = ? AND slot = ?",
        (now_ms, tokens, user_id, slot),
    )
