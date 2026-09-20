"""
A transcription of itemtooltip.gd's stat block, so the rows it will draw can be
read here rather than by hovering 123 items in the editor.

    _populate_stats / _add_gear_rows / _add_consumable_rows /
    _add_requirement_rows / _add_worth_rows / _add_comparison_row

If a row is wrong here it is wrong there.
"""
import json
import sim_client as C

RESTORE = ["NONE", "HP", "MANA", "STAMINA"]
PERIOD = {"warrior": 1.00, "mage": 0.45, "healer": 0.10, "tank": 0.25}


def band(damage, spread):
    if damage <= 0:
        return (0, 0)
    s = max(0.0, min(0.9, spread))
    import math
    low = max(1, math.floor(damage * (1.0 - s)))
    return (low, max(low, math.ceil(damage * (1.0 + s))))


def rows_for(item_id, quantity, class_id, level, skills, equipped):
    d = C.ITEMS[item_id]
    out = []
    slot = C.slot_name(d["equip_slot"])

    if slot:
        out.append(("Slot", slot.capitalize(), "neutral"))
        if d["damage"] > 0:
            lo, hi = band(d["damage"], d.get("damage_spread", 0.25))
            out.append(("Damage", "%d - %d" % (lo, hi), "neutral"))
            usable = (not d["required_classes"]) or class_id in d["required_classes"]
            if usable:
                out.append(("Damage per second",
                            "%d" % round((lo + hi) / 2 / PERIOD[class_id]), "neutral"))
            out += compare(d, equipped, "damage", slot, class_id)
        if d["armor_value"] > 0:
            out.append(("Armour", str(d["armor_value"]), "neutral"))
            out += compare(d, equipped, "armor_value", slot, class_id)
        if d["required_classes"]:
            mine = class_id in d["required_classes"]
            out.append(("Class", ", ".join(c.capitalize() for c in d["required_classes"]),
                        "neutral" if mine else "BAD"))

    if d["restore_target"] and d["restore_amount"] > 0:
        out.append(("Restores", "%d %s" % (d["restore_amount"], RESTORE[d["restore_target"]]),
                    "GOOD"))

    if d["required_level"] > 1:
        out.append(("Required level", str(d["required_level"]),
                    "neutral" if level >= d["required_level"] else "BAD"))
    skill = d["required_skill"].strip()
    if skill and d["required_skill_level"] > 1:
        have = skills.get(skill, 1)
        out.append(("Required %s" % skill.capitalize(), str(d["required_skill_level"]),
                    "neutral" if have >= d["required_skill_level"] else "BAD"))

    if d["tier"] > 1:
        out.append(("Tier", str(d["tier"]), "neutral"))
    if d["value"] > 0:
        text = "%d gold" % d["value"]
        if quantity > 1:
            text = "%d gold  (%d)" % (d["value"], d["value"] * quantity)
        out.append(("Value", text, "GOLD"))
    return out


def compare(d, equipped, field, slot, class_id=None):
    # Class-gated, same as _usable_by_player() in the tooltip: two things
    # that were never alternatives are not a comparison.
    if d["required_classes"] and class_id not in d["required_classes"]:
        return []
    worn_id = equipped.get(slot, "")
    if not worn_id or worn_id == d["item_id"]:
        return []
    worn = C.ITEMS.get(worn_id)
    if worn is None:
        return []
    delta = d[field] - worn[field]
    if delta == 0:
        return []
    return [("vs %s" % worn["display_name"], "%+d" % delta,
             "GOOD" if delta > 0 else "BAD")]


def render(item_id, quantity, class_id, level, skills, equipped):
    d = C.ITEMS[item_id]
    width = 46
    print("  +" + "-" * width + "+")
    print("  | %-*s |" % (width - 2, d["display_name"]))
    print("  +" + "-" * width + "+")
    for label, value, colour in rows_for(item_id, quantity, class_id, level, skills, equipped):
        mark = {"BAD": " <red>", "GOOD": " <green>", "GOLD": " <gold>"}.get(colour, "")
        print("  | %-22s %19s |%s" % (label + ":", value, mark))
    print("  +" + "-" * width + "+")
