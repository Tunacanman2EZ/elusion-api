"""moderation/schema.py - moderation/'s tables:
chat_reports, a line a player told staff about.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- A LINE A PLAYER TOLD STAFF ABOUT. The line is COPIED - who said it,
        -- where, what it said - because the report has to outlive the line: a
        -- mod deletes it, the ring buffer trims it, and the evidence would go
        -- with it. One per reporter per line; a second press is not a second
        -- report. Resolving closes every report on the same line together.
        CREATE TABLE IF NOT EXISTS chat_reports (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id    INTEGER NOT NULL,
            reporter_id   INTEGER NOT NULL,
            reporter_name TEXT    NOT NULL,
            reported_id   INTEGER NOT NULL,
            reported_name TEXT    NOT NULL,
            channel       TEXT    NOT NULL,
            body          TEXT    NOT NULL DEFAULT '',
            image_id      TEXT    NOT NULL DEFAULT '',
            said_at       INTEGER NOT NULL DEFAULT 0,
            reason        TEXT    NOT NULL,
            created_at    INTEGER NOT NULL,
            resolved_at   INTEGER NOT NULL DEFAULT 0,
            resolved_by   TEXT    NOT NULL DEFAULT '',
            outcome       TEXT    NOT NULL DEFAULT '',
            UNIQUE (message_id, reporter_id)
        );

        CREATE INDEX IF NOT EXISTS idx_chat_reports_open
            ON chat_reports(resolved_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_chat_reports_by
            ON chat_reports(reporter_id, created_at);
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
