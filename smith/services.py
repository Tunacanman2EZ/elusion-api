"""smith/services.py - the rules: what tempering a tier costs, what each Royal
Certification costs and asks, what the gold comes to once the owner's scale is
on it, and the answers. Pure: no database, no request.

Every number comes from the catalogue's constants (GameConstants, through
gamedata.json: temper_step, temper_cap, temper_gold_rate, temper_costs,
certifications), handed in as `constants`. A catalogue from before the smith
has none of them, and then there is nothing to temper and nothing to earn."""
import math

# What the refusals say, by reason, and the status each answers with. One
# table, so the quote's "reason" and the temper's refusal are the same words.
#
# WHICH CODE (CLAUDE.md, "Which refusal code"): the route is the caller's
# already, so a check on what was asked answers 403 when it is a rank the
# character has not reached (the level gate's code), 404 for something it
# does not hold (the recipe's "You are missing"), 400 for gold it does not
# have (the shop's), and 409 for a piece the smith will not work at all (the
# fire's "That cannot be cooked").
REFUSALS = {
    "notgear": (409, "The smith tempers weapons and armour."),
    "norolls": (409, "That piece has no rolls to temper."),
    "unknown": (409, "The smith tempers weapons and armour."),
    "perfect": (409, "That piece is already perfect."),
    "tempered": (409, "That piece is fully tempered."),
    "tier": (409, "The smith cannot temper that."),
}

GEAR_TYPES = ("WEAPON", "ARMOR")


def half_up(number):
    """A gold figure to the nearest whole coin, halves up - not Python's
    round(), which rounds a half to the even neighbour and would make 2.5 gold
    cost 2 and 3.5 cost 4."""
    return int(math.floor(float(number) + 0.5))


def _materials(raw):
    """[(item_id, quantity)] from a catalogue materials list; bad rows dropped."""
    out = []
    for entry in raw or []:
        try:
            item_id, quantity = str(entry["item_id"]), int(entry["quantity"])
        except (KeyError, TypeError, ValueError):
            continue
        if item_id and quantity > 0:
            out.append((item_id, quantity))
    return out


def temper_costs(constants):
    """{tier: {"tier", "materials": [(item_id, quantity)], "certification"}}
    from the catalogue's temper_costs."""
    out = {}
    for entry in (constants or {}).get("temper_costs") or []:
        try:
            tier = int(entry["tier"])
            needs = max(0, int(entry.get("certification", 0) or 0))
        except (KeyError, TypeError, ValueError):
            continue
        out[tier] = {"tier": tier, "materials": _materials(entry.get("materials")), "certification": needs}
    return out


def certifications(constants):
    """[{"rank", "name", "max_tier", "level", "gold", "materials"}] from the
    catalogue's certifications, lowest rank first."""
    out = []
    for entry in (constants or {}).get("certifications") or []:
        try:
            out.append({"rank": int(entry["rank"]), "name": str(entry.get("name", "")),
                        "max_tier": int(entry.get("max_tier", 0) or 0),
                        "level": max(1, int(entry.get("level", 1) or 1)),
                        "gold": max(0, int(entry.get("gold", 0) or 0)),
                        "materials": _materials(entry.get("materials"))})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda c: c["rank"])


def rank_name(certs, rank):
    """The name of a rank, "" for none (rank 0, nothing earned yet)."""
    return next((c["name"] for c in certs if c["rank"] == rank), "")


def temper_step(constants):
    try:
        return max(0, int((constants or {}).get("temper_step", 0) or 0))
    except (TypeError, ValueError):
        return 0


def temper_cap(constants, quality_high):
    """The cap a temper raises a stat to: the catalogue's, never past the top
    of the ordinary roll (gamedata.temper_roll() says why)."""
    try:
        cap = int((constants or {}).get("temper_cap", quality_high) or quality_high)
    except (TypeError, ValueError):
        cap = quality_high
    return min(cap, int(quality_high))


