"""world/data.py - the SQL. world_ticks is this package's (schema.py): one row
per stir, numbered from 1, and every write to it is here. kill_reports (every
paid kill) is read for how hunted each land has been; it is the kill route's
to write. Other readers of world_ticks: the land insights (insights/), and the
combat books (presence.py's stirred_lands()). No commits: the callers make
them."""


def last_tick(db):
    return db.execute("SELECT * FROM world_ticks ORDER BY tick DESC LIMIT 1").fetchone()


def tick_row(db, tick):
    return db.execute("SELECT * FROM world_ticks WHERE tick = ?", (tick,)).fetchone()


def recent(db, limit):
    return db.execute("SELECT * FROM world_ticks ORDER BY tick DESC LIMIT ?", (limit,)).fetchall()


def highest_tick(db):
    """The last tick number written, or None before the first."""
    return db.execute("SELECT MAX(tick) AS t FROM world_ticks").fetchone()["t"]


def end_at(db, tick, now):
    """Stir `tick` ends at `now` - one a stir event, a called-off event or
    the owner's own stir replaces."""
    db.execute("UPDATE world_ticks SET ends_at = ? WHERE tick = ?", (now, int(tick)))


def begin(db, tick, element, started_at, ends_at, chosen_by):
    """Write stir `tick`. TWO ASKING AT ONCE both try to write the same tick
    number; the PRIMARY KEY lets exactly one in. True for that one."""
    cursor = db.execute(
        "INSERT OR IGNORE INTO world_ticks (tick, element, started_at, ends_at, chosen_by)"
        " VALUES (?, ?, ?, ?, ?)",
        (int(tick), int(element), int(started_at), int(ends_at), chosen_by),
    )
    return cursor.rowcount == 1


def stirred_lately(db, element, now, grace):
    """Whether `element` stirs now or stopped within `grace` seconds (THE
    SWELL's kill ceiling)."""
    return db.execute(
        "SELECT 1 FROM world_ticks WHERE element = ? AND started_at <= ? AND ends_at >= ? LIMIT 1",
        (int(element), int(now) + 60, int(now) - int(grace)),
    ).fetchone() is not None


def kills_since(db, since):
    """[(enemy_id, paid kills)] since `since`."""
    return db.execute(
        "SELECT enemy_id, COUNT(*) AS n FROM kill_reports WHERE at >= ? GROUP BY enemy_id",
        (since,),
    ).fetchall()
