"""kingdom - the Kingdom Tax board: what the realm has taken, and from whom (a
package since 18 Oct; the route is as it was in app.py).

    GET /api/economy/kingdom   the totals, the breakdown by reason, the top
                               players and guilds - each with its patron's
                               title (0.74.0) - and the caller's own line,
                               with what it has given the crown and the title
                               it is climbing to
    POST /api/kingdom/donate   a gift of gold to the crown (0.74.0): burned,
                               reason donation, for a title on the board

EVERY COIN DESTROYED IS A CONTRIBUTION - the trade tax (trade/), the shop, a
revive, what the dead leave behind, and a gift to the crown - and lusions are
a second column, never added to the gold. DERIVED, NEVER COUNTED: the board is
a read of the two ledgers, and there is no running total anywhere to disagree
with them. A patron's title is derived the same way, from the account's
donation rows (services.title_for()).

THE SHARED HALF IS CACHED for KINGDOM_BOARD_CACHE_SECONDS, because it is the
same for everybody and costs a pass over the ledger; the caller's own line and
guild are read on every request. register() hands back cache_clear(), which
app.py keeps as _kingdom_cache_clear() for the suites.

Built like trade/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL - reads only). It never imports app.py;
register() is handed what it needs. WHAT STAYS IN app.py: the ledgers
themselves (gold_delta() and the lusion grants write them) and
/api/economy/supply, the audit of every coin, which is a dev's tool rather than
a scoreboard.
"""
from .routes import register  # noqa: F401
from .services import KINGDOM_BOARD_CACHE_SECONDS, KINGDOM_BOARD_SIZE  # noqa: F401
