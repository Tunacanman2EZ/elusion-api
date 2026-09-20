"""
A faithful Python port of the GDScript equipment rules, run against the real
gamedata.json, so the client half can be tested without opening Godot.

Every function below is a line-by-line transcription of its counterpart:

    ItemData.slot_name()               src/types/itemdata.gd
    player.equip_check() / equip()     src/characters/player.gd
    CharacterData.prune_equipment()    src/systems/characterdata.gd
    ServerStorage._save_body()         src/systems/serverstorage.gd

If a rule is wrong here it is wrong there, and vice versa - which is the only
thing that makes this worth running.
"""

import json

SLOT_ORDER = ["NONE", "WEAPON", "HELM", "CHEST", "LEGS", "BOOTS", "SHIELD",
              "RING", "AMULET"]
HOTBAR_SIZE = 9

_GD = json.load(open("gamedata.json"))
ITEMS = {i["item_id"]: i for i in _GD["items"]}
ENEMIES = {e["enemy_id"]: e for e in _GD["enemies"]}


# --- ItemData -------------------------------------------------------------

def slot_name(slot_index):
    if slot_index <= 0:
        return ""
    for value, key in enumerate(SLOT_ORDER):
        if value == slot_index:
            return key.lower()
    return ""


def slot_names():
    return [k.lower() for v, k in enumerate(SLOT_ORDER) if v > 0]


# --- player.gd ------------------------------------------------------------

class Player:
    def __init__(self, class_id, level):
        self.class_id = class_id
        self.level = level
        self.equipped = {}
        self.inventory_data = []

    def equip_check(self, item_id):
        data = ITEMS.get(item_id)
        if data is None:
            return {"ok": False, "reason": "unknown"}
        name = slot_name(data["equip_slot"])
        if name == "":
            return {"ok": False, "reason": "notgear"}
        if data["required_classes"]:
            if self.class_id not in data["required_classes"]:
                return {"ok": False, "reason": "class",
                        "allowed": data["required_classes"]}
        if self.level < data["required_level"]:
            return {"ok": False, "reason": "level",
                    "needs": data["required_level"]}
        return {"ok": True, "slot": name}

    def equip(self, item_id):
        verdict = self.equip_check(item_id)
        if not verdict["ok"]:
            return False
        self.equipped[verdict["slot"]] = item_id
        return True

    def unequip(self, name):
        was = self.equipped.get(name, "")
        if was:
            del self.equipped[name]
        return was

    def equipped_weapon_damage(self):
        data = ITEMS.get(self.equipped.get("weapon", ""))
        return 0 if data is None else data["damage"]

    def equipped_armor_value(self):
        total = 0
        for name in self.equipped:
            data = ITEMS.get(self.equipped[name])
            if data is not None:
                total += data["armor_value"]
        return total


# --- characterdata.gd -----------------------------------------------------

def prune_equipment(equipment, inventory):
    cleaned = {}
    if not isinstance(equipment, dict):
        return cleaned
    held = set()
    if isinstance(inventory, list):
        for entry in inventory:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("item_id", ""))
            if entry_id:
                held.add(entry_id)
    for name, item_id in equipment.items():
        item_id = str(item_id)
        if item_id == "" or item_id not in held:
            continue
        data = ITEMS.get(item_id)
        if data is None:
            continue
        if slot_name(data["equip_slot"]) != str(name):
            continue
        cleaned[str(name)] = item_id
    return cleaned


# --- serverstorage.gd -----------------------------------------------------

def string_array(value, size):
    source = value if isinstance(value, list) else []
    return [str(source[i]) if i < len(source) else "" for i in range(size)]


def save_body(index, slot):
    body = {
        "slot": index,
        "class_id": str(slot.get("character", "")),
        "name": str(slot.get("character", "")),
        "level": int(slot.get("level", 1)),
        "active_pet_id": str(slot.get("active_pet_id", "")),
    }
    if "equipment" in slot:
        body["equipment"] = dict(slot["equipment"])
    if "hotbar_assignments" in slot:
        body["hotbar"] = string_array(slot["hotbar_assignments"], HOTBAR_SIZE)
    return body


