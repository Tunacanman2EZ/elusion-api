"""chat/services.py - the rules: how long a line is, how long chat keeps,
how fast an account may speak, which channels there are, what a picture may be
and how often, and the answers. Pure: no database, no request - the one
exception being Pillow, which turns bytes into a picture here or not at all."""
import hashlib
import io
import re

try:
    from PIL import Image, ImageSequence
    PILLOW_AVAILABLE = True
except ImportError:  # pragma: no cover - the server is built with Pillow
    Image = None
    ImageSequence = None
    PILLOW_AVAILABLE = False


# =============================================================================
# WORLD CHAT
# =============================================================================
# One public channel that everybody logged in can read and write. The three
# things that make chat expensive - a length cap, a flood limit, and a way to
# take a line back down - are all here, because a chat channel without them is
# a megaphone handed to whoever types fastest.

# The same 200 as a broadcast. Long enough for a sentence, short enough that no
# single message can push everything else off a player's screen.
MAX_CHAT_LENGTH = 200

# A day. Chat is a conversation, not a record: scrollback from last Tuesday is
# of no use to anyone reading now, and keeping it is keeping a transcript of
# everything every player has ever said. Pruned on write, like broadcasts.
CHAT_RETENTION_SECONDS = 24 * 60 * 60

# The most one poll returns. A client that has been away all day gets the tail
# of the conversation, not the whole day of it.
CHAT_PAGE_LIMIT = 50

# HOW LONG A DELETION KEEPS BEING ANNOUNCED. See the chat_deletions table.
#
# It has to cover the gap between one poll and the next with room to spare, and
# it does not have to cover anything else: chatpanel.gd starts from the tail when
# the window opens, so a client away longer than this is not holding the line it
# would be told about. The chat poll is every 3 seconds, so two minutes is forty
# polls of margin - enough for a stalled request, a paused game or a laptop lid,
# and short enough that the table stays a handful of rows.
#
# BOUNDED BY DESIGN, not by a sweeper job: rows past the window are dropped on
# the next delete, the same way prune() works.
CHAT_DELETION_WINDOW_SECONDS = 120

# THE FLOOD BUCKET. Eight messages in hand, refilling at one every two seconds.
#
# The burst matters as much as the rate: a real person answering three people
# at once types three lines in a few seconds and must not be throttled for it,
# while a script holding the channel open needs a sustained rate, and half a
# message per second is not one.
CHAT_BUCKET_CAPACITY = 8.0
CHAT_TOKENS_PER_SECOND = 0.5

# 'guild' WAS DECLARED HERE BEFORE GUILDS EXISTED, so that the day they arrived
# would be one branch in the write check rather than a change to every client,
# cursor and tab. That day came: guild chat is live, gated only on being in a
# guild. The client took longer - its Guild tab went on saying "Guilds are not
# in the game yet." and never asked, until a player typed "oi bruv" into it and
# nothing happened. 'party' joined in game 0.42.0: a party's own room, stamped
# with its id like a guild line, so a line said in one party never shows in
# another.
CHAT_CHANNELS = ("world", "private", "friends", "guild", "party")
CHAT_DEFAULT_CHANNEL = "world"

# WHAT GUILD CHAT SAYS TO SOMEBODY WITH NO GUILD - reading it, or writing to it.
# One sentence, because the chat window shows whichever arrived last, and two
# wordings of the same fact read as two different problems. It says where to
# go, not only what is missing.
GUILD_CHAT_NO_GUILD = "You are not in a guild yet. Open Guild to found one, or answer an invite."

# WHAT PARTY CHAT SAYS TO SOMEBODY WITH NO PARTY, read or written - one
# sentence, as GUILD_CHAT_NO_GUILD is, saying where to go.
PARTY_CHAT_NO_PARTY = "You are not in a party. Open Party to invite somebody, or answer an invite."


def refilled(tokens, last_chat_at_ms, now_ms):
    """The bucket now: refill first, then spend - the same shape as the kill
    bucket, including the min() that stops a never-used account arriving with
    decades of credit."""
    elapsed_seconds = max(int(now_ms) - int(last_chat_at_ms or 0), 0) / 1000.0
    return min(CHAT_BUCKET_CAPACITY, float(tokens) + elapsed_seconds * CHAT_TOKENS_PER_SECOND)


