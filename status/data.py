"""status/data.py - the SQL. saves holds the stats (app.py's init_db());
consume_grants is what the server authorised a pool to rise by - written here
and read here: note_refill() is handed back from register() to every area that
authorises one (a potion, a revive, a respawn, a level-up), and the presence
server's heals write their own rows in their own process. users.deaths counts
a death. No commits: the routes make them."""


def row_of(db, user_id, slot):
    """One character's whole row, or None."""
    return db.execute(
        "SELECT * FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def note_refill(db, user_id, slot, item_id, at, target="", amount=0):
    """
    One row of what the server authorised a pool to rise by: a potion drunk,
    a revive, a respawn, a level-up. The healing check (reconcile_heals())
    reads these rows to tell an earned rise from a claimed one.

    WHAT IT AUTHORISED, NOT MERELY THAT IT HAPPENED. `target` is the pool by
    name ('HP', 'MANA', 'STAMINA') and `amount` how much, for a potion; a
    refill that fills every pool - a revive, a respawn, a level - names no
    pool and no amount ('' and 0, the columns' own defaults), and its id is
    one of the refill grants the check knows. With only a timestamp to go on,
    the cheapest potion in the game explained a rise of any size in any pool.

    ONE TABLE AND ONE WRITER, because they all answer the same question - did
    the server authorise this rise - and a second place to write it would be
    a second place to forget to look, or to write it differently.
    """
    db.execute(
        "INSERT INTO consume_grants (user_id, slot, item_id, target, amount, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, slot, item_id, target, int(amount), at),
    )


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
