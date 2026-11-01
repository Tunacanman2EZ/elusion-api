"""minigames/services.py - the rules, pure: which minigames there are, how long
somebody waits (game 0.93.3), and what an Endless run could have sent since
its Go (0.93.4)."""

import math

DAY = 24 * 60 * 60

# Every minigame: its id (the area it is played in), its name and the wait
# after going in. The order is the order the game lists them in.
GAMES = {
    "endless": {"name": "The Endless", "wait": DAY},
    # THE HEALER'S RUN (game 0.99.0, healerrun.gd): the owner, 31 Oct, "this
    # minigame can be played 3 times a day". Three Go's in any day (a rolling
    # one - the oldest of the three frees a run 24 hours after it), and its
    # gold paid at Leave by POST /api/minigames/finish.
    "healerrun": {"name": "The Healer's Run", "wait": DAY, "plays": 3},
}


def plays_of(game):
    """How many Go's a game allows in its wait: 1 unless it says."""
    return int(game.get("plays", 1))


def ready_in_plays(starts, plays, wait, now):
    """Seconds until somebody whose Go's were at `starts` may go in again,
    `plays` allowed in any `wait`; 0 while one is left."""
    recent = sorted(int(at) for at in starts if int(at) > int(now) - int(wait))
    if len(recent) < int(plays):
        return 0
    return max(0, recent[-int(plays)] + int(wait) - int(now))


# =============================================================================
# THE HEALER'S RUN'S GOLD (game 0.99.0)
# =============================================================================
# The owner: "gold should be rewarded on the screen then moved to char when
# hitting leave", 0 to 1000 a run, by how far it got. The game shows the same
# sum as it goes (it is handed healer_pays() at Go), and says at Leave how far
# it got; the server pays from that - held to what the time since the Go
# allows - once a run, in the ledger as "minigame".
#
#   each wave passed   HEALER_WAVE_GOLD by stretch (5 stretches of 5): 600
#   the gate's boss    HEALER_BOSS_GOLD                                  150
#   the goal marker    HEALER_GOAL_GOLD                                  250
#
# THE HONEST LIMIT: a modified game could say it reached the goal. It cannot
# say so sooner than HEALER_MIN_WAVE_SECONDS a wave and HEALER_MIN_BOSS_SECONDS
# for the boss after its Go, nor more than three times a day: 3,000 gold a day
# at the very most.

HEALER_STRETCHES = 5
HEALER_WAVES_PER_STRETCH = 5
HEALER_WAVE_GOLD = (10, 16, 24, 30, 40)
HEALER_BOSS_GOLD = 150
HEALER_GOAL_GOLD = 250
HEALER_MIN_WAVE_SECONDS = 3
HEALER_MIN_BOSS_SECONDS = 15
# A run not finished in this long pays nothing: closed without Leave.
HEALER_RUN_KEPT_SECONDS = 3 * 60 * 60


def healer_waves():
    return HEALER_STRETCHES * HEALER_WAVES_PER_STRETCH


def healer_pays():
    """The table the game counts gold by on screen (handed it at Go)."""
    return {"wave_gold": list(HEALER_WAVE_GOLD), "waves_per_stretch": HEALER_WAVES_PER_STRETCH,
            "boss_gold": HEALER_BOSS_GOLD, "goal_gold": HEALER_GOAL_GOLD,
            "most": healer_gold(healer_waves(), True, True)}


def healer_believed(waves, boss, goal, seconds):
    """How far a run said it got, held to what `seconds` since its Go allow:
    (waves, boss, goal)."""
    waves = max(0, min(int(waves), healer_waves(), int(seconds) // HEALER_MIN_WAVE_SECONDS))
    full = waves == healer_waves()
    boss = bool(boss) and full and int(seconds) >= healer_waves() * HEALER_MIN_WAVE_SECONDS + HEALER_MIN_BOSS_SECONDS
    goal = bool(goal) and boss
    return waves, boss, goal


def healer_gold(waves, boss, goal):
    """The gold for a run that passed `waves` waves, and beat the boss and
    reached the goal marker or not."""
    waves = max(0, min(int(waves), healer_waves()))
    gold = sum(HEALER_WAVE_GOLD[i // HEALER_WAVES_PER_STRETCH] for i in range(waves))
    if waves == healer_waves() and boss:
        gold += HEALER_BOSS_GOLD
        if goal:
            gold += HEALER_GOAL_GOLD
    return gold


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
