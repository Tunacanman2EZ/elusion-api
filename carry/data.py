"""carry/data.py - the SQL. carry_items is the carry; bank_items is read for
whether an account holds an item, and the two grids' shared cell writes name
their table and key columns, so the bank's go through here too. The tables are
app.py's (init_db()). No commits: the routes make them, and the grants share
their caller's transaction on purpose."""


def cells(db, user_id, slot):
    """Every filled carried cell, lowest position first."""
    return db.execute(
        "SELECT position, item_id, quantity FROM carry_items WHERE user_id = ? AND slot = ? ORDER BY position",
        (user_id, slot),
    ).fetchall()


def held(db, user_id, slot, item_id):
    """[(position, quantity)] of one item, highest position first."""
    rows = db.execute(
        "SELECT position, quantity FROM carry_items "
        "WHERE user_id = ? AND slot = ? AND item_id = ? ORDER BY position DESC",
        (user_id, slot, item_id),
    ).fetchall()
    return [(int(r["position"]), int(r["quantity"])) for r in rows]


def put(db, user_id, slot, writes):
    """Write a grant's cells: [(position, item_id, quantity)]."""
    for position, written_id, written_qty in writes:
        db.execute(
            """
            INSERT INTO carry_items (user_id, slot, position, item_id, quantity)
                 VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, slot, position)
              DO UPDATE SET item_id = excluded.item_id, quantity = excluded.quantity
            """,
            (user_id, slot, position, written_id, written_qty),
        )


def take(db, user_id, slot, changes):
    """Write a take's cells: [(position, what is left there)]."""
    for position, left in changes:
        if left <= 0:
            # The row is deleted rather than set to 0: carry_items carries a
            # CHECK (quantity > 0) and an empty cell is an absent row.
            db.execute(
                "DELETE FROM carry_items WHERE user_id = ? AND slot = ? AND position = ?",
                (user_id, slot, position),
            )
        else:
            db.execute(
                "UPDATE carry_items SET quantity = ? "
                "WHERE user_id = ? AND slot = ? AND position = ?",
                (left, user_id, slot, position),
            )


def owns(db, user_id, slot, item_id):
    """True when this account holds item_id in this character's carry or in
    the account's bank."""
    if db.execute(
        "SELECT 1 FROM carry_items WHERE user_id = ? AND slot = ? AND item_id = ? LIMIT 1",
        (user_id, slot, item_id),
    ).fetchone():
        return True
    return db.execute(
        "SELECT 1 FROM bank_items WHERE user_id = ? AND item_id = ? LIMIT 1",
        (user_id, item_id),
    ).fetchone() is not None


def purse(db, user_id, slot):
    """The carried gold, or None when there is no such character."""
    row = db.execute("SELECT gold FROM saves WHERE user_id = ? AND slot = ?",
                     (user_id, slot)).fetchone()
    return None if row is None else int(row["gold"] or 0)


def replace(db, user_id, slot, covered, parsed):
    """The owner's whole-carry write: every cell below `covered` replaced by
    `parsed` ([(user_id, slot, position, item_id, quantity)]).

    DELETE then INSERT, in one transaction. A replace has no upsert form: cells
    the client cleared have to disappear, and an upsert would leave them.
    sqlite3 opens a transaction implicitly on the first write and holds it
    until commit, so the table is never observably empty."""
    db.execute("DELETE FROM carry_items WHERE user_id = ? AND slot = ? AND position < ?",
               (user_id, slot, covered))
    if parsed:
        db.executemany(
            "INSERT INTO carry_items (user_id, slot, position, item_id, quantity) VALUES (?, ?, ?, ?, ?)",
            parsed,
        )


# ---- either grid: named by table and key columns ----------------------------

def apply_cell_changes(db, table, key_columns, key_values, changes):
    """Write back what take_from_cells() worked out. Zero means delete the row."""
    where = " AND ".join("%s = ?" % column for column in key_columns)
    for position, new_quantity in changes:
        if new_quantity <= 0:
            db.execute(
                "DELETE FROM %s WHERE %s AND position = ?" % (table, where),
                (*key_values, position),
            )
        else:
            db.execute(
                "UPDATE %s SET quantity = ? WHERE %s AND position = ?" % (table, where),
                (new_quantity, *key_values, position),
            )


def grid_cells(db, table, key_columns, key_values, positions):
    """{position: (item_id, quantity)} for the named cells that hold anything."""
    where = " AND ".join("%s = ?" % column for column in key_columns)
    marks = ", ".join("?" for _ in positions)
    rows = db.execute(
        "SELECT position, item_id, quantity FROM %s WHERE %s AND position IN (%s)"
        % (table, where, marks),
        (*key_values, *positions),
    ).fetchall()
    return {int(r["position"]): (r["item_id"], int(r["quantity"])) for r in rows}


def grid_rewrite(db, table, key_columns, key_values, positions, after):
    """Replace the named cells with `after` ({position: (item_id, quantity)})."""
    where = " AND ".join("%s = ?" % column for column in key_columns)
    marks = ", ".join("?" for _ in positions)
    db.execute("DELETE FROM %s WHERE %s AND position IN (%s)" % (table, where, marks),
               (*key_values, *positions))
    columns = ", ".join(key_columns)
    holes = ", ".join("?" for _ in key_columns)
    for position, (item_id, quantity) in sorted(after.items()):
        db.execute(
            "INSERT INTO %s (%s, position, item_id, quantity) VALUES (%s, ?, ?, ?)"
            % (table, columns, holes),
            (*key_values, position, item_id, int(quantity)),
        )
