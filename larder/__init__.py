"""larder - meals: recipes of two, three or more ingredients cooked on the
fire (0.74.0).

    POST /api/cooking/recipe    cook one meal: every ingredient taken, the burn
                                rolled as a fish's is, the meal and its cooking
                                XP paid unless it burned
    GET  /api/cooking/recipes   every recipe the catalogue has - what it needs,
                                the level, the XP, what it heals and its buff -
                                so the game can check its own list

THE OWNER, 22 Oct: "we have a whole lot of materials and ways to expand cooking
in clockwork lets get those done", and "i want to make recipes people can make
with the food also". A meal is an ItemData with a recipe (its ingredients, the
cooking level it needs, the XP it pays, the level it stops burning at), and a
meal may be an ingredient of another: Shadowcap Soup takes Bone Broth, the
Royal Banquet takes Sushi and a Tart. The exporter has already refused a
recipe that names no item or that goes round in a circle.

BUILT TO THE FIRE'S RULE (gathering/'s /api/cooking/cook): the client sends
which meal, never what went into it or whether it burned. Validate and 4xx
before the first write, one transaction, one commit, and the answer in the
cook's shape - the same keys, so one panel reads both - with what was taken
beside it. Burnt takes the ingredients and pays nothing, as a burnt fish does:
otherwise the burn would be decoration.

EATING ONE IS consuming/'s, through the route every potion uses: a meal is a
CONSUMABLE that anybody of its character level may eat (no skill gate, so a
cook can sell what they make), and a meal with a buff starts the character's
food buff there.

Built like gathering/ (ARCHITECTURE.md): routes.py (the edge), services.py
(the rules, pure), data.py (the SQL). It never imports app.py; register() is
handed what it needs - the grants (carry/'s and skills/'s), the vitals and the
catalogue, whose recipe_of() and roll_recipe() are the rules of a recipe.
"""
from .routes import register  # noqa: F401
