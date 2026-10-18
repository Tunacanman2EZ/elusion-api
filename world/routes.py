"""world/routes.py - the edge: the owner's three routes for the stirring lands,
as they were in app.py, and the stir itself - which land, when the next one
begins - that every other area asks through what register() hands back.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth            the token check (puts the account on g.user)
    require_owner           the owner only - the same bare 404 as no route
    require_fresh_password  the password again, on a hardened session
    get_db                  the request's database connection
    bad_request             a message -> app.py's 400
    get_setting             a server_settings key -> its stored value, or None
    set_setting             (key, value, by) - store one
    announce                (body, by) - say it to every game, as the world
    log_staff_action        (actor, kind, target, target_id, detail) - the
                            moderation log; this writes the "world" kind
    element_of              an enemy_id -> its land (Element.Type), 0 for none
    events                  () -> the events/ package's EventsAPI, or None
                            until it is registered (a stir event wins; a
                            kill's pay is times the events on now)
    insights                () -> the insights/ package's InsightsAPI, or
                            None (it chooses the land, and says why)

The last two are ASKED ON EVERY USE: both are registered after this, at the
end of app.py, and are handed what this hands back.

and HANDS BACK a namespace of what other areas ask of the world - the kill
route what a kill pays (stir_for), the broadcast poll what the game is told
(public), the events their guardrails (settings), the insights the next stir
(next_stir) - which app.py keeps under the names it always called them by.

THE ENDPOINT NAMES ARE app.py's - read_world, set_world, stir_world - and
routes.lock holds them.
"""
import json
import sqlite3
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services
from .services import LANDS, WORLD_HUNT_WINDOW, WORLD_KEY, WORLD_LIMITS, WORLD_RECENT_TICKS


