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
# How long one may last, and how far ahead one may be set: a year (game
# 0.52.0, when the GM panel's calendar became a whole one - "a real calendar
# that updates with the years"; 60 days before). 366 so the same date next
# year is always in, a leap day between or not.
SHORTEST = HOUR
LONGEST = 72 * HOUR
FURTHEST = 366 * DAY
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


# =============================================================================
# GUARDRAILS (game 0.49.0)
# =============================================================================
# The owner's: "don't allow 3x XP + 3x loot + 3x gold all at once in the same
# area". Here the unit is a land's monsters, not an area - a land's monsters
# live in many areas, and the server pays by the monster - and there is no
# gold kind: gold comes out of loot bags, so more loot is more gold.
#
# TWO LAYERS:
#   - when an event is SET, guard() works out the most any one land's
#     monsters could pay at any moment it is on - this event, every event set
#     over it, and the stirring land's own XP and loot as the World tab has
#     them now (worst case: any land may be the one stirring) - and refuses
#     it, saying why, if that is past the ceilings below;
#   - when a kill is PAID, ceiling() holds what it gets to the same ceilings,
#     whatever happened since: the stirring lands' settings may be raised
#     after an event was set.
MOST_XP = 3.0
MOST_LOOT = 3.0
# XP times loot for the same monster: Double XP and Double Loot together on
# the stirring land (2 x 2 x its own 1.5) is 6, and allowed; 3x XP with 2x
# loot is 9, and not.
MOST_TOGETHER = 6.0
_EPS = 1e-9


def ceiling(bags, xp):
    """(bag scale, XP scale) held to the ceilings. Past MOST_TOGETHER, loot
    gives way: it is the one that floods the economy."""
    xp = min(float(xp), MOST_XP)
    bags = min(float(bags), MOST_LOOT)
    if bags * xp > MOST_TOGETHER + _EPS:
        bags = max(1.0, MOST_TOGETHER / xp)
    return bags, xp


def no_stir():
    return {"on": False, "xp": 1.0, "bags": 1.0}


def _could_cover(event, land, stir_on):
    """Whether an xp or loot event could cover land `land`'s monsters - the
    stirring land's event covers any land, if the lands stir at all."""
    covered = int(event["land"])
    if covered == LAND_ANY:
        return True
    if covered == LAND_STIRRING:
        return bool(stir_on)
    return covered == int(land)


def worst_case(candidate, others, stir, lands):
    """For each land, the most its monsters could be paid at any moment the
    candidate is on: [{"land", "name", "xp", "loot", "together", "at",
    "xp_by", "loot_by"}], xp_by and loot_by the names of the events that set
    each (the stirring land's own counted in, never named)."""
    stir = stir or no_stir()
    start, end = int(candidate["starts_at"]), int(candidate["ends_at"])
    others = [e for e in others if not e.get("cancelled_at") and e.get("id") != candidate.get("id")
              and int(e["starts_at"]) < end and int(e["ends_at"]) > start]
    moments = sorted({start} | {int(e["starts_at"]) for e in others if start < int(e["starts_at"]) < end})
    stir_xp = float(stir.get("xp", 1.0)) if stir.get("on") else 1.0
    stir_bags = float(stir.get("bags", 1.0)) if stir.get("on") else 1.0
    out = []
    for land in sorted(lands):
        best = None
        for at in moments:
            on = [candidate] + [e for e in others if int(e["starts_at"]) <= at < int(e["ends_at"])]
            xp, xp_by, loot, loot_by = 1.0, "", 1.0, ""
            for event in on:
                if event["kind"] not in ("xp", "loot") or not _could_cover(event, land, stir.get("on")):
                    continue
                if event["kind"] == "xp" and float(event["amount"]) > xp:
                    xp, xp_by = float(event["amount"]), event["name"]
                elif event["kind"] == "loot" and float(event["amount"]) > loot:
                    loot, loot_by = float(event["amount"]), event["name"]
            row = {"land": int(land), "name": lands[land], "xp": round(xp * stir_xp, 3),
                   "loot": round(loot * stir_bags, 3), "at": at, "xp_by": xp_by, "loot_by": loot_by}
            row["together"] = round(row["xp"] * row["loot"], 3)
            if best is None or (row["together"], row["xp"] + row["loot"]) > (best["together"], best["xp"] + best["loot"]):
                best = row
        out.append(best)
    return out


def _over(row):
    """How far past the ceilings a land's worst moment is (0 within them)."""
    return max(row["xp"] - MOST_XP, row["loot"] - MOST_LOOT, (row["together"] - MOST_TOGETHER) / 2.0, 0.0)


