"""consuming/data.py - the SQL. saves and carry_items are app.py's tables
(init_db()). What a potion authorised is written down through status/'s
note_refill(), handed in - the one writer of consume_grants, which the healing
check and the combat books (presence.py) read. The potion itself leaves the
bag through app.py's backpack grant, handed in. No commits: the route makes
them.

ONE TABLE IS THIS PACKAGE'S (0.74.0): food_buffs (schema.py), the meal a
character ate last while it lasts. Its rows are added here and nowhere else.
Others READ it: the character's load (saves/), the presence ticket the combat
books bound a fighter by (players/) and the aura's heal (presence.py); a
character deleted takes its row with it (saves/data.py drop())."""


def set_buff(db, user_id, slot, item_id, kind, amount, ends_at):
    """The meal just eaten, replacing the one before. No commit."""
    db.execute(
        "INSERT OR REPLACE INTO food_buffs (user_id, slot, item_id, kind, amount, ends_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, slot, item_id, kind, int(amount), int(ends_at)),
    )


def buff_of(db, user_id, slot, now):
    """The meal in force at `now` for this character, or None."""
    return db.execute(
        "SELECT item_id, kind, amount, ends_at FROM food_buffs"
        " WHERE user_id = ? AND slot = ? AND ends_at > ?",
        (user_id, slot, int(now)),
    ).fetchone()


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


