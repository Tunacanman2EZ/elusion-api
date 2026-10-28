"""minigames/data.py - the SQL (game 0.93.3). Imports nothing."""


def entries_of(db, user_id):
    """{game: entered_at} for one account."""
    rows = db.execute("SELECT game, entered_at FROM minigame_entries WHERE user_id = ?",
                      (int(user_id),)).fetchall()
    return {row["game"]: int(row["entered_at"]) for row in rows}


def enter(db, user_id, game, now):
    """Write the day's going in (replacing the last)."""
    db.execute("INSERT OR REPLACE INTO minigame_entries (user_id, game, entered_at) VALUES (?, ?, ?)",
               (int(user_id), str(game), int(now)))