def limits_words():
    return "the most a kill can be is %sx XP, %sx loot and %sx the two together" % (
        _times(MOST_XP), _times(MOST_LOOT), _times(MOST_TOGETHER))


def guard(candidate, others, stir, lands):
    """(why not, worst) - why not is None when the candidate is within the
    ceilings with everything else on at the same time."""
    worst = worst_case(candidate, others, stir, lands)
    if candidate["kind"] not in ("xp", "loot"):
        return None, worst
    breaking = [row for row in worst if _over(row) > _EPS]
    if not breaking:
        return None, worst
    row = max(breaking, key=_over)
    stir = stir or no_stir()
    # WHOSE MONSTERS: if it is only too much with the stirring land's own
    # counted in, it is the stirring land's; else the lands it is too much
    # for, or every monster.
    calm = [r for r in worst_case(candidate, others, dict(stir, xp=1.0, bags=1.0), lands) if _over(r) > _EPS]
    if not calm:
        whose = "the stirring land's monsters"
    elif len(calm) == len(lands):
        whose = "every monster"
    else:
        names = [r["name"] for r in calm]
        whose = "the %s lands' monsters" % (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
    helped = []
    for name in (row["xp_by"], row["loot_by"]):
        if name and name != candidate["name"] and name not in helped:
            helped.append(name)
    stir_parts = []
    if stir.get("on") and float(stir.get("xp", 1.0)) > 1.0 + _EPS:
        stir_parts.append("%sx XP" % _times(stir["xp"]))
    if stir.get("on") and float(stir.get("bags", 1.0)) > 1.0 + _EPS:
        stir_parts.append("%sx loot" % _times(stir["bags"]))
    reasons = []
    if helped:
        reasons.append("with " + " and ".join(helped))
    if stir_parts:
        reasons.append("the stirring land's own %s" % " and ".join(stir_parts))
    parts = []
    if row["xp"] > 1.0 + _EPS:
        parts.append("%sx XP" % _times(row["xp"]))
    if row["loot"] > 1.0 + _EPS:
        parts.append("%sx loot" % _times(row["loot"]))
    paid = " and ".join(parts) + (" (%sx together)" % _times(row["together"]) if len(parts) == 2 else "")
    why = "Too much at once for %s: %s%s - %s." % (
        whose, paid, (", " + ", ".join(reasons)) if reasons else "", limits_words())
    return why + " Make it smaller, cover fewer monsters, or move it.", worst


def warnings(candidate, others, stir, lands):
    """What is allowed but will not do what it says."""
    stir = stir or no_stir()
    said = []
    if candidate["kind"] == "stir" and not stir.get("on"):
        said.append("The stirring lands are switched off, so this will stir nothing until they are on (World tab).")
    if int(candidate.get("land", 0)) == LAND_STIRRING and not stir.get("on"):
        said.append("The stirring lands are switched off, so no land is stirring for this to cover.")
    if candidate["kind"] == "stir":
        clash = [e["name"] for e in others if e["kind"] == "stir" and not e.get("cancelled_at")
                 and e.get("id") != candidate.get("id")
                 and int(e["starts_at"]) < int(candidate["ends_at"]) and int(e["ends_at"]) > int(candidate["starts_at"])]
        if clash:
            said.append("It overlaps %s: while both are on, the one that began last stirs." % " and ".join(clash))
    else:
        # TWO OF A KIND DO NOT ADD UP (game 0.50.1): a kill gets the bigger,
        # never both (kill_scales()), and the game shows only that one - the
        # owner set Double XP twice in a minute and every player saw it twice.
        # Said, not refused: a second, longer one is how an event is made to
        # run on.
        clash = [e["name"] for e in others if e["kind"] == candidate["kind"] and not e.get("cancelled_at")
                 and e.get("id") != candidate.get("id") and _share_monsters(e, candidate)
                 and int(e["starts_at"]) < int(candidate["ends_at"]) and int(e["ends_at"]) > int(candidate["starts_at"])]
        if clash:
            said.append("It overlaps %s: while both are on, a kill gets the bigger %s, never both, and the game "
                        "shows only that one." % (" and ".join(clash), "XP" if candidate["kind"] == "xp" else "loot"))
    return said


def _share_monsters(a, b):
    """Whether two xp or loot events could cover the same monster: yes unless
    they name two different lands. The stirring land's may be either."""
    return not (int(a["land"]) > 0 and int(b["land"]) > 0 and int(a["land"]) != int(b["land"]))


# =============================================================================
# THE DRY RUN'S FORECAST (game 0.49.0)
# =============================================================================
# "If 20 players join during this event, expected XP/gold output range is
# X-Y." From the last two weeks of real play: what each account's kills paid
# in each hour it played (a PLAYER-HOUR), and the gold it earned. The middle
# half of those hours - the 25th to the 75th percentile - is the range; hours
# when an XP or loot event was on are left out, so the usual is usual.
BASELINE_DAYS = 14
# Who the dry run forecasts for when it is not told: the owner's "20 players".
FORECAST_PLAYERS = 20
MOST_FORECAST = 500
# Fewer player-hours than this and the forecast says it is a guess.
FEW_PLAYER_HOURS = 10
LOW_Q = 0.25
HIGH_Q = 0.75


def quantile(values, q):
    """Nearest rank: the value a fraction q of the way up the sorted list."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def baseline(xp_rows, gold_rows, enemy_rows, element_of, event_windows):
    """{"player_hours", "xp": [low, middle, high], "gold": [...], "shares":
    {land: share of kills}} - per player-hour. xp_rows and gold_rows are
    (user, hour, amount), enemy_rows (enemy_id, hour, kills); event_windows
    the (start, end) of the xp and loot events in the time read, whose hours
    are left out of all of it."""
    def busy(hour):
        return any(start < (hour + 1) * HOUR and end > hour * HOUR for start, end in event_windows)

    hours = {}
    for user, hour, xp in xp_rows:
        if not busy(hour):
            hours.setdefault((user, hour), [0, 0])[0] += int(xp)
    for user, hour, gold in gold_rows:
        if not busy(hour):
            hours.setdefault((user, hour), [0, 0])[1] += int(gold)
    xps = [v[0] for v in hours.values()]
    golds = [v[1] for v in hours.values()]
    kills = {}
    total = 0
    for enemy, hour, count in enemy_rows:
        if busy(hour):
            continue
        land = int(element_of(enemy) or 0)
        kills[land] = kills.get(land, 0) + int(count)
        total += int(count)
    return {"player_hours": len(hours),
            "xp": [quantile(xps, LOW_Q), quantile(xps, 0.5), quantile(xps, HIGH_Q)],
            "gold": [quantile(golds, LOW_Q), quantile(golds, 0.5), quantile(golds, HIGH_Q)],
            "shares": {land: (count / float(total)) for land, count in kills.items()} if total else {}}


def share_covered(land, shares, lands, stir_on):
    """The share of play an xp or loot event covers: all of it, one land's,
    or the stirring land's - a seventh of the lands' kills, as near as can be
    said ahead of which land it will be."""
    land = int(land)
    if land == LAND_ANY:
        return 1.0
    if land == LAND_STIRRING:
        return (sum(shares.get(k, 0.0) for k in lands) / float(len(lands))) if stir_on and lands else 0.0
    return float(shares.get(land, 0.0))


def forecast(event, players, base, lands, stir_on):
    """What `players` players playing the whole event would earn, low to high,
    without it and with it."""
    hours = max(0.0, (int(event["ends_at"]) - int(event["starts_at"])) / float(HOUR))
    share = share_covered(event.get("land", 0), base.get("shares", {}), lands, stir_on) \
        if event["kind"] in ("xp", "loot") else 0.0
    gain = (float(event.get("amount", 1.0)) - 1.0) * share
    xp_times = 1.0 + gain if event["kind"] == "xp" else 1.0
    gold_times = 1.0 + gain if event["kind"] == "loot" else 1.0
    scale = int(players) * hours

    def span(rates, times):
        return [int(round(rates[0] * scale * times)), int(round(rates[2] * scale * times))]

    return {"players": int(players), "hours": hours, "player_hours": int(base.get("player_hours", 0)),
            "enough": int(base.get("player_hours", 0)) >= FEW_PLAYER_HOURS,
            "share": round(share, 3), "xp_times": round(xp_times, 3), "gold_times": round(gold_times, 3),
            "xp": {"usual": span(base["xp"], 1.0), "with": span(base["xp"], xp_times)},
            "gold": {"usual": span(base["gold"], 1.0), "with": span(base["gold"], gold_times)},
            "per_player_hour": {"xp": list(base["xp"]), "gold": list(base["gold"])}}


# =============================================================================
# THE EVENT REPORT (game 0.49.0)
# =============================================================================
# "Dashboards that show, for each event: XP/hour and gold/hour before, during
# and after; player count online against time; which events actually move the
# needle on an empty world." For each event that began in the last
# REPORT_DAYS: three windows of its length - the one before it, its own, the
# one after - and in each, the kills and the XP and gold they paid per hour,
# who played, who signed up, and how many were online (online_samples).
SAMPLE_SECONDS = 300
# How often one server process counts who is online (the bucket keeps the
# most it saw), and how long the counts are kept.
SAMPLE_EVERY = 60
SAMPLES_KEPT = 120 * DAY
REPORT_DAYS = 30
CHART_DAYS = 7


def count_online(rows, left_out_names):
    """(everyone, players) from online_now()'s rows: players are the
    player rank, the owner and co-owners left out."""
    names = {str(n).casefold() for n in left_out_names if n}
    users = {}
    for user_id, role, username in rows:
        users[user_id] = role == "player" and str(username).casefold() not in names
    return len(users), sum(1 for player in users.values() if player)


def ended_at(event):
    """When it stopped: its end, or when it was called off if sooner."""
    stop = int(event["ends_at"])
    if event.get("cancelled_at"):
        stop = min(stop, int(event["cancelled_at"]))
    return stop


def windows(event, now):
    """{"before", "during", "after"}: (start, end) each, the length of the
    event as it ran; "after" None until it has begun, cut at now."""
    start = int(event["starts_at"])
    stop = ended_at(event)
    length = max(1, stop - start)
    return {"before": (start - length, start),
            "during": (start, min(stop, now)),
            "after": (stop, min(stop + length, now)) if stop < now else None}


def online_figures(sampled, start, end, first_bucket):
    """(average players online, the most at once, average everyone) over
    [start, end), or None before the counting began. A five minutes with no
    row is nobody: a game that is open polls."""
    if first_bucket is None or start // SAMPLE_SECONDS < first_bucket or end <= start:
        return None
    buckets = range(start // SAMPLE_SECONDS, (end + SAMPLE_SECONDS - 1) // SAMPLE_SECONDS)
    if not buckets:
        return None
    players = [sampled.get(b, (0, 0))[1] for b in buckets]
    everyone = [sampled.get(b, (0, 0))[0] for b in buckets]
    return {"players": round(sum(players) / float(len(buckets)), 2), "peak": max(players),
            "everyone": round(sum(everyone) / float(len(buckets)), 2)}


def window_figures(start, end, kills, xp, killers, gold, new_players, online, xp_known):
    hours = max(0.0, (end - start) / float(HOUR))
    per = (lambda value: int(round(value / hours)) if hours > 0 else 0)
    return {"start": int(start), "end": int(end), "hours": round(hours, 2),
            "kills": kills if xp_known else None, "xp": xp if xp_known else None,
            "xp_per_hour": per(xp) if xp_known else None, "kills_per_hour": per(kills) if xp_known else None,
            "gold": gold, "gold_per_hour": per(gold), "played": killers if xp_known else None,
            "new_players": new_players, "online": online}


def lift(report):
    """Players online on average during, less before - None if either was
    not counted."""
    before = (report["windows"].get("before") or {}).get("online")
    during = (report["windows"].get("during") or {}).get("online")
    if not before or not during:
        return None
    return round(during["players"] - before["players"], 2)


def verdict(report):
    """One sentence: did it bring anyone?"""
    change = report.get("lift")
    during = (report["windows"].get("during") or {})
    before = (report["windows"].get("before") or {})
    if change is None:
        return "Online not counted for it - the counting began later."
    if during.get("online", {}).get("peak", 0) == 0 and before.get("online", {}).get("peak", 0) == 0:
        return "Nobody was online before it or during it."
    if change > 0.05:
        return "Brought players: %s more online on average (%s before, %s during)." % (
            _times(change), _times(before["online"]["players"]), _times(during["online"]["players"]))
    if change < -0.05:
        return "Fewer online during it than before (%s before, %s during)." % (
            _times(before["online"]["players"]), _times(during["online"]["players"]))
    return "No change in who was online (%s before and during)." % _times(during["online"]["players"])


def by_kind(reports):
    """[{kind, events, counted, lift}]: across the finished events of each
    kind that were counted, how many more players were online during them on
    average - which kind moves the needle. One still on is half a story."""
    out = []
    for kind in KINDS:
        mine = [r for r in reports if r["kind"] == kind]
        counted = [r["lift"] for r in mine if r.get("lift") is not None and r.get("state") != "on"]
        out.append({"kind": kind, "events": len(mine), "counted": len(counted),
                    "lift": round(sum(counted) / float(len(counted)), 2) if counted else None})
    return out
