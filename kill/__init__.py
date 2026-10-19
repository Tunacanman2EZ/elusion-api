"""kill - a monster dies: the client says which, and the server decides what
that was worth (a package since 18 Oct; the route is as it was in app.py).

    POST /api/combat/kill   report a kill and receive its rewards: the XP and
                            any level it brings, the attack XP, the loot bag,
                            and the kill's record - in one transaction

THE CLIENT REPORTS WHICH ENEMY DIED, AND NOTHING ELSE. Before this route, the
game's BaseEnemy._die() granted the XP, rolled the loot and spawned the bag on
the player's own machine, and a modified game could award itself every pet in
the game. Now the server decides what the enemy is worth (the catalogue),
rolls the loot with its own SystemRandom - which the client cannot observe,
predict or re-run - applies the level-ups and commits the lot before the game
learns any of it.

WHAT IT CANNOT DO is tell a real kill from a claimed one: the server does not
know where anything is. So a claim is held to two limits that compose - a
token bucket on the RATE of kills, and a ceiling on the COUNT of one monster
that could have respawned lately (services.py has the arithmetic for both) -
and a monster that cannot be killed for profit (the large poison slime, which
splits) pays nothing. The fuller answer is presence.py's books, which watch a
fight instead of taking its word (SECURITY.md) - and since 0.57.0, with the
owner's receipts switch on, a kill is paid only against the books' own count
of that death (services.RECEIPT_SECONDS): until the books have it, the answer
is 202, "not yet", and the game asks again.

ONE TRANSACTION, on purpose, for everything a kill pays: the level, the XP and
the bucket in one statement, the attack XP through the skills' grant, the
level-up's refill noted for the healing check, and the kill's records. A kill
is never banked without its record, and a record never exists for a kill that
was rolled back. The loot bag is stored after (loot/'s create_bag(), which
commits it), with a mythic's notice to everyone online.

Built like switches/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py, and is handed in: the character's maxima
(_derived_stats()), the skills' XP grant, the stirring lands' multipliers and
the broadcast line, each shared with other areas. The loot bag's making is
loot/'s, and the level-up's refill note status/'s (note_refill()), both handed
in through app.py.
"""
from .routes import register  # noqa: F401
from .data import record_kill  # noqa: F401
from .services import (KILL_BUCKET_CAPACITY, KILL_LOG_RETENTION_SECONDS, KILL_RESPAWN_FLOOR_SECONDS,  # noqa: F401
                       KILL_TOKENS_PER_SECOND, KILL_WINDOW_SECONDS, MYTHIC_BROADCAST_KIND, RECEIPT_BUILD,
                       RECEIPT_RETRY_MS, RECEIPT_SECONDS, mythic_find_text)
