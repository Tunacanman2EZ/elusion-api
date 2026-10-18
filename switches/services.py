"""switches/services.py - the rules: how long the owner may hold the door open
before the server closes, and what the world is told when it does. Pure: no
database, no request. (What each switch reads as when its row is missing or
broken is app.py's - maintenance_state(), pvp_enabled() and the rest - because
the gates that ask on every request are app.py's.)"""

# Long enough that every client actually in the world gets at least one
# heartbeat inside the window, which is what makes "everyone saved" true rather
# than hopeful. ONLINE_WINDOW_SECONDS is the yardstick for "is anyone there".
MAINTENANCE_DEFAULT_GRACE_SECONDS = 60

# A grace window is a promise to wait, and an hour is the longest promise this
# endpoint will make. Past that the owner wants a scheduled restart, not a
# switch someone will forget is on.
MAINTENANCE_MAX_GRACE_SECONDS = 3600


def grace(raw):
    """The grace window asked for, in whole seconds from 0 to
    MAINTENANCE_MAX_GRACE_SECONDS - or None for something that is not a number
    at all, which the route refuses rather than guesses at."""
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return max(0, min(seconds, MAINTENANCE_MAX_GRACE_SECONDS))


def closing_notice(message, grace_seconds, limit):
    """
    The line the world is told as the server closes: the owner's message and
    the countdown, in at most `limit` characters.

    THE COUNTDOWN IS NEVER THE PART CUT. post_broadcast() trims the whole line
    to MAX_BROADCAST_LENGTH, and the owner's message may already be 200 - so
    a long message used to push "(closing in 60s - ...)" off the end, and the
    players lost the one thing in the notice they had to act on. The message
    is what gives way now, with an ellipsis to say so.
    """
    tail = (" (closing in %ds - your progress is being saved)" % grace_seconds) if grace_seconds > 0 \
        else " (closing now)"
    room = limit - len(tail)
    said = message if len(message) <= room else message[:room - 3].rstrip() + "..."
    return said + tail
