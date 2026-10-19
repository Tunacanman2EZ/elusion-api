"""death - the death screen's two exits (a package since 18 Oct; the routes are
as they were in app.py).

    POST /api/character/revive    pay - in lusions, or in gold - and come back
                                  with everything you carried and wore
    POST /api/character/respawn   pay nothing, and lose the carried gold, the
                                  bag and the worn gear

DEATH WAS FREE, AND THE WHOLE TRANSACTION HAPPENED ON THE PLAYER'S MACHINE.
gameover.gd read the lusion balance, compared it to revive_cost, called
add_account_lusions(-cost), wrote full hp/mana/stamina into the save slot
itself, and reloaded the world. The server saw a smaller lusion figure and a
healthier character arrive by PUT, and believed both - because PUT
/api/account/lusions stored whatever number it was sent, so there was no
authoritative balance to charge against in the first place.

THE PENALTY FOR DYING IS THE ONLY THING LUSIONS DO. That is what makes this
worth an endpoint rather than a shrug - a currency with one sink IS that sink,
and a client that can write the balance has removed the cost of death from the
game.

ALSO THE OTHER HALF OF THE HEALING RECONCILER. app.py's _reconcile_heals()
flags a rise that regeneration and consumables cannot account for, and an
honest revive is exactly such a rise - a jump from 0 to full in no time at all.
Both exits leave a row in consume_grants like a potion does, through the same
writer (status/'s note_refill(), handed in).

THE RESPAWN WAS THE OTHER HALF OF A MIGRATION THAT ONLY EVER GOT DONE ONCE.
The revive moved onto the server and the exit next to it, the one that pays
nothing, was left as it was: gameover.gd wrote full pools into the save slot
and zeroed the carry gold in the same dictionary. Both halves were wrong in
opposite directions:

  THE HEAL WAS A CLIENT DECISION, so the next status push arrived as a rise
  from hp 0 to hp 504 with nothing authorising it. The reconciler did its job
  and clamped it to what five seconds of regeneration could produce - 52. THE
  RECONCILER WAS NOT THE BUG. It was the only thing in the system telling the
  truth.

  THE PENALTY WAS ALSO A CLIENT DECISION, and that one silently did NOTHING.
  `gold` is in SERVER_OWNED_STATS, so PUT /api/player/status ignores whatever
  balance a client sends. The carry gold was zeroed in the local slot, never
  destroyed here, and came back in full on the next login. Meanwhile the empty
  inventory WAS accepted, because losing items is a loss and only gains are
  reconciled. So true death took the items, refunded the gold, and left the
  character on 52 hp.

Both are the same rule, the one in CLAUDE.md: the client sends what it DID,
never what it now HAS. "I accepted death" is a fact about the player. "I have
504 hp and no gold" is two decisions, and both belong here.

Built like equipment/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: the three ledgers a death pays through
(gold_delta(), bank_gold_delta(), lusion_delta()), the account row
(_ensure_account()), _derived_stats(), the grant ids the reconciler reads
(REVIVE_GRANT_ID, RESPAWN_GRANT_ID), LUSION_GOLD_VALUE and the status read.
"""
from .routes import register  # noqa: F401
from .services import PAY_WITH  # noqa: F401
