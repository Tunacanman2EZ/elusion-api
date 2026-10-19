"""kill/schema.py - kill/'s tables:
kill_reports, every kill a client claims (kept two weeks),
kill_tally, the kill record (kept for good), and
kill_receipts, which of the books' deaths have paid a kill (0.57.0, kept two
weeks), with their indexes.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

NOT ITS SEED. _migrate_seed_kill_tally() in app.py fills kill_tally once
from kill_reports and stamps server_settings, the core's - so it stays
there, and runs after create() below.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- EVERY KILL A CLIENT CLAIMS.
        --
        -- E-3 says the kill EVENT is asserted rather than verified: the server
        -- rolls the rewards itself, refuses reward-less enemies and rate-limits
        -- with a token bucket, but never confirms a fight happened. The note
        -- that matters is "the rate limit caps the SPEED of the fraud, not its
        -- existence" - and until this table, it did not even cap the
        -- INVISIBILITY of it. Nothing recorded a kill. A client reporting one
        -- boss an hour, forever, looked exactly like a player who enjoys the
        -- boss.
        --
        -- THIS DOES NOT CLOSE E-3 AND IS NOT PRETENDING TO. Verification needs
        -- the server to know what spawned, which needs the client to register
        -- spawns - a change on both sides of the wire. What this does is make
        -- the thing measurable, which is the step before any check worth
        -- enforcing: you cannot detect an anomaly you never wrote down, and a
        -- threshold picked without data is how honest players get clamped.
        --
        -- REWARDS ARE STORED AS PAID, not recomputed later. The enemy's profile
        -- can be retuned tomorrow; what this account was actually given is a
        -- fact about that moment and has to survive the retune, exactly like
        -- staff_actions storing names alongside ids.
        CREATE TABLE IF NOT EXISTS kill_reports (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL,
            enemy_id   TEXT    NOT NULL,
            xp         INTEGER NOT NULL DEFAULT 0,
            attack_xp  INTEGER NOT NULL DEFAULT 0,
            level_at   INTEGER NOT NULL DEFAULT 0,
            at         INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        -- THE KILL RECORD (7 Oct, the owner: "a button in game that records
        -- all players kills with icons of the enemies"). One row per character
        -- per kind of monster, counted at every paid kill in combat_kill's own
        -- transaction and KEPT FOR GOOD - kill_reports above is pruned after
        -- two weeks, so it can say how fast, never how many. A TALLY, so it has
        -- a birthday: kill_tally_since in server_settings, written when it was
        -- first filled from what kill_reports still held. See THE KILL RECORD.
        CREATE TABLE IF NOT EXISTS kill_tally (
            user_id    INTEGER NOT NULL,
            slot       INTEGER NOT NULL,
            enemy_id   TEXT    NOT NULL,
            kills      INTEGER NOT NULL DEFAULT 0,
            first_at   INTEGER NOT NULL,
            last_at    INTEGER NOT NULL,
            PRIMARY KEY (user_id, slot, enemy_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        -- "everyone's kills of each monster" reads the whole table grouped by
        -- enemy; this is that grouping's index.
        CREATE INDEX IF NOT EXISTS idx_kill_tally_enemy ON kill_tally(enemy_id, user_id);

        -- "what has this account been killing, and how fast" - one query.
        CREATE INDEX IF NOT EXISTS idx_kill_reports_user ON kill_reports(user_id, at);
        -- "who is reporting this enemy most" - the other one.
        CREATE INDEX IF NOT EXISTS idx_kill_reports_enemy ON kill_reports(enemy_id, at);

        -- THE RECEIPTS A KILL WAS PAID WITH (0.57.0; E3_SCOPE.md option C,
        -- step 2). With the owner's receipts switch on, a kill is paid only
        -- against an AGREED row in combat_kills - the books' own count of that
        -- monster reaching zero, for this account - and each row pays ONE kill.
        -- This is the "used" mark: its own table, because combat_kills is
        -- presence.py's alone to write. The primary key is what makes a second
        -- claim on the same death fail, in either worker. Pruned with
        -- kill_reports, two weeks.
        CREATE TABLE IF NOT EXISTS kill_receipts (
            combat_kill_id INTEGER PRIMARY KEY,
            user_id        INTEGER NOT NULL,
            slot           INTEGER NOT NULL,
            at             INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_kill_receipts_at ON kill_receipts(at);
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