def too_fast(tokens):
    """The 429 for a bucket below one message, or None.

    429 and a Retry-After, not a 400: nothing is wrong with the message, it
    simply arrived too soon, and an honest client can wait and resend."""
    if tokens >= 1.0:
        return None
    wait = int((1.0 - tokens) / CHAT_TOKENS_PER_SECOND) + 1
    return (
        {
            "error": "Too Many Requests",
            "message": "Messages are arriving faster than one every %g seconds."
                       % (1.0 / CHAT_TOKENS_PER_SECOND),
        },
        429,
        {"Retry-After": str(wait)},
    )


def message_answer(row, guild_tag, name_hue_of):
    """One row as the client reads it. Same field names as a broadcast."""
    keys = row.keys()
    return {
        "id": int(row["id"]),
        "by": row["username"],
        "role": row["role"],
        "body": row["body"],
        "at": int(row["created_at"]),
        "channel": row["channel"] if "channel" in keys else "world",
        # Empty for a line that is only words, which is nearly all of them.
        "image": row["image_id"] if "image_id" in keys else "",
        # The guild they were in when they said it. "" for no guild, and for
        # every line written before the column existed.
        "guild": row["guild"] if "guild" in keys else "",
        "guild_tag": guild_tag(row["guild"]) if "guild" in keys and row["guild"] else "",
        # The colour they had chosen when they said it. None for "never chose
        # one", and for every line written before the column existed.
        "name_hue": name_hue_of(row),
    }


# =============================================================================
# PICTURES IN CHAT - THE RELAY
# =============================================================================
# A player pastes a link, or drops a file; the SERVER decides whether it is
# really a picture, and stores the bytes. Everyone else is shown the server's
# copy. (The fetch of a link is app.py's - _fetch_image_bytes(), with every hop
# checked against the server's own network - and is handed in.)
#
# WHAT THAT BUYS, and it is the whole reason this exists rather than the two
# lines it would take to put the raw URL in the message body:
#
#   - NOBODY'S IP GOES ANYWHERE. A link rendered client-side is a fetch every
#     viewer performs on command, and the person who posted it is holding the
#     log. One relay, by the server, once.
#   - WHAT WAS MODERATED IS WHAT STAYS. A URL can serve one picture to the mod
#     who approved it and another to everyone afterwards. A stored copy cannot.
#   - IT IS ACTUALLY A PICTURE. Pillow decodes it here, so a client is never
#     handed bytes that merely claimed to be an image.
#   - ANIMATION WORKS AT ALL. See sheet_from_animation().
#
# WHAT IT DOES NOT BUY: this is not a content filter. It stops a link being a
# weapon; it does not stop somebody posting something unpleasant. That is what
# /api/chat/delete and a ban are for.

# Eight megabytes in, which is a long GIF or a photograph straight off a
# phone, and past which somebody is not sharing a picture.
#
# THIS IS THE INPUT CAP, NOT THE STORED SIZE. What is kept is bounded far
# harder and separately - IMAGE_MAX_SIDE for a still, IMAGE_MAX_FRAMES and
# IMAGE_MAX_FRAME_SIDE for an animation - so raising this costs decode time
# and buffered memory, not disk. It went up from four because four was
# refusing ordinary GIFs, and a picture that bounces is worse than one that
# takes an extra second.
IMAGE_MAX_BYTES = 8 * 1024 * 1024

# The longest side a stored still may have. Anything bigger is scaled down:
# chat draws these a couple of hundred pixels wide and storing a 6000px
# original would be paying for detail nobody will ever see.
IMAGE_MAX_SIDE = 1024

# Animation is bounded harder, because the cost is per frame in both storage
# and the size of the spritesheet the client has to hold.
IMAGE_MAX_FRAMES = 48
IMAGE_MAX_FRAME_SIDE = 256
IMAGE_SHEET_COLUMNS = 8

