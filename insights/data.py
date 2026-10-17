"""insights/data.py - the SQL of the land insights, and nothing else.

READS ONLY: kill_reports (every paid kill, kept two weeks - with the level the
character was at), world_ticks (every stir), world_events and online_samples
(the events/ package's), users (who is a player). No table of its own. Every
function takes a sqlite3 connection with rows by name; a database without a
table answers with nothing.
"""
import sqlite3


def _all(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []


def _left_out(names):
    names = [str(n).casefold() for n in names if n]
    return (" AND lower(u.username) NOT IN (%s)" % ",".join("?" * len(names)), names) if names else ("", [])


def kills_by_enemy(db, since):
    """[(enemy_id, kills)] since `since` - everyone's: a land is hunted
    whoever hunts it."""
    return [(str(r[0]), int(r[1])) for r in _all(
        db, "SELECT enemy_id, COUNT(*) FROM kill_reports WHERE at >= ? GROUP BY enemy_id", (int(since),))]


def players_by_enemy(db, since, left_out_names=()):
    """[(enemy_id, user_id)] - each player who killed each monster since
    `since`. Players only: the player rank, the owner and co-owners left out."""
    clause, names = _left_out(left_out_names)
    return [(str(r[0]), int(r[1])) for r in _all(
        db, "SELECT DISTINCT k.enemy_id, k.user_id FROM kill_reports k JOIN users u ON u.id = k.user_id"
            " WHERE k.at >= ? AND u.role = 'player'" + clause, [int(since)] + names)]


def newcomers_by_enemy(db, since, below_level, left_out_names=()):
    """[(enemy_id, user_id)] - each player under `below_level` (at the kill)
    who killed each monster since `since`: who is learning where."""
    clause, names = _left_out(left_out_names)
    return [(str(r[0]), int(r[1])) for r in _all(
        db, "SELECT DISTINCT k.enemy_id, k.user_id FROM kill_reports k JOIN users u ON u.id = k.user_id"
            " WHERE k.at >= ? AND k.level_at < ? AND u.role = 'player'" + clause,
        [int(since), int(below_level)] + names)]


def kills_by_enemy_hour(db, since):
    """[(enemy_id, hour of the day in UTC, kills)] since `since`."""
    return [(str(r[0]), int(r[1]), int(r[2])) for r in _all(
        db, "SELECT enemy_id, (at / 3600) % 24 AS hour, COUNT(*) FROM kill_reports WHERE at >= ?"
            " GROUP BY enemy_id, hour", (int(since),))]


def last_stirred(db):
    """{element: when it last began to stir}."""
    return {int(r[0]): int(r[1]) for r in _all(db, "SELECT element, MAX(started_at) FROM world_ticks GROUP BY element")}


def events_begun(db, since, now):
    """[(kind, land, starts_at)] - every event that began in [since, now],
    not called off before it began."""
    return [(str(r[0]), int(r[1]), int(r[2])) for r in _all(
        db, "SELECT kind, land, starts_at FROM world_events WHERE starts_at >= ? AND starts_at <= ?"
            " AND (cancelled_at = 0 OR cancelled_at > starts_at)", (int(since), int(now)))]


def online_samples(db, first_bucket, last_bucket):
    """{bucket: players} in [first, last), and the first bucket ever counted
    (None if none)."""
    rows = {int(r[0]): int(r[1]) for r in _all(
        db, "SELECT bucket, players FROM online_samples WHERE bucket >= ? AND bucket < ?",
        (int(first_bucket), int(last_bucket)))}
    first = _all(db, "SELECT MIN(bucket) FROM online_samples")
    return rows, (int(first[0][0]) if first and first[0][0] is not None else None)
