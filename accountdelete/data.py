"""accountdelete/data.py - the SQL: what an account holds, and emptying it.

Every statement names the account by the id the route was handed (the
caller's own, from the token), so nothing here can reach anybody else's rows -
except the four places that are about two people: friends, ignores and party
invitations (either side), and the names taken out of the staff log, the
reports, the guild's history and the gifts ledger.

And the one file that is not the database: the list of deleted accounts
kept beside it (services.py, THE LIST THAT OUTLIVES A RESTORE) - remember()
adds a line, read_list() reads it back.
"""
import os

# Rows that are the player's and go, by user_id - in an order where nothing
# deleted later still points at something deleted earlier. loot_bag_items go
# with loot_bags (ON DELETE CASCADE); the bank's and the bags' items carry no
# money, so they need no ledger.
OWN_TABLES = (
    "carry_items", "bank_items", "skills", "smith_ranks", "food_buffs", "consume_grants",
    "character_deletions", "save_snapshots", "kill_tally", "kill_reports", "kill_receipts",
    "loot_bags", "combat_flags", "combat_kills", "position_samples", "client_errors",
    "trade_holds", "pending_teleports", "presence_tickets", "auth_codes", "account_ips",
    "account_installs", "trusted_devices", "ended_sessions", "sessions",
    "guild_invites", "guild_members", "party_members", "chat_image_holders",
    "minigame_entries", "minigame_kills", "loot_chest_items", "google_codes", "google_accounts",
    # A Healer's Run's runs (0.99.0) were left behind until game 0.120.0
    # added the best times beside them.
    "minigame_runs", "minigame_bests",
)


def characters(db, user_id):
    return db.execute("SELECT slot, class_id, gold FROM saves WHERE user_id = ? ORDER BY slot",
                      (user_id,)).fetchall()


def account(db, user_id):
    return db.execute("SELECT bank_gold, lusions FROM accounts WHERE user_id = ?", (user_id,)).fetchone()


