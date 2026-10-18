"""chat/data.py - the SQL. chat_messages, chat_deletions, chat_images,
chat_image_holders and ignores are app.py's tables (init_db()), as are the
users columns the two buckets live in (chat_tokens, last_chat_at,
last_image_at, last_world_image_at). Every write to them is here. The
reports (chat_reports) are the moderation routes'; this file only closes a
deleted line's reports and asks whether a report carries a picture. Other
areas READ chat in app.py: the poll's news, a report, the staff panels. No
commits: the routes make them."""
import hashlib
import secrets

from . import services

# APPENDED TO A CHAT READ'S WHERE, with the reader's id as its one parameter:
# NOBODY YOU IGNORE, in any channel. The poll's news uses the same clause.
IGNORED_AUTHORS_CLAUSE = " AND user_id NOT IN (SELECT ignored_id FROM ignores WHERE user_id = ?)"


# -- the flood bucket ---------------------------------------------------------

def bucket(db, user_id):
    """(last_chat_at, chat_tokens) on the account, or None for a deleted one."""
    return db.execute(
        "SELECT last_chat_at, chat_tokens FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def spend_token(db, user_id, tokens_left, now_ms):
    db.execute(
        "UPDATE users SET chat_tokens = ?, last_chat_at = ? WHERE id = ?",
        (tokens_left, now_ms, user_id),
    )


# -- lines ----------------------------------------------------------------------

def guild_said_from(db, user_id):
    """The guild the author flies now, for the line's snapshot - or ""."""
    row = db.execute(
        "SELECT gl.name FROM guild_members m JOIN guilds gl ON gl.id = m.guild_id"
        " WHERE m.user_id = ?", (int(user_id),)).fetchone()
    return row["name"] if row else ""


def add_line(db, user_id, username, role, body, now, channel, target_id, image_id, guild, name_hue):
    cursor = db.execute(
        "INSERT INTO chat_messages"
        " (user_id, username, role, body, created_at, channel, target_id, image_id, guild,"
        "  name_hue)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (int(user_id), username, role, body, now,
         channel, int(target_id or 0), str(image_id or ""), guild, name_hue),
    )
    return cursor.lastrowid


def prune(db, channel, now, keep_per_channel):
    """
    Keep the channel's tail, and drop anything past the day.

    Pruned on write, like broadcasts: no sweeper job to forget to run, and the
    cost lands on the account that is filling the table.
    """
    db.execute(
        "DELETE FROM chat_messages WHERE created_at < ?",
        (now - services.CHAT_RETENTION_SECONDS,),
    )
    db.execute(
        "DELETE FROM chat_messages WHERE channel = ? AND id NOT IN ("
        "  SELECT id FROM chat_messages WHERE channel = ?"
        "  ORDER BY id DESC LIMIT ?)",
        (channel, channel, keep_per_channel),
    )


def tail(db, where, params, limit):
    """The newest `limit` lines matching `where`, oldest first."""
    rows = db.execute(
        "SELECT * FROM chat_messages WHERE %s ORDER BY id DESC LIMIT ?" % where,
        tuple(params + [limit]),
    ).fetchall()
    return list(reversed(rows))


def after(db, where, params, since, limit):
    """Lines matching `where` past the client's cursor, oldest first."""
    return db.execute(
        "SELECT * FROM chat_messages WHERE %s AND id > ? ORDER BY id ASC LIMIT ?"
        % where,
        tuple(params + [since, limit]),
    ).fetchall()


def newest_id(db, where, params):
    return db.execute(
        "SELECT MAX(id) AS n FROM chat_messages WHERE %s" % where,
        tuple(params),
    ).fetchone()["n"]


def removed_since(db, where, params, since):
    """The ids taken down since `since`, by THE SAME `where` AND `params` the
    lines were read with - see read_chat()."""
    return db.execute(
        "SELECT message_id FROM chat_deletions WHERE %s AND deleted_at >= ?"
        " ORDER BY message_id" % where,
        tuple(params + [since]),
    ).fetchall()


