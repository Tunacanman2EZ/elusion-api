"""ownertools - the owner's tools: gold, a level, a skill and an item for
testing or for a player, the gifts ledger's report, a player's save history
and putting it back, and moving players (a package since 19 Oct; the routes
are as they were in app.py).

    POST /api/staff/gold        gold into your own purse or bank, or out of it
    POST /api/staff/level       a character's level - yours, or the one a
                                player is playing
    POST /api/staff/skill       your own character's skill levels
    POST /api/staff/grant       an item to yourself, or into the character a
                                player is playing
    GET  /api/staff/gifts       what the owner and the co-owners have given
                                away, and what it comes to
    GET  /api/staff/snapshots   a player's save history
    POST /api/staff/rollback    a player's character put back to a snapshot
    POST /api/staff/teleport    a player, or everyone, moved to a place (dev
                                and up; everyone, the owner only)
    POST /api/teleport/ack      the game saying a teleport was carried out

THE OWNER'S, AND SAID SO ON EVERY ONE. Every route but the acknowledgement is
behind an owner check (require_owner, which answers a 404 rather than a 403)
or, for moving a player, require_role("dev"); the ones that change a
character, a purse or the bag ask for the password again. Each writes its line
in the moderation log in its own transaction, and a change made to SOMEBODY
ELSE is on their record ("give", "player_level", "rollback"), with a snapshot
taken first so it can be undone, and their game signed out or flagged so it
reloads what the server holds. A co-owner may touch anyone but the owner.

EVERY GIFT IS COUNTED. Gold and items the owner gives - to a player or to
himself - are rows in owner_gifts, written by app.py's ledger helpers in the
gift's own transaction; the report here reads them and sets them beside the
gold in the economy.

Built like kill/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py, and why: taking a snapshot and what one
holds (the save route takes them too), the gift's flag and its reading
(_mark_gift(), _gifts_in() - the trade resync reads them on every poll), the
gifts ledger's writers (the boot backfill writes with them), the teleport
queue (the broadcast poll hands a game its teleport), the grants into the
backpack, the gold ledger, and who may touch whom.
"""
from .routes import register  # noqa: F401
from .services import DEBUG_GOLD_MAX, ROLLBACK_REASON, STAFF_LEVEL_MAX, STAFF_SKILL_ORDER  # noqa: F401