def guild_seat(db, user_id):
    """(guild id, rank, how many members) or None."""
    row = db.execute("SELECT guild_id, rank FROM guild_members WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        return None
    members = db.execute("SELECT COUNT(*) FROM guild_members WHERE guild_id = ?",
                         (row["guild_id"],)).fetchone()[0]
    return int(row["guild_id"]), str(row["rank"]), int(members)


def party_of(db, user_id):
    row = db.execute("SELECT party_id FROM party_members WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["party_id"]) if row is not None else None


def chat_lines(db, user_id):
    """Every line they said, whole rows (chat/'s take_down() reads the channel
    and target from them)."""
    return db.execute("SELECT * FROM chat_messages WHERE user_id = ?", (user_id,)).fetchall()


def held_images(db, user_id):
    return {r["image_id"] for r in db.execute(
        "SELECT image_id FROM chat_image_holders WHERE user_id = ?", (user_id,)).fetchall()}


def drop_orphan_images(db, image_ids):
    """A picture no line shows any more, and that nobody else put up, goes -
    bytes and all. One another line still shows, or another player also
    uploaded, stays: it is theirs too."""
    dropped = 0
    for image_id in image_ids:
        shown = db.execute("SELECT 1 FROM chat_messages WHERE image_id = ? LIMIT 1", (image_id,)).fetchone()
        held = db.execute("SELECT 1 FROM chat_image_holders WHERE image_id = ? LIMIT 1", (image_id,)).fetchone()
        if shown is None and held is None:
            db.execute("DELETE FROM chat_images WHERE id = ?", (image_id,))
            dropped += 1
    return dropped


def empty(db, user_id, old_name, stand_in, now):
    """Everything that is the account's, gone; the names in what is kept, the
    stand-in; the users row, only its id and when it was made. The caller has
    burned the gold and lusions through the ledgers first."""
    for table in OWN_TABLES:
        db.execute("DELETE FROM %s WHERE user_id = ?" % table, (user_id,))
    db.execute("DELETE FROM saves WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM party_invites WHERE user_id = ? OR from_id = ?", (user_id, user_id))
    db.execute("DELETE FROM friends WHERE requester_id = ? OR addressee_id = ?", (user_id, user_id))
    db.execute("DELETE FROM ignores WHERE user_id = ? OR ignored_id = ?", (user_id, user_id))
    db.execute("DELETE FROM login_attempts WHERE username = ?", (old_name,))

    # KEPT, WITHOUT THE NAME.
    db.execute("UPDATE staff_actions SET actor_name = ? WHERE actor_id = ?", (stand_in, user_id))
    db.execute("UPDATE staff_actions SET target_name = ? WHERE target_id = ?", (stand_in, user_id))
    db.execute("UPDATE chat_reports SET reporter_name = ? WHERE reporter_id = ?", (stand_in, user_id))
    db.execute("UPDATE chat_reports SET reported_name = ? WHERE reported_id = ?", (stand_in, user_id))
    db.execute("UPDATE owner_gifts SET username = ? WHERE user_id = ?", (stand_in, user_id))
    db.execute("UPDATE guild_events SET actor = ? WHERE actor = ? COLLATE NOCASE", (stand_in, old_name))
    db.execute("UPDATE guild_events SET target = ? WHERE target = ? COLLATE NOCASE", (stand_in, old_name))

    # THE ROW ITSELF: its id (the ledgers, the trades and the log name it) and
    # when it was made. Nothing else of the person.
    db.execute(
        "UPDATE users SET username = ?, password_hash = '!', role = 'player', email = '',"
        " email_verified = 0, name_hue = NULL, is_banned = 0, ban_expires_at = NULL, ban_reason = '',"
        " banned_by = '', banned_at = 0, last_chat_at = 0, chat_tokens = 0, last_world_image_at = 0,"
        " last_image_at = 0, failed_logins = 0, lockout_until = 0, chat_muted_until = 0,"
        " chat_mute_reason = '', chat_muted_by = '', deaths = 0, deleted_at = ? WHERE id = ?",
        (stand_in, now, user_id))


def is_deleted(db, user_id):
    row = db.execute("SELECT deleted_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and int(row["deleted_at"] or 0) > 0


def made_at(db, user_id):
    """When the account was made, or None for no such row."""
    row = db.execute("SELECT created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return int(row["created_at"]) if row is not None else None


def whole_again(db, listed):
    """The listed accounts that are whole in this database - not emptied, and
    made at the very second the list says: [(id, username)]. On a database
    the list belongs to, none."""
    back = []
    for user_id, created_at in sorted(listed.items()):
        row = db.execute("SELECT id, username, created_at, deleted_at FROM users WHERE id = ?",
                         (user_id,)).fetchone()
        if row is not None and int(row["deleted_at"] or 0) == 0 and int(row["created_at"]) == created_at:
            back.append((int(row["id"]), str(row["username"])))
    return back


def call_off_trades(db, user_id, now):
    """An open trade naming them, called off - as it would lapse. Only a
    restored account can be in one when it goes; the route refuses."""
    db.execute("UPDATE trades SET state = 'cancelled', updated_at = ?"
               " WHERE state = 'open' AND (a_user = ? OR b_user = ?)", (now, user_id, user_id))


def hand_on_guild(db, guild_id, user_id):
    """Their guild to the member there longest, when they led it with others
    in it. Only a restored account can; the route asks the leader to hand it
    on first."""
    heir = db.execute("SELECT user_id FROM guild_members WHERE guild_id = ? AND user_id != ?"
                      " ORDER BY joined_at, user_id LIMIT 1", (guild_id, user_id)).fetchone()
    if heir is not None:
        db.execute("UPDATE guild_members SET rank = 'leader' WHERE user_id = ?", (heir["user_id"],))


def remember(path, line):
    """One line on the end of the list, on the disk before the deletion
    commits: a crash between the two leaves an account deleted at the next
    start that its owner had asked to delete, never the other way round."""
    handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(handle, line.encode("ascii"))
        os.fsync(handle)
    finally:
        os.close(handle)


def read_list(path):
    """The list's text, or "" when there is none yet."""
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read()
    except FileNotFoundError:
        return ""

