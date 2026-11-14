"""minigames/services.py - the rules, pure: which minigames there are, how long
somebody waits (game 0.93.3), what an Endless run could have sent since its Go
(0.93.4), and a Forge run's (0.120.0)."""

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
    # THE FORGE (game 0.120.0, forgerun.gd): the owner, 4 Nov, "Rewards +
    # daily wait" - once a day, as the Endless; every fire sprite slain pays
    # its own loot into the character's chest (loot only), held to what the
    # run's schedule could have sent (FORGE below); and the time a run held
    # is kept, the best of it (`times`, POST /api/minigames/best).
    "forge": {"name": "The Forge", "wait": DAY, "times": True},
    # CUT THE CLOTH (game 0.131.0, cloth/): the owner, 5 Nov, "add the other
    # stuff now" - rewards and a daily limit through the server. Three runs a
    # day, as the Healer's Run, and gold for every round cleared (CLOTH
    # below), paid at Leave by POST /api/minigames/finish.
    "cloth": {"name": "Cut the Cloth", "wait": DAY, "plays": 3},
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
            "most": healer_gold(healer_waves(), True, True), "tiers": healer_tier_pays()}


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


# THREE TIERS, A BOSS EACH (game 0.100.0). The owner: "lets progress tiers
# after round 8 to the next difficulty diff boss": every 8 waves end with a
# boss of a different land, beaten it the next tier starts harder, and after
# the third boss the goal marker. And "up to 5000 gold for passing phase 2":
# 0 to 5000 a run, most of it past the second boss -
#
#   each wave passed   HEALER_TIER_WAVE_GOLD by tier (3 tiers of 8):  4280
#   each boss beaten   HEALER_TIER_BOSS_GOLD, the first, second, third: 6420
#   the goal marker    HEALER_TIER_GOAL_GOLD                          9300
#
# - 700 by the second boss, the other 19,300 in the third tier. Since game
# 0.104.0 (the owner: "t3 needs to reward up to 20,000 gold high stakes hi
# rewards") - it was 100 a wave, 1500 the third boss and 2000 the goal, 5000
# in all.
#
# A game says it played the tiers by sending `bosses` (how many beaten, 0-3)
# at Leave; one that sends `boss` (true or false) is from before 0.100.0 and
# is paid by the table above, so a game not yet updated is paid what it shows.
# The same limits on time: HEALER_MIN_WAVE_SECONDS a wave and
# HEALER_MIN_BOSS_SECONDS a boss since its Go - so at the very most 60,000
# gold a day from it (three runs).

HEALER_TIERS = 3
HEALER_WAVES_PER_TIER = 8
HEALER_TIER_WAVE_GOLD = (10, 25, 500)
HEALER_TIER_BOSS_GOLD = (120, 300, 6000)
HEALER_TIER_GOAL_GOLD = 9300


def healer_tier_waves():
    return HEALER_TIERS * HEALER_WAVES_PER_TIER


def healer_tier_pays():
    """The tiers' table, handed to the game at Go beside the old one."""
    return {"wave_gold": list(HEALER_TIER_WAVE_GOLD), "waves_per_tier": HEALER_WAVES_PER_TIER,
            "boss_gold": list(HEALER_TIER_BOSS_GOLD), "goal_gold": HEALER_TIER_GOAL_GOLD,
            "most": healer_tier_gold(healer_tier_waves(), HEALER_TIERS, True)}


