"""events/services.py - the rules of the world's events.

Pure functions: rows and a clock in, answers out. Nothing here reads a
database or a request, so test_events.py can hold every rule on its own.

THE KINDS, and what each one does while it is on:

    xp     every kill it covers gives `amount` times the XP
    loot   every kill it covers drops a loot bag `amount` times as often
    stir   the stirring lands stir `land` for the whole event

WHICH KILLS: `land` 0 is every monster; 1-7 a land's monsters (Element.Type
in the game); -1 (LAND_STIRRING) the monsters of whichever land is stirring
when the kill lands. A stir event names its land, 1-7.

TWO EVENTS OF ONE KIND AT ONCE DO NOT MULTIPLY: the bigger counts. Two double
XP events scheduled over each other by mistake are still double XP, not four
times. They DO multiply with the stirring lands' own XP and loot - an event is
an event, and a stir is the world.
"""

DAY = 86400
HOUR = 3600
KINDS = ("xp", "loot", "stir")
LAND_ANY = 0
LAND_STIRRING = -1
# What an amount may be (xp and loot): 1.25x to 3x.
AMOUNT_LOW = 1.25
AMOUNT_HIGH = 3.0
# How long one may last, and how far ahead one may be set.
SHORTEST = HOUR
LONGEST = 72 * HOUR
FURTHEST = 60 * DAY
# A start this far in the past still counts as "now": the GM's clock and the
# server's are not the same clock.
LATE_START = 5 * 60
# How many may be waiting or on at once.
MOST_WAITING = 20
NAME_LENGTH = 40
# How far ahead the game is told what is coming.
AHEAD = 7 * DAY
NEXT_SHOWN = 5


def _times(value):
    """2.0 -> '2', 1.5 -> '1.5', 1.25 -> '1.25'."""
    text = ("%.2f" % float(value)).rstrip("0").rstrip(".")
    return text


def land_words(land, lands):
    """'every monster', "the fire lands' monsters", "the stirring land's monsters"."""
    land = int(land)
    if land == LAND_ANY:
        return "every monster"
    if land == LAND_STIRRING:
        return "the stirring land's monsters"
    return "the %s lands' monsters" % lands.get(land, "unknown")


def default_name(kind, amount, land, lands):
    """The name an event gets when the GM gives it none."""
    if kind == "stir":
        return "The %s Lands Stir" % str(lands.get(int(land), "unknown")).capitalize()
    lead = "Double" if abs(float(amount) - 2.0) < 1e-9 else ("Triple" if abs(float(amount) - 3.0) < 1e-9
                                                             else _times(amount) + "x")
    return "%s %s" % (lead, "XP" if kind == "xp" else "Loot")


def describe(event, lands):
    """What it does, in one sentence, from its numbers."""
    kind = event["kind"]
    if kind == "xp":
        return "%sx XP from %s." % (_times(event["amount"]), land_words(event["land"], lands))
    if kind == "loot":
        return "Loot bags drop %sx as often from %s." % (_times(event["amount"]), land_words(event["land"], lands))
    return "The %s lands stir for the whole event." % lands.get(int(event["land"]), "unknown")


