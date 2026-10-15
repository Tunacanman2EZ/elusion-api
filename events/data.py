"""events/data.py - the SQL of the world's events, and nothing else.

Two tables this package owns - their schemas are here and ensure_tables()
writes them, so app.py's init_db() does not need to know they exist:

    world_events    the schedule. A cancelled event is kept (cancelled_at) -
                    the schedule is a record.
    online_samples  how many were online, one row per five minutes in which
                    anybody polled (game 0.49.0, the event report). No row is
                    nobody: a game that is open polls every few seconds.

And reads of tables app.py owns, for the event report and the dry run, the
playerstats/ way - read only: kill_reports (what each kill paid, kept two
weeks), gold_ledger (every coin that entered the world, kept for good),
sessions and users.

Every function takes a sqlite3 connection with rows by name; the caller
commits. A database without a table answers with nothing.
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
CREATE TABLE IF NOT EXISTS online_samples (
    bucket   INTEGER PRIMARY KEY,
    at       INTEGER NOT NULL,
    everyone INTEGER NOT NULL DEFAULT 0,
    players  INTEGER NOT NULL DEFAULT 0
);
"""

# Gold a player EARNED by playing: coins out of loot bags, and what the shop
# paid for what they found. Not a staff grant, a rollback or a cashed pile
# (piles only reach a bag by a grant or a trade).
EARNED_REASONS = ("loot", "shop_sell")

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


# =============================================================================
# THE ONLINE SAMPLES (game 0.49.0)
# =============================================================================

def online_now(db, since, now):
    """Everyone with a live session seen since `since`: [(user_id, role,
    username)] - services.count_online() says who is a player."""
    try:
        return [(int(r["user_id"]), str(r["role"]), str(r["username"])) for r in db.execute(
            "SELECT DISTINCT s.user_id AS user_id, u.role AS role, u.username AS username"
            " FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.last_seen_at >= ? AND s.expires_at > ?", (int(since), int(now))).fetchall()]
    except sqlite3.OperationalError:
        return []


def write_sample(db, bucket, at, everyone, players):
    """One count into its five minutes' row, which keeps the most it is told:
    a player who was on for two minutes of the five was on."""
    try:
        db.execute("INSERT INTO online_samples (bucket, at, everyone, players) VALUES (?, ?, ?, ?)"
                   " ON CONFLICT(bucket) DO UPDATE SET everyone = MAX(everyone, excluded.everyone),"
                   " players = MAX(players, excluded.players)",
                   (int(bucket), int(at), int(everyone), int(players)))
    except sqlite3.OperationalError:
        return False
    return True


def prune_samples(db, before_bucket):
    try:
        db.execute("DELETE FROM online_samples WHERE bucket < ?", (int(before_bucket),))
    except sqlite3.OperationalError:
        pass


def samples(db, first_bucket, last_bucket):
    """{bucket: (everyone, players)} for the buckets in [first, last)."""
    try:
        return {int(r["bucket"]): (int(r["everyone"]), int(r["players"])) for r in db.execute(
            "SELECT bucket, everyone, players FROM online_samples WHERE bucket >= ? AND bucket < ?",
            (int(first_bucket), int(last_bucket))).fetchall()}
    except sqlite3.OperationalError:
        return {}


def first_sample_bucket(db):
    """The first bucket ever sampled, or None: before it, "nobody online" is
    "not counted yet"."""
    try:
        row = db.execute("SELECT MIN(bucket) FROM online_samples").fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0]) if row is not None and row[0] is not None else None


# =============================================================================
# WHAT PLAY PAID (the event report and the dry run)
# =============================================================================

def _left_out(names):
    names = [str(n).casefold() for n in names if n]
    return (" AND lower(u.username) NOT IN (%s)" % ",".join("?" * len(names)), names) if names else ("", [])


def kills_in(db, start, end, left_out_names=()):
    """(kills, xp, players who killed - players only) in [start, end)."""
    clause, names = _left_out(left_out_names)
    try:
        total = db.execute("SELECT COUNT(*), COALESCE(SUM(xp), 0) FROM kill_reports WHERE at >= ? AND at < ?",
                           (int(start), int(end))).fetchone()
        killers = db.execute(
            "SELECT COUNT(DISTINCT k.user_id) FROM kill_reports k JOIN users u ON u.id = k.user_id"
            " WHERE k.at >= ? AND k.at < ? AND u.role = 'player'" + clause, [int(start), int(end)] + names).fetchone()
    except sqlite3.OperationalError:
        return 0, 0, 0
    return int(total[0] or 0), int(total[1] or 0), int(killers[0] or 0)


def gold_in(db, start, end):
    """Gold earned by play in [start, end)."""
    try:
        row = db.execute(
            "SELECT COALESCE(SUM(delta), 0) FROM gold_ledger WHERE at >= ? AND at < ? AND delta > 0"
            " AND reason IN (%s)" % ",".join("?" * len(EARNED_REASONS)),
            [int(start), int(end)] + list(EARNED_REASONS)).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def new_players_in(db, start, end, left_out_names=()):
    clause, names = _left_out(left_out_names)
    try:
        row = db.execute("SELECT COUNT(*) FROM users u WHERE u.created_at >= ? AND u.created_at < ?"
                         " AND u.role = 'player'" + clause, [int(start), int(end)] + names).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def xp_by_player_hour(db, since):
    """[(user_id, hour, xp)]: what each account's kills paid in each hour it
    killed anything, since `since` - everyone's, staff too: it is a rate of
    play, not a count of players."""
    try:
        return [(int(r[0]), int(r[1]), int(r[2] or 0)) for r in db.execute(
            "SELECT user_id, at / 3600 AS hour, SUM(xp) FROM kill_reports WHERE at >= ?"
            " GROUP BY user_id, hour", (int(since),)).fetchall()]
    except sqlite3.OperationalError:
        return []


def gold_by_player_hour(db, since):
    """[(user_id, hour, gold earned)] since `since`."""
    try:
        return [(int(r[0]), int(r[1]), int(r[2] or 0)) for r in db.execute(
            "SELECT user_id, at / 3600 AS hour, SUM(delta) FROM gold_ledger WHERE at >= ? AND delta > 0"
            " AND user_id IS NOT NULL AND reason IN (%s) GROUP BY user_id, hour" % ",".join("?" * len(EARNED_REASONS)),
            [int(since)] + list(EARNED_REASONS)).fetchall()]
    except sqlite3.OperationalError:
        return []


def kills_by_enemy(db, since):
    """[(enemy_id, hour, kills)] since `since` - by the hour, so the hours an
    event was on can be left out of the usual."""
    try:
        return [(str(r[0]), int(r[1]), int(r[2])) for r in db.execute(
            "SELECT enemy_id, at / 3600 AS hour, COUNT(*) FROM kill_reports WHERE at >= ? GROUP BY enemy_id, hour",
            (int(since),)).fetchall()]
    except sqlite3.OperationalError:
        return []


def started_between(db, since, now):
    """Every event that began in [since, now], not called off before it
    began - newest first. The event report's list."""
    return _rows(db, "SELECT * FROM world_events WHERE starts_at >= ? AND starts_at <= ?"
                     " AND (cancelled_at = 0 OR cancelled_at > starts_at) ORDER BY starts_at DESC, id DESC",
                 (int(since), int(now)))


def overlapping(db, start, end):
    """Events not cancelled that are on at some moment of [start, end)."""
    return _rows(db, "SELECT * FROM world_events WHERE cancelled_at = 0 AND starts_at < ? AND ends_at > ?"
                     " ORDER BY starts_at, id", (int(end), int(start)))