def line(db, message_id):
    return db.execute(
        "SELECT * FROM chat_messages WHERE id = ?", (message_id,)
    ).fetchone()


def take_down(db, row, now):
    """The line gone, and its deletion noted for the clients that already have
    it - with the deleted line's own channel, user_id and target_id, so the
    note is filtered by the read's own rule - and notes past the window
    dropped: past it nobody can still be holding the line."""
    message_id = int(row["id"])
    db.execute("DELETE FROM chat_messages WHERE id = ?", (message_id,))
    db.execute(
        "INSERT OR REPLACE INTO chat_deletions"
        " (message_id, channel, user_id, target_id, deleted_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (message_id, str(row["channel"] or services.CHAT_DEFAULT_CHANNEL),
         int(row["user_id"] or 0), int(row["target_id"] or 0), now),
    )
    db.execute("DELETE FROM chat_deletions WHERE deleted_at < ?",
               (now - services.CHAT_DELETION_WINDOW_SECONDS,))


def answer_reports_on(db, message_id, by, now):
    """A DELETED LINE'S REPORTS ARE ANSWERED. Deleting it is what they asked
    for, and leaving them open would have the next mod look at a line that is
    gone."""
    db.execute("UPDATE chat_reports SET resolved_at = ?, resolved_by = ?, outcome = 'deleted'"
               " WHERE message_id = ? AND resolved_at = 0",
               (now, by, message_id))


# -- pictures -------------------------------------------------------------------

def image_meta(db, ids):
    """{id: row} - how to draw each of these pictures, in one query."""
    out = {}
    if ids:
        marks = ",".join("?" for _ in ids)
        for row in db.execute(
            "SELECT id, kind, width, height, frames, columns, frame_ms"
            " FROM chat_images WHERE id IN (%s)" % marks, tuple(ids)
        ).fetchall():
            out[row["id"]] = row
    return out


def image_held(db, image_id):
    """Whether the store has this picture at all."""
    return db.execute(
        "SELECT id FROM chat_images WHERE id = ?", (image_id,)).fetchone() is not None


def image_bytes(db, image_id):
    """(data, format) of a stored picture, or None."""
    return db.execute(
        "SELECT data, format FROM chat_images WHERE id = ?", (image_id,)).fetchone()


def lines_showing(db, image_id):
    """(user_id, channel, target_id) of every line showing this picture."""
    return db.execute(
        "SELECT user_id, channel, target_id FROM chat_messages WHERE image_id = ?",
        (image_id,)).fetchall()


def still_shown(db, image_id):
    """How many lines still show this picture."""
    return int(db.execute(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE image_id = ?", (image_id,)
    ).fetchone()["n"] or 0)


def holds(db, image_id, user_id):
    """Whether this account put these bytes on the server."""
    return db.execute("SELECT 1 FROM chat_image_holders WHERE image_id = ? AND user_id = ?",
                      (image_id, user_id)).fetchone() is not None


def reported(db, image_id):
    """Whether a report carries this picture."""
    return db.execute(
        "SELECT 1 FROM chat_reports WHERE image_id = ? LIMIT 1", (image_id,)).fetchone() is not None


def store_image(db, record, source_url, by, by_id, now):
    """Keep it, evicting the oldest while the store is over its cap, and note
    `by_id` as one of the accounts that put it here."""
    db.execute(
        "INSERT OR IGNORE INTO chat_images"
        " (id, kind, width, height, frames, columns, frame_ms, bytes,"
        "  source, created_by, created_at, format, data)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (record["id"], record["kind"], record["width"], record["height"],
         record["frames"], record["columns"], record["frame_ms"], record["bytes"],
         str(source_url)[:400], str(by)[:64], now,
         record.get("format", "png"), record["data"]),
    )
    if int(by_id or 0) > 0:
        db.execute("INSERT OR IGNORE INTO chat_image_holders (image_id, user_id, created_at)"
                   " VALUES (?, ?, ?)", (record["id"], int(by_id), now))

    evict(db)


