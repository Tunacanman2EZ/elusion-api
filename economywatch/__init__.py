"""economywatch - the owner's watch over the economy: where gold comes from
and where it goes, and whether the books still balance (0.74.0).

    GET  /api/owner/economy/flow?days=7   the flow, by UTC day and reason
    GET  /api/owner/economy/canary        the last hourly canary check
    POST /api/owner/economy/canary        check now

The owner's first two of five economy ideas, "max power": "Checking the gold
totals automatically ... Set up once on the server, it would tell you the same
day if gold or lusions appear or vanish without being recorded", and "A daily
report of where gold comes from and where it goes, by source."

DERIVED, NEVER COUNTED, like the kingdom board: the flow is a read of the two
ledgers (every coin gold_delta() moves carries a reason), and the canary is
canary.py itself - its checks, on a read-only connection - so the timer, the
command line and Check now on the GM panel are one set of checks.

Built like kingdom/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL - reads only). It never imports app.py, nor
canary.py: register() is handed what it needs.
"""
from .routes import register  # noqa: F401
from .services import CANARY_STALE_SECONDS, DAYS_DEFAULT, DAYS_MOST  # noqa: F401
