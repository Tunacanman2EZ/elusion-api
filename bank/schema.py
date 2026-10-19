"""bank/schema.py - bank/'s tables:
bank_items, the account's bank grid.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

NOT ITS MIGRATION. _migrate_bank_to_account() in app.py rebuilds an old
per-character bank_items, and pools saves.bank_gold into accounts in the
same pass - three tables, two of them the core's - so it stays there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- THE BANK IS ACCOUNT-SHARED, NOT PER-CHARACTER. characterdata.gd puts
        -- bank_gold and bank_inventory in account_data, commented "account-
        -- shared, safe from death" - that is the whole point of the feature.
        -- This table used to carry a `slot` column, which would have made
        -- anything your warrior banked invisible to your mage.
        --
        -- Positional for the same reason carry_items is: the client holds it as
        -- an array of cells with nulls for the empty ones, and the player
        -- expects things to stay where they put them.
        CREATE TABLE IF NOT EXISTS bank_items (
            user_id  INTEGER NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (user_id, position),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
