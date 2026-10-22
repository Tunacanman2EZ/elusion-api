"""smith - the anvil in town: tempering a piece of gear, and the Royal
Certification a smith needs for the higher tiers (0.74.0).

    GET  /api/smith/status    a character's certification, every rank with
                              what it costs and whether it is earned, and what
                              tempering each tier costs
    POST /api/smith/quote     what tempering the piece in one carried cell
                              would make of it and cost, without doing it
    POST /api/smith/temper    temper it: the materials taken, the gold burned
                              (smith_temper), the cell's id rewritten
    POST /api/smith/certify   earn the next Royal Certification: the materials
                              taken, the gold burned (royal_certification)

THE OWNER, 22 Oct: "Cooking + a smith" - "a new smith spends materials plus
gold to upgrade gear" - and of the economy, "upgrades that cost materials, plus
a 'Royal Certification' step to reach higher tiers". Both are SINKS: the gold
leaves the world through gold_delta(), the materials leave it through the
carry's grant, and what comes back is a better roll on a piece the player
already owned - nothing a vendor pays more for (a roll changes what a piece
does, not what it is worth: gamedata.py, QUALITY ROLLS).

TEMPERING REWRITES THE ROLL IN THE ID. "jadechest~a104h96r605" tempered once
is "jadechest~a107h99r605": every rolled stat up temper_step points, never past
temper_cap, the resistance exactly as it was (gamedata.temper_roll()). Because
the roll travels in the id, the tempered piece moves, wears, banks, trades and
sells by every path that already exists, and the cell check that guards each
of them checks the new roll too. A Perfect piece is refused (it is already
past any cap), and so is a piece with every stat at the cap. A plain piece -
the shop's free one among them - has nothing to temper. A held find (a mythic
or a Perfect, trade/'s holds) keeps its hold under the new id.

THE RANKS ARE PER CHARACTER, in smith_ranks (schema.py, this package's own,
made by init_db()): the character who paid for a certification is the one
who holds it, and a character deleted takes its ranks with it (saves/'s
drop()). Every number - the step, the cap, the gold rate, each tier's
materials and certification, each rank's level, gold and materials - is the
game's (GameConstants, through gamedata.json), and the gold is scaled by the
owner's temper_gold_scale and certification_gold_scale (economyrules/).

Built like shop/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs - the ledger, the carry's grants and one-cell steps, the trade
hold's move and the economy rules, all app.py's or other areas'.
"""
from .routes import register  # noqa: F401
from .services import certifications, temper_costs  # noqa: F401
