"""events/routes.py - the HTTP edge of the world's events, and EventsAPI, the
one object app.py keeps for the rest of the server to ask.

    GET  /api/world/events/active     logged in: {events, next, now}
    GET  /api/world/events/schedule   owner: {events (all, newest first), limits, now}
    POST /api/world/events            owner, password again: schedule one
    POST /api/world/events/cancel     owner, password again: call one off {id}

The owner's routes are require_owner - the same bare 404 as no route to
anybody else - and the two that change the world ask for the password again
(require_fresh_password), like the stirring lands' switches. Both are written
in the staff log as "event".
"""
import time

from flask import g, request

from . import data, services


class EventsAPI:
    """What the rest of the server asks about events, every answer safe: any
    failure is "no event", because the poll that carries them is also the
    heartbeat, and a kill must always be paid."""

    def __init__(self, get_db, announce, lands, log_error):
        self._get_db = get_db
        self._announce = announce
        self.lands = dict(lands)
        self._log_error = log_error

    def _live(self, db, now):
        return data.live(db, now, services.AHEAD)

    def public(self, db, now=None):
        """{events, next, now} - and the first to ask after an event begins
        says so to everyone, once. Caller commits."""
        now = int(time.time()) if now is None else int(now)
        try:
            rows = self._live(db, now)
            for event in rows:
                if services.is_on(event, now) and not event["announced"] and data.mark_announced(db, event["id"]):
                    self._announce(services.begun_text(event, now, self.lands), event["created_by"])
            return services.active_and_next(rows, now, self.lands)
        except Exception:  # noqa: BLE001 - the heartbeat must outlive the events
            self._log_error("[EVENTS] could not read the events; none are on")
            return {"events": [], "next": [], "now": now}

    def kill_scales(self, db, element, stirring_land, now=None):
        """(bag scale, XP scale) for a kill of a monster of `element`."""
        now = int(time.time()) if now is None else int(now)
        try:
            return services.kill_scales(self._live(db, now), now, int(element or 0), int(stirring_land or 0))
        except Exception:  # noqa: BLE001
            self._log_error("[EVENTS] could not read the events for a kill; an ordinary kill")
            return 1.0, 1.0

    def forced_stir(self, db, now=None):
        """The stir event on now, as a row, or None."""
        now = int(time.time()) if now is None else int(now)
        try:
            return services.forced_stir(self._live(db, now), now)
        except Exception:  # noqa: BLE001
            self._log_error("[EVENTS] could not read the events for the stirring lands")
            return None


def register(app, *, require_auth, require_owner, require_fresh_password, get_db, announce,
             log_action, clean_text, lands):
    """Add the routes to `app`, make the table, and hand back the EventsAPI.
    `announce(body, by)` posts to everyone; `log_action(detail)` writes the
    staff log for the caller; `clean_text(raw, limit)` cleans typed words;
    `lands` is {element: name}."""
    api = EventsAPI(get_db, announce, lands, lambda message: app.logger.exception(message))
    with app.app_context():
        db = get_db()
        data.ensure_tables(db)
        db.commit()

    def read_active_events():
        """
        The world's events: what is on now and what is coming (any player)
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
            description: events (on now), next (coming, nearest first), now (the server's clock)
          401:
            description: Missing, invalid or expired token
        """
        db = get_db()
        body = api.public(db)
        db.commit()   # an event announced by asking is kept
        return body, 200

    def read_event_schedule():
        """
        The world's events, past, present and to come, cancelled ones too (owner only)
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
            description: events (newest start first), limits, now
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        now = int(time.time())
        db = get_db()
        rows = data.schedule(db, now - 30 * services.DAY)
        listed = []
        for event in rows:
            row = services.public_event(event, api.lands)
            row.update({"created_by": event["created_by"], "cancelled": bool(event["cancelled_at"]),
                        "cancelled_by": event["cancelled_by"],
                        "state": ("cancelled" if event["cancelled_at"] else "on" if services.is_on(event, now)
                                  else "over" if event["ends_at"] <= now else "coming")})
            listed.append(row)
        return {"events": listed, "now": now,
                "limits": {"amount": [services.AMOUNT_LOW, services.AMOUNT_HIGH],
                           "hours": [services.SHORTEST // services.HOUR, services.LONGEST // services.HOUR],
                           "days_ahead": services.FURTHEST // services.DAY,
                           "waiting": services.MOST_WAITING}}, 200

    def schedule_event():
        """
        Schedule an event (owner only): kind xp, loot or stir; starts_at; hours; amount or land; name
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: event
            schema:
              type: object
              required: [kind, starts_at, hours]
        responses:
          201:
            description: The event as scheduled
          400:
            description: Not an event this server can hold
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
          409:
            description: Too many events already waiting
        """
        now = int(time.time())
        event, why = services.validate(request.get_json(silent=True), now, api.lands, clean_text)
        if event is None:
            return {"error": "Bad Request", "message": why}, 400
        db = get_db()
        if data.count_upcoming(db, now) >= services.MOST_WAITING:
            return {"error": "Conflict", "message": "%d events are already waiting - call one off first."
                    % services.MOST_WAITING}, 409
        event["id"] = data.insert(db, event, g.user["username"], now)
        if event["starts_at"] > now:
            announce(services.coming_text(event, now, api.lands), g.user["username"])
        log_action("scheduled #%d %s (%s %s, land %d) for %s"
                   % (event["id"], event["name"], event["kind"], event["amount"], event["land"],
                      services.span_words(event["ends_at"] - event["starts_at"])))
        db.commit()
        return {"event": services.public_event(event, api.lands), "now": now}, 201

    def cancel_event():
        """
        Call off an event, on now or still to come (owner only)
        ---
        tags:
          - Status
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: event
            schema:
              type: object
              required: [id]
        responses:
          200:
            description: Called off
          400:
            description: No id
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner, or no such event still to happen
        """
        payload = request.get_json(silent=True) or {}
        event_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(event_id, bool) or not isinstance(event_id, int):
            return {"error": "Bad Request", "message": "send the event's id"}, 400
        now = int(time.time())
        db = get_db()
        event = data.get(db, event_id)
        if event is None or not data.cancel(db, event_id, g.user["username"], now):
            return {"error": "Not Found", "message": "No such event still to happen."}, 404
        announce(services.called_off_text(event, now), g.user["username"])
        log_action("called off #%d %s" % (event_id, event["name"]))
        db.commit()
        return {"cancelled": event_id, "now": now}, 200

    app.add_url_rule("/api/world/events/active", endpoint="read_active_events",
                     view_func=require_auth(read_active_events), methods=["GET"])
    app.add_url_rule("/api/world/events/schedule", endpoint="read_event_schedule",
                     view_func=require_auth(require_owner(read_event_schedule)), methods=["GET"])
    app.add_url_rule("/api/world/events", endpoint="schedule_event",
                     view_func=require_auth(require_owner(require_fresh_password(schedule_event))), methods=["POST"])
    app.add_url_rule("/api/world/events/cancel", endpoint="cancel_event",
                     view_func=require_auth(require_owner(require_fresh_password(cancel_event))), methods=["POST"])
    return api
