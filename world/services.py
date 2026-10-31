"""world/services.py - the rules of the stirring lands: the seven lands, the
owner's settings and what each may be, how a land is chosen when the hunting
ties, what a stir says, what the game is told, and what a kill of a stirring
land's monster is paid. Pure: no database, no request."""
import json
import random

# The server_settings row the owner's settings live in.
WORLD_KEY = "world"
# Element.Type 1 to 7 in the game (src/shared/element.gd), named as the game
# names them. ELECTRIC and POISON (8, 9) are creatures' colours, not lands.
LANDS = {1: "dark", 2: "light", 3: "ice", 4: "wind", 5: "earth", 6: "fire", 7: "water"}
WORLD_HUNT_WINDOW = 24 * 3600
WORLD_RECENT_TICKS = 8
# Shipped ON, with five of the six effects: the owner's choice of 3-hour
# stirs, harder hits, more loot and resistance counting for more, and - to be
# felt - more health and speed. More XP is there, switched off, for the owner
# to try.
WORLD_DEFAULTS = {
    "on": True,
    "hours": 3,
    "hit_on": True, "hit_percent": 25,
    "health_on": True, "health_percent": 50,
    "speed_on": True, "speed_percent": 15,
    "bags_on": True, "bags_times": 1.5,
    "resist_on": True, "resist_times": 1.5,
    "xp_on": False, "xp_percent": 25,
    # THE SWELL (game 0.97.1; the owner: "lets make the tests into a real
    # feature that changes with the land"): while a land stirs, each of its
    # monsters in the Big Field has (swell_size - 1) more of its kind beside
    # it - real ones, shared and paid. Shipped on, at twice.
    "swell_on": True, "swell_size": 2,
}
# The most a swell may be: each monster and three more. The books, the kill
# ceiling and killwatch allow for it (swell_spots_for(), SWELL_GRACE_SECONDS).
SWELL_MOST = 4
# How long after its stir ends a land's swell may still be met: its monsters
# leave once out of a fight, and kills report a moment later. presence.py's
# STIR_GRACE_SECONDS is the same hour.
SWELL_GRACE_SECONDS = 3600
# What each number may be: (lowest, highest, whole numbers only).
WORLD_LIMITS = {
    "hours": (1, 24, True),
    "hit_percent": (0, 100, True),
    "health_percent": (0, 200, True),
    "speed_percent": (0, 50, True),
    "bags_times": (1.0, 3.0, False),
    "resist_times": (1.0, 2.0, False),
    "xp_percent": (0, 100, True),
    "swell_size": (2, SWELL_MOST, True),
}
WORLD_SWITCHES = ("on", "hit_on", "health_on", "speed_on", "bags_on", "resist_on", "xp_on", "swell_on")


def settings_from(raw):
    """The owner's settings from the stored row: WORLD_DEFAULTS where none
    were ever saved (`raw` None), and OFF where what was saved cannot be
    read. A saved value out of its range is held to it; one of the wrong
    kind is the default."""
    if raw is None:
        return dict(WORLD_DEFAULTS)
    try:
        stored = json.loads(raw)
    except (ValueError, TypeError):
        stored = None
    if not isinstance(stored, dict):
        return dict(WORLD_DEFAULTS, on=False)
    out = dict(WORLD_DEFAULTS)
    for key in WORLD_SWITCHES:
        if isinstance(stored.get(key), bool):
            out[key] = stored[key]
    for key, (low, high, whole) in WORLD_LIMITS.items():
        value = stored.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = min(max(value, low), high)
        out[key] = int(round(value)) if whole else round(float(value), 2)
    return out