def validate(payload, now, lands, clean_text):
    """(the event to write, None) or (None, why not). `clean_text` is the
    server's own cleaner for words a person typed (clean_player_text)."""
    if not isinstance(payload, dict):
        return None, "send the event: kind, starts_at, hours, and amount or land"
    kind = payload.get("kind")
    if kind not in KINDS:
        return None, "kind is one of xp, loot, stir"
    land = payload.get("land", LAND_ANY if kind != "stir" else None)
    if isinstance(land, bool) or not isinstance(land, int):
        return None, "land is a whole number: 0 every monster, -1 the stirring land, 1-7 a land"
    if kind == "stir":
        if land not in lands:
            return None, "a stir event names one of the seven lands, 1 to 7"
        amount = 1.0
    else:
        if land != LAND_ANY and land != LAND_STIRRING and land not in lands:
            return None, "land is 0 (every monster), -1 (the stirring land) or 1 to 7"
        amount = payload.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) \
                or not AMOUNT_LOW - 1e-9 <= float(amount) <= AMOUNT_HIGH + 1e-9:
            return None, "amount is a number from %s to %s" % (_times(AMOUNT_LOW), _times(AMOUNT_HIGH))
        amount = round(float(amount), 2)
    starts_at = payload.get("starts_at")
    if isinstance(starts_at, bool) or not isinstance(starts_at, int):
        return None, "starts_at is a unix time, in whole seconds"
    if starts_at < now - LATE_START:
        return None, "starts_at is in the past"
    if starts_at > now + FURTHEST:
        return None, "starts_at is at most %d days ahead" % (FURTHEST // DAY)
    hours = payload.get("hours")
    if isinstance(hours, bool) or not isinstance(hours, int) \
            or not SHORTEST // HOUR <= hours <= LONGEST // HOUR:
        return None, "hours is a whole number from %d to %d" % (SHORTEST // HOUR, LONGEST // HOUR)
    starts_at = max(int(starts_at), int(now)) if starts_at < now else int(starts_at)
    name = clean_text(payload.get("name", ""), NAME_LENGTH) if isinstance(payload.get("name", ""), str) else ""
    event = {"kind": kind, "amount": amount, "land": int(land), "starts_at": starts_at,
             "ends_at": starts_at + hours * HOUR}
    event["name"] = name or default_name(kind, amount, land, lands)
    return event, None


def is_on(event, now):
    return not event.get("cancelled_at") and event["starts_at"] <= now < event["ends_at"]


def public_event(event, lands):
    """One event as the game reads it - the contract."""
    multipliers = {}
    if event["kind"] in ("xp", "loot"):
        multipliers[event["kind"]] = float(event["amount"])
    return {
        "id": int(event["id"]),
        "name": event["name"],
        "kind": event["kind"],
        "description": describe(event, lands),
        "starts_at": int(event["starts_at"]),
        "ends_at": int(event["ends_at"]),
        "land": int(event["land"]),
        "multipliers": multipliers,
    }


def active_and_next(rows, now, lands):
    """The contract's body: {"events": what is on, "next": what is coming
    (the nearest NEXT_SHOWN within AHEAD), "now"}."""
    on = [public_event(e, lands) for e in rows if is_on(e, now)]
    coming = sorted((e for e in rows if not e.get("cancelled_at") and e["starts_at"] > now
                     and e["starts_at"] <= now + AHEAD), key=lambda e: (e["starts_at"], e["id"]))
    return {"events": on, "next": [public_event(e, lands) for e in coming[:NEXT_SHOWN]], "now": int(now)}


def covers(event, element, stirring_land):
    """Whether an xp or loot event covers a kill of a monster of `element`
    while `stirring_land` stirs (0 for none)."""
    land = int(event["land"])
    if land == LAND_ANY:
        return True
    if land == LAND_STIRRING:
        return stirring_land != 0 and int(element) == int(stirring_land)
    return int(element) == land


def kill_scales(rows, now, element, stirring_land):
    """(bag scale, XP scale) the events on now give a kill of a monster of
    `element` - the bigger of each kind, never the product."""
    bags = 1.0
    xp = 1.0
    for event in rows:
        if not is_on(event, now) or not covers(event, element, stirring_land):
            continue
        if event["kind"] == "loot":
            bags = max(bags, float(event["amount"]))
        elif event["kind"] == "xp":
            xp = max(xp, float(event["amount"]))
    return bags, xp


def forced_stir(rows, now):
    """The stir event on now - the one that began last, if two overlap - or
    None."""
    on = [e for e in rows if e["kind"] == "stir" and is_on(e, now)]
    return max(on, key=lambda e: (e["starts_at"], e["id"])) if on else None


def span_words(seconds):
    """'3 hours', '1 hour', '2 days 4 hours', '45 minutes'."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, DAY)
    hours, rest = divmod(rest, HOUR)
    minutes = rest // 60
    parts = []
    if days:
        parts.append("%d day%s" % (days, "" if days == 1 else "s"))
    if hours:
        parts.append("%d hour%s" % (hours, "" if hours == 1 else "s"))
    if not days and not hours:
        parts.append("%d minute%s" % (max(1, minutes), "" if max(1, minutes) == 1 else "s"))
    return " ".join(parts)


def coming_text(event, now, lands):
    return "Coming up: %s, in %s, for %s - %s" % (event["name"], span_words(event["starts_at"] - now),
                                                 span_words(event["ends_at"] - event["starts_at"]),
                                                 describe(event, lands))


def begun_text(event, now, lands):
    return "%s has begun, for %s: %s" % (event["name"], span_words(event["ends_at"] - now), describe(event, lands))


def called_off_text(event, now):
    if event["starts_at"] <= now:
        return "%s has been called off." % event["name"]
    return "%s will not happen after all." % event["name"]
