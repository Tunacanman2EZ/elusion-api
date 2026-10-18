"""skills/services.py - the rules: the ceiling a skills write is held to, and
how much XP a training report may cash. Pure: no database, no request."""


def capped(parsed, max_level, max_xp):
    """
    A skills write's rows - (user_id, slot, skill_id, level, xp) - held to the
    ceiling, and the names of the skills that were over it: (rows, hits).

    CLAMPED RATHER THAN REJECTED, so a single over-cap skill never discards the
    whole (otherwise honest) sync.
    """
    rows, hits = [], []
    for (uid, s, name, level, xp) in parsed:
        if level > max_level or xp > max_xp:
            hits.append(name)
            level = min(level, max_level)
            xp = min(xp, max_xp)
        rows.append((uid, s, name, level, xp))
    return rows, hits


def budget(now_ms, last_ms, ceiling):
    """The seconds of training a report may cash: the time since the last one,
    never below nothing and never above `ceiling` - a fresh character
    (last_train_at 0) or one back from hours offline gets at most the ceiling,
    never the whole gap."""
    elapsed_s = (now_ms - last_ms) / 1000.0
    return max(0.0, min(elapsed_s, ceiling))


def earnable(reported, per_second, elapsed_s):
    """The raw XP a report is held to: what it says, or what is physically
    earnable at `per_second` in `elapsed_s`, whichever is less."""
    allowed = per_second * elapsed_s
    return min(float(reported), allowed)
