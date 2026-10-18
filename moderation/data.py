"""moderation/data.py - the SQL. chat_reports and the users and sessions
tables are app.py's (init_db()). Every write to chat_reports is here, and the
sanctions' writes to users (the ban, mute and rank columns) and to sessions
(a ban or a kick ends them all). Other readers: the broadcast poll's open
counts (through the two counts below, handed back by register()), the staff
account view, and chat's ignore list (chat/). No commits: the routes make
them."""


# -- the counts staff see on the poll -------------------------------------------

def open_line_count(db):
    """Lines with an open report, which is what a member of staff has to look
    at - three reports of one line are one thing to do."""
    return int(db.execute("SELECT COUNT(DISTINCT message_id) FROM chat_reports"
                          " WHERE resolved_at = 0").fetchone()[0] or 0)


def open_player_count(db):
    """Players with an open report - the Staff button's number. Twenty lines
    from one spammer are one player to deal with, and a count of lines made a
    single bad afternoon read as a crisis."""
    return int(db.execute("SELECT COUNT(DISTINCT reported_id) FROM chat_reports"
                          " WHERE resolved_at = 0").fetchone()[0] or 0)


# -- filing a report --------------------------------------------------------------

def prune(db, before):
    """Closed reports from before `before`. Pruned on write, like chat: no
    sweeper job to forget to run. Open ones are never pruned - a report nobody
    has looked at is still waiting, however old it is."""
    db.execute("DELETE FROM chat_reports WHERE resolved_at > 0 AND resolved_at < ?", (before,))


def chat_line(db, message_id):
    return db.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,)).fetchone()


def line_exists(db, message_id):
    """Still in chat, or already gone - deleted, or trimmed by age."""
    return db.execute("SELECT 1 FROM chat_messages WHERE id = ?", (message_id,)).fetchone() is not None


def report_by(db, message_id, reporter_id):
    """This reporter's report under `message_id` (its resolved_at), or None."""
    return db.execute("SELECT resolved_at FROM chat_reports WHERE message_id = ? AND reporter_id = ?",
                      (message_id, reporter_id)).fetchone()


def filed_since(db, reporter_id, since):
    """How many reports this account has filed since `since`."""
    return int(db.execute("SELECT COUNT(*) FROM chat_reports WHERE reporter_id = ? AND created_at > ?",
                          (reporter_id, since)).fetchone()[0])


def file_line_report(db, message_id, reporter_id, reporter_name, line, channel, reason, now):
    """A report of one chat line - THE LINE COPIED IN, so it survives being
    deleted, which is usually what a report leads to."""
    db.execute(
        "INSERT INTO chat_reports (message_id, reporter_id, reporter_name, reported_id,"
        " reported_name, channel, body, image_id, said_at, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, reporter_id, reporter_name, int(line["user_id"] or 0), line["username"],
         channel, str(line["body"] or ""), str(line["image_id"] or ""), int(line["created_at"] or 0),
         reason, now))


def file_player_report(db, key, reporter_id, reporter_name, reported_id, reported_name, body, reason, now):
    db.execute(
        "INSERT INTO chat_reports (message_id, reporter_id, reporter_name, reported_id,"
        " reported_name, channel, body, image_id, said_at, reason, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'player', ?, '', ?, ?, ?)",
        (key, reporter_id, reporter_name, reported_id, reported_name, body, now, reason, now))


def reopen_player_report(db, key, reporter_id, reported_name, body, reason, now):
    """A report about a player that was closed, filed again."""
    db.execute("UPDATE chat_reports SET reason = ?, body = ?, said_at = ?, created_at = ?,"
               " resolved_at = 0, resolved_by = '', outcome = '', reported_name = ?"
               " WHERE message_id = ? AND reporter_id = ?",
               (reason, body, now, now, reported_name, key, reporter_id))


# -- the Reports tab -------------------------------------------------------------

def _state_where(state):
    return "WHERE resolved_at = 0" if state == "open" else ""


def report_rows(db, state, limit=500):
    """The newest report rows - open ones, or every one ("all")."""
    return db.execute("SELECT * FROM chat_reports %s ORDER BY created_at DESC, id DESC LIMIT %d"
                      % (_state_where(state), int(limit))).fetchall()