def register(app, *, require_auth, require_owner, require_fresh_password, get_db, bad_request, get_setting,
             set_setting, announce, log_staff_action, element_of, events, insights):

    def world_settings():
        """The owner's settings for the stirring lands: WORLD_DEFAULTS where
        none were ever saved, and OFF where what was saved cannot be read. A
        saved value out of its range is held to it; one of the wrong kind is
        the default."""
        return services.settings_from(get_setting(WORLD_KEY))

    def land_hunts(db, now=None):
        """{element: paid kills of that land's monsters in the last
        WORLD_HUNT_WINDOW} for all seven lands."""
        now = int(time.time()) if now is None else int(now)
        counts = {element: 0 for element in LANDS}
        for row in data.kills_since(db, now - WORLD_HUNT_WINDOW):
            element = element_of(row["enemy_id"])
            if element in counts:
                counts[element] += int(row["n"])
        return counts

    def pick_land_why(db, tick, previous, now=None):
        """(land, passed over, newcomers, hunts, candidates) - pick_land() and
        the reasons for it, for the owner's "next stir"."""
        candidates = sorted(e for e in LANDS if e != previous)
        chooser = services.chooser_for(tick)
        try:
            hunts = land_hunts(db, now)
        except sqlite3.Error:
            return chooser.choice(candidates), [], {}, {}, candidates
        chosen_by = insights()
        if chosen_by is not None:
            land, passed, newcomers = chosen_by.choose(db, candidates, hunts, chooser, now)
            return land, passed, newcomers, hunts, candidates
        return services.least_hunted(candidates, hunts, chooser), [], {}, hunts, candidates

    def pick_land(db, tick, previous, now=None):
        """The land that stirs at `tick`: the least hunted lately, never
        `previous` - and (game 0.50.0, insights/) never one where a new player
        is learning, while another will do; a tie settled by a Random seeded
        with the tick."""
        return pick_land_why(db, tick, previous, now)[0]

    def begin_stir(db, tick, element, started_at, settings, chosen_by="", ends_at=None):
        """Write stir `tick`. True if this call wrote it - and only then is it
        announced; a tick somebody else wrote first is theirs to announce.
        `ends_at` for a stir an event set (game 0.48.0), else a whole stir.
        Caller commits."""
        ends_at = int(started_at) + int(settings["hours"]) * 3600 if ends_at is None else int(ends_at)
        if not data.begin(db, tick, element, started_at, ends_at, chosen_by):
            return False
        announce(services.stir_text(element, settings, services.stir_hours(started_at, ends_at)), chosen_by)
        return True

    def current_world_tick(db, settings=None, now=None):
        """The stir going on now - writing the next one if the last has ended -
        or None while the lands are switched off. Caller commits."""
        settings = world_settings() if settings is None else settings
        if not settings["on"]:
            return None
        now = int(time.time()) if now is None else int(now)
        last = data.last_tick(db)
        # A STIR EVENT (game 0.48.0, events/) WINS: while one is on, its land
        # stirs until the event ends. The stir going on when it begins ends
        # then; once it is over, the rhythm carries on from its end.
        happening = events()
        forced = happening.forced_stir(db, now) if happening is not None else None
        if forced is not None:
            by = "event #%d" % int(forced["id"])
            if last is not None and (last["chosen_by"] or "") == by and now < int(last["ends_at"]):
                return services.row_of(last)
            if last is not None and int(last["ends_at"]) > now:
                data.end_at(db, last["tick"], now)
            tick = int(last["tick"]) + 1 if last is not None else 1
            begin_stir(db, tick, int(forced["land"]), now, settings, by, ends_at=int(forced["ends_at"]))
            return services.row_of(data.tick_row(db, tick))
        if last is not None and (last["chosen_by"] or "").startswith("event #") and now < int(last["ends_at"]):
            # ITS EVENT WAS CALLED OFF: the stir it set ends now, and the
            # rhythm starts again from here.
            data.end_at(db, last["tick"], now)
            last = data.last_tick(db)
        if last is not None and now < int(last["ends_at"]):
            return services.row_of(last)
        tick = int(last["tick"]) + 1 if last is not None else 1
        length = int(settings["hours"]) * 3600
        # THE RHYTHM HOLDS while people are playing: the next stir begins where
        # the last one ended. After a quiet spell longer than a whole stir, it
        # begins now - nobody was there to see the ones in between.
        started = int(last["ends_at"]) if last is not None and now - int(last["ends_at"]) < length else now
        previous = int(last["element"]) if last is not None else 0
        begin_stir(db, tick, pick_land(db, tick, previous, now), started, settings)
        return services.row_of(data.tick_row(db, tick))

    def world_next_stir(db, now=None):
        """The next stir as things stand (game 0.50.0): {element, name, at,
        reason} - the land the world would pick when the stir on now ends, or
        a stir event's - or None while the lands are off or nothing stirs."""
        now = int(time.time()) if now is None else int(now)
        settings = world_settings()
        if not settings["on"]:
            return None
        row = current_world_tick(db, settings, now)
        if row is None:
            return None
        at = int(row["ends_at"])
        happening = events()
        forced = happening.forced_stir(db, at) if happening is not None else None
        if forced is not None:
            land = int(forced["land"])
            return {"element": land, "name": LANDS.get(land, ""), "at": at,
                    "reason": "%s: %s is on then." % (LANDS.get(land, "").capitalize(), forced["name"])}
        land, passed, newcomers, hunts, candidates = pick_land_why(db, int(row["tick"]) + 1, int(row["element"]), now)
        chosen_by = insights()
        why = chosen_by.reason(land, hunts, newcomers, passed, candidates) if chosen_by is not None else \
            "%s: the least hunted in the last day." % LANDS.get(land, "").capitalize()
        return {"element": land, "name": LANDS.get(land, ""), "at": at, "reason": why}

    def world_public(db):
        """The world as the game needs it, on the broadcast poll: which land
        stirs, until when, and the numbers the game applies itself. "element"
        0 for none - switched off, or anything at all going wrong."""
        try:
            settings = world_settings()
            row = current_world_tick(db, settings)
        except Exception:   # the heartbeat must outlive the world
            app.logger.exception("[WORLD] could not read the stirring lands; nothing stirs")
            return {"on": False, "tick": 0, "element": 0}
        if row is None:
            return {"on": bool(settings["on"]), "tick": 0, "element": 0}
        return services.public(row, settings, time.time())

    def world_stir_for(db, enemy_id):
        """(bag scale, XP scale) for a kill of `enemy_id`: the stirring land's,
        if the monster is of it, times the events on now (game 0.48.0,
        events/) - and (1.0, 1.0) for any part that goes wrong."""
        element = 0
        stirring = 0
        bags, xp = 1.0, 1.0
        try:
            element = element_of(enemy_id)
            settings = world_settings()
            row = current_world_tick(db, settings)
            stirring = int(row["element"]) if row is not None else 0
            if row is not None and element == row["element"]:
                bags, xp = services.stirred_kill(settings)
        except Exception:
            app.logger.exception("[WORLD] could not read the stirring lands for a kill; an ordinary kill")
            bags, xp = 1.0, 1.0
        happening = events()
        if happening is not None:
            event_bags, event_xp = happening.kill_scales(db, element, stirring)
            bags, xp = bags * event_bags, xp * event_xp
            # THE GUARDRAILS' CEILING (game 0.49.0): whatever is on, and
            # whatever the stirring lands were raised to after an event was
            # set, a kill is paid at most 3x XP, 3x loot and 6x the two
            # together.
            bags, xp = happening.ceiling(bags, xp)
        return bags, xp

    def owner_payload(db):
        settings = world_settings()
        state = world_public(db)
        recent = [services.row_of(r) for r in data.recent(db, WORLD_RECENT_TICKS)]
        try:
            hunts = land_hunts(db)
        except sqlite3.Error:
            hunts = {element: 0 for element in LANDS}
        try:
            upcoming = world_next_stir(db)
        except Exception:  # noqa: BLE001 - the owner's page must outlive a guess
            app.logger.exception("[WORLD] could not work out the next stir")
            upcoming = None
        return {
            "settings": settings,
            "state": state,
            "recent": recent,
            "lands": [{"element": e, "name": LANDS[e], "kills": hunts.get(e, 0)} for e in sorted(LANDS)],
            # THE NEXT STIR AND WHY (game 0.50.0): what the world would pick
            # now for when this stir ends. The World tab offers it for "Stir it
            # now".
            "next": upcoming,
            "limits": {k: [low, high] for k, (low, high, _) in WORLD_LIMITS.items()},
            "now": int(time.time()),
        }

    def read_world():
        """
        The stirring lands: the settings, the land stirring now, the last few, and
        how hunted each land has been (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: settings, state, recent, lands, limits
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        db = get_db()
        payload = owner_payload(db)
        db.commit()   # a stir written by asking is kept
        return payload, 200

    def set_world():
        """
        Change the stirring lands' settings (owner only): any of on, hours,
        hit_on, hit_percent, health_on, health_percent, speed_on, speed_percent,
        bags_on, bags_times, resist_on, resist_times, xp_on, xp_percent
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: settings
            schema:
              type: object
        responses:
          200:
            description: The settings as they now stand, with the world
          400:
            description: An unknown setting, or a value of the wrong kind or out of range
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not payload:
            return bad_request("send the settings to change, e.g. {\"on\": true}")
        current = world_settings()
        changed, refusal = services.changed_settings(current, payload)
        if refusal is not None:
            return bad_request(refusal)

        db = get_db()
        set_setting(WORLD_KEY, json.dumps(changed), g.user["username"])
        if current["on"] and not changed["on"]:
            announce("The lands grow still.", g.user["username"])
        elif changed["on"] and not current["on"]:
            # BACK ON: a stir still running is said again; one that ended is
            # replaced by asking, and that one announces itself.
            before = data.highest_tick(db)
            row = current_world_tick(db, changed)
            if row is not None and row["tick"] == (before or 0):
                announce(services.stir_text(row["element"], changed, int(changed["hours"])), g.user["username"])
        log_staff_action(g.user, "world", "server", None,
                         ", ".join("%s=%s" % (k, payload[k]) for k in sorted(payload)))
        db.commit()
        return owner_payload(db), 200

    def stir_world():
        """
        Stir a land now, for a whole stir (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: land
            schema:
              type: object
              required: [element]
              properties:
                element: {type: integer, description: "1 dark, 2 light, 3 ice, 4 wind, 5 earth, 6 fire, 7 water"}
        responses:
          200:
            description: The world, with the new stir
          400:
            description: Not one of the seven lands
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
          409:
            description: The stirring lands are switched off
        """
        payload = request.get_json(silent=True) or {}
        element = payload.get("element")
        if isinstance(element, bool) or not isinstance(element, int) or element not in LANDS:
            return bad_request("element is one of the seven lands, 1 to 7")
        settings = world_settings()
        if not settings["on"]:
            return {"error": "Conflict", "message": "The stirring lands are switched off - switch them on first."}, 409
        db = get_db()
        now = int(time.time())
        last = data.last_tick(db)
        # THE STIR GOING ON ENDS NOW, and the owner's begins: a whole stir
        # long, not the rest of the one it replaced.
        if last is not None and int(last["ends_at"]) > now:
            data.end_at(db, last["tick"], now)
        tick = int(last["tick"]) + 1 if last is not None else 1
        begin_stir(db, tick, element, now, settings, g.user["username"])
        log_staff_action(g.user, "world", "server", None, "stirred the %s lands" % LANDS[element])
        db.commit()
        return owner_payload(db), 200

    app.add_url_rule("/api/server/world", endpoint="read_world",
                     view_func=require_auth(require_owner(read_world)), methods=["GET"])
    app.add_url_rule("/api/server/world", endpoint="set_world",
                     view_func=require_auth(require_owner(require_fresh_password(set_world))), methods=["POST"])
    app.add_url_rule("/api/server/world/stir", endpoint="stir_world",
                     view_func=require_auth(require_owner(require_fresh_password(stir_world))), methods=["POST"])

    return SimpleNamespace(settings=world_settings, land_hunts=land_hunts, pick_land=pick_land,
                           begin_stir=begin_stir, current_tick=current_world_tick, next_stir=world_next_stir,
                           public=world_public, stir_for=world_stir_for)
