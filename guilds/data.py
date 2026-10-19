"""guilds/data.py - the SQL. guilds, guild_members, guild_invites and
guild_events are this package's tables (schema.py). Every write to them is
here.
Other areas READ them in app.py - a chat line's guild, the broadcast poll's
invitations, the players list, the kingdom board - and who is in which guild
is asked through app.py's guild_membership(), which is membership() below.
No commits: each route makes its own."""

import time

from . import services


def membership(db, user_id):
    """This account's guild_members row, or None. Never raises on a stranger."""
    return db.execute(
        "SELECT * FROM guild_members WHERE user_id = ?", (user_id,)).fetchone()


def by_id(db, guild_id):
    return db.execute(
        "SELECT * FROM guilds WHERE id = ?", (guild_id,)).fetchone()


def by_name(db, name):
    """The guild by its name in any case - `folded` carries the unique index."""
    return db.execute(
        "SELECT * FROM guilds WHERE folded = ?", (services.folded(name),)).fetchone()


def member_ids(db, guild_id):
    """Every account in this guild, as a set of ids. Empty for no guild."""
    if not guild_id:
        return set()
    rows = db.execute(
        "SELECT user_id FROM guild_members WHERE guild_id = ?",
        (guild_id,)).fetchall()
    return set(int(r["user_id"]) for r in rows)


def roster_rows(db, guild_id):
    """Each member with their account and when any login of theirs was last
    seen: the leader first, then officers, then by name."""
    return db.execute(
        "SELECT u.id, u.username, u.role, u.name_hue, m.rank, m.joined_at,"
        "       COALESCE(MAX(s.last_seen_at), 0) AS last_seen_at"
        "  FROM guild_members m"
        "  JOIN users u ON u.id = m.user_id"
        "  LEFT JOIN sessions s ON s.user_id = u.id"
        " WHERE m.guild_id = ?"
        " GROUP BY u.id"
        " ORDER BY CASE m.rank WHEN 'leader' THEN 0 WHEN 'officer' THEN 1"
        "                      ELSE 2 END, u.username",
        (guild_id,)).fetchall()


def playing_saves(db, ids, playing_slot_sql, now, online_window):
    """{user_id: save} - the character each of these accounts is playing.

    WHO THEY ARE PLAYING, one query for the whole roster, by app.py's
    PLAYING_SLOT_SQL - the character their client says it is on, else the one
    saved last. The players menu and the trade window ask the same question
    the same way.

    IT USED TO BE "SAVED LAST" ALONE, via MAX(updated_at), and that is wrong
    in exactly the minute a guildmate switches character: the roster showed
    yesterday's warrior while they stood there as a mage. A member with no
    character yet still gets nothing rather than a guess."""
    playing = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for save in db.execute(
            "SELECT user_id, name, class_id, level, area FROM saves"
            " WHERE user_id IN (%s) AND slot = %s"
            % (marks, playing_slot_sql.format(user="saves.user_id")),
            list(ids) + [now - online_window, now],
        ).fetchall():
            playing[int(save["user_id"])] = save
    return playing


