"""parties/schema.py - parties/'s tables:
parties, party_members and party_invites.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- PARTIES (game 0.42.0). A few players together for a while: a Party
        -- window, a party chat and each other on the map. Not a guild - no
        -- name, no ranks, no cost, gone when it is down to one.
        --
        -- ONE PARTY PER PLAYER, enforced by party_members' primary key, as
        -- guild_members' is. is_leader marks the one who may invite, remove
        -- and hand it on; party_fix() keeps exactly one while two remain.
        CREATE TABLE IF NOT EXISTS parties (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS party_members (
            user_id   INTEGER PRIMARY KEY,
            party_id  INTEGER NOT NULL,
            is_leader INTEGER NOT NULL DEFAULT 0,
            joined_at INTEGER NOT NULL,
            FOREIGN KEY (user_id)  REFERENCES users(id)   ON DELETE CASCADE,
            FOREIGN KEY (party_id) REFERENCES parties(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_party_members_party
            ON party_members(party_id, joined_at);

        -- AN INVITATION FROM A PERSON, not from a party: somebody with no
        -- party yet asks somebody else, and the party is made when it is
        -- accepted. Waits PARTY_INVITE_SECONDS for an answer.
        CREATE TABLE IF NOT EXISTS party_invites (
            user_id    INTEGER NOT NULL,
            from_id    INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, from_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (from_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_party_invites_from
            ON party_invites(from_id);
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
