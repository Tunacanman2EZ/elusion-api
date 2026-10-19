"""staffdesk/services.py - the rules: how big a page of the staff list or the
log is, what the list can be filtered to, every kind the moderation log holds
and the named sets of them, which kinds are private to the people who can act
on the account, and how a search is matched. Pure: no database, no request."""

STAFF_PAGE_DEFAULT = 50
STAFF_PAGE_MAX = 200

# Every filter is a WHERE clause, never a Python filter over a fetched page:
# filtering after the LIMIT hands back a page of three when fifty matched
# further down, and "more" would be a lie in both directions.
STAFF_LIST_SHOWS = ("all", "online", "banned", "staff")

# EVERY ACTION NAME log_staff_action() IS CALLED WITH. The log's filter checks
# against it, so a typo answers 400 rather than an empty page that reads as
# "nobody has ever been banned", and the route hands it to the client so the
# panel's dropdown is not a second copy. test_moderation.py reads every
# log_staff_action() call in app.py and in every package, and fails on a name
# missing from here.
STAFF_ACTION_KINDS = (
    "ban", "unban", "kick", "warn", "note", "role",
    "grant", "level", "skill", "teleport", "chat_delete", "mute", "unmute", "report",
    "guild_rename", "guild_disband",
    "maintenance", "minbuild", "pvp", "trade",
    "give", "rollback",
    # 0.21.0: somebody else's level set, and the co-owners' switch.
    "player_level", "coowners",
    # 0.46.0: the stirring lands' settings, and a land stirred by hand.
    "world",
    # 0.48.0: an event scheduled or called off (events/).
    "event",
    # 0.57.0: the kill receipts' switch (switches/).
    "receipts",
)

# NAMED SETS OF KINDS the log can be asked for by one name (?action=moderation).
#
# "moderation" IS WHAT THE LOG AND A PLAYER'S RECORD OPEN ON: what staff did
# about players and what players said. Left out are the server switches
# (maintenance, minbuild, pvp) and the testing tools (grant, level, skill, teleport) - the
# owner granting himself twelve swords while testing a shop is not something
# a mod looking for "has anybody warned this player" should scroll past.
# "Everything" is still one choice away; nothing is hidden from anybody who
# could read it before.
STAFF_ACTION_GROUPS = {
    # "give" and "rollback" ARE IN IT, unlike "grant": they are done to a
    # player's character, not to the owner's own, and "has this player been
    # rolled back before" is exactly what a mod reading their record asks.
    "moderation": ("ban", "unban", "kick", "warn", "note", "role", "chat_delete",
                   "mute", "unmute", "report", "guild_rename", "guild_disband",
                   "give", "rollback", "player_level"),
}

# WRITTEN FOR THE PEOPLE WHO CAN ACT ON THE ACCOUNT, NOT FOR THE ACCOUNT.
#
# A note or a warning is an opinion about a person, kept so the next member of
# staff to meet them does not start from nothing. It is read under the rule
# that already guards IP addresses - can_act_on(), strictly above - so a mod
# reads the notes on players and not the notes on other mods, and nobody ever
# reads the notes about themselves. The player never sees either: no route a
# player can reach reads staff_actions at all.
#
# A ban is not private. The banned player is told why, and every mod can
# already see ban state on the list, so the rest of the log is open to staff.
STAFF_PRIVATE_KINDS = ("note", "warn")

# The same ceiling as a ban reason, for the same reader.
STAFF_NOTE_MAX = 500

# What the account list counts against each name, so a repeat offender stands
# out on the list before anybody opens their record.
STAFF_RECORD_KINDS = ("ban", "kick", "mute", "warn", "note")

# How much of an account's history the account view shows: its last login
# attempts, its staff history, its live sessions.
RECENT_SHOWN = 20


def like_contains(text):
    """
    A LIKE pattern matching `text` anywhere, with LIKE's own wildcards escaped.

    `_` is a wildcard to LIKE and a legal username character, so an unescaped
    search for "a_b" also finds "axb" - on a server where half the names have
    an underscore in them.
    """
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def visible_tally(tally, actionable):
    """An account's record counts as this viewer may see them.

    The private kinds are counted under their own rule - see
    STAFF_PRIVATE_KINDS. A count is not the text, but "3 warnings" on another
    mod's row is the notes about them in summary."""
    tally = dict(tally)
    if not actionable:
        for kind in STAFF_PRIVATE_KINDS:
            tally.pop(kind, None)
    return tally


def log_entry(row):
    """A staff_actions row as the log shows it."""
    return {
        "id": int(row["id"]),
        "at": int(row["created_at"]),
        "by": row["actor_name"],
        "action": row["action"],
        "target": row["target_name"],
        "detail": row["detail"],
    }


def one_line(text):
    """One line. The panel shows a note as a row in a list, and a pasted block
    with newlines in it would be a row that swallows the rows under it."""
    return " ".join(str(text).split())
