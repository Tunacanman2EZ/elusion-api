"""minigames/services.py - the rules, pure: which minigames there are, how long
somebody waits (game 0.93.3), and what an Endless run could have sent since
its Go (0.93.4)."""

import math

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


# =============================================================================
# WHAT A RUN COULD HAVE SENT (game 0.93.4)
# =============================================================================
# The server cannot see a run. What it can do is hold the kills reported since
# the account's Go to what the run's own schedule - gamedata.json's "endless",
# written by the game's exporter from endlessrun.gd - could have sent by now,
# AT THE BIGGEST PARTY, every stage of it waves as often and as big as the
# stage allows, from the moment of the Go:
#
#   elites  every stage reached so far, ceil(stage_seconds / gap) waves of
#           `wave`, plus ELITES_ALIVE_ALLOWANCE: a party's arena can hold that
#           many alive, so somebody joining a run already going may meet them
#           all at once
#   bosses  one each boss_every stages reached, plus one (the run joined may
#           be at its boss)
#
# STAGES_BELIEVED: no run is believed past it, so a whole day's waiting does
# not make room for a run nobody could survive. The honest limit: a modified
# game could report every elite the schedule allows - one Go a day caps that
# at one long run's worth.

STAGES_BELIEVED = 30


def stages_reached(schedule, seconds):
    """How many stages a run begun at the Go could have reached `seconds`
    after it: the first is reached at once."""
    stage_seconds = max(1, int(schedule.get("stage_seconds", 60) or 60))
    stages = schedule.get("stages") or []
    return max(0, min(len(stages), STAGES_BELIEVED, int(seconds) // stage_seconds + 1))


def elites_ceiling(schedule, seconds):
    """The most elites a run begun at the Go could have sent by now."""
    stage_seconds = max(1, int(schedule.get("stage_seconds", 60) or 60))
    total = 0
    for stage in (schedule.get("stages") or [])[:stages_reached(schedule, seconds)]:
        gap = max(1, int(stage.get("gap", 1) or 1))
        total += math.ceil(stage_seconds / gap) * max(0, int(stage.get("wave", 0) or 0))
    return total + max(0, int(schedule.get("alive_most", 0) or 0))


def bosses_ceiling(schedule, seconds):
    """The most bosses a run begun at the Go could have sent by now."""
    every = max(1, int(schedule.get("boss_every", 5) or 5))
    return stages_reached(schedule, seconds) // every + 1


def sends(schedule, enemy_id):
    """"elite", "boss", or "" for a monster the run never sends."""
    if enemy_id in (schedule.get("bosses") or []):
        return "boss"
    if enemy_id in (schedule.get("elites") or []):
        return "elite"
    return ""
