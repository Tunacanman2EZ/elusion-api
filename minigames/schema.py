"""minigames/schema.py - minigames/'s tables: minigame_entries, when each
account last went into each minigame (game 0.93.3), minigame_kills, every
paid kill in one (0.93.4), minigame_runs, each run of a minigame played
several times a day (0.99.0), and minigame_bests, each account's best time in
a timed one (0.120.0).

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
            pet       INTEGER NOT NULL DEFAULT 0,
            gold      INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_minigame_kills_user ON minigame_kills(user_id, game, at);

        -- EVERY RUN OF A MINIGAME PLAYED SEVERAL TIMES A DAY (0.99.0,
        -- minigames/): the Healer's Run's three a day are counted here, one
        -- row a Go, and each is finished - its gold paid - once.
        CREATE TABLE IF NOT EXISTS minigame_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            slot         INTEGER NOT NULL,
            game         TEXT    NOT NULL,
            started_at   INTEGER NOT NULL,
            finished_at  INTEGER NOT NULL DEFAULT 0,
            waves        INTEGER NOT NULL DEFAULT 0,
            boss         INTEGER NOT NULL DEFAULT 0,
            goal         INTEGER NOT NULL DEFAULT 0,
            gold         INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_minigame_runs_user ON minigame_runs(user_id, game, started_at);

        -- EACH ACCOUNT'S BEST TIME IN A TIMED MINIGAME (game 0.120.0,
        -- minigames/): the Forge's - how long a run held. One row per account
        -- and minigame, kept only when beaten; `at` is when.
        CREATE TABLE IF NOT EXISTS minigame_bests (
            user_id  INTEGER NOT NULL,
            game     TEXT    NOT NULL,
            seconds  INTEGER NOT NULL,
            at       INTEGER NOT NULL,
            PRIMARY KEY (user_id, game),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
"""


def create(db):
    """The table, IF NOT EXISTS, so nothing happens on a database that has
    it. An executescript, which commits first: init_db() calls this before
    its migrations begin."""
    db.executescript(SCHEMA)


def upgrade(db, add_column):
    """The columns added since these tables first shipped, for a database made
    before them. add_column(db, table, column, definition) is init_db()'s
    helper, handed in (a package never imports app.py). db.execute only -
    init_db() runs this inside its one migration transaction.

    minigame_kills.pet (game 0.121.0): 1 when the kill won its pet - the
    Forge lets one through a day (minigames/routes.py). Every kill before it
    won none as far as the rule is concerned, which is what the default says."""
    add_column(db, "minigame_kills", "pet", "INTEGER NOT NULL DEFAULT 0")
    # minigame_kills.gold (14 Nov, A DAY'S MINIGAME GOLD): what the coins a
    # kill rolled into the chest are worth, counted against the day's cap. A
    # kill from before counted nothing, which is what the default says.
    add_column(db, "minigame_kills", "gold", "INTEGER NOT NULL DEFAULT 0")
