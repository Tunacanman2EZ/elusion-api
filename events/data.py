"""events/data.py - the SQL of the world's events, and nothing else.

One table, world_events, which this package owns: its schema is here and
ensure_tables() writes it, so app.py's init_db() does not need to know it
exists. A cancelled event is kept (cancelled_at) - the schedule is a record.

Every function takes a sqlite3 connection with rows by name; the caller
commits. A database without the table answers with nothing.
"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS world_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    amount       REAL    NOT NULL DEFAULT 1.0,
    land         INTEGER NOT NULL DEFAULT 0,
    starts_at    INTEGER NOT NULL,
    ends_at      INTEGER NOT NULL,
    created_by   TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    cancelled_at INTEGER NOT NULL DEFAULT 0,
    cancelled_by TEXT    NOT NULL DEFAULT '',
    announced    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_world_events_window ON world_events(ends_at, starts_at);
"""

COLUMNS = ("id", "name", "kind", "amount", "land", "starts_at", "ends_at", "created_by",
           "created_at", "cancelled_at", "cancelled_by", "announced")


def ensure_tables(db):
    db.executescript(SCHEMA)


def _rows(db, sql, args=()):
    try:
        return [dict(zip(COLUMNS, (r[c] for c in COLUMNS))) for r in db.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []


def live(db, now, ahead):
    """Events not cancelled that have not ended and begin within `ahead`
    seconds of `now` - what is on, and what is coming."""
    return _rows(db, "SELECT * FROM world_events WHERE cancelled_at = 0 AND ends_at > ? AND starts_at <= ?"
                     " ORDER BY starts_at, id", (int(now), int(now) + int(ahead)))


def schedule(db, since):
    """Every event that ends after `since`, cancelled ones too, newest start
    first - the owner's list."""
    return _rows(db, "SELECT * FROM world_events WHERE ends_at >= ? ORDER BY starts_at DESC, id DESC",
                 (int(since),))


def get(db, event_id):
    found = _rows(db, "SELECT * FROM world_events WHERE id = ?", (int(event_id),))
    return found[0] if found else None


def count_upcoming(db, now):
    try:
        return int(db.execute("SELECT COUNT(*) FROM world_events WHERE cancelled_at = 0 AND ends_at > ?",
                              (int(now),)).fetchone()[0])
    except sqlite3.OperationalError:
        return 0


def insert(db, event, by, now):
    """Write one event (already checked by services.validate()); its id."""
    cursor = db.execute(
        "INSERT INTO world_events (name, kind, amount, land, starts_at, ends_at, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event["name"], event["kind"], float(event["amount"]), int(event["land"]), int(event["starts_at"]),
         int(event["ends_at"]), str(by or "")[:64], int(now)))
    return int(cursor.lastrowid)


def cancel(db, event_id, by, now):
    """Call one off. True if this call did it - it was not already called off
    and had not ended."""
    cursor = db.execute(
        "UPDATE world_events SET cancelled_at = ?, cancelled_by = ? WHERE id = ? AND cancelled_at = 0 AND ends_at > ?",
        (int(now), str(by or "")[:64], int(event_id), int(now)))
    return cursor.rowcount == 1


def mark_announced(db, event_id):
    """True for exactly one caller: the one that gets to say it has begun."""
    try:
        cursor = db.execute("UPDATE world_events SET announced = 1 WHERE id = ? AND announced = 0", (int(event_id),))
    except sqlite3.OperationalError:
        return False
    return cursor.rowcount == 1
