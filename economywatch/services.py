"""economywatch/services.py - the rules: the flow's window, and how a pile of
(day, reason) rows becomes a report by day and by reason. Pure: no database, no
request, no clock but the one handed in."""
import time

DAY = 86400
# The owner's "a daily report": a week unless asked for more, three months at
# most - a quarter of a ledger is as far back as one screen can say anything
# about, and the bound is what keeps one read cheap.
DAYS_DEFAULT = 7
DAYS_MOST = 90

# WHEN A PAST DAY STOPS CHANGING. A ledger row's `at` is the moment it is
# written, so yesterday's rows are all in by midnight - give or take a
# transaction that began a moment before it. Ten minutes past midnight a day
# is SETTLED: the flow reads it once, keeps it, and from then on reads only
# the days that are not, which is today. A week's report is then one small
# read of today's rows, however long the ledger grows.
SETTLE_SECONDS = 600

# HOW OLD THE LAST HOURLY CHECK MAY BE before the panel says the timer looks
# stopped: two hours and a margin, so one slow or skipped run is not an alarm
# and two missed in a row are.
CANARY_STALE_SECONDS = 2 * 3600 + 15 * 60


def first_day(now, days):
    """The UTC day number the window starts on: today and the `days` - 1
    before it."""
    return int(now) // DAY - (int(days) - 1)


def last_settled_day(now):
    """The newest UTC day number whose rows can no longer change."""
    return (int(now) - SETTLE_SECONDS) // DAY - 1


def day_name(day_number):
    """A UTC day number as the panel prints it: "2026-10-22"."""
    return time.strftime("%Y-%m-%d", time.gmtime(int(day_number) * DAY))


def flow(rows, first, days):
    """The report for one currency, from data.flow_rows():

        {"days": [one per day, oldest first, every day there even with nothing:
                  {"day", "start", "minted", "burned", "net",
                   "reasons": {reason: {"minted", "burned"}}}],
         "reasons": [one per reason, the busiest first:
                     {"reason", "minted", "burned", "net", "rows"}],
         "minted", "burned", "net"}

    MINTED AND BURNED ARE KEPT APART, never netted inside a reason: a reason
    that mints and burns in one day (an owner's grant, then the revive it paid
    for) would otherwise read as nothing happening, which is the one thing a
    report about where gold goes must not say."""
    by_day = {}
    by_reason = {}
    for day, reason, minted, burned, count in rows:
        if day < first or day >= first + days:
            continue
        line = by_day.setdefault(day, {})
        cell = line.setdefault(reason, {"minted": 0, "burned": 0})
        cell["minted"] += int(minted)
        cell["burned"] += int(burned)
        total = by_reason.setdefault(reason, {"reason": reason, "minted": 0, "burned": 0, "rows": 0})
        total["minted"] += int(minted)
        total["burned"] += int(burned)
        total["rows"] += int(count)
    out_days = []
    for day in range(first, first + days):
        reasons = by_day.get(day, {})
        minted = sum(cell["minted"] for cell in reasons.values())
        burned = sum(cell["burned"] for cell in reasons.values())
        out_days.append({"day": day_name(day), "start": day * DAY, "minted": minted, "burned": burned,
                         "net": minted - burned,
                         "reasons": {name: dict(reasons[name]) for name in sorted(reasons)}})
    out_reasons = []
    for total in by_reason.values():
        total["net"] = total["minted"] - total["burned"]
        out_reasons.append(total)
    # BUSIEST FIRST: what moved the most gold, whichever way, is what the
    # owner wants to read first. Ties by name, so the order never shuffles.
    out_reasons.sort(key=lambda r: (-(r["minted"] + r["burned"]), r["reason"]))
    minted = sum(d["minted"] for d in out_days)
    burned = sum(d["burned"] for d in out_days)
    return {"days": out_days, "reasons": out_reasons, "minted": minted, "burned": burned, "net": minted - burned}


def canary_answer(report, now):
    """What GET /api/owner/economy/canary answers for the last status file
    (None: none yet): the report, how old it is, and whether that is older
    than an hourly timer should ever leave it."""
    if report is None:
        return {"checked_at": None, "ok": None, "failures": [], "checks": [], "age_seconds": None,
                "stale": None}
    out = dict(report)
    age = max(0, int(now) - int(report["checked_at"]))
    out["age_seconds"] = age
    out["stale"] = age > CANARY_STALE_SECONDS
    return out