def log_guild_event(db, guild_id, kind, actor="", target="", detail="", now=None):
    """One line of a guild's history, in the caller's transaction.

    IN THE SAME TRANSACTION as the thing it describes, like staff_actions: a
    promotion that happened with no line about it, or a line about one that
    did not happen, are both worse than no history at all.
    """
    if now is None:
        now = int(time.time())
    db.execute(
        "INSERT INTO guild_events (guild_id, at, kind, actor, target, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (int(guild_id), int(now), kind, actor or "", target or "", detail or ""))
    # PRUNED ON WRITE, per guild, so one busy guild cannot push a quiet one's
    # history out and nothing has to run on a schedule.
    db.execute(
        "DELETE FROM guild_events WHERE guild_id = ? AND id NOT IN ("
        "  SELECT id FROM guild_events WHERE guild_id = ? ORDER BY id DESC LIMIT ?)",
        (int(guild_id), int(guild_id), services.GUILD_EVENTS_KEPT))


def activity(db, guild_id, limit=services.GUILD_ACTIVITY_SHOWN):
    """The newest events, newest first, as the panel reads them."""
    return [{
        "at": int(row["at"]),
        "kind": row["kind"],
        "actor": row["actor"],
        "target": row["target"],
        "detail": row["detail"],
    } for row in db.execute(
        "SELECT at, kind, actor, target, detail FROM guild_events"
        " WHERE guild_id = ? ORDER BY id DESC LIMIT ?", (int(guild_id), int(limit))
    ).fetchall()]


def invites_to(db, user_id):
    """The invitations waiting on this account's answer, oldest first."""
    return db.execute(
        "SELECT i.invited_by, i.created_at, gl.name"
        "  FROM guild_invites i JOIN guilds gl ON gl.id = i.guild_id"
        " WHERE i.user_id = ? ORDER BY i.created_at",
        (user_id,)).fetchall()


def carried_gold(db, user_id, slot):
    """The saves row's purse ({gold}), or None for an empty slot."""
    return db.execute(
        "SELECT gold FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot)).fetchone()


def found(db, name, founder, now):
    """A new guild row; its id."""
    cur = db.execute(
        "INSERT INTO guilds (name, folded, founded_by, created_at)"
        " VALUES (?, ?, ?, ?)",
        (name, name.lower(), founder, now))
    return int(cur.lastrowid)


def add_member(db, user_id, guild_id, rank, now):
    db.execute(
        "INSERT INTO guild_members (user_id, guild_id, rank, joined_at)"
        " VALUES (?, ?, ?, ?)",
        (user_id, guild_id, rank, now))


def drop_invites_to(db, user_id):
    """Every invitation this account holds."""
    db.execute("DELETE FROM guild_invites WHERE user_id = ?", (user_id,))


def invites_waiting(db, guild_id):
    """How many invitations this guild has outstanding."""
    return int(db.execute(
        "SELECT COUNT(*) AS n FROM guild_invites WHERE guild_id = ?",
        (guild_id,)).fetchone()["n"] or 0)


def invite(db, guild_id, user_id, invited_by, now):
    """An invitation - INSERT OR IGNORE: inviting somebody twice is not an
    error, it just does not make a second invitation. True if this one is
    new."""
    made = db.execute(
        "INSERT OR IGNORE INTO guild_invites"
        " (guild_id, user_id, invited_by, created_at) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, invited_by, now))
    return made.rowcount > 0


def invite_of(db, guild_id, user_id):
    return db.execute(
        "SELECT * FROM guild_invites WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id)).fetchone()


def drop_invite(db, guild_id, user_id):
    db.execute("DELETE FROM guild_invites WHERE guild_id = ? AND user_id = ?",
               (guild_id, user_id))


def remove_member(db, user_id):
    db.execute("DELETE FROM guild_members WHERE user_id = ?", (user_id,))


def drop_guild(db, guild_id):
    """The guild row alone - for one its last member walked out of."""
    db.execute("DELETE FROM guilds WHERE id = ?", (guild_id,))


def take_down(db, guild_id):
    """The guild, its members and its invitations.

    Both tables cascade on the guild row, but SQLite only enforces foreign
    keys when the pragma is on, and a guild that leaves orphaned rows behind
    is a player stuck in a guild that no longer exists. Deleted explicitly,
    in order."""
    db.execute("DELETE FROM guild_members WHERE guild_id = ?", (guild_id,))
    db.execute("DELETE FROM guild_invites WHERE guild_id = ?", (guild_id,))
    db.execute("DELETE FROM guilds WHERE id = ?", (guild_id,))


def set_rank(db, user_id, rank):
    db.execute("UPDATE guild_members SET rank = ? WHERE user_id = ?", (rank, user_id))


def rename(db, guild_id, name):
    """BOTH COLUMNS, AND THIS IS NOT OPTIONAL. `folded` is the name
    lowercased, it carries the unique index, and by_name() looks the guild up
    BY IT rather than by `name`.

    The first version of this UPDATE set `name` alone. The rename "worked" -
    the table said the new name and the route returned it - and the guild went
    on answering to the OLD one while being unfindable by the new one. A staff
    member renaming a slur would have published a fix that changed nothing
    anybody could act on. Caught by a test that asked, through the route,
    whether the old name still resolved.

    One statement, so there is no instant where they disagree."""
    db.execute("UPDATE guilds SET name = ?, folded = ? WHERE id = ?",
               (name, services.folded(name), guild_id))


def every_guild(db):
    """Every guild with its size, by name."""
    return db.execute(
        "SELECT gl.*, COUNT(m.user_id) AS size"
        "  FROM guilds gl LEFT JOIN guild_members m ON m.guild_id = gl.id"
        " GROUP BY gl.id ORDER BY gl.name"
    ).fetchall()


def leader_of(db, guild_id):
    """The leader's {username}, or None."""
    return db.execute(
        "SELECT u.username FROM guild_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.guild_id = ? AND m.rank = 'leader'", (guild_id,)).fetchone()
