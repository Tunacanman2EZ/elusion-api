"""saves/schema.py - saves/'s tables:
character_deletions, a deleted character kept for a while.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

NOT `saves` ITSELF. Every area writes a character - the core's gold, the
bank, death, the kill, trade - so that table is the core's, in init_db().

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- A DELETED CHARACTER, KEPT FOR A WHILE. POST /api/character/delete
        -- removes the character's rows; this keeps a copy of what it was - the
        -- save row, its bag and its skills, as JSON - so "I did not delete
        -- that" or "I clicked the wrong one" can be answered, and the character
        -- put back by hand if it should be. Nothing reads it back
        -- automatically: a restore is a decision, not a route.
        --
        -- The newest CHARACTER_DELETIONS_KEPT per account, pruned on write, so
        -- creating and deleting in a loop cannot grow it without bound.
        CREATE TABLE IF NOT EXISTS character_deletions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL,
            class_id   TEXT    NOT NULL,
            name       TEXT    NOT NULL,
            level      INTEGER NOT NULL,
            gold       INTEGER NOT NULL DEFAULT 0,
            deleted_at INTEGER NOT NULL,
            snapshot   TEXT    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_character_deletions_user
            ON character_deletions(user_id, deleted_at);
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
