"""skills/data.py - the SQL. skills is the table (app.py's init_db());
saves.last_train_at is when a character last reported training. No commits:
the routes make them, and grant_xp() shares its caller's transaction on
purpose."""


def all_of(db, user_id, slot):
    """Every skill row this character has, by name."""
    return db.execute(
        "SELECT skill_id, level, xp FROM skills WHERE user_id = ? AND slot = ? ORDER BY skill_id",
        (user_id, slot),
    ).fetchall()


def skill(db, user_id, slot, skill_id):
    """One skill's level and xp, or None when it was never trained."""
    return db.execute(
        "SELECT level, xp FROM skills WHERE user_id = ? AND slot = ? AND skill_id = ?",
        (user_id, slot, skill_id),
    ).fetchone()


def put(db, user_id, slot, skill_id, level, xp):
    """One skill's new level and xp - an upsert, leaving every other skill
    exactly where it was."""
    db.execute(
        """
        INSERT INTO skills (user_id, slot, skill_id, level, xp)
             VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, slot, skill_id)
          DO UPDATE SET level = excluded.level, xp = excluded.xp
        """,
        (user_id, slot, skill_id, level, xp),
    )


def replace_unowned(db, user_id, slot, server_owned, parsed):
    """A skills write: every skill the server does not own replaced by
    `parsed` ([(user_id, slot, skill_id, level, xp)]). The server's own are
    never touched."""
    db.execute(
        "DELETE FROM skills WHERE user_id = ? AND slot = ? AND skill_id NOT IN (%s)"
        % ",".join("?" * len(server_owned)),
        (user_id, slot, *sorted(server_owned)),
    )
    if parsed:
        db.executemany(
            "INSERT INTO skills (user_id, slot, skill_id, level, xp) VALUES (?, ?, ?, ?, ?)",
            parsed,
        )


def trainee(db, user_id, slot):
    """class_id (for the specialty) and last_train_at, or None for no such
    character."""
    return db.execute(
        "SELECT class_id, last_train_at FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def trained(db, user_id, slot, now_ms, now):
    """Stamp the report time."""
    db.execute(
        "UPDATE saves SET last_train_at = ?, updated_at = ? WHERE user_id = ? AND slot = ?",
        (now_ms, now, user_id, slot),
    )