# A decompression bomb is a 200-byte file that decodes to a billion pixels.
# Pillow has a ceiling of its own; this one is ours and is far lower.
IMAGE_MAX_DECODED_PIXELS = 40 * 1024 * 1024

# One relay per account per this many seconds. This is the anti-flood limit on
# the act of handing the server a picture at all, and it applies everywhere.
IMAGE_COOLDOWN_SECONDS = 8

# ONE PICTURE IN WORLD CHAT PER HALF HOUR.
#
# A DIFFERENT KIND OF LIMIT FROM THE ONE ABOVE, which is why it is a second
# number rather than a bigger version of the first. IMAGE_COOLDOWN_SECONDS is
# about load: it stops one account making the server fetch and decode twenty
# files at once. This is about the room: world chat is every player at once,
# and a picture there is unskippable for all of them in a way a line of text
# is not.
#
# WHERE IT IS CHECKED MATTERS. Not at upload - that route does not know which
# channel the picture is headed for, and somebody uploading a picture to
# whisper to one friend has done nothing to world. It is checked and stamped
# in /api/chat/send, where the channel is finally known.
#
# ONLY THE OWNER IS EXEMPT. Not mods, not dev: a rate limit that staff can
# ignore is one players notice, and the exemption exists so the person running
# the server can post an announcement or check that pictures still work
# without waiting half an hour, not as a rank perk.
WORLD_IMAGE_COOLDOWN_SECONDS = 30 * 60

# The whole store. Oldest out first when it is reached, so a chat that has
# been running for a year cannot fill the disk.
IMAGE_STORE_MAX_BYTES = 128 * 1024 * 1024


