"""bank/data.py - the SQL. bank_items is this package's table (schema.py);
accounts and saves are app.py's (init_db()). Every write to bank_items is
here, but for the shared grid helpers' (a move, a discard, taking from
cells), which app.py hands in and which serve the backpack too; and the two
balances a gold transfer moves.
No commits: the routes make them."""


def bank_rows(db, user_id):
    return db.execute(
        "SELECT position, item_id, quantity FROM bank_items WHERE user_id = ? ORDER BY position",
        (user_id,),
    ).fetchall()


def carry_rows(db, user_id, slot):
    return db.execute(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchall()


def put_cells(db, user_id, writes):
    """Each (position, item_id, quantity) written into its bank cell."""
    for position, item_id, quantity in writes:
        db.execute(
            """
            INSERT INTO bank_items (user_id, position, item_id, quantity)
                 VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, position)
              DO UPDATE SET item_id = excluded.item_id, quantity = excluded.quantity
            """,
            (user_id, position, item_id, quantity),
        )


def replace_bank(db, user_id, parsed):
    """The whole bank, as the owner's tooling writes it."""
    db.execute("DELETE FROM bank_items WHERE user_id = ?", (user_id,))
    if parsed:
        db.executemany(
            "INSERT INTO bank_items (user_id, position, item_id, quantity) VALUES (?, ?, ?, ?)",
            [(user_id, position, item_id, quantity) for position, item_id, quantity in parsed],
        )


def purse(db, user_id, slot):
    """The character's carried gold, or None for no such character."""
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return None if row is None else int(row["gold"])


def set_balances(db, user_id, slot, carried, banked, now):
    """Both sides of a gold transfer. Two tables, so one transaction rather
    than one statement: sqlite3 opens a transaction on the first write and
    holds it until commit, so there is no moment where the gold exists in
    neither place."""
    db.execute(
        "UPDATE saves SET gold = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (carried, now, user_id, slot),
    )
    db.execute("UPDATE accounts SET bank_gold = ? WHERE user_id = ?", (banked, user_id))
