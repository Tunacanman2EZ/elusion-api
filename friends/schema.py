"""friends/schema.py - friends/'s tables:
friends, who knows who.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- WHO KNOWS WHO.
        --
        -- ONE ROW PER PAIR, and the key is directional on purpose: which way
        -- round it is stored is the record of who asked. A friendship is an
        -- accepted row read from either side, so the answer to "are we
        -- friends" is one lookup in each direction and never two rows that can
        -- disagree with each other.
        --
        -- WHY 'pending' IS A STATE AND NOT A SEPARATE TABLE. A request and a
        -- friendship are the same relationship at two moments in its life.
        -- Split across two tables, accepting means a delete and an insert that
        -- have to succeed together, and the pair can exist in both at once.
        --
        -- A DECLINE DELETES THE ROW rather than recording a refusal. Storing
        -- 'declined' would be a list of everyone who ever said no to you, kept
        -- on the server, readable by whoever eventually breaks in - and it
        -- would have to expire anyway, because a no today is not a no forever.
        -- Re-asking is instead policed by MAX_PENDING_SENT.
        CREATE TABLE IF NOT EXISTS friends (
            requester_id INTEGER NOT NULL,
            addressee_id INTEGER NOT NULL,
            state        TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (state IN ('pending', 'accepted')),
            created_at   INTEGER NOT NULL,
            decided_at   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (requester_id, addressee_id),
            FOREIGN KEY (requester_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (addressee_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- Both directions are queried on every /api/friends call, and the
        -- primary key only covers one of them.
        CREATE INDEX IF NOT EXISTS idx_friends_addressee
            ON friends(addressee_id, state);
        CREATE INDEX IF NOT EXISTS idx_friends_requester
            ON friends(requester_id, state);
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
