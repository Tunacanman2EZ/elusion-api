"""clienterrors/schema.py - clienterrors/'s one table, client_errors: the
script errors players' games reported (game 0.88.0).

WHY HERE. A table lives with the one area that adds its rows (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration.
"""

SCHEMA = """
        -- WHAT BROKE IN THE PLAYERS' GAMES (0.88.0, clienterrors/). One row per
        -- account, day, game version and error - where it was (a script and a
        -- line) and what it said - with how many times it happened that day,
        -- in which area last, and when first and last. A game sends each error
        -- once a minute with its count, so a loop that errors every frame is
        -- one row and a number, not a million rows.
        --
        -- day is the unix day (seconds // 86400), so "today" is one integer
        -- and the week is a range. Kept DAYS_KEPT days, pruned on write.
        -- user_id is kept only to count PLAYERS ("3 players hit this"), never
        -- shown: the read route answers no name.
        CREATE TABLE IF NOT EXISTS client_errors (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            day         INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            version     TEXT    NOT NULL DEFAULT '',
            build       INTEGER NOT NULL DEFAULT 0,
            where_at    TEXT    NOT NULL,
            what        TEXT    NOT NULL,
            area        TEXT    NOT NULL DEFAULT '',
            times       INTEGER NOT NULL DEFAULT 1,
            first_at    INTEGER NOT NULL,
            last_at     INTEGER NOT NULL,
            UNIQUE (day, user_id, version, where_at, what),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- The read is "since a moment, every account": a range on last_at.
        CREATE INDEX IF NOT EXISTS idx_client_errors_last
            ON client_errors(last_at);
        -- The write's cap counts one account's rows for one day.
        CREATE INDEX IF NOT EXISTS idx_client_errors_user_day
            ON client_errors(user_id, day);
"""


def create(db):
    """The table and its indexes, each IF NOT EXISTS. An executescript, which
    commits first: init_db() calls this before its migrations begin."""
    db.executescript(SCHEMA)
