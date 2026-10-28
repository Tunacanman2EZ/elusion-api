"""minigames/services.py - the rules, pure: which minigames there are, and
how long somebody waits (game 0.93.3)."""

DAY = 24 * 60 * 60

# Every minigame: its id (the area it is played in), its name and the wait
# after going in. The order is the order the game lists them in.
GAMES = {
    "endless": {"name": "The Endless", "wait": DAY},
}


def ready_in(entered_at, wait, now):
    """Seconds until somebody who went in at `entered_at` (0: never) may go
    in again; 0 when they may now."""
    if not entered_at:
        return 0
    return max(0, int(entered_at) + int(wait) - int(now))


def wait_words(seconds):
    """'13h 20m', '45m', 'under a minute' - how a wait is said."""
    seconds = int(seconds)
    if seconds < 60:
        return "under a minute"
    hours, minutes = seconds // 3600, (seconds % 3600) // 60
    if hours and minutes:
        return "%dh %dm" % (hours, minutes)
    if hours:
        return "%dh" % hours
    return "%dm" % minutes
