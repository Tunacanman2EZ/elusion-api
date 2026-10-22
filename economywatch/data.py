"""economywatch/data.py - the SQL. Reads only: gold_ledger, lusion_ledger,
saves and accounts are app.py's (init_db()), and the flow writes nothing - it
is DERIVED, NEVER COUNTED, like the kingdom board.

THE WINDOW IS AN INDEX RANGE. Every row the flow reads is inside its window -
whole UTC days - and idx_gold_ledger_at / idx_lusion_ledger_at (on `at`)
bring SQLite to the window's first row and stop it at the last, so a read
costs the window's rows and never the ledger's history. The kingdom board's
lesson (kingdom/routes.py: a second pass over every negative row was 303ms of
its 427ms) is why this was measured before it shipped - the query's numbers
are with the index, in app.py. The whole route on that ledger (two million
rows over a year), with routes.py keeping each settled day: a week 48ms the
first time a worker is asked and 4ms after; ninety days 436ms once, 7ms after.
"""

# Two statements, not one with the table's name spliced in: a name in SQL
# text is a name nothing checks.
_GOLD_FLOW = (
    "SELECT at / 86400 AS day, reason,"
    "       SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END) AS minted,"
    "       -SUM(CASE WHEN delta < 0 THEN delta ELSE 0 END) AS burned,"
    "       COUNT(*) AS n"
    "  FROM gold_ledger WHERE at >= ? AND at < ? GROUP BY day, reason")
_LUSION_FLOW = (
    "SELECT at / 86400 AS day, reason,"
    "       SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END) AS minted,"
    "       -SUM(CASE WHEN delta < 0 THEN delta ELSE 0 END) AS burned,"
    "       COUNT(*) AS n"
    "  FROM lusion_ledger WHERE at >= ? AND at < ? GROUP BY day, reason")


def flow_rows(db, currency, since, until):
    """[(UTC day number, reason, minted, burned, rows)] for every ledger row
    of `currency` ("gold" or "lusions") at or after `since` and before
    `until`."""
    query = _GOLD_FLOW if currency == "gold" else _LUSION_FLOW
    return [(int(row[0]), str(row[1]), int(row[2] or 0), int(row[3] or 0), int(row[4] or 0))
            for row in db.execute(query, (int(since), int(until))).fetchall()]


def gold_in_world(db):
    """(carried, banked): every purse and every bank, now. One pass over the
    characters and one over the accounts - a few thousand rows, not the
    ledger."""
    carried = int(db.execute("SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0] or 0)
    banked = int(db.execute("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts").fetchone()[0] or 0)
    return carried, banked


def lusions_in_world(db):
    """Every lusion held, now: they are the account's, so one sum."""
    return int(db.execute("SELECT COALESCE(SUM(lusions), 0) FROM accounts").fetchone()[0] or 0)
