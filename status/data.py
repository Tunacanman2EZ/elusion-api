"""status/data.py - the SQL. saves holds the stats (app.py's init_db());
consume_grants is what the server authorised a pool to rise by - written by
consuming/, death/, the kill route and the presence server's heals, read here;
users.deaths counts a death. No commits: the routes make them."""


def row_of(db, user_id, slot):
    """One character's whole row, or None."""
    return db.execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def grants_since(db, user_id, slot, since):
    """Every grant written for this character at or after `since`."""
    return db.execute(
        "SELECT item_id, target, amount FROM consume_grants "
        "WHERE user_id = ? AND slot = ? AND at >= ?",
        (user_id, slot, since),
    )


def count_death(db, user_id):
    db.execute("UPDATE users SET deaths = deaths + 1 WHERE id = ?", (user_id,))


def write(db, user_id, slot, updates, now):
    """Write `updates` ({column: value}) to the character's row.

    The column names are the route's own - STATUS_FIELDS, DERIVED_STATS and
    pools_at - never a request's keys, so building the SET from them is safe."""
    assignments = ", ".join("%s = ?" % f for f in updates)
    values = list(updates.values()) + [now, user_id, slot]
    db.execute(
        "UPDATE saves SET %s, updated_at = ? WHERE user_id = ? AND slot = ?" % assignments,
        values,
    )