def gold_rate(constants, scale):
    """temper_gold_rate times the owner's temper_gold_scale."""
    try:
        rate = float((constants or {}).get("temper_gold_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    return max(0.0, rate) * max(0.0, float(scale))


def temper_gold(value, rate):
    """What tempering a piece worth `value` costs at `rate` (gold_rate()):
    value times the rate, to the nearest coin, and AT LEAST ONE - a free temper
    is a sink that stopped sinking, and the owner's scale can go to zero."""
    return max(1, half_up(max(0, int(value)) * float(rate)))


def certification_gold(gold, scale):
    """A certification's gold with the owner's certification_gold_scale on it.
    Zero is allowed: the owner may give the ranks away for materials alone."""
    return max(0, half_up(max(0, int(gold)) * max(0.0, float(scale))))


def with_have(materials, held):
    """[{"item_id", "quantity", "have"}] - what is asked, beside what the
    character carries of each."""
    return [{"item_id": item_id, "quantity": int(quantity), "have": int(held.get(item_id, 0))}
            for item_id, quantity in materials]


def first_missing(materials, held):
    """The first material carried in fewer than asked, or None."""
    return next((item_id for item_id, quantity in materials if int(held.get(item_id, 0)) < quantity), None)


def status_view(constants, quality_high, rank, earned, held, temper_scale, cert_scale, purse):
    """GET /api/smith/status's answer, and certify's: the character's rank and
    its name, every rank with its gold scaled, its materials beside what is
    carried and whether it is earned, and every tier's temper cost."""
    certs = certifications(constants)
    rate = gold_rate(constants, temper_scale)
    names = {c["rank"]: c["name"] for c in certs}
    return {
        "rank": int(rank),
        "rank_name": rank_name(certs, rank),
        "ranks": [{"rank": c["rank"], "name": c["name"], "max_tier": c["max_tier"], "level": c["level"],
                   "gold": certification_gold(c["gold"], cert_scale),
                   "materials": with_have(c["materials"], held),
                   "earned": c["rank"] in earned, "earned_at": int(earned.get(c["rank"], 0))}
                  for c in certs],
        "temper_costs": [{"tier": cost["tier"], "gold_rate": round(rate, 6),
                          "materials": with_have(cost["materials"], held),
                          "certification": cost["certification"],
                          "certification_name": names.get(cost["certification"], "")}
                         for _tier, cost in sorted(temper_costs(constants).items())],
        "temper_step": temper_step(constants),
        "temper_cap": temper_cap(constants, quality_high),
        "gold": int(purse),
    }


def appraise(row, item_id, constants, quality_high, rank, held, purse, temper_scale, roll, certs, name_of):
    """
    What the smith would do to `item_id`, as the quote answers it, with the
    refusal the temper would give (None when it would go ahead):

        ({"item_id", "new_item_id", "before", "after", "gold", "materials",
          "certification", "can", "reason"}, (status, message) or None)

    `row` is the piece's catalogue row (item_row()), `roll` is
    gamedata.temper_roll, `held` {item_id: carried}, `purse` the carried gold,
    `name_of` an item_id -> the name a refusal says.
    The checks run in the order the temper makes them - what the piece is,
    then what it needs of the character - and the first that fails is the
    reason. Everything a quote can know is still filled in past it, so the
    window shows the cost of a rank not yet earned.
    """
    answer = {"item_id": item_id, "new_item_id": item_id, "before": {}, "after": {}, "gold": 0,
              "materials": [], "certification": 0, "can": False, "reason": ""}
    refusal = None
    if row is None or str(row.get("type_name", "")) not in GEAR_TYPES:
        refusal = REFUSALS["notgear"]
    else:
        new_id, before, after, why = roll(item_id, temper_step(constants), temper_cap(constants, quality_high))
        answer.update(new_item_id=new_id, before=before, after=after)
        if why:
            refusal = REFUSALS.get(why, REFUSALS["unknown"])
        cost = temper_costs(constants).get(int(row.get("tier", 0) or 0))
        if cost is None:
            refusal = refusal or REFUSALS["tier"]
        else:
            answer["gold"] = temper_gold(row.get("value", 0), gold_rate(constants, temper_scale))
            answer["materials"] = with_have(cost["materials"], held)
            answer["certification"] = cost["certification"]
            if refusal is None and int(rank) < cost["certification"]:
                refusal = (403, "You need to be a %s to temper tier %d gear."
                           % (rank_name(certs, cost["certification"]) or "certified smith", cost["tier"]))
            missing = first_missing(cost["materials"], held)
            if refusal is None and missing is not None:
                refusal = (404, "You are missing %s." % name_of(missing))
            if refusal is None and int(purse) < answer["gold"]:
                refusal = (400, "That costs %d gold and you have %d." % (answer["gold"], int(purse)))
    answer["can"] = refusal is None
    answer["reason"] = "" if refusal is None else refusal[1]
    return answer, refusal
