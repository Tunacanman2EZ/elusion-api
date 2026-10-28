"""minigames/data.py - the SQL (game 0.93.3; the kills 0.93.4). Imports nothing.
No commits: each route commits its own."""


def entries_of(db, user_id):
    """{game: entered_at} for one account."""
    rows = db.execute("SELECT game, entered_at FROM minigame_entries WHERE user_id = ?",
                      (int(user_id),)).fetchall()
    return {row["game"]: int(row["entered_at"]) for row in rows}


def enter(db, user_id, game, now):
    """Write the day's going in (replacing the last)."""
    db.execute("INSERT OR REPLACE INTO minigame_entries (user_id, game, entered_at) VALUES (?, ?, ?)",
               (int(user_id), str(game), int(now)))


# How long a minigame's kill is kept: the day it counts in, and a week for the
# owner to read.
KILLS_KEPT_SECONDS = 7 * 24 * 60 * 60


def kills_since(db, user_id, game, since, boss):
    """How many paid kills the account has made in that minigame since
    `since` - bosses (boss=True) or the rest."""
    return db.execute(
        "SELECT COUNT(*) FROM minigame_kills WHERE user_id = ? AND game = ? AND at >= ? AND boss = ?",
        (int(user_id), str(game), int(since), 1 if boss else 0),
    ).fetchone()[0]


def record_kill(db, user_id, slot, game, enemy_id, boss, now):
    """One paid kill - and every one older than KILLS_KEPT_SECONDS out."""
    db.execute(
        "INSERT INTO minigame_kills (user_id, slot, game, enemy_id, boss, at) VALUES (?, ?, ?, ?, ?, ?)",
        (int(user_id), int(slot), str(game), str(enemy_id), 1 if boss else 0, int(now)),
    )
    db.execute("DELETE FROM minigame_kills WHERE at < ?", (int(now) - KILLS_KEPT_SECONDS,))
