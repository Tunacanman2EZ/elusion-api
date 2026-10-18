"""guilds/services.py - the rules: what a guild may be called, how big it
gets, what founding one costs, the rank ladder, how much history it keeps,
and the answers. Pure: no database, no request."""

# 3 to 12 characters, starting with a letter or a number. Spaces, apostrophes
# and hyphens are allowed inside so real names work - "The Crowned", "Dave's
# Lads" - and nothing else is, so a guild name cannot carry BBCode into a chat
# line or look like another player's username.
#
# TWELVE, DOWN FROM TWENTY-FOUR, and the reason is the nameplate rather than
# the database. The name is about to appear above every member's head, on every
# chat line they write and on the kingdom board; at 24 characters that is a
# banner following somebody around. Twelve fits.
#
# EXISTING LONGER NAMES KEEP WORKING. This pattern gates CREATION and RENAMING,
# not reading - a guild founded under the old rule is not broken by the new one,
# and guild_tag() bounds what it draws. Rename it and it comes under the cap.
# Refusing to load them instead would be a migration that silently deletes
# somebody's guild because a display decision changed.
GUILD_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9 '\-]{2,11}"
MAX_GUILD_NAME = 12


def guild_tag(name):
    """The guild as it appears above a head, in a chat line, on the board.

    THE NAME, UPPERCASED, AND NOTHING ELSE INVENTED. A separate tag field was
    the alternative and it is worse in the way that matters here: it is a
    SECOND thing to moderate. Every rule about what a guild may be called would
    need writing twice, a guild could be called one acceptable thing and tagged
    another, and staff would have two fields to check instead of one. One name,
    shown one way.

    BOUNDED ANYWAY, at MAX_GUILD_NAME, because guilds founded before the cap
    dropped to twelve still exist and their names are up to 24 characters long.
    The cap is enforced on the way in for new names and here for old ones, so
    nothing can put a banner over somebody's head whatever it is called.
    """
    text = str(name or "").strip().upper()
    return text[:MAX_GUILD_NAME]


def folded(name):
    """The name as the unique index holds it, and as a guild is looked up."""
    return str(name).strip().lower()


# FIFTY, which is a number to revisit once there are guilds to watch. The read
# that matters - the roster - is one indexed query and returns every member, so
# this is really a cap on how big that answer gets.
MAX_GUILD_MEMBERS = 50

# How many invitations a guild may have outstanding. Without a cap, inviting
# every account on the server is one loop.
MAX_GUILD_INVITES = 40

# WHAT IT COSTS TO FOUND ONE, and it is DESTROYED rather than paid to anybody
# - the same sink the Kingdom Tax is, and it lands in the same ledger so the
# supply invariant still holds. The point is not the gold: it is that thirty
# empty guilds squatting good names should cost something.
GUILD_FOUND_COST = 5000

# Lowest first. Used by guild_rank_at_least(), and the order is the meaning.
GUILD_RANKS = ("member", "officer", "leader")


def guild_rank_at_least(rank, minimum):
    """True when `rank` sits at or above `minimum` in GUILD_RANKS.

    An unrecognised rank sorts as the lowest, never the highest - the same
    rule role_at_least() follows for staff, and for the same reason.
    """
    if rank not in GUILD_RANKS or minimum not in GUILD_RANKS:
        return False
    return GUILD_RANKS.index(rank) >= GUILD_RANKS.index(minimum)


def founding_split(carried, cost=GUILD_FOUND_COST):
    """(from the carried purse, from the bank) for a founding.

    CARRY FIRST, THEN BANK - the same order reviving uses, and for the same
    reason: gold in the bank is gold you deliberately put somewhere safe, so
    it is spent last."""
    from_carry = min(int(carried), cost)
    return from_carry, cost - from_carry


# THE GUILD'S OWN HISTORY. How much each guild keeps, and how much of it the
# panel is sent. A hundred is months of an active guild; the panel shows the
# newest dozen because it is a glance, not an archive.
GUILD_EVENTS_KEPT = 100
GUILD_ACTIVITY_SHOWN = 12

# Every kind log_guild_event() is called with. test_guildlife.py reads the
# calls in app.py and this package and fails on one that is not listed, so the
# client's words for them cannot silently miss a new one.
GUILD_EVENT_KINDS = (
    "founded", "joined", "left", "invited", "promoted", "demoted",
    "leader", "removed", "renamed",
)


def roster(rows, playing, now, online_window, role_for, name_hue_of):
    """The roster as the panel draws it: name, rank, whether they are on, and
    the character they are playing. `rows` are data.roster_rows(), `playing`
    {user_id: save row} from data.playing_saves(); role_for and name_hue_of
    are app.py's own (how every list shows a person).

    ONLINE IS THE SAME TEST THE FRIENDS LIST USES, from the same column, so
    the two panels cannot disagree about who is playing."""
    out = []
    for row in rows:
        seen = int(row["last_seen_at"] or 0)
        save = playing.get(int(row["id"]))
        out.append({
            "username": row["username"],
            "role": role_for(row),
            "name_hue": name_hue_of(row),
            "rank": row["rank"],
            "joined_at": int(row["joined_at"] or 0),
            "last_seen_at": seen,
            "online": bool(seen and now - seen <= online_window),
            # AREA IS SENT WHETHER OR NOT THEY ARE ON, and the panel shows it
            # only for somebody who is. Where a guildmate logged out is not a
            # secret from their own guild, but "in Field" beside somebody who
            # left three days ago would read as now.
            "character": save["name"] if save is not None else "",
            "class_id": save["class_id"] if save is not None else "",
            "level": int(save["level"] or 1) if save is not None else 0,
            "area": save["area"] if save is not None else "",
        })
    return out


def guild_answer(guild_row, members, activity):
    """A guild as any route returns it."""
    return {
        "name": guild_row["name"],
        # WHAT THE CLIENT DRAWS. Derived here rather than in Godot so the
        # nameplate, the chat line and the board cannot disagree about how a
        # guild is spelled - and so a rule change is one edit on this side
        # instead of three on the other.
        "tag": guild_tag(guild_row["name"]),
        "founded_by": guild_row["founded_by"],
        "created_at": int(guild_row["created_at"] or 0),
        "members": members,
        "size": len(members),
        "capacity": MAX_GUILD_MEMBERS,
        # Members only: every route that returns this is one a member called.
        "activity": activity,
    }