def reported_players(db, state, limit):
    """One row per reported player, WORST FIRST: the most different people
    reporting them, then the most recent report.

    COUNTED IN SQL, ACROSS EVERY OPEN REPORT, not over a fetched page: a
    spammer with three hundred reported lines is one card that says 300, and
    does not push everybody else off the end of a 500-row read."""
    return db.execute(
        "SELECT reported_id, MAX(reported_name) AS reported_name,"
        " COUNT(DISTINCT message_id) AS lines, COUNT(*) AS reports,"
        " COUNT(DISTINCT reporter_id) AS people,"
        " MIN(created_at) AS first_at, MAX(created_at) AS last_at"
        " FROM chat_reports %s GROUP BY reported_id"
        " ORDER BY people DESC, last_at DESC LIMIT ?" % _state_where(state),
        (limit,)).fetchall()


def _about(state):
    return "reported_id = ?" + (" AND resolved_at = 0" if state == "open" else "")


def reasons_about(db, reported_id, state):
    return {
        r["reason"]: int(r["n"]) for r in db.execute(
            "SELECT reason, COUNT(*) AS n FROM chat_reports WHERE %s GROUP BY reason"
            " ORDER BY n DESC, reason" % _about(state), (reported_id,)).fetchall()}


def reporters_of(db, reported_id, state):
    return [r[0] for r in db.execute(
        "SELECT DISTINCT reporter_name FROM chat_reports WHERE %s"
        " ORDER BY reporter_name LIMIT 20" % _about(state), (reported_id,)).fetchall()]


def rows_about(db, reported_id, state, limit):
    return db.execute(
        "SELECT * FROM chat_reports WHERE %s ORDER BY created_at DESC, id DESC LIMIT ?" % _about(state),
        (reported_id, limit)).fetchall()


# -- closing ---------------------------------------------------------------------

def first_open_on_line(db, message_id):
    return db.execute("SELECT * FROM chat_reports WHERE message_id = ? AND resolved_at = 0"
                      " ORDER BY id LIMIT 1", (message_id,)).fetchone()


def close_line(db, message_id, by, outcome, now):
    """Every open report on one line, closed. How many reports that was."""
    return db.execute("UPDATE chat_reports SET resolved_at = ?, resolved_by = ?, outcome = ?"
                      " WHERE message_id = ? AND resolved_at = 0",
                      (now, by, outcome, message_id)).rowcount


def newest_open_about(db, user_id):
    return db.execute("SELECT body FROM chat_reports WHERE reported_id = ? AND resolved_at = 0"
                      " ORDER BY created_at DESC, id DESC LIMIT 1", (int(user_id),)).fetchone()


def close_about(db, by, user_id, outcome, now):
    """Close every open report about one account. Returns how many LINES that
    was."""
    lines = int(db.execute(
        "SELECT COUNT(DISTINCT message_id) FROM chat_reports"
        " WHERE reported_id = ? AND resolved_at = 0", (int(user_id),)).fetchone()[0] or 0)
    if lines:
        db.execute("UPDATE chat_reports SET resolved_at = ?, resolved_by = ?, outcome = ?"
                   " WHERE reported_id = ? AND resolved_at = 0",
                   (now, by, outcome, int(user_id)))
    return lines


# -- the sanctions ---------------------------------------------------------------

def ban(db, user_id, expires_at, reason, by, now):
    db.execute(
        """
        UPDATE users
           SET is_banned = 1, ban_expires_at = ?, ban_reason = ?,
               banned_by = ?, banned_at = ?
         WHERE id = ?
        """,
        (expires_at, reason, by, now, user_id),
    )


def unban(db, user_id):
    db.execute(
        """
        UPDATE users
           SET is_banned = 0, ban_expires_at = NULL, ban_reason = '',
               banned_by = '', banned_at = 0
         WHERE id = ?
        """,
        (user_id,),
    )


def live_sessions(db, user_id, now):
    return int(db.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND expires_at > ?",
        (user_id, now),
    ).fetchone()["n"] or 0)


def end_sessions(db, user_id):
    """Every session the account holds - not only the unexpired ones: an
    expired session grants nothing, but leaving it behind means "signed out
    everywhere" is not quite true."""
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_role(db, user_id, role):
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))


def mute(db, user_id, until, reason, by):
    db.execute("UPDATE users SET chat_muted_until = ?, chat_mute_reason = ?, chat_muted_by = ?"
               " WHERE id = ?", (until, reason, by, user_id))


def unmute(db, user_id):
    db.execute("UPDATE users SET chat_muted_until = 0, chat_mute_reason = '', chat_muted_by = ''"
               " WHERE id = ?", (user_id,))
