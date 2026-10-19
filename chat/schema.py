"""chat/schema.py - chat/'s tables:
chat_messages, chat_images, chat_image_holders, chat_deletions
and ignores - what the players say, the pictures, who put each one
up, the deletions the feed carries, and who a player will not hear.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

WORD FOR WORD BUT ONE WORD: chat_messages' comment pointed at "the
broadcasts comment above", and broadcasts stayed in init_db().

upgrade() adds the columns that came later to a database made before
them; init_db() calls it after every create(), among its migrations.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- WHAT THE PLAYERS SAY TO EACH OTHER.
        --
        -- The sibling the broadcasts comment in init_db() predicted. It is a separate
        -- table rather than an author column on broadcasts because the two are
        -- not the same kind of thing: a broadcast is the server speaking and
        -- can never be wrong, while this is a public surface that needs a
        -- length cap, a flood bucket and a way for a mod to take a line back
        -- down. Keeping them apart also keeps the kill switch's notice out of
        -- reach of anything a player can do to the chat table.
        --
        -- USERNAME AND ROLE ARE COPIED IN, not joined. A message is a record of
        -- what was said and who said it AT THAT MOMENT: promoting someone to
        -- dev should not retroactively re-badge every line they have ever
        -- written, and renaming an account should not rewrite its history. It
        -- also keeps the hottest poll in the game off a join.
        --
        -- Same AUTOINCREMENT cursor as broadcasts, for the same reason.
        CREATE TABLE IF NOT EXISTS chat_messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            username   TEXT    NOT NULL,
            role       TEXT    NOT NULL DEFAULT 'player',
            body       TEXT    NOT NULL,
            created_at INTEGER NOT NULL,
            -- WHICH CONVERSATION THIS LINE BELONGS TO.
            --
            -- ONE TABLE, NOT ONE PER CHANNEL. Every channel wants the same
            -- columns, the same flood bucket, the same "take that line down"
            -- and the same cursor; four tables would be four copies of all of
            -- it, and the fourth would be the one that forgot the moderation.
            --
            -- target_id IS READ DIFFERENTLY PER CHANNEL, which is the only
            -- awkward part of that choice and is worth the trade:
            --   world     unused, 0
            --   private   the account this was said TO
            --   friends   unused, 0 - the audience is the author's friend list
            --   guild     the guild, once guilds exist
            channel    TEXT    NOT NULL DEFAULT 'world',
            target_id  INTEGER NOT NULL DEFAULT 0,
            -- A RELAYED PICTURE, by id into chat_images. Empty for a line that
            -- is only words, which is nearly all of them.
            image_id   TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_chat_id ON chat_messages(id);

        -- Every read is "this channel, after this id", and for a private
        -- conversation it is also "between these two accounts".
        CREATE INDEX IF NOT EXISTS idx_chat_channel ON chat_messages(channel, id);
        CREATE INDEX IF NOT EXISTS idx_chat_private
            ON chat_messages(channel, user_id, target_id, id);
        -- "What was said TO me": the newest whisper to an account, and the
        -- newest line in a guild, both asked on every broadcast poll.
        CREATE INDEX IF NOT EXISTS idx_chat_to
            ON chat_messages(channel, target_id, id);

        -- PICTURES, FETCHED BY THE SERVER AND RE-SERVED BY IT.
        --
        -- WHY THE SERVER HOLDS THE BYTES instead of clients loading the URL a
        -- player pasted. A link in a chat window is a fetch every client in
        -- the channel performs on command: whoever posted it learns the IP of
        -- everyone who saw it, and can change what is behind the link after
        -- it has been moderated. Relaying costs one fetch, by the server,
        -- once - and what players receive is a picture this server has
        -- already decoded and knows is a picture.
        --
        -- KEYED BY THE HASH OF THE BYTES, so the same image posted twice is
        -- stored once and the id is not guessable from the URL.
        --
        -- ANIMATION IS FLATTENED TO A SPRITESHEET on the way in. Godot has no
        -- GIF decoder; it does have AnimatedTexture and has been drawing
        -- spritesheets since the first enemy. Decoding where Pillow already
        -- lives and shipping the client something it can already play is the
        -- difference between animated pictures working and not.
        CREATE TABLE IF NOT EXISTS chat_images (
            id         TEXT    PRIMARY KEY,
            kind       TEXT    NOT NULL DEFAULT 'still',
            width      INTEGER NOT NULL,
            height     INTEGER NOT NULL,
            frames     INTEGER NOT NULL DEFAULT 1,
            columns    INTEGER NOT NULL DEFAULT 1,
            frame_ms   INTEGER NOT NULL DEFAULT 0,
            bytes      INTEGER NOT NULL,
            source     TEXT    NOT NULL DEFAULT '',
            created_by TEXT    NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            format     TEXT    NOT NULL DEFAULT 'png',
            data       BLOB    NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chat_images_age
            ON chat_images(created_at);

        -- WHO PUT EACH PICTURE ON THE SERVER, one row per account. Not a column
        -- on chat_images, because the store deduplicates on the content hash:
        -- when two players upload the same file it is ONE row, and created_by
        -- only remembers the first. The second uploader still has to be able
        -- to see and send what they uploaded - see WHO MAY SEE A PICTURE.
        -- Removed with the picture (eviction, or a mod deleting its last line).
        CREATE TABLE IF NOT EXISTS chat_image_holders (
            image_id   TEXT    NOT NULL,
            user_id    INTEGER NOT NULL,
            created_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (image_id, user_id)
        );

        -- A DELETION IS AN EVENT, AND THE FEED HAD NO WAY TO CARRY ONE.
        --
        -- GET /api/chat answers "messages with id greater than `since`", which is
        -- the right question for a growing log and cannot express the opposite
        -- one: something you were already given is no longer true. So deleting a
        -- message removed the row, the message stopped appearing in FUTURE polls,
        -- and every client already holding it kept it - on screen, and with the
        -- picture decoded in memory. A mod took a line down and it stayed on
        -- every screen that had already seen it, which is precisely the set of
        -- people the deletion was for.
        --
        -- WHY A SEPARATE TABLE RATHER THAN A deleted_at COLUMN. A tombstone in
        -- chat_messages would mean every existing read has to learn to say
        -- "AND deleted_at = 0" - a dozen places to forget one, and forgetting it
        -- shows the deleted line again. This table is additive: nothing that
        -- reads chat_messages needs to change, because the row really is gone.
        --
        -- WHY IT STORES THE DELETED MESSAGE'S OWN channel, user_id AND target_id
        -- RATHER THAN JUST THE ID. Those are the three columns GET /api/chat
        -- builds its "who may read what" clause out of, so the poll can filter
        -- deletions with THE VERY SAME CLAUSE it filters messages with. One rule,
        -- one copy of it, and the property falls out for free: a message you were
        -- never allowed to see cannot produce a deletion you are told about.
        --
        -- Without that, world chat would carry the ids of deleted whispers to
        -- everybody. No content in an id, but still the fact that a line existed
        -- between two people and was taken down. Three columns is the whole cost.
        --
        -- WHY THERE IS NO SECOND CURSOR, which is the part that makes this
        -- small. `since` cannot find a deletion below a client's cursor, so the
        -- poll reports deletions by WHEN THEY HAPPENED instead - anything in the
        -- last CHAT_DELETION_WINDOW_SECONDS. That is only sufficient because of
        -- something the client already does: chatpanel.gd starts from the TAIL
        -- rather than from a stored cursor when the window opens, so a client
        -- that was away longer than the window cannot be holding the line
        -- anyway. The window covers exactly the clients that were watching.
        CREATE TABLE IF NOT EXISTS chat_deletions (
            message_id INTEGER PRIMARY KEY,
            channel    TEXT    NOT NULL DEFAULT 'world',
            user_id    INTEGER NOT NULL DEFAULT 0,
            target_id  INTEGER NOT NULL DEFAULT 0,
            deleted_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_chat_deletions_age
            ON chat_deletions(deleted_at);

        -- WHO A PLAYER HAS CHOSEN NOT TO HEAR. See IGNORE, REPORT, MUTE. One row
        -- per pair, directional: user_id is the one who ignores. Their lines
        -- are left out of every read of mine; their whispers, friend requests
        -- and trades to me are refused.
        CREATE TABLE IF NOT EXISTS ignores (
            user_id    INTEGER NOT NULL,
            ignored_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, ignored_id)
        );
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)


def upgrade(db, add_column):
    """The columns added since these tables first shipped, for a database made
    before them. add_column(db, table, column, definition) is init_db()'s
    helper, handed in (a package never imports app.py): it adds a column only
    where it is missing. db.execute only - init_db() runs this inside its one
    migration transaction, which an executescript would commit early."""
    # CHANNELS. World chat shipped before there was more than one channel, so
    # every line already in the table was said in the world one - which is
    # exactly what the default says, so the existing history stays readable
    # rather than becoming a pile of messages belonging to nowhere.
    add_column(db, "chat_messages", "channel", "TEXT NOT NULL DEFAULT 'world'")
    add_column(db, "chat_messages", "target_id", "INTEGER NOT NULL DEFAULT 0")
    add_column(db, "chat_messages", "image_id", "TEXT NOT NULL DEFAULT ''")
    # Which lines show a picture: chat/'s may_see_image() asks it on every picture
    # fetched. After the column it indexes, for the same reason as app.py's
    # idx_sessions_seen - in SCHEMA above it would stop an old database from
    # booting.
    db.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_image ON chat_messages(image_id)")

    # DEFAULTS TO png, WHICH IS WHAT EVERY EXISTING ROW REALLY IS. Stills are
    # now kept as WebP when WebP is smaller, and it usually is by a lot - a
    # photograph that arrives as 265 KB was being stored and re-served as a
    # 1.7 MB PNG, which every single person in the channel then downloaded.
    add_column(db, "chat_images", "format", "TEXT NOT NULL DEFAULT 'png'")

    # The guild a line was said from. Old rows get "", which reads as "no
    # guild" - correct, because before this column there was nothing to show.
    add_column(db, "chat_messages", "guild", "TEXT NOT NULL DEFAULT ''")

    # THE NAME COLOUR THE SPEAKER CHOSE, on each line as a snapshot of
    # users.name_hue (init_db() says what the hue is), for the reason the
    # `guild` column beside it gives: the read is a poll every three seconds
    # and must not grow a join, and a line is what somebody looked like when
    # they said it. NULL on an old line: never chose, drawn as the default.
    add_column(db, "chat_messages", "name_hue", "INTEGER")
