"""skills/routes.py - the edge: the skills write and the training report, as
they were in app.py, and the XP grant every other area that trains a skill is
handed.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth          the token check (puts the account on g.user)
    get_db                the request's database connection
    bad_request           a message -> app.py's 400
    parse_slot            a raw slot -> its int, or None
    max_slot              the highest slot, for the 400's words
    parse_stat            a raw number -> a non-negative int, or None
    slot_exists           (user_id, slot) -> whether that character exists
    has_owner_powers      username -> whether this is the owner (or a
                          co-owner), whose skills write is not held to the
                          ceiling
    catalogue             gamedata: the XP curve (apply_xp(),
                          xp_needed_for_skill_level()) - asked on every grant
    valid_skills          every skill a character can have (VALID_SKILLS)
    server_owned          the skills only the server grants
                          (SERVER_OWNED_SKILLS) - a write drops them
    trainable             the skills a training report grants
                          (TRAINABLE_SKILLS)
    max_level, max_xp     the skill ceiling (MAX_SKILL_LEVEL, MAX_SKILL_XP)
    max_train_seconds     the most budget one report may cash
                          (MAX_TRAIN_ELAPSED_SECONDS)
    max_train_xp_per_sec  {skill: the fastest honest earn rate}
                          (MAX_TRAIN_XP_PER_SEC)
    proficient_amount     (class_id, skill_id, raw) -> the XP after the class
                          specialty - one rule for this and the kill

register() hands back skills_of() and grant_xp(); app.py keeps them callable
as skills_payload() and _grant_skill_xp(), the names the kill route, the
consume, fishing and cooking have always been handed.

THE ENDPOINT NAMES ARE app.py's - write_skills, train_skills - and routes.lock
holds them.
"""
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services


