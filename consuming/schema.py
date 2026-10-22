"""consuming/schema.py - consuming/'s table: food_buffs, the meal a character
ate last while it lasts (0.74.0).

WHY HERE. A table lives with the one area that adds its rows (ARCHITECTURE.md),
and eating is the only thing that starts a buff: the consume route writes
every row. Others only read it - the character's load (saves/), the presence
ticket the combat books bound a fighter by (players/) and the aura's heal
(presence.py) - and a character deleted takes its row with it (saves/'s
drop()). init_db() calls create() with the other packages', after the core's
tables and before any migration.
"""

SCHEMA = """
        -- THE MEAL IN FORCE, one a character (0.74.0). The owner: meals that
        -- "heal more and give a short buff". One at a time and a new one
        -- replaces it, so the key is the character and a second meal is an
        -- INSERT OR REPLACE, not a second row. kind is the BuffKind's NAME
        -- ("MIGHT", "GUARD", "VIGOUR", "WISDOM"), never its integer -
        -- gamedata.buff_of() says why. ends_at is unix seconds; a row past it
        -- is no buff, and is left for the next meal to replace rather than
        -- swept: there is never more than one a character.
        CREATE TABLE IF NOT EXISTS food_buffs (
            user_id INTEGER NOT NULL,
            slot    INTEGER NOT NULL,
            item_id TEXT    NOT NULL,
            kind    TEXT    NOT NULL,
            amount  INTEGER NOT NULL,
            ends_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, slot),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
"""


def create(db):
    """The table above, IF NOT EXISTS, so nothing happens on a database that
    has it. An executescript, which commits first: init_db() calls this before
    its migrations begin."""
    db.executescript(SCHEMA)
