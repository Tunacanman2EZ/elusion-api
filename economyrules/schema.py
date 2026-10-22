"""economyrules/schema.py - economyrules/'s tables: the owner's economy
numbers (economy_rules) and the log of every change to them
(economy_rule_changes). New in 0.74.0.

WHY HERE. A table lives with the one area that adds its rows (ARCHITECTURE.md),
and only this package's PUT writes either. init_db() calls create() with the
other packages', after the core's tables and before any migration.
"""

SCHEMA = """
        -- THE OWNER'S ECONOMY NUMBERS (0.74.0). The owner: "Today, changing the
        -- trade tax or a price means a new version. If those numbers lived on
        -- the server, you could adjust them in a minute." One row per number
        -- the owner has SET; a rule with no row is its default, which is the
        -- number the game shipped with (economyrules/services.py, RULES) - so
        -- an empty table plays exactly as the server did before it existed.
        --
        -- key is a rule's name and is never renamed. value is checked against
        -- the rule's bounds when it is written AND when it is read, so a row
        -- written by hand outside them reads as the default rather than as a
        -- shop that pays more than it charges. updated_by is the owner's name
        -- as it was, kept like owner_gifts.actor_name: the row still says who
        -- after the account is gone.
        CREATE TABLE IF NOT EXISTS economy_rules (
            key         TEXT    PRIMARY KEY,
            value       REAL    NOT NULL,
            updated_at  INTEGER NOT NULL,
            updated_by  TEXT    NOT NULL DEFAULT ''
        );

        -- EVERY CHANGE TO THEM, one row each, never edited and never pruned:
        -- what the number was, what it became, who and when. NULL on either
        -- side is "the default" - set from the game's own number, or put back
        -- to it - which a value alone cannot say. The owner's panel shows the
        -- newest; the staff log has a line for each as well.
        CREATE TABLE IF NOT EXISTS economy_rule_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          INTEGER NOT NULL,
            key         TEXT    NOT NULL,
            old_value   REAL,
            new_value   REAL,
            by_id       INTEGER,
            by_name     TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (by_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_economy_rule_changes_at
            ON economy_rule_changes(at);
"""


def create(db):
    """Make the tables if they are not there. An executescript, which commits
    - so init_db() calls it with the other packages' creates, never among the
    migrations."""
    db.executescript(SCHEMA)
