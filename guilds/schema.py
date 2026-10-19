"""guilds/schema.py - guilds/'s tables:
guilds, guild_members, guild_invites and guild_events.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- A GUILD, AND WHO IS IN IT.
        --
        -- THREE TABLES, AND THE SHAPE IS THE RULE. guild_members has user_id
        -- as its PRIMARY KEY, which is not a detail - it is how "one guild per
        -- player" is enforced. Expressed as a column with a uniqueness check
        -- in Python instead, the second guild somebody joined would depend on
        -- two requests not overlapping, and they eventually do.
        --
        -- `folded` is the name lowercased, with its own unique index, so
        -- "The Crowned" and "the crowned" cannot both exist. The display name
        -- keeps whatever capitals the founder typed.
        CREATE TABLE IF NOT EXISTS guilds (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            folded     TEXT    NOT NULL,
            founded_by TEXT    NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_guilds_folded
            ON guilds(folded);

        CREATE TABLE IF NOT EXISTS guild_members (
            user_id   INTEGER PRIMARY KEY,
            guild_id  INTEGER NOT NULL,
            rank      TEXT    NOT NULL DEFAULT 'member'
                      CHECK (rank IN ('leader', 'officer', 'member')),
            joined_at INTEGER NOT NULL,
            FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE,
            FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
        );

        -- The roster read is "everyone in guild N", which the primary key on
        -- user_id does not answer.
        CREATE INDEX IF NOT EXISTS idx_guild_members_guild
            ON guild_members(guild_id, rank);

        -- AN INVITATION WAITING FOR AN ANSWER, exactly like a friend request
        -- and for the same reason: being put in a guild without being asked
        -- means being given a rank, a chat channel and a name beside yours
        -- that somebody else chose.
        CREATE TABLE IF NOT EXISTS guild_invites (
            guild_id   INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            invited_by TEXT    NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id),
            FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id)  REFERENCES users(id)  ON DELETE CASCADE
        );

        -- "What have I been invited to" is the common read, and the primary
        -- key leads with guild_id.
        CREATE INDEX IF NOT EXISTS idx_guild_invites_user
            ON guild_invites(user_id);

        -- WHAT HAPPENED IN A GUILD, for its own members: founded, joined,
        -- left, invited, promoted, demoted, handed on, removed, renamed.
        --
        -- NAMES, NOT IDS, for the reason staff_actions gives: the line has to
        -- read the same after somebody has left the guild or deleted their
        -- account. It is the guild's history, not a join against who is in it
        -- now.
        --
        -- NOT THE MODERATION LOG. staff_actions is what staff did and is read
        -- by staff; this is what members did and is read by members. Staff
        -- renaming a guild appears in both, because both audiences need it.
        --
        -- Bounded per guild - see GUILD_EVENTS_KEPT - and gone with the guild.
        CREATE TABLE IF NOT EXISTS guild_events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            at       INTEGER NOT NULL,
            kind     TEXT    NOT NULL,
            actor    TEXT    NOT NULL DEFAULT '',
            target   TEXT    NOT NULL DEFAULT '',
            detail   TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (guild_id) REFERENCES guilds(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_guild_events_guild
            ON guild_events(guild_id, id);
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)
