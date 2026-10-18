"""trade - the only place two players' property changes hands, and the kingdom
tax it pays (a package since 18 Oct; the routes are as they were in app.py).

    POST /api/trade/offer      open a trade with a player who is online
    GET  /api/trade            the trade you are in, or how the last one ended,
                               and a bag to re-read if one changed
    POST /api/trade/update     replace your side of the offer
    POST /api/trade/confirm    accept the revision you were shown; the trade
                               runs when both have
    POST /api/trade/cancel     call it off - either side, at any time
    GET  /api/trade/history    your last few finished trades
    GET  /api/staff/trades     every trade an account has been in (mod+)

THE KINGDOM TAKES A CUT OF EVERY TRADE, and the gold is DESTROYED, through
app.py's gold_delta() - that is the only reason it helps. A fee paid to an NPC
moves gold and leaves the supply where it was. What is taxed is what you
RECEIVE, valued at ItemData.value and gold included, so barter is not the
untaxed way round it; it is charged after the swap, so the gold you were just
handed can pay it. The board that shows what the kingdom took is kingdom/.

NO ROUTE NAMES A TRADE. Every one starts from the caller (data.find_open,
data.find_recent - test_ownership.py, O-2).

A FRESH MYTHIC OR PERFECT WAITS TRADE_HOLD_SECONDS before it can change hands
(data.place_hold, data.hold_refusal). The loot take and the shop place the
holds, through app.py's place_trade_hold().

Built like guilds/ and chat/ (ARCHITECTURE.md): routes.py (the edge),
services.py (the rules, pure), data.py (the SQL). It never imports app.py;
register() is handed what it needs. WHAT STAYS IN app.py: the backpack grants
the execution pays with (_take_from_backpack(), _add_to_backpack() - the carry
is step 10), the gold ledger (gold_delta()), what the owner's trade switch
reads as (trades_enabled(); switches/ throws it), which character an account
is playing (_playing_slot(), shared with the nearby list and parties), and the
resync flag (_mark_resync(), _take_resync()), which owner gifts ride too.
"""
from .data import (find_open, find_page, find_recent, hold_refusal, open_count,  # noqa: F401
                   place_hold, record, summary)
from .routes import register  # noqa: F401
from .services import (STAFF_TRADE_STATES, TRADE_CANCELLED_KEPT_SECONDS,  # noqa: F401
                       TRADE_EXPIRY_SECONDS, TRADE_HISTORY_SHOWN, TRADE_HOLD_SECONDS,
                       TRADE_MAX_ITEM_TYPES, held_kind)
