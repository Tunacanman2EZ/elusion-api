"""shop/data.py - the SQL: what a character carries in gold, read before a
purchase. Everything a sale or a purchase writes goes through what register()
is handed - the backpack grant and the grid's cells (carry/), the gold ledger
and the trade hold (app.py) - so no table is written here."""


def purse(db, user_id, slot):
    """The carried gold, or 0 for no such character."""
    row = db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()
    return int(row["gold"]) if row is not None else 0
