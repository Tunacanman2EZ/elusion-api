"""status - a character's live stats: health, mana, stamina, and the level, XP
and gold beside them (a package since 18 Oct; the routes are as they were in
app.py).

    GET  /api/player/status   the HUD's numbers for one character
    PUT  /api/player/status   the game reports its pools; the server keeps
                              what it owns, derives the maxima, and trims any
                              rise it cannot explain

THE CLIENT PROPOSES; THE SERVER DECIDES. Of everything a status write may name
(STATUS_FIELDS), the level, XP and gold are the server's (SERVER_OWNED_STATS)
and the three maxima are derived from class, level and what is worn
(DERIVED_STATS). All of those are answered in `ignored`, never refused: an
honest game sends its whole block and cannot know which fields the server has
taken over. What is left is hp, mana and stamina - the result of combat and
regeneration, which the server does not simulate.

SO EVERY RISE HAS TO BE EARNED. reconcile_heals() compares the pools stored to
the pools claimed: a rise inside what regeneration gives over the time since
the pools were last written is the normal case; a rise past it needs a grant -
a potion drunk, a healer's heal, a revive, a level-up - written by the server
in the last few seconds. Past the tight line (HEAL_ALLOWANCE_MARGIN) it is
logged; past the loose one (HEAL_CLAMP_MARGIN), while HEAL_CLAMP_ENFORCED, it
is trimmed to what was explained. The tunables are app.py's, with their
history, and are asked for on every check.

A DEATH IS COUNTED ONCE: the write that takes a stored hp above zero to zero.
A dead game keeps reporting zero while the death screen is up, and counting a
state rather than the transition would let anybody run the total up by leaving
it open.

Built like saves/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs and hands back status_of(), kept in app.py as status_payload(),
and note_refill(), the one writer of a refill's note, handed to every area
that authorises a pool to rise.
WHAT STAYS IN app.py: the healing tunables and the grant ids every refill
writes, the stat parser and its ceiling, _derived_stats(), and the boot check
that says which catalogue-gated protections are not armed.
"""
from .routes import register  # noqa: F401
from .services import DERIVED_STATS, SERVER_OWNED_STATS, STATUS_FIELDS  # noqa: F401
