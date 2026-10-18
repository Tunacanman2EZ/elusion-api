"""moderation/services.py - the rules: how long a mod may ban or mute for, what a
report may be about and how long a closed one is kept, how a report about a
player is keyed, and what a sanction's log line says. Pure: no database, no
request.

THE TUNABLES A SUITE TURNS DOWN stay in app.py and are asked for on every use:
REPORTS_PER_HOUR (test_chatsafety), REPORT_LINES_PER_PLAYER (test_chatsafety)
and MUTE_MAX_MINUTES_MOD (test_moderation, which reads the staff powers page
off it too)."""
import re

# How long a mod may BAN someone for. Anything longer, and anything permanent,
# needs a dev or the owner.
#
# (It said "mute" until somebody read it beside the sanction ladder. There was
# no mute in this server then - the ladder was kick, then a bounded ban, then a
# permanent one - and a comment naming a feature nobody built is how SECURITY.md
# nearly shipped a moderator power that did not exist. Same class of bug as
# api.gd's "characterhud.gd answers it with an immediate heartbeat()". The chat
# mute came later, with limits of its own: MUTE_MAX_MINUTES below, and app.py's
# MUTE_MAX_MINUTES_MOD.)
#
# The point is not that thirty days is special. It is that a permanent removal
# and a timeout are different decisions, and the person having a bad night at
# 2am should only be able to make the reversible one.
MAX_MOD_BAN_DAYS = 30

# The longest chat mute, for dev and the owner. A mod's is app.py's
# MUTE_MAX_MINUTES_MOD - at most a day; longer is a ban's job, and a ban has a
# rank ladder of its own.
MUTE_MAX_MINUTES = 30 * 24 * 60

REPORT_REASONS = ("spam", "harassment", "hate", "cheating", "other")

# CLOSED REPORTS ARE KEPT THIS LONG, then dropped the next time a report is
# filed or closed. What staff decided stays in staff_actions for good (one
# "report" line per close); this table only holds the evidence a question
# about that decision would need, and 90 days is longer than any mute or
# temporary ban a mod can give.
REPORT_KEEP_SECONDS = 90 * 24 * 60 * 60

# ONE CARD PER REPORTED PLAYER on the Reports tab, worst first. Twenty lines
# from one spammer are one thing to do, not twenty. (How many of their lines a
# card shows is app.py's REPORT_LINES_PER_PLAYER.)
REPORT_PLAYERS_SHOWN = 50

# How many lines the per-line list - the shape every build before the
# per-player cards reads - carries at most.
REPORT_LINES_SHOWN = 100

# The two ways a report is closed.
REPORT_OUTCOMES = ("dismissed", "actioned")

TOO_MANY_REPORTS = {"error": "Too Many Requests",
                    "message": "You have sent a lot of reports this hour. Staff have them - "
                               "try again later."}


def player_report_key(reported_id, reporter_id):
    """The message_id a report about a PLAYER is filed under: NEGATIVE, one per
    reporter and player, which no chat line ever has. So one person reporting
    one player again is not a second report while the first is open, and
    re-opens it once it was closed."""
    return -((int(reported_id) << 32) | int(reporter_id))


def where_seen(area):
    """"in <area>" for the staff card, from an area id - or "in the world"
    for anything that is not one."""
    area = str(area or "").strip().lower()
    return "in %s" % area.replace("_", " ") if re.match(r"^[a-z0-9_]{1,64}$", area) else "in the world"


def closed_words(lines):
    """What a sanction's log line adds when it closed reports: the log says
    why they left the Reports tab."""
    if lines <= 0:
        return ""
    return " - closed %d reported line%s" % (lines, "" if lines == 1 else "s")


def ban_words(days, reason):
    """A ban's line in the log: "permanent: <reason>" or "<n> days: <reason>"."""
    return "permanent: %s" % reason if days is None else "%d days: %s" % (days, reason)


def fold_lines(rows, most):
    """Report rows, newest first, folded into one group per reported line -
    [(message_id, [rows, newest first])], at most `most` lines."""
    grouped = {}
    order = []
    for row in rows:
        key = int(row["message_id"])
        if key not in grouped:
            if len(order) >= most:
                continue
            order.append(key)
            grouped[key] = []
        grouped[key].append(row)
    return [(key, grouped[key]) for key in order]


def reason_counts(rows):
    """{reason: how many} over a line's reports."""
    reasons = {}
    for row in rows:
        reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
    return reasons
