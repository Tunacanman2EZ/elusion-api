"""players/schema.py - players/'s tables:
presence_tickets, the tickets to the presence server.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- A TICKET TO THE PRESENCE SERVER (presence.py), which shows players to
        -- each other over a WebSocket. The game asks for one with its login
        -- (POST /api/presence/ticket) and hands it to the socket, so the socket
        -- never sees a login and every rule about who may sign in stays here.
        -- token_hash, not the ticket. `identity` is who the player is on other
        -- people's screens - name, rank, colour, guild, class, level and the
        -- pets they hold - written by this server from its own rows, as JSON.
        -- session_hash is the login that asked: presence.py drops a connection
        -- whose login is gone (logout, ban, kick, a login elsewhere) by joining
        -- on sessions, so none of those routes has to know the socket exists.
        CREATE TABLE IF NOT EXISTS presence_tickets (
            token_hash   TEXT    PRIMARY KEY,
            session_hash TEXT    NOT NULL,
            user_id      INTEGER NOT NULL,
            slot         INTEGER NOT NULL,
            identity     TEXT    NOT NULL,
            issued_at    INTEGER NOT NULL,
            expires_at   INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_presence_tickets_expiry
            ON presence_tickets(expires_at);
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