def changed_settings(current, payload):
    """(`current` with the owner's `payload` applied, None) - or (None, the
    words of the 400) for an unknown setting or a value of the wrong kind or
    out of its range. Nothing is applied unless all of it may be."""
    changed = dict(current)
    for key, value in payload.items():
        if key in WORLD_SWITCHES:
            if not isinstance(value, bool):
                return None, "%s is true or false" % key
            changed[key] = value
        elif key in WORLD_LIMITS:
            low, high, whole = WORLD_LIMITS[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or (whole and float(value) != int(value)) or not low <= value <= high:
                return None, "%s is %s from %g to %g" % (key, "a whole number" if whole else "a number",
                                                          low, high)
            changed[key] = int(value) if whole else round(float(value), 2)
        else:
            return None, "%s is not a setting of the stirring lands" % key
    return changed, None


def row_of(row):
    """A world_ticks row as every answer shows it, or None."""
    if row is None:
        return None
    return {"tick": int(row["tick"]), "element": int(row["element"]),
            "name": LANDS.get(int(row["element"]), ""), "started_at": int(row["started_at"]),
            "ends_at": int(row["ends_at"]), "chosen_by": row["chosen_by"] or ""}


def chooser_for(tick):
    """THE SAME TICK ALWAYS PICKS THE SAME LAND, whoever asks: a tie is
    broken by a Random seeded with the tick number."""
    return random.Random(tick * 7919 + 104729)


def least_hunted(candidates, hunts, chooser):
    """The least hunted of `candidates`, a tie settled by `chooser`."""
    least = min(hunts[e] for e in candidates)
    return chooser.choice([e for e in candidates if hunts[e] == least])


def stir_hours(started_at, ends_at):
    """A stir's length in whole hours, for its announcement - never under
    one."""
    return max(1, int(round((int(ends_at) - int(started_at)) / 3600.0)))


def stir_text(element, settings, hours):
    """What the world says when a land stirs, from the effects that are on."""
    name = LANDS.get(int(element), "unknown")
    does = []
    if settings["hit_on"] and settings["hit_percent"] > 0:
        does.append("hit %d%% harder" % settings["hit_percent"])
    if settings["health_on"] and settings["health_percent"] > 0:
        does.append("have %d%% more health" % settings["health_percent"])
    if settings["speed_on"] and settings["speed_percent"] > 0:
        does.append("move %d%% faster" % settings["speed_percent"])
    if settings["bags_on"] and settings["bags_times"] > 1.0:
        does.append("drop loot %sx as often" % ("%g" % settings["bags_times"]))
    if settings["xp_on"] and settings["xp_percent"] > 0:
        does.append("give %d%% more XP" % settings["xp_percent"])
    text = "The %s lands stir for %d hour%s" % (name, hours, "" if hours == 1 else "s")
    if does:
        text += ": %s monsters %s" % (name, ", ".join(does[:-1]) + (" and " if len(does) > 1 else "") + does[-1])
    if settings["resist_on"] and settings["resist_times"] > 1.0:
        text += (", and " if does else ": ") + "%s resistance counts %sx" % (name, "%g" % settings["resist_times"])
    if swell_of(settings) > 1:
        text += ". In the Big Field its monsters swell %dx" % swell_of(settings)
    return text + "."


def swell_of(settings):
    """How many of each stirring monster stand in the Big Field: the size,
    or 1 with the swell off."""
    return int(settings["swell_size"]) if settings.get("swell_on") else 1


def public(row, settings, now):
    """The world as the game needs it, on the broadcast poll, for the stir
    `row` (row_of()): which land, until when, and the numbers the game
    applies itself."""
    return {
        "on": True,
        "tick": row["tick"],
        "element": row["element"],
        "name": row["name"],
        "started_at": row["started_at"],
        "ends_at": row["ends_at"],
        "hit": settings["hit_percent"] / 100.0 if settings["hit_on"] else 0.0,
        "health": settings["health_percent"] / 100.0 if settings["health_on"] else 0.0,
        "speed": settings["speed_percent"] / 100.0 if settings["speed_on"] else 0.0,
        "resist": settings["resist_times"] if settings["resist_on"] else 1.0,
        "bags": settings["bags_times"] if settings["bags_on"] else 1.0,
        "xp": settings["xp_percent"] / 100.0 if settings["xp_on"] else 0.0,
        # THE SWELL: how many of each of its monsters the Big Field holds (1:
        # none added). A game from before 0.97.1 ignores it.
        "swell": swell_of(settings),
        # The server's clock, so the game counts down to ends_at on it rather
        # than on a computer clock that may be minutes out.
        "now": int(now),
    }


def stirred_kill(settings):
    """(bag scale, XP scale) for a kill of the stirring land's own monster."""
    return (float(settings["bags_times"]) if settings["bags_on"] else 1.0,
            1.0 + (settings["xp_percent"] / 100.0 if settings["xp_on"] else 0.0))
