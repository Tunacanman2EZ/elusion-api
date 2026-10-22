"""larder/services.py - the rules: which ingredient is missing, and the
answers. Pure: no database, no request."""


def first_missing(ingredients, held):
    """The first ingredient carried in fewer than the recipe asks, or None.
    ingredients is recipe_of()'s [(item_id, quantity)]; held {item_id: count}."""
    return next((item_id for item_id, quantity in ingredients if int(held.get(item_id, 0)) < quantity), None)


def cooked_meal(recipe_id, outcome, written, levels_gained, level, status, inventory, skills, ingredients):
    """
    The answer to a meal on the fire: THE FIRE'S OWN KEYS, as
    gathering/services.py cooked() gives them for a fish - item_id (here the
    recipe's), burnt, output, xp, carry_positions, levelled_up, cooking_level,
    status, inventory, skills - and what it took, "consumed".

    A COPY OF THAT SHAPE, NOT AN IMPORT, because a package never imports
    another; test_larder.py holds the two answers to the same keys, so the
    cooking window cannot find one of them a field short.
    """
    return {
        "item_id": recipe_id,
        "burnt": outcome["burnt"],
        "output": outcome["output"],
        "xp": outcome["xp"],
        "carry_positions": written,
        "levelled_up": levels_gained > 0,
        "cooking_level": level,
        "status": status,
        "inventory": inventory,
        "skills": skills,
        "consumed": [{"item_id": item_id, "quantity": int(quantity)} for item_id, quantity in ingredients],
    }


def recipe_view(recipe_id, row, recipe, buff, restores):
    """One line of GET /api/cooking/recipes: the meal, what it needs and pays,
    what it heals and its buff (or None)."""
    target, amount = restores
    return {
        "id": recipe_id,
        "name": str(row.get("display_name") or recipe_id),
        "level": recipe["level"],
        "xp": recipe["xp"],
        "mastery_level": recipe["mastery_level"],
        "required_level": int(row.get("required_level", 1) or 1),
        "ingredients": [{"item_id": item_id, "quantity": int(quantity)} for item_id, quantity in recipe["ingredients"]],
        "restores": {"target": target, "amount": int(amount)},
        "buff": None if buff is None else {"kind": buff[0], "amount": int(buff[1]), "seconds": int(buff[2])},
    }
