"""economyrules - the economy's numbers, set on the server without shipping a
new version (0.74.0).

The owner, 22 Oct, of five ideas for the economy: "we need to do these tasks
also max power" - the fifth was "economy numbers you can change without
shipping. Today, changing the trade tax or a price means a new version. If
those numbers lived on the server, you could adjust them in a minute." Every
number here has a default (what the game held before this existed), a lowest
and a highest value, and one place that reads it: value(). See services.py.

    GET /api/economy/rules         any player: the four numbers the game draws
    GET /api/owner/economy/rules   the owner and a co-owner: every rule
    PUT /api/owner/economy/rules   the owner: one rule, {key, value}

WHO READS THEM. The general store's till, buy-back and shelf (shop/, handed
shop_terms), the trade tax's quote and charge (trade/, handed tax_terms), and
the larder and the smith (value(db, "material_drop_scale"),
"temper_gold_scale", "certification_gold_scale"). A rule nobody set is not
passed at all, so the catalogue's own number is used exactly as it was.

Built like trade/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules and value()), data.py (the SQL), schema.py (its two tables). It never
imports app.py; register() is handed what it needs.
"""
from .routes import register  # noqa: F401
from .services import (  # noqa: F401
    CACHE_SECONDS, PUBLIC, RULES, SHOP_ID, check, clear_cache, default, owner_value, public, shop_terms,
    tax_terms, value,
)