def sheet_from_animation(source):
    """
    Flatten an animation into one PNG spritesheet plus how to play it.

    GODOT CANNOT DECODE A GIF. It can play a spritesheet, and has been doing
    that for every enemy in the game since the beginning - so the animation is
    taken apart here, where Pillow already is, and the client is handed
    something it already knows what to do with.
    """
    frames = []
    delay_ms = 0
    for frame in ImageSequence.Iterator(source):
        delay_ms = max(delay_ms, int(frame.info.get("duration", 0) or 0))
        picture = frame.convert("RGBA")
        picture.thumbnail((IMAGE_MAX_FRAME_SIDE, IMAGE_MAX_FRAME_SIDE))
        frames.append(picture)
        if len(frames) >= IMAGE_MAX_FRAMES:
            break

    if not frames:
        return None

    # EVERY CELL THE SAME SIZE, taken from the first frame. A GIF may hand back
    # frames of different sizes (partial-update frames are a real thing), and a
    # grid whose cells vary is not a grid.
    cell_w, cell_h = frames[0].size
    columns = min(IMAGE_SHEET_COLUMNS, len(frames))
    rows = (len(frames) + columns - 1) // columns

    sheet = Image.new("RGBA", (cell_w * columns, cell_h * rows), (0, 0, 0, 0))
    for index, picture in enumerate(frames):
        if picture.size != (cell_w, cell_h):
            fitted = Image.new("RGBA", (cell_w, cell_h), (0, 0, 0, 0))
            fitted.paste(picture, (0, 0))
            picture = fitted
        sheet.paste(picture, ((index % columns) * cell_w, (index // columns) * cell_h))

    return {
        "sheet": sheet,
        "kind": "animated",
        "width": cell_w,
        "height": cell_h,
        "frames": len(frames),
        "columns": columns,
        # 100ms is what a browser uses for a GIF that does not say, and a GIF
        # that does not say is usually an old one that assumed it.
        "frame_ms": delay_ms if delay_ms > 0 else 100,
    }


def decode_relayed_image(raw):
    """(record, None) or (None, reason). Never trusts what the bytes claim."""
    if not PILLOW_AVAILABLE:
        return None, "this server cannot handle pictures (Pillow is not installed)"

    # A CEILING BEFORE ANY DECODING HAPPENS. Pillow has one of its own that is
    # far higher; this is the one that matters for a 200-byte file claiming to
    # be forty thousand pixels square.
    Image.MAX_IMAGE_PIXELS = IMAGE_MAX_DECODED_PIXELS

    try:
        # verify() is a structural check and it CONSUMES the object, which is
        # why the bytes are opened twice. Cheap, and it rejects a truncated or
        # hand-made file before any pixels are read.
        Image.open(io.BytesIO(raw)).verify()
        source = Image.open(io.BytesIO(raw))
    except Exception:
        return None, "that file is not a picture this server can read"

    try:
        if source.width * source.height > IMAGE_MAX_DECODED_PIXELS:
            return None, "that picture is too large to open"

        if getattr(source, "n_frames", 1) > 1:
            record = sheet_from_animation(source)
            if record is None:
                return None, "that animation had no frames"
        else:
            still = source.convert("RGBA")
            still.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
            record = {
                "sheet": still,
                "kind": "still",
                "width": still.width,
                "height": still.height,
                "frames": 1,
                "columns": 1,
                "frame_ms": 0,
            }
    except Exception:
        return None, "that picture could not be decoded"

    # RE-ENCODED, ALWAYS. What leaves here is a file this server wrote, not
    # the one that arrived - so whatever else was in it (a comment block, a
    # second image, a payload for some other program's parser) is gone.
    #
    # PNG FOR ANIMATION, THE SMALLER OF THE TWO FOR A STILL. The frames of an
    # animation are capped at 256px and are usually flat or pixel art, which
    # is what PNG is best at and what lossy compression is worst at; a still
    # is as likely to be a photograph, where PNG is several times larger than
    # it needs to be. Measured per picture rather than guessed, because the
    # guess is wrong often enough to matter - see the same argument on the
    # client, which does this before sending.
    sheet = record["sheet"]
    del record["sheet"]

    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG", optimize=True)
    record["data"] = buffer.getvalue()
    record["format"] = "png"

    if record["kind"] == "still":
        try:
            lossy = io.BytesIO()
            sheet.save(lossy, format="WEBP", quality=88, method=4)
            if 0 < lossy.tell() < len(record["data"]):
                record["data"] = lossy.getvalue()
                record["format"] = "webp"
        except Exception:
            # No WebP in this Pillow build, or it refused this picture. PNG
            # stands - a bigger file is not a failure.
            pass

    record["bytes"] = len(record["data"])
    record["id"] = hashlib.sha256(record["data"]).hexdigest()
    return record, None


def image_answer(row):
    """The metadata a client needs in order to draw it."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "width": int(row["width"]),
        "height": int(row["height"]),
        "frames": int(row["frames"]),
        "columns": int(row["columns"]),
        "frame_ms": int(row["frame_ms"]),
    }


def describe_wait(seconds):
    """"22 minutes", "40 seconds" - the sentence a person can act on."""
    seconds = max(0, int(seconds))
    if seconds >= 120:
        return "%d minutes" % ((seconds + 59) // 60)
    if seconds >= 60:
        return "a minute"
    return "%d seconds" % seconds


def world_wait_left(last_world_image_at, now):
    """Seconds still to wait before a picture may go to world: 0 for never."""
    last = int(last_world_image_at or 0)
    if last <= 0:
        return 0
    return max(0, WORLD_IMAGE_COOLDOWN_SECONDS - (now - last))


def picture_label(name):
    """An uploaded file's name as the `source` column keeps it.

    THE NAME IS A LABEL, NEVER A PATH. It is written to the `source` column so
    a moderator looking at a reported picture can see it was "upload:
    screenshot.png" rather than a link, and it is used for nothing else - not
    to open anything, not to decide the format. What the file claims to be has
    no bearing on what decode_relayed_image() makes of the bytes."""
    name = re.sub(r"[^A-Za-z0-9._ -]", "", str(name or "").strip())
    # Runs of dots collapsed and leading ones dropped, so "../../etc/passwd"
    # cannot survive as "......etcpasswd". Nothing opens this string, so that
    # is cosmetic rather than a defence - but a moderator reading the source
    # column should not have to work out whether what they are looking at was
    # an attack, and a label that still looks like one invites somebody to
    # later "helpfully" use it as a path.
    name = re.sub(r"\.{2,}", ".", name).lstrip(". ")[:120]
    return "upload:" + name if name else "upload"
