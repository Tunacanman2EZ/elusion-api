"""events/routes.py - the HTTP edge of the world's events, and EventsAPI, the
one object app.py keeps for the rest of the server to ask.

    GET  /api/world/events/active     logged in: {events, next, now}
    GET  /api/world/events/schedule   owner: {events (all, newest first), limits, now}
    POST /api/world/events            owner, password again: schedule one
    POST /api/world/events/cancel     owner, password again: call one off {id}
    POST /api/world/events/preview    owner: the dry run - would it be allowed,
                                      the most it pays, what it might bring in
    GET  /api/world/events/report     owner: each event's before, during and
                                      after, and who was online when

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

    def __init__(self, get_db, announce, lands, log_error, left_out_names=None, stir=None,
                 online_window=45):
        self._get_db = get_db
        self._announce = announce
        self.lands = dict(lands)
        self._log_error = log_error
        self._left_out_names = left_out_names or (lambda: ())
        self._stir = stir or services.no_stir
        self.online_window = int(online_window)
        # The last minute this process counted who is online.
        self._counted_minute = None

    def left_out_names(self):
        try:
            return tuple(self._left_out_names())
        except Exception:  # noqa: BLE001
            return ()

    def stir(self):
        """The stirring lands' own XP and loot as set now: {on, xp, bags}."""
        try:
            said = self._stir()
            return {"on": bool(said.get("on")), "xp": float(said.get("xp", 1.0)), "bags": float(said.get("bags", 1.0))}
        except Exception:  # noqa: BLE001 - unknown is "as if off"
            self._log_error("[EVENTS] could not read the stirring lands' settings")
            return services.no_stir()

    def sample(self, db, now=None):
        """Count who is online, at most once a minute per process, into the
        five minutes' row (which keeps the most it was told). Caller commits.
        Never raises: this rides the poll."""
        now = int(time.time()) if now is None else int(now)
        minute = now // services.SAMPLE_EVERY
        if minute == self._counted_minute:
            return
        self._counted_minute = minute
        try:
            everyone, players = services.count_online(data.online_now(db, now - self.online_window, now),
                                                      self.left_out_names())
            bucket = now // services.SAMPLE_SECONDS
            data.write_sample(db, bucket, now, everyone, players)
            if minute % 60 == 0:
                data.prune_samples(db, bucket - services.SAMPLES_KEPT // services.SAMPLE_SECONDS)
        except Exception:  # noqa: BLE001
            self._log_error("[EVENTS] could not count who is online")

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

    @staticmethod
    def ceiling(bags, xp):
        """A kill's whole (bag scale, XP scale) - events and the stirring land
        together - held to the guardrails' ceilings."""
        return services.ceiling(bags, xp)

    def forced_stir(self, db, now=None):
        """The stir event on now, as a row, or None."""
        now = int(time.time()) if now is None else int(now)
        try:
            return services.forced_stir(self._live(db, now), now)
        except Exception:  # noqa: BLE001
            self._log_error("[EVENTS] could not read the events for the stirring lands")
            return None


def register(app, *, require_auth, require_owner, require_fresh_password, get_db, announce,
             log_action, clean_text, lands, left_out_names=None, stir=None, element_of=None,
             online_window=45, kills_kept=14 * 86400):
    """Add the routes to `app`, make the tables, and hand back the EventsAPI.
    `announce(body, by)` posts to everyone; `log_action(detail)` writes the
    staff log for the caller; `clean_text(raw, limit)` cleans typed words;
    `lands` is {element: name}. For the guardrails, the dry run and the
    report (0.49.0): `left_out_names()` the owner and co-owners, never counted
    as players; `stir()` the stirring lands' own {on, xp, bags} as set now;
    `element_of(enemy_id)` a monster's land; `online_window` how recently a
    session must have been seen to be online; `kills_kept` how long the kill
    log is kept, past which a window's XP is not known."""
    api = EventsAPI(get_db, announce, lands, lambda message: app.logger.exception(message),
                    left_out_names=left_out_names, stir=stir, online_window=online_window)
    element_of = element_of or (lambda _enemy: 0)
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
        # THE GUARDRAILS (0.49.0): not past the ceilings with everything else
        # on at the same time.
        others = data.overlapping(db, event["starts_at"], event["ends_at"])
        why, _worst = services.guard(event, others, api.stir(), api.lands)
        if why is not None:
            return {"error": "Conflict", "message": why}, 409
        warned = services.warnings(event, others, api.stir(), api.lands)
        event["id"] = data.insert(db, event, g.user["username"], now)
        if event["starts_at"] > now:
            announce(services.coming_text(event, now, api.lands), g.user["username"])
        log_action("scheduled #%d %s (%s %s, land %d) for %s"
                   % (event["id"], event["name"], event["kind"], event["amount"], event["land"],
                      services.span_words(event["ends_at"] - event["starts_at"])))
        db.commit()
        return {"event": services.public_event(event, api.lands), "warnings": warned, "now": now}, 201

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

    def preview_event():
        """
        The dry run (owner only): would this event be allowed, the most any land's monsters would pay, and what it might bring in
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
              properties:
                players:
                  type: integer
                  description: how many players to forecast for (1-500, 20 if not sent)
        responses:
          200:
            description: allowed, why (if not), warnings, worst (each land), limits, forecast, peak_online
          400:
            description: Not an event this server can hold
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        now = int(time.time())
        payload = request.get_json(silent=True)
        event, why = services.validate(payload, now, api.lands, clean_text)
        if event is None:
            return {"error": "Bad Request", "message": why}, 400
        players = payload.get("players", services.FORECAST_PLAYERS) if isinstance(payload, dict) else None
        if isinstance(players, bool) or not isinstance(players, int) or not 1 <= players <= services.MOST_FORECAST:
            return {"error": "Bad Request", "message": "players is a whole number from 1 to %d"
                    % services.MOST_FORECAST}, 400
        event["id"] = None
        db = get_db()
        stir = api.stir()
        others = data.overlapping(db, event["starts_at"], event["ends_at"])
        refused, worst = services.guard(event, others, stir, api.lands)
        since = now - services.BASELINE_DAYS * services.DAY
        busy = [(int(e["starts_at"]), services.ended_at(e)) for e in data.started_between(db, since - 3 * services.DAY, now)
                if e["kind"] in ("xp", "loot")]
        base = services.baseline(data.xp_by_player_hour(db, since), data.gold_by_player_hour(db, since),
                                 data.kills_by_enemy(db, since), element_of, busy)
        sampled = data.samples(db, since // services.SAMPLE_SECONDS, now // services.SAMPLE_SECONDS + 1)
        return {"allowed": refused is None, "why": refused or "",
                "warnings": services.warnings(event, others, stir, api.lands),
                "event": dict(services.public_event(dict(event, id=0), api.lands), id=None),
                "worst": worst, "with": [e["name"] for e in others],
                "limits": {"xp": services.MOST_XP, "loot": services.MOST_LOOT, "together": services.MOST_TOGETHER},
                "forecast": services.forecast(event, players, base, api.lands, stir["on"]),
                "peak_online": max([v[1] for v in sampled.values()] or [0]), "now": now}, 200

    def read_event_report():
        """
        Each event of the last 30 days: the window before it, its own, the one after - XP and gold per hour, who played, who was online (owner only)
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
            description: events (newest first, each with windows, lift, verdict), kinds, online (the last 7 days), sampling_since, now
          401:
            description: Missing, invalid or expired token
          404:
            description: Not the owner
        """
        now = int(time.time())
        db = get_db()
        names = api.left_out_names()
        first = data.first_sample_bucket(db)
        reports = []
        for event in data.started_between(db, now - services.REPORT_DAYS * services.DAY, now):
            spans = services.windows(event, now)
            low = min(span[0] for span in spans.values() if span)
            high = max(span[1] for span in spans.values() if span)
            sampled = data.samples(db, low // services.SAMPLE_SECONDS, high // services.SAMPLE_SECONDS + 1)
            figures = {}
            for name, span in spans.items():
                if span is None:
                    figures[name] = None
                    continue
                start, end = span
                kills, xp, played = data.kills_in(db, start, end, names)
                figures[name] = services.window_figures(
                    start, end, kills, xp, played, data.gold_in(db, start, end),
                    data.new_players_in(db, start, end, names),
                    services.online_figures(sampled, start, end, first), start >= now - kills_kept)
            row = services.public_event(event, api.lands)
            row.update({"ended_at": services.ended_at(event), "cancelled": bool(event["cancelled_at"]),
                        "state": ("on" if services.is_on(event, now) else "cancelled" if event["cancelled_at"]
                                  else "over"), "windows": figures})
            row["lift"] = services.lift(row)
            row["verdict"] = services.verdict(row)
            reports.append(row)
        chart_from = now - services.CHART_DAYS * services.DAY
        chart = data.samples(db, chart_from // services.SAMPLE_SECONDS, now // services.SAMPLE_SECONDS + 1)
        return {"events": reports, "kinds": services.by_kind(reports),
                "online": [{"at": bucket * services.SAMPLE_SECONDS, "everyone": counts[0], "players": counts[1]}
                           for bucket, counts in sorted(chart.items())],
                "chart_from": chart_from, "sample_seconds": services.SAMPLE_SECONDS,
                "sampling_since": first * services.SAMPLE_SECONDS if first is not None else 0,
                "kills_kept_days": kills_kept // services.DAY, "now": now}, 200

    app.add_url_rule("/api/world/events/active", endpoint="read_active_events",
                     view_func=require_auth(read_active_events), methods=["GET"])
    app.add_url_rule("/api/world/events/schedule", endpoint="read_event_schedule",
                     view_func=require_auth(require_owner(read_event_schedule)), methods=["GET"])
    app.add_url_rule("/api/world/events", endpoint="schedule_event",
                     view_func=require_auth(require_owner(require_fresh_password(schedule_event))), methods=["POST"])
    app.add_url_rule("/api/world/events/cancel", endpoint="cancel_event",
                     view_func=require_auth(require_owner(require_fresh_password(cancel_event))), methods=["POST"])
    # READ ONLY, the two of 0.49.0: neither writes, so neither asks for the
    # password again.
    app.add_url_rule("/api/world/events/preview", endpoint="preview_event",
                     view_func=require_auth(require_owner(preview_event)), methods=["POST"])
    app.add_url_rule("/api/world/events/report", endpoint="read_event_report",
                     view_func=require_auth(require_owner(read_event_report)), methods=["GET"])
    return api
