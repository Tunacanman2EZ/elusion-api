"""players/data.py - the SQL: who is online, who shares an area, what a
character holds for its ticket, and the ticket. The tables are app.py's
(init_db()) but the one written here: presence_tickets, this package's
(schema.py), which presence.py reads.

`playing_slot_sql` is app.py's PLAYING_SLOT_SQL, handed in: which character an
account is playing, as one SQL expression that takes two parameters in order,
the online cutoff and then now."""


def online(db, cutoff, now, limit, playing_slot_sql):
    """Every account seen since `cutoff` whose session is still valid at `now`:
    its name, rank and colour, the character it is playing and that
    character's guild. One row an account, by name, at most `limit`."""
    return db.execute(
        """
        SELECT users.username                AS username,
               users.role                    AS role,
               users.name_hue                AS name_hue,
               MAX(sessions.last_seen_at)    AS seen,
               saves.name                    AS name,
               saves.level                   AS level,
               saves.area                    AS area,
               saves.updated_at              AS updated_at,
               guilds.name                   AS guild
          FROM sessions
          JOIN users ON users.id = sessions.user_id
          -- THE CHARACTER THEY ARE PLAYING, and only that one, by the rule the
          -- trade window and the guild roster use (PLAYING_SLOT_SQL). It used
          -- to join every save and keep whichever was saved last.
          LEFT JOIN saves ON saves.user_id = sessions.user_id
                         AND saves.slot = %s
          -- THE GUILD RIDES THE JOIN THAT IS ALREADY HERE. Two LEFT JOINs on
          -- indexed keys, against a list already capped at ONLINE_LIST_LIMIT
          -- rows - not a second query, and not a lookup per player in Python.
          LEFT JOIN guild_members ON guild_members.user_id = users.id
          LEFT JOIN guilds ON guilds.id = guild_members.guild_id
         WHERE sessions.last_seen_at > ?
           AND sessions.expires_at > ?
      GROUP BY users.id
      ORDER BY users.username ASC
         LIMIT ?
        """ % playing_slot_sql.format(user="users.id"),
        # PLAYING_SLOT_SQL's two first - it sits in the JOIN, above the WHERE.
        (cutoff, now,
         cutoff, now, limit),
    ).fetchall()


def area_of(db, user_id, slot):
    """The character's row with its area, or None for no such character."""
    return db.execute(
        "SELECT area FROM saves WHERE user_id = ? AND slot = ?", (user_id, slot)
    ).fetchone()


def nearby(db, area, user_id, cutoff, now, limit, playing_slot_sql):
    """Everybody else online in `area`, by the character each is playing:
    highest level first, at most `limit`."""
    return db.execute(
        """
        SELECT DISTINCT users.username AS username,
               users.role     AS role,
               users.name_hue AS name_hue,
               saves.slot   AS slot,
               saves.name   AS name,
               saves.level  AS level
          FROM saves
          JOIN users    ON users.id = saves.user_id
          JOIN sessions ON sessions.user_id = saves.user_id
         WHERE saves.area = ?
           AND saves.user_id != ?
           AND sessions.last_seen_at > ?
           AND sessions.expires_at > ?
           -- ONLY THE CHARACTER THEY ARE PLAYING. An account has up to four,
           -- and without this every one of them parked in this area was
           -- listed - so "Trade" beside somebody's old alt opened a trade
           -- with a character nobody was playing.
           AND saves.slot = %s
      ORDER BY saves.level DESC, users.username ASC
         LIMIT ?
        """ % playing_slot_sql.format(user="saves.user_id"),
        # LAST SEEN **AND** STILL VALID. Both, and the first version of this fix
        # had only one of them, which test_economy.py caught in one line.
        #
        # This filtered on sessions.expires_at > now, which is true for anybody
        # holding an unexpired token - and a token lasts TOKEN_TTL, thirty days,
        # surviving the game being closed. So "in this area with you" listed
        # everyone who had logged in since last month and happened to save here.
        #
        # ONLINE_WINDOW_SECONDS is app.py's own answer to "is anyone there",
        # and its comment warns about exactly this mistake in exactly these
        # words: "NOT 'has a live session'... a kick list sorted by it put last
        # week's visitors at the top." The heartbeat stamps last_seen_at every
        # 15 seconds, so 45 is three missed beats.
        #
        # AND THE EXPIRY STILL MATTERS. A beat within the window says the client
        # was there; an unexpired token says it is still allowed to be. Dropping
        # the second one meant a session that had been revoked or had run out
        # went on being listed for its last 45 seconds - which is exactly what
        # "an offline player is not listed as nearby" in test_economy.py exists
        # to catch, and did.
        (area, user_id, cutoff, now,
         # PLAYING_SLOT_SQL's own two, in its order: cutoff, then now.
         cutoff, now,
         limit),
    ).fetchall()


def character(db, user_id, slot):
    """What a ticket says about the character: its name, class, level and the
    equipment as stored. None for no such character."""
    return db.execute(
        "SELECT name, class_id, level, equipment FROM saves WHERE user_id = ? AND slot = ?",
        (user_id, slot),
    ).fetchone()


def held(db, user_id, slot):
    """Every item id the account holds for this character: in its bag, or in
    the account's shared bank."""
    return [r["item_id"] for r in db.execute(
        "SELECT item_id FROM carry_items WHERE user_id = ? AND slot = ?"
        " UNION SELECT item_id FROM bank_items WHERE user_id = ?",
        (user_id, slot, user_id),
    ).fetchall()]


def carried(db, user_id, slot):
    """Every item id in the character's own cells, each once."""
    return [r["item_id"] for r in db.execute(
        "SELECT DISTINCT item_id FROM carry_items WHERE user_id = ? AND slot = ?", (user_id, slot))]


def fighting_skills(db, user_id, slot):
    """{skill_id: level} for the three fighting skills the character has."""
    return {r["skill_id"]: int(r["level"]) for r in db.execute(
        "SELECT skill_id, level FROM skills WHERE user_id = ? AND slot = ?"
        " AND skill_id IN ('attack', 'magic', 'agility')", (user_id, slot))}


def issue_ticket(db, token_hash, session_hash, user_id, slot, identity, now, expires_at):
    """Write one ticket, the dead ones pruned first. No commit."""
    # Pruned as they are issued, like chat: a ticket lives two minutes, so the
    # table is never more than two minutes of renewals deep.
    db.execute("DELETE FROM presence_tickets WHERE expires_at <= ?", (now,))
    db.execute(
        "INSERT INTO presence_tickets (token_hash, session_hash, user_id, slot, identity,"
        " issued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (token_hash, session_hash, user_id, slot, identity, now, expires_at),
    )
