"""larder/data.py - the SQL. Reads only: carry_items is app.py's (init_db()),
and every ingredient taken and meal made goes through the carry's grants,
handed in. No table of its own - a recipe is the catalogue's, and a meal in
the bag is a carry cell like any other. No commits: the route makes them."""


def held(db, user_id, slot):
    """{item_id: how many} across every carried cell, bag and hotbar."""
    return {r["item_id"]: int(r["n"]) for r in db.execute(
        "SELECT item_id, SUM(quantity) AS n FROM carry_items WHERE user_id = ? AND slot = ?"
        " GROUP BY item_id", (user_id, slot))}
