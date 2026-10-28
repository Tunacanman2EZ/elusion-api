"""minigames/schema.py - minigames/'s tables: minigame_entries, when each
account last went into each minigame (game 0.93.3), and minigame_kills, every
paid kill in one (0.93.4).

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

        -- EVERY PAID KILL IN A MINIGAME (0.93.4, minigames/): the Endless's
        -- elites and bosses, each paid into the character's chest. Counted
        -- since the account's Go against what the run could have sent
        -- (services.elites_ceiling()); kept a week for the owner to read.
        CREATE TABLE IF NOT EXISTS minigame_kills (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER NOT NULL,
            slot      INTEGER NOT NULL,
            game      TEXT    NOT NULL,
            enemy_id  TEXT    NOT NULL,
            boss      INTEGER NOT NULL DEFAULT 0,
            at        INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_minigame_kills_user ON minigame_kills(user_id, game, at);
"""


def create(db):
    """The table, IF NOT EXISTS, so nothing happens on a database that has
    it. An executescript, which commits first: init_db() calls this before
    its migrations begin."""
    db.executescript(SCHEMA)
