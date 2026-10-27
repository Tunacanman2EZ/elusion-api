"""parties/data.py - the SQL. parties, party_members and party_invites are
this package's tables (schema.py). Every write to them is here. Other areas
READ them through app.py's party_membership(), party_member_rows() and
_party_invites_to(), which are membership(), member_rows() and invites_to()
below - party chat, the broadcast poll, the players menu - and presence.py
reads party_members for who may call whom. No commits: each route makes its
own."""

from . import services


def membership(db, user_id):
    """The account's party_members row, or None."""
    return db.execute("SELECT * FROM party_members WHERE user_id = ?",
                      (int(user_id),)).fetchone()


def member_rows(db, party_id):
    """A party's members, the leader first, then by when they joined."""
    return db.execute(
        "SELECT m.user_id, m.is_leader, m.joined_at, u.username"
        "  FROM party_members m JOIN users u ON u.id = m.user_id"
        " WHERE m.party_id = ? ORDER BY m.is_leader DESC, m.joined_at, m.user_id",
        (int(party_id),)).fetchall()


def fix(db, party_id):
    """After somebody left, was removed or deleted their account: a party of
    one is no party - it goes - and two or more always have exactly one
    leader, the longest there. Returns True if the party is gone."""
    rows = db.execute("SELECT user_id, is_leader FROM party_members WHERE party_id = ?"
                      " ORDER BY joined_at, user_id", (int(party_id),)).fetchall()
    if len(rows) <= 1:
        db.execute("DELETE FROM party_members WHERE party_id = ?", (int(party_id),))
        db.execute("DELETE FROM parties WHERE id = ?", (int(party_id),))
        return True
    leaders = [r for r in rows if int(r["is_leader"] or 0)]
    if len(leaders) != 1:
        db.execute("UPDATE party_members SET is_leader = 0 WHERE party_id = ?", (int(party_id),))
        db.execute("UPDATE party_members SET is_leader = 1 WHERE user_id = ?",
                   (int(rows[0]["user_id"]),))
    return False


def drop_gone(db, party_id, now, present=0):
    """Members of this party not seen for services.PARTY_GONE_SECONDS - every
    login of theirs quiet that long, or none left at all (logged out) - taken
    out, then fix(): a party left with one ends. Returns the names taken out.
    `present` is the account asking, plainly here whatever its stamp says.
    Read the constant on the module each time, so a suite can turn it; 0 is
    off (test_packages' bare app stands in for logins and has no sessions)."""
    if int(services.PARTY_GONE_SECONDS) <= 0:
        return []
    rows = member_rows(db, party_id)
    ids = [int(r["user_id"]) for r in rows]
    seen = last_seen(db, ids)
    cutoff = now - int(services.PARTY_GONE_SECONDS)
    gone = [r for r in rows if int(r["user_id"]) != int(present)
            and seen.get(int(r["user_id"]), 0) <= cutoff]
    for row in gone:
        remove_member(db, int(row["user_id"]))
    if gone:
        fix(db, party_id)
    return [str(r["username"]) for r in gone]


def invites_to(db, user_id, now):
    """Invitations waiting on this player's answer, newest first. A READ, on
    the poll every ten seconds: the ones run out are skipped here and pruned by
    the invite route, never deleted from a GET."""
    return db.execute(
        "SELECT u.username, i.created_at FROM party_invites i JOIN users u ON u.id = i.from_id"
        " WHERE i.user_id = ? AND i.created_at > ? ORDER BY i.created_at DESC, i.rowid DESC",
        (int(user_id), now - services.PARTY_INVITE_SECONDS)).fetchall()


def playing_saves(db, ids, playing_slot_sql, now, online_window):
    """{user_id: save} - the character each is playing (app.py's
    PLAYING_SLOT_SQL), with its health."""
    playing = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for save in db.execute(
            "SELECT user_id, name, class_id, level, area, hp, max_hp FROM saves"
            " WHERE user_id IN (%s) AND slot = %s"
            % (marks, playing_slot_sql.format(user="saves.user_id")),
            list(ids) + [now - online_window, now],
        ).fetchall():
            playing[int(save["user_id"])] = save
    return playing


def last_seen(db, ids):
    """{user_id: when any login of theirs was last seen}."""
    seen = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for row in db.execute(
            "SELECT user_id, MAX(last_seen_at) AS seen FROM sessions"
            " WHERE user_id IN (%s) GROUP BY user_id" % marks, list(ids)).fetchall():
            seen[int(row["user_id"])] = int(row["seen"] or 0)
    return seen


def prune_invites(db, now):
    """Every invitation past PARTY_INVITE_SECONDS."""
    db.execute("DELETE FROM party_invites WHERE created_at <= ?", (now - services.PARTY_INVITE_SECONDS,))


def invites_sent(db, from_id, but_to):
    """How many invitations this account has waiting, not counting one to
    `but_to` (which a second press only restarts)."""
    return int(db.execute("SELECT COUNT(*) AS n FROM party_invites WHERE from_id = ? AND user_id != ?",
                          (from_id, but_to)).fetchone()["n"] or 0)


def invite(db, user_id, from_id, now):
    """ASKED AGAIN, ASKED FRESH: a second press restarts the clock rather than
    making a second invitation."""
    db.execute("INSERT OR REPLACE INTO party_invites (user_id, from_id, created_at) VALUES (?, ?, ?)",
               (user_id, from_id, now))


def invite_from(db, user_id, from_id, now):
    """The invitation from `from_id` still waiting on `user_id`, or None."""
    return db.execute(
        "SELECT * FROM party_invites WHERE user_id = ? AND from_id = ? AND created_at > ?",
        (user_id, from_id, now - services.PARTY_INVITE_SECONDS)).fetchone()


def drop_invite(db, user_id, from_id):
    db.execute("DELETE FROM party_invites WHERE user_id = ? AND from_id = ?", (user_id, from_id))


def drop_invites_to(db, user_id):
    db.execute("DELETE FROM party_invites WHERE user_id = ?", (user_id,))


def drop_invites_from(db, from_id):
    db.execute("DELETE FROM party_invites WHERE from_id = ?", (from_id,))


def new_party(db, leader_id, now):
    """A party, made now, with the one who asked leading it; its id."""
    party_id = int(db.execute("INSERT INTO parties (created_at) VALUES (?)", (now,)).lastrowid)
    db.execute("INSERT INTO party_members (user_id, party_id, is_leader, joined_at) VALUES (?, ?, 1, ?)",
               (leader_id, party_id, now))
    return party_id


def join(db, user_id, party_id, now):
    """IN THE ORDER THEY CAME, to the second and past it: two joining in one
    second still have an order, which the leader's hand-on reads."""
    last = db.execute("SELECT MAX(joined_at) AS n FROM party_members WHERE party_id = ?",
                      (party_id,)).fetchone()["n"]
    db.execute("INSERT INTO party_members (user_id, party_id, is_leader, joined_at) VALUES (?, ?, 0, ?)",
               (user_id, party_id, max(now, int(last or 0) + 1)))


def remove_member(db, user_id):
    db.execute("DELETE FROM party_members WHERE user_id = ?", (user_id,))


def hand_on(db, party_id, user_id):
    """Every leader mark in the party cleared, then this one's set."""
    db.execute("UPDATE party_members SET is_leader = 0 WHERE party_id = ?", (party_id,))
    db.execute("UPDATE party_members SET is_leader = 1 WHERE user_id = ?", (user_id,))
