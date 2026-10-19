"""world/schema.py - world/'s tables:
world_ticks, one row per stir of the stirring lands.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- THE STIRRING LANDS (game 0.46.0): one row per stir, numbered from 1.
        -- element is Element.Type 1-7; chosen_by '' when the world picked it,
        -- the owner's name when they stirred it by hand.
        CREATE TABLE IF NOT EXISTS world_ticks (
            tick       INTEGER PRIMARY KEY,
            element    INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            ends_at    INTEGER NOT NULL,
            chosen_by  TEXT    NOT NULL DEFAULT ''
        );
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
