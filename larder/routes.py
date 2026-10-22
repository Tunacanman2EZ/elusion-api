"""larder/routes.py - the edge: a meal cooked, and the recipe book (0.74.0).

register() is handed app.py's own pieces - it never imports app.py:

    require_auth        the token check (puts the account on g.user)
    get_db              the request's database connection
    bad_request         a message -> app.py's 400
    parse_slot          a raw slot -> its int, or None
    max_slot            the highest slot, for the 400's words
    slot_exists         (user_id, slot) -> whether a character is there
    catalogue           gamedata: the recipes (recipe_ids(), recipe_of()), the
                        burn (roll_recipe()), what a meal heals and its buff,
                        every item's name - asked on every call, so a suite's
                        seeded die is heard
    skills_of           (user_id, slot) -> {skill: {level, xp, ...}}
    take_from_backpack  (user_id, slot, item_id, quantity, keys_last=) -> True,
                        or False having written nothing (carry/'s grant)
    add_to_backpack     (user_id, slot, item_id, quantity) -> the cells
                        written, or None having written nothing
    grant_skill_xp      (user_id, slot, skill_id, xp) -> (level, xp, levels
                        gained); no commit (skills/'s)
    status_of           (user_id, slot) -> the status block every carry
                        answer hands back
    inventory_of        (user_id, slot) -> the carry, cell by cell
    bag_cells           the backpack's size, for "Your backpack is full"

THE ENDPOINT NAMES ARE THIS FILE'S - cooking_recipe, cooking_recipes - and
routes.lock holds them.
"""
from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, slot_exists, catalogue, skills_of,
             take_from_backpack, add_to_backpack, grant_skill_xp, status_of, inventory_of, bag_cells):

    def name_of(item_id):
        return str((catalogue.item_row(item_id) or {}).get("display_name") or item_id)

    def cooking_recipe():
        """
        Cook one meal from its ingredients
        ---
        tags:
          - Cooking
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [slot, recipe_id]
              properties:
                slot:      {type: integer, example: 0}
                recipe_id: {type: string,  example: herbbread, description: "The meal's item_id"}
        responses:
          200:
            description: "What happened, in /api/cooking/cook's shape (item_id is the recipe's), and consumed - every ingredient taken, burnt or not"
          400:
            description: Bad slot or recipe_id
          403:
            description: Cooking level too low
          404:
            description: That slot is empty, or an ingredient is not carried
          409:
            description: Backpack full, or there is no such recipe
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)
        if not slot_exists(user_id, slot):
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        recipe_id = str(payload.get("recipe_id", "")).strip()
        if not recipe_id or len(recipe_id) > 64:
            return bad_request("recipe_id must be 1-64 characters")

        # THE MEAL IS NAMED BY THE CLIENT AND NOTHING ELSE IS. Which dish to
        # make is a choice; what goes into it, the level it needs and whether
        # it burns are all read from what the server holds.
        recipe = catalogue.recipe_of(recipe_id)
        if recipe is None:
            return {"error": "Conflict", "message": "There is no recipe for that."}, 409

        skills = skills_of(user_id, slot)
        cooking_level = int(skills.get("cooking", {}).get("level", 1))
        if cooking_level < recipe["level"]:
            return {"error": "Forbidden", "message": "You need cooking level %d." % recipe["level"]}, 403

        # EVERY INGREDIENT, IN THE COUNT THE RECIPE ASKS, CARRIED - the bag and
        # the hotbar both, as a potion is drunk from either; never the bank.
        db = get_db()
        missing = services.first_missing(recipe["ingredients"], data.held(db, user_id, slot))
        if missing is not None:
            return {"error": "Not Found", "message": "You are missing %s." % name_of(missing)}, 404

        outcome = catalogue.roll_recipe(recipe_id, cooking_level)
        if not outcome["ok"]:
            # recipe_of() said yes a moment ago; a catalogue stand-in that
            # disagrees is answered as the fire answers.
            if outcome["reason"] == "level":
                return {"error": "Forbidden", "message": "You need cooking level %d." % outcome["needs"]}, 403
            return {"error": "Conflict", "message": "There is no recipe for that."}, 409

        # ---- everything above this line is validation; everything below commits
        # THE BAG BEFORE THE KEYS: a stack of eggs on key 4 is the player's
        # own arrangement, and is the last to be spent.
        for item_id, quantity in recipe["ingredients"]:
            if not take_from_backpack(user_id, slot, item_id, quantity, keys_last=True):
                db.rollback()
                return {"error": "Not Found", "message": "You are missing %s." % name_of(item_id)}, 404

        written = []
        if not outcome["burnt"]:
            written = add_to_backpack(user_id, slot, recipe_id, 1)
            if written is None:
                # Nothing is kept - the ingredients stay where they were.
                db.rollback()
                return {"error": "Conflict", "message": "Your backpack is full (%d slots)." % bag_cells}, 409

        level, _xp, levels_gained = grant_skill_xp(user_id, slot, "cooking", outcome["xp"])
        db.commit()

        return services.cooked_meal(recipe_id, outcome, written, levels_gained, level, status_of(user_id, slot),
                                    inventory_of(user_id, slot), skills_of(user_id, slot),
                                    recipe["ingredients"]), 200

    def cooking_recipes():
        """
        Every recipe: what it needs, the cooking level, the XP, what it heals and its buff
        ---
        tags:
          - Cooking
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: "recipes, lowest cooking level first: id, name, level, xp, mastery_level, required_level (to eat it), ingredients, restores, buff (or null)"
          401:
            description: Missing, invalid or expired token
        """
        # THE SERVER'S BOOK, so the game can check its own: a recipe the two
        # disagree about is a catalogue one side has not caught up with, and
        # the Recipes view can say so rather than offer a meal the fire refuses.
        recipes = []
        for recipe_id in catalogue.recipe_ids():
            recipe = catalogue.recipe_of(recipe_id)
            row = catalogue.item_row(recipe_id)
            if recipe is None or row is None:
                continue
            recipes.append(services.recipe_view(recipe_id, row, recipe, catalogue.buff_of(recipe_id),
                                                catalogue.restore_for(recipe_id)))
        return {"recipes": recipes}, 200

    app.add_url_rule("/api/cooking/recipe", endpoint="cooking_recipe",
                     view_func=require_auth(cooking_recipe), methods=["POST"])
    app.add_url_rule("/api/cooking/recipes", endpoint="cooking_recipes",
                     view_func=require_auth(cooking_recipes), methods=["GET"])
