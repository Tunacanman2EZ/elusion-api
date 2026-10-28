"""minigames/schema.py - minigames/'s one table, minigame_entries: when each
account last went into each minigame (game 0.93.3).

WHY HERE. A table lives with the one area that adds its rows (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration.
"""

SCHEMA = """
        -- WHEN EACH ACCOUNT LAST WENT IN (0.93.3, minigames/). One row per
        -- account and minigame, rewritten by each Go that is let through: the
        -- wait runs from entered_at. One row, not a log - the question is
        -- only "when may they go in again".
        CREATE TABLE IF NOT EXISTS minigame_entries (
            user_id     INTEGER NOT NULL,
            game        TEXT    NOT NULL,
            entered_at  INTEGER NOT NULL,
            PRIMARY KEY (user_id, game),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
"""


def create(db):
    """The table, IF NOT EXISTS, so nothing happens on a database that has
    it. An executescript, which commits first: init_db() calls this before
    its migrations begin."""
    db.executescript(SCHEMA)