def evict(db):
    """OLDEST OUT UNTIL IT FITS. A loop rather than one DELETE, because "enough
    rows to get under a byte total" is not something SQL answers in one go."""
    for _ in range(64):
        total = db.execute(
            "SELECT COALESCE(SUM(bytes), 0) AS n FROM chat_images").fetchone()["n"]
        if int(total or 0) <= services.IMAGE_STORE_MAX_BYTES:
            break
        oldest = db.execute(
            "SELECT id FROM chat_images ORDER BY created_at ASC LIMIT 1").fetchone()
        if oldest is None:
            break
        drop_image(db, oldest["id"])


def drop_image(db, image_id):
    """A picture and the record of who put it here, together."""
    db.execute("DELETE FROM chat_images WHERE id = ?", (image_id,))
    db.execute("DELETE FROM chat_image_holders WHERE image_id = ?", (image_id,))


def private_copy(db, image_id, by_row, now):
    """A copy of a stored picture under a fresh, random id, for one private
    line. The id is SHA-256 of 32 random bytes and the picture, so it is as
    unguessable as a content id and shared with nothing."""
    row = db.execute("SELECT data FROM chat_images WHERE id = ?", (image_id,)).fetchone()
    new_id = hashlib.sha256(secrets.token_bytes(32) + bytes(row["data"])).hexdigest()
    db.execute(
        "INSERT INTO chat_images"
        " (id, kind, width, height, frames, columns, frame_ms, bytes,"
        "  source, created_by, created_at, format, data)"
        " SELECT ?, kind, width, height, frames, columns, frame_ms, bytes,"
        "  source, ?, ?, format, data FROM chat_images WHERE id = ?",
        (new_id, str(by_row["username"])[:64], now, image_id),
    )
    db.execute("INSERT OR IGNORE INTO chat_image_holders (image_id, user_id, created_at)"
               " VALUES (?, ?, ?)", (new_id, int(by_row["id"]), now))
    evict(db)
    return new_id


def last_image_at(db, user_id):
    row = db.execute(
        "SELECT last_image_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else int(row["last_image_at"] or 0)


def stamp_image(db, user_id, now):
    db.execute("UPDATE users SET last_image_at = ? WHERE id = ?", (now, user_id))


def last_world_image_at(db, user_id):
    """READ FROM THE TABLE, NOT FROM g.user. user_for_token() selects a short
    list of columns - id, username, role, the ban fields - and not this one,
    so asking the row directly returned 0 forever and the limit silently did
    nothing. Its own lookup on the primary key, exactly like the upload
    cooldown beside it does for last_image_at. None for no such account."""
    row = db.execute(
        "SELECT last_world_image_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return None if row is None else int(row["last_world_image_at"] or 0)


def stamp_world_image(db, user_id, now):
    """Start the half hour. Called only once a picture is really in world."""
    db.execute(
        "UPDATE users SET last_world_image_at = ? WHERE id = ?", (now, user_id))


# -- the ignore list -----------------------------------------------------------

def ignores(db, user_id, other_id):
    """Does `user_id` ignore `other_id`?"""
    return db.execute("SELECT 1 FROM ignores WHERE user_id = ? AND ignored_id = ?",
                      (int(user_id), int(other_id))).fetchone() is not None


def ignored_by(db, user_id):
    """Everybody this account ignores, by name, and since when."""
    return db.execute(
        "SELECT u.username, i.created_at FROM ignores i JOIN users u ON u.id = i.ignored_id"
        " WHERE i.user_id = ? ORDER BY u.username COLLATE NOCASE", (user_id,)).fetchall()


def ignoring_count(db, user_id):
    return int(db.execute("SELECT COUNT(*) FROM ignores WHERE user_id = ?", (user_id,)).fetchone()[0])


def ignore(db, user_id, other_id, now):
    db.execute("INSERT INTO ignores (user_id, ignored_id, created_at) VALUES (?, ?, ?)",
               (user_id, int(other_id), now))


def unignore(db, user_id, other_id):
    """True if there was one to take back."""
    return db.execute("DELETE FROM ignores WHERE user_id = ? AND ignored_id = ?",
                      (int(user_id), int(other_id))).rowcount > 0
