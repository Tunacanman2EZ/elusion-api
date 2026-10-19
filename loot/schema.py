"""loot/schema.py - loot/'s tables:
loot_bags and loot_bag_items, a bag the server rolled and what
is in it.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- A loot bag the server rolled and still owns. The client renders a
        -- copy; taking anything out of it is a request, checked against these
        -- rows. That is what takes `gold` off the list of things a client can
        -- simply declare.
        CREATE TABLE IF NOT EXISTS loot_bags (
            bag_id     TEXT    PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL,
            enemy_id   TEXT    NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_loot_bags_created ON loot_bags(created_at);

        -- Position-keyed like every other container here. "Take slot 2" is a
        -- request the server can answer exactly once; "take a healthpotion" is
        -- ambiguous when the bag holds two stacks, and a client sending it twice
        -- would be asking for a duplication bug.
        CREATE TABLE IF NOT EXISTS loot_bag_items (
            bag_id   TEXT    NOT NULL,
            position INTEGER NOT NULL,
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (bag_id, position),
            FOREIGN KEY (bag_id) REFERENCES loot_bags(bag_id) ON DELETE CASCADE
        );
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