def slot_from_server(data):
    status = data.get("status", {})
    return {
        "character": str(data.get("class_id", "")),
        "active_pet_id": str(data.get("active_pet_id", "")),
        "equipment": dict(data.get("equipment", {})),
        "hotbar_assignments": string_array(data.get("hotbar", []), HOTBAR_SIZE),
        "level": int(status.get("level", 1)),
        "inventory": data.get("inventory", []),
    }


def bag(*item_ids):
    return [{"item_id": i, "quantity": 1} for i in item_ids]


# --- equipmentslot.gd / equipmentpanel.gd ---------------------------------
# EquipmentSlot._can_drop_data(): this square, then the character's own rules.
# EquipmentPanel._refresh_summary(): what the doll prints underneath.

# EACH CLASS'S base_attack_damage() AND attack_period(), HAND-COPIED, AND THAT
# IS THE WEAKNESS WORTH NAMING.
#
# These four numbers live in GDScript - warrior.gd's base_melee_damage, mage.gd
# and healer.gd's damage_per_magic, tank.gd's aura_damage - and nothing exports
# them, so this table is a copy that can silently stop matching the game. It
# did: mage 25, healer 3 and tank 9 were the PRE-RETUNE values, and they sat
# here for the whole of the retune that replaced them. Every dps figure this
# module produced was wrong, and test_equipment.py's ladder section failed 31
# checks against a game that was correctly balanced.
#
# The figures that matter are damage / period, which is why 9 over 0.45s and 2
# over 0.10s are not the typos they look like:
#
#     warrior  24 / 1.00 = 24.0/s     the reference
#     mage      9 / 0.45 = 20.0/s     0.85x - ranged, and the cast explodes
#     healer    2 / 0.10 = 20.0/s     0.80x - ranged, ten shots a second
#     tank      4 / 0.25 = 16.0/s     lower on purpose: it hits EVERYTHING in
#                                     range, so one target is its worst case
#
# When these are added to the Godot export, read them from gamedata and delete
# the literals. Until then, test_equipment.py asserts the dps they produce
# against the ratios the class scripts state, so a drift this size fails loudly
# instead of quietly rewriting every number downstream.
BASE_DAMAGE = {"warrior": 24, "mage": 9, "healer": 2, "tank": 4}
ATTACK_PERIOD = {"warrior": 1.00, "mage": 0.45, "healer": 0.10, "tank": 0.25}
ARMOUR_HALF_POINT = 200.0


def can_drop(square_slot_name, item_id, player):
    """EquipmentSlot._can_drop_data()."""
    data = ITEMS.get(item_id)
    if data is None:
        return False
    if slot_name(data["equip_slot"]) != square_slot_name:
        return False
    return player.equip_check(item_id)["ok"]


def weapon_band(damage, spread):
    import math
    if damage <= 0:
        return (0, 0)
    s = max(0.0, min(0.9, spread))
    low = max(1, math.floor(damage * (1.0 - s)))
    return (low, max(low, math.ceil(damage * (1.0 + s))))


def damage_multiplier(skill_level):
    return 1.0 + (skill_level - 1) * 0.01 * 2


def attack_damage_range(player, skill_level=1):
    """player.attack_damage_range()."""
    base = BASE_DAMAGE[player.class_id]
    low = high = base
    data = ITEMS.get(player.equipped.get("weapon", ""))
    if data is not None and data["damage"] > 0:
        lo, hi = weapon_band(data["damage"], data.get("damage_spread", 0.25))
        low += lo
        high += hi
    m = damage_multiplier(skill_level)
    return (round(low * m), round(high * m))


def armour_reduction(value):
    return 0.0 if value <= 0 else value / (value + ARMOUR_HALF_POINT)


def summary(player, skill_level=1):
    """EquipmentPanel._refresh_summary()."""
    lo, hi = attack_damage_range(player, skill_level)
    armour = player.equipped_armor_value()
    return {
        "Damage": "%d - %d" % (lo, hi),
        "Damage per second": "%d" % round((lo + hi) / 2 / ATTACK_PERIOD[player.class_id]),
        "Armour": str(armour),
        "Damage taken": "%d%% less" % round(armour_reduction(armour) * 100),
    }
