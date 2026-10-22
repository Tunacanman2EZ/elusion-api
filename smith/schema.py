"""smith/schema.py - smith/'s table: smith_ranks, the Royal Certifications a
character has earned (0.74.0).

WHY HERE. A table lives with the one area that adds its rows (ARCHITECTURE.md):
only POST /api/smith/certify writes one. A character deleted takes its rows
with it (saves/'s drop()). init_db() calls create() with the other packages',
after the core's tables and before any migration.
"""

SCHEMA = """
        -- THE ROYAL CERTIFICATIONS A CHARACTER HAS EARNED (0.74.0), one row a
        -- rank, so when each was earned is kept and "the rank" is the highest
        -- of them. Per character, because the character paid for it. Only the
        -- next rank can be earned (smith/routes.py), so a character's rows are
        -- always 1 to its rank, never a gap.
        CREATE TABLE IF NOT EXISTS smith_ranks (
            user_id   INTEGER NOT NULL,
            slot      INTEGER NOT NULL,
            rank      INTEGER NOT NULL,
            earned_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, slot, rank),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
"""


def create(db):
    """The table above, IF NOT EXISTS, so nothing happens on a database that
    has it. An executescript, which commits first: init_db() calls this before
    its migrations begin."""
    db.executescript(SCHEMA)
