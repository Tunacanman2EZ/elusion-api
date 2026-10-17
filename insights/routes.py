"""insights/routes.py - the HTTP edge of the land insights, and InsightsAPI,
the one object app.py keeps for the world's own pick.

    GET /api/server/insights   owner: {lands, next, hours, total_week, now}

require_owner - the same bare 404 as no route to anybody else - and read
only, so it asks for no password again.
"""
import time

from . import data, services


class InsightsAPI:
    """What the world asks: who is learning where, and which land stirs
    next. Every answer safe - a failure is the old rule (least hunted)."""

    def __init__(self, lands, element_of, left_out_names, log_error):
        self.lands = dict(lands)
        self._element_of = element_of
        self._left_out_names = left_out_names or (lambda: ())
        self._log_error = log_error

    def element_of(self, enemy_id):
        try:
            return int(self._element_of(enemy_id) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def left_out_names(self):
        try:
            return tuple(self._left_out_names())
        except Exception:  # noqa: BLE001
            return ()

    def newcomers(self, db, now=None):
        """{land: new players with a kill there in the last NEWCOMER_WINDOW}."""
        now = int(time.time()) if now is None else int(now)
        return services.people_per_land(
            data.newcomers_by_enemy(db, now - services.NEWCOMER_WINDOW, services.NEW_PLAYER_LEVEL,
                                    self.left_out_names()), self.element_of, self.lands)

    def choose(self, db, candidates, hunts, chooser, now=None):
        """(land, passed over, newcomers) for the next stir. Never raises: a
        failure is the least hunted, the rule before new players counted."""
        try:
            met = self.newcomers(db, now)
            land, passed = services.choose(candidates, hunts, met, chooser)
            return land, passed, met
        except Exception:  # noqa: BLE001
            self._log_error("[INSIGHTS] could not weigh the new players; the least hunted stirs")
            least = min(hunts.get(e, 0) for e in candidates)
            return chooser.choice([e for e in sorted(candidates) if hunts.get(e, 0) == least]), [], {}

    def reason(self, land, hunts, newcomers, passed, candidates):
        return services.reason(land, hunts, newcomers, passed, candidates, self.lands)


def register(app, *, require_auth, require_owner, get_db, lands, element_of, left_out_names=None,
             next_stir=None):
    """Add the route and hand back the InsightsAPI. `element_of(enemy_id)` is
    a monster's land; `left_out_names()` the owner and co-owners, never
    counted as players; `next_stir(db)` the world's next stir and why (or
    None) - app.py's, which knows the ticks."""
    api = InsightsAPI(lands, element_of, left_out_names, lambda message: app.logger.exception(message))
    next_stir = next_stir or (lambda _db: None)

    def read_insights():
        """
        Every land: kills today and this week, players, new players there now, when it last stirred and had an event, its status and busiest hour; the next stir and why; who is online by hour (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
        responses:
          200:
            description: lands (one per land), next (or null), hours (24 averages by UTC hour, and days counted), total_week, few, now
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        now = int(time.time())
        db = get_db()
        names = api.left_out_names()
        day = services.per_land(data.kills_by_enemy(db, now - services.DAY), api.element_of, api.lands)
        week = services.per_land(data.kills_by_enemy(db, now - services.WEEK), api.element_of, api.lands)
        players = services.people_per_land(data.players_by_enemy(db, now - services.WEEK, names),
                                           api.element_of, api.lands)
        newcomers = api.newcomers(db, now)
        events = services.last_event_for(data.events_begun(db, now - 60 * services.DAY, now), api.lands)
        busiest = services.busiest_hour(data.kills_by_enemy_hour(db, now - services.HOURS_DAYS * services.DAY),
                                        api.element_of, api.lands)
        sampled, first = data.online_samples(db, (now - services.HOURS_DAYS * services.DAY) // services.SAMPLE_SECONDS,
                                             now // services.SAMPLE_SECONDS + 1)
        try:
            upcoming = next_stir(db)
        except Exception:  # noqa: BLE001
            app.logger.exception("[INSIGHTS] could not work out the next stir")
            upcoming = None
        db.commit()   # asking may have begun the next stir, as a poll would
        total = sum(week.values())
        return {"lands": services.lands_report(api.lands, day, week, players, newcomers, data.last_stirred(db),
                                               events, busiest),
                "next": upcoming, "hours": services.online_by_hour(sampled, first, now),
                "total_week": total, "few": total < services.FEW_KILLS,
                "new_player_level": services.NEW_PLAYER_LEVEL, "now": now}, 200

    app.add_url_rule("/api/server/insights", endpoint="read_insights",
                     view_func=require_auth(require_owner(read_insights)), methods=["GET"])
    return api