def register(app, *, require_auth, get_db, bad_request, parse_slot, max_slot, parse_stat, slot_exists,
             has_owner_powers, catalogue, valid_skills, server_owned, trainable, max_level, max_xp,
             max_train_seconds, max_train_xp_per_sec, proficient_amount):

    def skills_of(user_id, slot):
        """{skill_id: {"level", "xp"}} for every skill this character has a row
        for - a skill never trained has none."""
        return {row["skill_id"]: {"level": int(row["level"]), "xp": int(row["xp"])}
                for row in data.all_of(get_db(), user_id, slot)}

    def grant_xp(user_id, slot, skill_id, gained):
        """
        Add XP to one skill and return (level, xp, levels_gained).

        THE FIRST SERVER-SIDE SKILL GRANT. Until it, the skills table was
        written only by PUT /api/character/skills, which replaces the whole set
        with whatever the client sends - see the note there about why every
        server-owned skill is now carved out of that.

        AN UPSERT, NOT A DELETE-THEN-INSERT. write_skills() replaces every row
        because it is given every row; this is given one skill and must leave the
        rest of the character's progress exactly where it was.

        THE THRESHOLD IS RECOMPUTED, NOT STORED. The skills table is (level, xp)
        with no xp_to_next column, unlike saves. Deriving it each grant from the
        same curve the client uses costs one exponent and removes a column that
        could disagree with the level sitting next to it.

        NO COMMIT - the caller's transaction, so a kill or a cook never banks
        the XP without the rest of what it did.
        """
        gained = max(int(gained), 0)
        db = get_db()
        row = data.skill(db, user_id, slot, skill_id)
        if gained <= 0:
            return (int(row["level"]), int(row["xp"]), 0) if row else (1, 0, 0)

        level = int(row["level"]) if row else 1
        xp = int(row["xp"]) if row else 0

        level, xp, _next, levels_gained = catalogue.apply_xp(
            level, xp, catalogue.xp_needed_for_skill_level(skill_id, level), gained,
            need=lambda at: catalogue.xp_needed_for_skill_level(skill_id, at),
        )

        level = min(level, max_level)
        xp = min(xp, max_xp)

        data.put(db, user_id, slot, skill_id, level, xp)
        return level, xp, levels_gained

    def write_skills():
        """
        Replace a character's skills
        ---
        tags:
          - Character
        consumes:
          - application/json
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
              required: [slot, skills]
              properties:
                slot: {type: integer, example: 0}
                skills:
                  type: object
                  description: '{"attack": {"level": 12, "xp": 340}, ...}'
        responses:
          200:
            description: The skills as stored
          400:
            description: Bad slot, unknown skill, or a malformed level/xp
          404:
            description: That slot is empty
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

        skills = payload.get("skills")
        if not isinstance(skills, dict):
            return bad_request("skills must be an object")

        parsed = []
        for skill_id, values in skills.items():
            name = str(skill_id).strip().lower()
            if name not in valid_skills:
                # Refused rather than ignored. A silently dropped skill is a level
                # the player earned and cannot see, with nothing anywhere to say so.
                return bad_request("Unknown skill '%s'. Known skills: %s" % (name, ", ".join(sorted(valid_skills))))

            if not isinstance(values, dict):
                return bad_request("skills.%s must be an object with level and xp" % name)

            level = parse_stat(values.get("level", 1))
            xp = parse_stat(values.get("xp", 0))
            if level is None or level < 1:
                return bad_request("skills.%s.level must be a positive integer" % name)
            if xp is None:
                return bad_request("skills.%s.xp must be a non-negative integer" % name)

            parsed.append((user_id, slot, name, level, xp))

        # SKILL CEILING. A claim over the cap is clamped rather than rejected, so a
        # single over-cap skill never discards the whole (otherwise honest) sync.
        # The owner is exempt, as with the backpack - and since 6 Oct 2026 only the
        # owner, matching write_inventory(); a mod is clamped like a player.
        #
        # ITS OPENING LINE USED TO SAY "skills have no server-side grant path yet",
        # AND THAT HAS NOT BEEN TRUE SINCE E-2 CLOSED. All six are granted
        # server-side now, so nothing this clamp produces ever reaches the database -
        # the drop filter below empties `parsed` unconditionally, because
        # VALID_SKILLS and SERVER_OWNED_SKILLS are the same six.
        #
        # THE CLAMP STAYS ANYWAY, and the reasoning is worth keeping because the
        # obvious move is to delete it. It is not wrong, it is not a duplicate of a
        # rule that lives somewhere better, and it is not stale - it is GENERIC code
        # with nothing to do today. The day a seventh skill is the client's to grant,
        # the filter below stops catching everything and this clamp is the thing
        # standing between a modified client and a level of 2^31. Deleting a
        # guardrail because it currently has nothing to guard is how you find out
        # what it was for.
        #
        # The ceiling that IS load-bearing today is in grant_xp() above, which caps
        # every server grant. That one is on the path everything actually takes.
        if not has_owner_powers(g.user["username"]):
            parsed, hits = services.capped(parsed, max_level, max_xp)
            if hits:
                print("[SKILLS] capped over-ceiling skills for %s slot %d: %s"
                      % (g.user["username"], slot, ", ".join(sorted(hits))))

        # THE SERVER OWNS ALL SIX NOW, so this line empties `parsed` every time and
        # this route writes nothing at all.
        #
        # It said "fine for the four skills the client still grants" until somebody
        # counted them. There are none: attack is granted at the kill, fishing and
        # cooking against items the server consumed, defense/agility/magic through
        # /api/skill/train. VALID_SKILLS and SERVER_OWNED_SKILLS are the same set.
        #
        # WHY THE ROUTE IS STILL HERE, doing a full parse and answering 200 for a
        # body it throws away. An un-updated client still sends all six on every
        # save, and 400-ing an otherwise honest sync over a field it may no longer
        # set would break saving for that build entirely. Dropped silently is the
        # same posture PUT /api/player/status takes with level and xp, and for the
        # same reason. The Godot client stopped sending them (see
        # serverstorage.gd::_push_slot), which removes the round trip for anyone on
        # a current build without stranding anybody on an old one.
        #
        # The 400s below it are contract and are tested - an unknown skill name, a
        # level of 0 and a non-object all still refuse. Validating a body and then
        # discarding it looks odd and is correct: the shape of the request is still
        # part of the promise even when its contents are not.
        #
        # THE DELETE AND THE INSERT BELOW ARE GENERIC, not dead. They are what makes
        # this route work again, correctly, the day a skill becomes the client's to
        # grant. See the clamp above for the same argument at more length.
        parsed = [row for row in parsed if row[2] not in server_owned]

        db = get_db()
        data.replace_unowned(db, user_id, slot, server_owned, parsed)
        db.commit()

        return {"slot": slot, "skills": skills_of(user_id, slot)}, 200

    def train_skills():
        """
        Report training activity for agility, defense and magic
        ---
        tags:
          - Character
        consumes:
          - application/json
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
              required: [slot]
              properties:
                slot:    {type: integer, example: 0}
                agility: {type: integer, description: "raw agility XP earned since the last report"}
                defense: {type: integer, description: "raw defense XP earned since the last report"}
                magic:   {type: integer, description: "raw magic XP earned since the last report"}
        responses:
          200:
            description: The three trained skills as stored after the grant
          400:
            description: Bad slot or a malformed amount
          404:
            description: That slot is empty
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        user_id = g.user["id"]

        slot = parse_slot(payload.get("slot"))
        if slot is None:
            return bad_request("slot must be an integer 0-%d" % max_slot)

        db = get_db()
        row = data.trainee(db, user_id, slot)
        if row is None:
            return {"error": "Not Found", "message": "No character in slot %d." % slot}, 404

        now_ms = int(time.time() * 1000)

        # THE BUDGET. Time since the last report, capped: a fresh character
        # (last_train_at 0) or one back from hours offline gets at most
        # MAX_TRAIN_ELAPSED_SECONDS of budget, never the whole gap. This is what
        # stops a single giant report from cashing an impossible amount at once.
        elapsed_s = services.budget(now_ms, int(row["last_train_at"]), max_train_seconds)

        results = {}
        for skill in trainable:
            reported = parse_stat(payload.get(skill, 0))
            if reported is None:
                return bad_request("%s must be a non-negative integer" % skill)
            # Clamp the raw report to what is physically earnable in the elapsed
            # budget, THEN apply proficiency. A client reporting a billion gets the
            # ceiling; an honest one is never anywhere near it.
            raw = services.earnable(reported, max_train_xp_per_sec[skill], elapsed_s)
            gained = proficient_amount(row["class_id"], skill, raw)
            level, xp, levels_gained = grant_xp(user_id, slot, skill, gained)
            results[skill] = {"level": level, "xp": xp, "levels_gained": levels_gained}

        # Stamp the report time in the SAME transaction as the grants, so a crash
        # cannot bank the XP and lose the timestamp - which would hand the next
        # report a second helping of the same budget.
        data.trained(db, user_id, slot, now_ms, int(time.time()))
        db.commit()

        return {"slot": slot, "skills": results}, 200

    app.add_url_rule("/api/character/skills", endpoint="write_skills",
                     view_func=require_auth(write_skills), methods=["PUT"])
    app.add_url_rule("/api/skill/train", endpoint="train_skills",
                     view_func=require_auth(train_skills), methods=["POST"])

    return SimpleNamespace(skills_of=skills_of, grant_xp=grant_xp)