def healer_tier_believed(waves, bosses, goal, seconds):
    """How far a tiered run said it got, held to what `seconds` since its Go
    allow: (waves, bosses, goal). A boss counts only once its tier's waves
    are passed, and only with the time for those waves and every boss so far."""
    seconds = int(seconds)
    waves = max(0, min(int(waves), healer_tier_waves(), seconds // HEALER_MIN_WAVE_SECONDS))
    bosses = max(0, min(int(bosses), HEALER_TIERS, waves // HEALER_WAVES_PER_TIER))
    while bosses > 0 and seconds < bosses * (HEALER_WAVES_PER_TIER * HEALER_MIN_WAVE_SECONDS
                                             + HEALER_MIN_BOSS_SECONDS):
        bosses -= 1
    goal = bool(goal) and bosses == HEALER_TIERS
    return waves, bosses, goal


def healer_tier_gold(waves, bosses, goal):
    """The gold for a tiered run that passed `waves` waves, beat `bosses`
    bosses, and reached the goal marker or not."""
    waves = max(0, min(int(waves), healer_tier_waves()))
    bosses = max(0, min(int(bosses), HEALER_TIERS, waves // HEALER_WAVES_PER_TIER))
    gold = sum(HEALER_TIER_WAVE_GOLD[i // HEALER_WAVES_PER_TIER] for i in range(waves))
    gold += sum(HEALER_TIER_BOSS_GOLD[:bosses])
    if goal and bosses == HEALER_TIERS:
        gold += HEALER_TIER_GOAL_GOLD
    return gold


# =============================================================================
# CUT THE CLOTH'S GOLD (game 0.131.0)
# =============================================================================
# Every round cleared pays: CLOTH_ROUND_GOLD for rounds 1-10 (5,050 for all
# ten, the Plague Mother's rounds 5 and 10 the most), then CLOTH_ENDLESS_GOLD
# for each Endless round and CLOTH_ENDLESS_BOSS_GOLD for each of its bosses
# (every CLOTH_BOSS_EVERY-th). The game counts the same sum on screen (it is
# handed cloth_pays() at Go) and says at Leave how many rounds it cleared.
#
# THE HONEST LIMIT: a modified game could say it cleared more. It cannot say
# so faster than CLOTH_MIN_ROUND_SECONDS a round since its Go, nor more than
# CLOTH_ROUNDS_MOST rounds (24,250 gold), nor more than three runs a day.

CLOTH_ROUND_GOLD = (100, 150, 200, 300, 600, 400, 500, 600, 700, 1500)
CLOTH_ENDLESS_GOLD = 800
CLOTH_ENDLESS_BOSS_GOLD = 1600
CLOTH_BOSS_EVERY = 5
CLOTH_MIN_ROUND_SECONDS = 15
CLOTH_ROUNDS_MOST = 30


def cloth_round_gold(which):
    """What clearing round `which` (from 1) pays."""
    if 1 <= which <= len(CLOTH_ROUND_GOLD):
        return CLOTH_ROUND_GOLD[which - 1]
    return CLOTH_ENDLESS_BOSS_GOLD if which % CLOTH_BOSS_EVERY == 0 else CLOTH_ENDLESS_GOLD


def cloth_gold(rounds):
    """The gold for a run that cleared `rounds` rounds."""
    rounds = max(0, min(int(rounds), CLOTH_ROUNDS_MOST))
    return sum(cloth_round_gold(which) for which in range(1, rounds + 1))


def cloth_believed(rounds, seconds):
    """How many rounds a run said it cleared, held to what `seconds` since its
    Go allow."""
    return max(0, min(int(rounds), CLOTH_ROUNDS_MOST, int(seconds) // CLOTH_MIN_ROUND_SECONDS))


def cloth_pays():
    """The table the game counts gold by on screen (handed it at Go)."""
    return {"rounds": list(CLOTH_ROUND_GOLD), "endless": CLOTH_ENDLESS_GOLD,
            "endless_boss": CLOTH_ENDLESS_BOSS_GOLD, "most": cloth_gold(CLOTH_ROUNDS_MOST)}


def pays_for(game_id):
    """What a run of `game_id` is handed at Go: its own table."""
    return cloth_pays() if game_id == "cloth" else healer_pays()


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
    """"elite", "boss", "sprite" (the Forge's), or "" for a monster the run
    never sends."""
    if enemy_id in (schedule.get("bosses") or []):
        return "boss"
    if enemy_id in (schedule.get("elites") or []):
        return "elite"
    if enemy_id in (schedule.get("sprites") or []):
        return "sprite"
    return ""


# What a refusal calls each kind, many of them.
KIND_WORDS = {"boss": "bosses", "elite": "elites", "sprite": "fire sprites"}


# =============================================================================
# WHAT A FORGE RUN COULD HAVE SENT (game 0.120.0)
# =============================================================================
# The Forge's fire sprites step out of the lava's edges every spawn gap, a few
# at a time, both quicker after every flood (forgerun.gd spawn_gap(),
# spawn_count()). gamedata.json's "forge", written by the game's exporter from
# forgerun.gd's schedule(), says it AT ITS FASTEST - every flood the moment the
# heat allows, no ice essence cooling it:
#
#   phases            one a flood (0-7): how long it lasts (the heat's fill
#                     and the flood), the spawn gap and how many each spawn
#   volcano           every slice gone: its gap and count, believed for
#                     volcano_believed seconds (everyone burns, harder each
#                     tick, and falls)
#   alive_most        the most standing at once, met all together
#
# One run's worth is the most a run begun at the Go could have sent by now
# (forge_run_sprites()). A run that ended is begun again on the island, so
# past one whole run the ceiling goes on at a whole run's average - back to
# back, which only a run cut short could beat by starting slow - up to
# FORGE_RUNS_BELIEVED runs a day. THE HONEST LIMIT: a modified game could
# report all of them; one Go a day caps it there.

FORGE_RUNS_BELIEVED = 6
# The longest a run's time is believed, whatever it says (an hour).
FORGE_BEST_MOST = 60 * 60

# THE FORGE'S PETS AND MYTHICS (game 0.121.0). The owner, of a pet out of the
# Forge: "hmm i got a pet", then "one could farm lusions in these events not
# sure i like that" - a duplicate pet turns into lusions - and "lets fix pets
# and mythics". Every fire sprite rolls its whole Field table, and a run sends
# hundreds: at the Field's 1 in 216 a 200-sprite run won a pet 60% of the
# time, and a day at the ceiling about 20 (and a mythic 8% of days). So in the
# Forge each "one in N" is FORGE_RARE_SCALE times longer, and the pets won
# since the Go stop at FORGE_PETS_A_DAY: a pet stays a moment, and the Field
# stays where fire sprite pets are hunted. Gold, gear and materials as ever.
FORGE_RARE_SCALE = 10
FORGE_PETS_A_DAY = 1


def _spawns(span, phase):
    gap = max(0.1, float(phase.get("gap", 1) or 1))
    count = max(0, int(phase.get("count", 0) or 0))
    return (math.ceil(float(span) / gap) + 1) * count if span > 0 else 0


def forge_run_seconds(schedule):
    """How long one run at its fastest lasts: every phase, and the volcano
    for as long as it is believed."""
    phases = sum(max(0.0, float(p.get("seconds", 0) or 0)) for p in (schedule.get("phases") or []))
    return phases + max(0, int(schedule.get("volcano_believed", 0) or 0))


def forge_run_sprites(schedule, seconds):
    """The most fire sprites one run begun at the Go could have sent in its
    first `seconds`."""
    left = max(0.0, float(seconds))
    total = 0
    for phase in schedule.get("phases") or []:
        if left <= 0:
            return total
        span = min(left, max(0.0, float(phase.get("seconds", 0) or 0)))
        total += _spawns(span, phase)
        left -= span
    volcano = schedule.get("volcano") or {}
    if left > 0 and volcano:
        total += _spawns(min(left, max(0, int(schedule.get("volcano_believed", 0) or 0))), volcano)
    return total


def forge_ceiling(schedule, seconds):
    """The most fire sprites the day's runs could have sent `seconds` after
    the Go - see above."""
    alive = max(0, int(schedule.get("alive_most", 0) or 0))
    length = forge_run_seconds(schedule)
    if length <= 0:
        return alive
    whole = forge_run_sprites(schedule, length)
    seconds = max(0, int(seconds))
    worth = forge_run_sprites(schedule, seconds) if seconds <= length else int(whole * seconds / length)
    return min(worth, whole * FORGE_RUNS_BELIEVED) + alive


def best_believed(seconds_said, since_go):
    """How long a run is believed to have held: what it says, no longer than
    the time since the Go, nor FORGE_BEST_MOST."""
    return max(0, min(int(seconds_said), int(since_go), FORGE_BEST_MOST))


# =============================================================================
# A DAY'S MINIGAME GOLD (14 Nov)
# =============================================================================
# The owner: "lets fix issues in security", choosing a cap of 100,000. A
# minigame is played on the player's machine and the server believes its
# result as far as the time since its Go allows (SECURITY_NOTES E-22): a
# modified game could claim the best run every time - 20,000 a Healer's Run
# and 24,250 a Cut the Cloth, three of each a day, and every coin the
# Endless's and the Forge's chests could roll. So one allowance an account
# across all of them, in any 24 hours: the gold paid at Leave and the coins
# rolled into a chest, counted together. A run past it is paid what is left;
# a chest past it rolls its gear and no coins. A good honest day is well under
# it; a perfect day at both gold games is 132,750 before any chest.
#
# ELUSION_MINIGAME_GOLD_DAY sets it on a server (envfile.KNOWN): a whole
# number, 0 for no cap. Unset or not a number is GOLD_A_DAY - a typo never
# switches the cap off.
GOLD_A_DAY = 100000


def gold_a_day(raw):
    """The cap from the setting's text."""
    text = str(raw if raw is not None else "").strip()
    if text.isascii() and text.isdigit():
        return int(text)
    return GOLD_A_DAY


def gold_allowed(want, paid_today, cap):
    """(paid, held back): `want` gold, of which only what is left of the day's
    `cap` after `paid_today`. A cap of 0 is none."""
    want = max(0, int(want))
    if int(cap) <= 0:
        return want, 0
    paid = min(want, max(0, int(cap) - int(paid_today)))
    return paid, want - paid


def coins_within(contents, worth, left):
    """A chest's roll held to `left` gold: (contents kept, gold kept, gold
    held back). `worth(item_id)` is the gold one of an item cashes into, 0 for
    anything that is not gold - gear, materials, pets and lusions pass
    untouched. Coins are kept in the order rolled, as many of each as fit;
    `left` None is no cap."""
    kept, gold, held = [], 0, 0
    room = None if left is None else max(0, int(left))
    for entry in contents:
        per = int(worth(str(entry.get("item_id", ""))) or 0)
        quantity = int(entry.get("quantity", 0))
        if per <= 0:
            kept.append(entry)
            continue
        fit = quantity if room is None else min(quantity, room // per)
        if fit > 0:
            kept.append(dict(entry, quantity=fit))
            gold += fit * per
            if room is not None:
                room -= fit * per
        held += (quantity - fit) * per
    return kept, gold, held

