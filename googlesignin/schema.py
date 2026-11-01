"""googlesignin/schema.py - its two tables (game 0.101.0).

WHY HERE. A table lives with the one area that adds its rows (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration.
"""

SCHEMA = """
        -- A GOOGLE ACCOUNT LINKED TO A GAME ACCOUNT (0.101.0, googlesignin/).
        -- One each way: sub is Google's own id for the person, UNIQUE, so one
        -- Google account can never make or open a second game account. email
        -- is kept for support; made_with_google is 1 for an account Google
        -- made (no password anybody knows), which therefore stays linked.
        CREATE TABLE IF NOT EXISTS google_accounts (
            user_id           INTEGER PRIMARY KEY,
            sub               TEXT    NOT NULL UNIQUE,
            email             TEXT    NOT NULL DEFAULT '',
            linked_at         INTEGER NOT NULL,
            made_with_google  INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- A SIGN-IN UNDER WAY (0.101.0). code_hash is the code's SHA-256 (the
        -- code itself is never kept); install_hash the computer that asked,
        -- the only one that may collect it; purpose 'signin' or 'link' (then
        -- user_id is the account linking). state: waiting, done, refused,
        -- used. sub, email and said are filled in when the page finishes it.
        CREATE TABLE IF NOT EXISTS google_codes (
            code_hash     TEXT    PRIMARY KEY,
            install_hash  TEXT    NOT NULL,
            ip            TEXT    NOT NULL DEFAULT '',
            purpose       TEXT    NOT NULL,
            user_id       INTEGER,
            state         TEXT    NOT NULL DEFAULT 'waiting',
            sub           TEXT    NOT NULL DEFAULT '',
            email         TEXT    NOT NULL DEFAULT '',
            said          TEXT    NOT NULL DEFAULT '',
            created_at    INTEGER NOT NULL,
            expires_at    INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_google_codes_ip ON google_codes(ip, created_at);
        CREATE INDEX IF NOT EXISTS idx_google_codes_expires ON google_codes(expires_at);
"""


def create(db):
    db.executescript(SCHEMA)
