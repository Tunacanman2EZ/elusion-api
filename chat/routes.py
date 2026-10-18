"""chat/routes.py - the edge: the chat routes, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth       the token check (puts the account on g.user)
    require_role       a staff rank -> its decorator (chat_delete)
    no_write_lock      marks a route that does slow work before it writes
    get_db             the request's database connection
    bad_request        a message -> app.py's 400
    find_user          a name -> its users row, or None
    clean_text         (raw, limit) -> one line, as typed (clean_player_text)
    role_for           users row -> the rank a list shows
    name_hue_of        users row -> the colour they chose, or None
    guild_tag          a guild's name -> how it is drawn
    role_at_least      (users row, rank) -> whether they hold it
    can_act_on         (actor, target) -> whether staff may act on them
    has_owner_powers   username -> whether they hold the owner's powers
    log_staff_action   (user, action, target, target_id, detail) - the log
    mute_state         users row -> None, or the mute they are under
    describe_mute      a mute -> the sentence a muted player reads
    friend_ids         (db, user_id) -> the accounts that accepted this one
    guild_membership   user_id -> their guild_members row, or None
    party_membership   (db, user_id) -> their party_members row, or None
    keep_per_channel   () -> the tail kept per channel (a tunable, asked)
    ignore_limit       () -> the most an account may ignore (a tunable, asked)
    fetch_image        url -> (bytes, None) or (None, why): the relay's one
                       call out, every hop checked against the server's own
                       network - app.py's, and a seam the suites replace

It hands back what other areas ask of chat (see __init__.py): post_chat(),
can_read_row(), may_see_image(), ignores().

THE ENDPOINT NAMES ARE app.py's - chat_send, read_chat, chat_relay_image,
chat_upload_image (two paths), chat_read_image, chat_delete, list_ignores,
add_ignore, remove_ignore - and routes.lock holds them.
"""
import re
import time
from types import SimpleNamespace

from flask import g, request

from . import data, services
from .services import (CHAT_CHANNELS, CHAT_DEFAULT_CHANNEL, CHAT_DELETION_WINDOW_SECONDS, CHAT_PAGE_LIMIT,
                       GUILD_CHAT_NO_GUILD, IMAGE_COOLDOWN_SECONDS, IMAGE_MAX_BYTES, MAX_CHAT_LENGTH,
                       PARTY_CHAT_NO_PARTY, WORLD_IMAGE_COOLDOWN_SECONDS)


def register(app, *, require_auth, require_role, no_write_lock, get_db, bad_request, find_user, clean_text,
             role_for, name_hue_of, guild_tag, role_at_least, can_act_on, has_owner_powers, log_staff_action,
             mute_state, describe_mute, friend_ids, guild_membership, party_membership, keep_per_channel,
             ignore_limit, fetch_image):

    # =========================================================================
    # WHAT OTHER AREAS ASK OF CHAT
    # =========================================================================

    def post_chat(user_row, body, channel="world", target_id=0, image_id=""):
        """
        Record one player message. Caller commits, like post_broadcast().

        Returns the new row id, or None when there was nothing to say - which
        now means neither words NOR a picture, because a picture on its own is
        a perfectly good message.
        """
        text = clean_text(body, MAX_CHAT_LENGTH)
        if text == "" and not image_id:
            return None

        db = get_db()
        now = int(time.time())
        # DENORMALISED, LIKE username AND role BESIDE IT, and for the same
        # reasons this row already carries those: the read is a poll every
        # three seconds and must not grow a join, and the line should still
        # make sense after the author has left the guild - or after the guild
        # is gone entirely.
        #
        # It is a SNAPSHOT, then: what they were flying when they said it, not
        # what they fly now. That is the honest reading of a chat log and it is
        # the rule the two columns next to it already follow.
        new_id = data.add_line(db, user_row["id"], user_row["username"], role_for(user_row), text, now,
                               channel, target_id, image_id, data.guild_said_from(db, user_row["id"]),
                               name_hue_of(user_row))
        data.prune(db, channel, now, keep_per_channel())
        return new_id

    def can_read_row(db, user_id, row):
        """Could `user_id` have read this line? The read's own rules, one line
        at a time - a report is only for a line you were shown."""
        channel = str(row["channel"] or CHAT_DEFAULT_CHANNEL)
        author = int(row["user_id"] or 0)
        if channel == "world":
            return True
        if channel == "private":
            return user_id in (author, int(row["target_id"] or 0))
        if channel == "friends":
            return author == user_id or author in friend_ids(db, user_id)
        if channel == "guild":
            seat = guild_membership(user_id)
            return seat is not None and int(seat["guild_id"]) == int(row["target_id"] or 0)
        if channel == "party":
            seat = party_membership(db, user_id)
            return seat is not None and int(seat["party_id"]) == int(row["target_id"] or 0)
        return False

    def may_see_image(db, user_row, image_id):
        """True when `user_row` could read a line showing `image_id`, put those
        bytes on the server themselves, or is staff with a report about it.

        WHO MAY SEE A PICTURE. A picture used to be a pure capability: any
        signed-in player holding its id was served it, because 256 unguessable
        bits could only have come from being shown it. Two honest limits
        followed from that, and both are closed here and in chat_send():

          - A PRIVATE PICTURE GETS ITS OWN ROW. The store deduplicates on the
            content hash, so a picture whispered and the same picture in world
            chat were one row with one id. Sending a picture to whispers,
            friends or a guild now makes a copy under a fresh random id
            (data.private_copy()), so a private picture never shares an id with
            anything else.
          - FETCHING ONE IS CHECKED. GET /api/chat/image/<id> serves a picture
            only to someone who could read a line showing it, by the read's own
            rules (can_read_row()); to an account that put those bytes on the
            server (chat_image_holders); or to a mod looking at a report of it.

        What it costs: a picture now lasts as long as a line showing it, which
        is the chat's own 24 hours, instead of until the 128 MB store happened
        to evict it. Refused is the same 404 as missing, so the route still says
        nothing about which ids exist."""
        uid = int(user_row["id"])
        for shown in data.lines_showing(db, image_id):
            if can_read_row(db, uid, shown):
                return True
        if data.holds(db, image_id, uid):
            return True
        # A REPORT IS A LINE STAFF WERE SHOWN. Somebody reported a whisper with
        # a picture in it; the mod judging it has to be able to look at it.
        return role_at_least(user_row, "mod") and data.reported(db, image_id)

    # =========================================================================
    # THE RULES THE ROUTES SHARE
    # =========================================================================

    def spend_chat_token(user_id):
        """
        Take one token from this account's chat bucket, refilling it first.

        Returns None when the message may go ahead, or a response tuple when it
        may not. The caller commits; on the refusal path there is nothing to
        commit.
        """
        db = get_db()
        row = data.bucket(db, user_id)
        if row is None:
            # The token authenticated against a row that has since been deleted.
            return {"error": "Unauthorized", "message": "No such account."}, 401
        now_ms = int(time.time() * 1000)
        tokens = services.refilled(row["chat_tokens"], row["last_chat_at"], now_ms)
        refusal = services.too_fast(tokens)
        if refusal is not None:
            return refusal
        data.spend_token(db, user_id, tokens - 1.0, now_ms)
        return None

    def chat_write_check(channel, target_row):
        """
        May the caller say something here? Returns None, or a refusal to return.

        THE ONE PLACE THAT DECIDES. Every channel's rule is on this screen,
        which is the only way to be sure a new one has been given a rule at all.
        """
        if channel == "world":
            return None

        if channel == "friends":
            # No target: a line here goes to whoever has accepted you, which the
            # read side works out from the friends table at the time of reading.
            return None

        if channel == "private":
            if target_row is None:
                return bad_request("private messages need a 'to' account")
            if int(target_row["id"]) == int(g.user["id"]):
                return bad_request("you cannot message yourself")
            if data.ignores(get_db(), int(target_row["id"]), int(g.user["id"])):
                return {"error": "Forbidden",
                        "message": "%s is not taking whispers from you." % target_row["username"]}, 403
            return None

        if channel == "guild":
            # IN A GUILD, OR THERE IS NOWHERE FOR IT TO GO. Still a 409 rather
            # than a 404 or a 403: the channel is real and the client is right
            # to ask about it - what is missing is a guild, which is a thing the
            # player can go and fix.
            if guild_membership(g.user["id"]) is None:
                return {
                    "error": "Conflict",
                    "message": GUILD_CHAT_NO_GUILD,
                }, 409
            return None

        if channel == "party":
            # IN A PARTY, OR THERE IS NOWHERE FOR IT TO GO - a 409, as guild's.
            if party_membership(get_db(), g.user["id"]) is None:
                return {"error": "Conflict", "message": PARTY_CHAT_NO_PARTY}, 409
            return None

        return bad_request("unknown channel")

    def chat_image_cooldown(now):
        """None when the caller may post a picture, or the 429 saying they may
        not.

        STAMPS AS IT PASSES. The cooldown has to start when the attempt begins,
        not when it finishes: a fetch can take six seconds and an upload can
        take longer, and a cooldown that only starts on success is an invitation
        to run twenty at once. Shared by the link relay and the direct upload so
        the two cannot drift into being two different limits.
        """
        db = get_db()
        last = data.last_image_at(db, g.user["id"])
        waited = now - last if last is not None else now
        if waited < IMAGE_COOLDOWN_SECONDS:
            return ({
                "error": "Too Many Requests",
                "message": "One picture every %d seconds." % IMAGE_COOLDOWN_SECONDS,
            }, 429, {"Retry-After": str(IMAGE_COOLDOWN_SECONDS - waited)})

        data.stamp_image(db, g.user["id"], now)
        db.commit()
        return None

    def world_image_wait(user, now=None):
        """Seconds this account must still wait before a picture may go to
        world.

        0 when they may post one now. The owner is never made to wait - see the
        note over WORLD_IMAGE_COOLDOWN_SECONDS for why that exemption stops
        there.

        READ-ONLY. Stamping is a separate call, because this is asked twice for
        every send - once by the poll so the client can say so in advance, and
        once by the send itself - and an "ask" that quietly spends something is
        a trap for the next person to call it.
        """
        if now is None:
            now = int(time.time())

        # THE OWNER IS ANSWERED WITHOUT A QUERY, which is both the exemption and
        # the cheap path for the only account that polls while also being the
        # one most likely to be testing pictures. A co-owner too, while the
        # switch is on.
        if has_owner_powers(user["username"]):
            return 0

        last = data.last_world_image_at(get_db(), user["id"])
        if last is None:
            return 0
        return services.world_wait_left(last, now)

    def keep_relayed_image(raw, source):
        """Decode, store and describe bytes that claim to be a picture.

        The one place bytes become a stored picture, whichever door they came
        in by. A link fetched from the internet and a file dragged off
        somebody's desktop get identical treatment from here on: verified,
        re-encoded from scratch, capped, and given an id that is the hash of
        what we wrote - so a file cannot smuggle anything past by being uploaded
        instead of linked.
        """
        record, why = services.decode_relayed_image(raw)
        if record is None:
            return bad_request(why)

        db = get_db()
        data.store_image(db, record, source, g.user["username"], g.user["id"], int(time.time()))
        db.commit()

        answer = services.image_answer({
            "id": record["id"], "kind": record["kind"],
            "width": record["width"], "height": record["height"],
            "frames": record["frames"], "columns": record["columns"],
            "frame_ms": record["frame_ms"],
        })
        answer["bytes"] = record["bytes"]
        return answer, 200

    def no_pictures():
        return {
            "error": "Service Unavailable",
            "message": "This server cannot handle pictures yet. Run: pip install Pillow",
        }, 503

    # =========================================================================
    # THE ROUTES
    # =========================================================================

    def chat_send():
        """
        Say something in world chat
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: message
            schema:
              type: object
              required: [body]
              properties:
                body:
                  type: string
                  description: "The message, trimmed and capped at 200 characters"
        responses:
          200:
            description: The message as everyone else will read it
          400:
            description: Empty message
          401:
            description: Missing, invalid or expired token
          429:
            description: Sending faster than the flood bucket allows
        """
        payload = request.get_json(silent=True) or {}
        # CLEANED BEFORE IT IS JUDGED EMPTY, so a message of nothing but
        # invisible characters is refused rather than posted as a blank line.
        text = clean_text(payload.get("body", ""), MAX_CHAT_LENGTH)
        image_id = str(payload.get("image", "")).strip()

        channel = str(payload.get("channel", CHAT_DEFAULT_CHANNEL)).strip().lower()
        if channel not in CHAT_CHANNELS:
            return bad_request("channel must be one of: %s" % ", ".join(CHAT_CHANNELS))

        # MUTED IS MUTED, in every channel and for pictures too. Before the
        # throttle, so a muted player's attempts cost them nothing they would
        # want back when it lifts.
        muted = mute_state(g.user)
        if muted is not None:
            return {"error": "Forbidden", "message": describe_mute(muted), "muted": muted}, 403

        # A PICTURE ON ITS OWN IS A MESSAGE. Demanding words alongside it would
        # be demanding a caption nobody writes.
        if text == "" and image_id == "":
            return bad_request("body must be a non-empty message")

        db = get_db()

        # THE PICTURE MUST ALREADY HAVE BEEN RELAYED. A client cannot name an id
        # this server has not fetched and decoded for itself - which is what
        # stops the field being a way to point everyone at an arbitrary URL
        # after all.
        #
        # AND THE SENDER MUST BE ABLE TO SEE IT. Otherwise an id glimpsed
        # somewhere - a report, a log - could be posted to world and served to
        # everyone. The same answer as a missing picture, so the refusal says
        # nothing either.
        if image_id != "":
            if not data.image_held(db, image_id) or not may_see_image(db, g.user, image_id):
                return bad_request("that picture is not on this server")

        target_row = None
        to_name = str(payload.get("to", "")).strip()
        if to_name != "":
            target_row = find_user(to_name)
            if target_row is None:
                return {"error": "Not Found", "message": "No such account."}, 404

        refusal = chat_write_check(channel, target_row)
        if refusal is not None:
            return refusal

        # ONE PICTURE IN WORLD PER HALF HOUR, checked here because here is where
        # the channel is finally known. Before the token spend below, so being
        # refused for this does not also cost a message allowance.
        now = int(time.time())
        if channel == "world" and image_id != "":
            waiting = world_image_wait(g.user, now)
            if waiting > 0:
                return ({
                    "error": "Too Many Requests",
                    "message": "One picture in world chat every %d minutes."
                               " Try again in %s." % (
                                   WORLD_IMAGE_COOLDOWN_SECONDS // 60,
                                   services.describe_wait(waiting)),
                }, 429, {"Retry-After": str(waiting)})

        # THROTTLE BEFORE WRITING, not after. Checking afterwards would mean the
        # flood is already in the table by the time it is refused.
        refusal = spend_chat_token(g.user["id"])
        if refusal is not None:
            return refusal

        target_id = int(target_row["id"]) if target_row is not None else 0

        # A GUILD LINE CARRIES ITS GUILD. target_id is the "who else does this
        # line concern" column - a person for a whisper, a guild here - and
        # writing it at post time is what makes the read leak-proof.
        if channel == "guild":
            seat = guild_membership(g.user["id"])
            if seat is None:
                return {"error": "Conflict",
                        "message": GUILD_CHAT_NO_GUILD}, 409
            target_id = int(seat["guild_id"])

        # A PARTY LINE CARRIES ITS PARTY, for the same reason (game 0.42.0).
        if channel == "party":
            seat = party_membership(db, g.user["id"])
            if seat is None:
                return {"error": "Conflict", "message": PARTY_CHAT_NO_PARTY}, 409
            target_id = int(seat["party_id"])

        # A PRIVATE LINE GETS ITS OWN COPY of the picture - see may_see_image().
        # After every refusal above, so a send that is turned away never leaves
        # a copy behind.
        if image_id != "" and channel != "world":
            image_id = data.private_copy(db, image_id, g.user, int(time.time()))

        new_id = post_chat(g.user, text, channel, target_id, image_id)

        # STAMPED ONLY ONCE THE PICTURE IS REALLY IN THE TABLE, and only for
        # world. A send that was refused above, or one to a friend, leaves the
        # half hour untouched.
        if channel == "world" and image_id != "":
            data.stamp_world_image(db, g.user["id"], now)

        # ONE COMMIT FOR ALL OF IT. The spent token, the message it paid for and
        # the prune land together, so a crash between them cannot give a message
        # away free.
        db.commit()

        return {
            "id": int(new_id or 0),
            "by": g.user["username"],
            "role": role_for(g.user),
            "name_hue": name_hue_of(g.user),
            "body": text[:MAX_CHAT_LENGTH],
            "channel": channel,
            "image": image_id,
            "to": target_row["username"] if target_row is not None else "",
        }, 200

    def read_chat():
        """
        Everything said in world chat since a given message id
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: query
            name: since
            type: integer
            description: "Return messages with an id greater than this. 0 for the recent tail."
        responses:
          200:
            description: >
              Messages in id order oldest first, plus the newest id, plus `removed` -
              the ids of messages taken down in the last two minutes, filtered by the
              same permission clause as the messages themselves. A client holding one
              of those ids drops it from its own feed.
          400:
            description: since was not a whole number
          401:
            description: Missing, invalid or expired token
        """
        raw_since = request.args.get("since", "0")
        try:
            since = max(0, int(raw_since))
        except (TypeError, ValueError):
            return bad_request("since must be a whole number")

        channel = str(request.args.get("channel", CHAT_DEFAULT_CHANNEL)).strip().lower()
        if channel not in CHAT_CHANNELS:
            return bad_request("channel must be one of: %s" % ", ".join(CHAT_CHANNELS))

        db = get_db()
        me = int(g.user["id"])

        # WHO MAY READ WHAT, built as a WHERE clause rather than filtered in
        # Python afterwards. A read that fetches everything and then hides some
        # of it is one forgotten line away from hiding none of it.
        where = "channel = ?"
        params = [channel]
        partner = ""

        if channel == "private":
            with_name = str(request.args.get("with", "")).strip()
            if with_name == "":
                return bad_request("a private conversation needs a 'with' account")
            other = find_user(with_name)
            if other is None:
                return {"error": "Not Found", "message": "No such account."}, 404
            partner = other["username"]
            them = int(other["id"])
            # BOTH DIRECTIONS, AND ONLY THESE TWO. Reading someone else's
            # conversation is the failure this clause exists to make impossible.
            where += (" AND ((user_id = ? AND target_id = ?)"
                      "   OR (user_id = ? AND target_id = ?))")
            params += [me, them, them, me]

        elif channel == "friends":
            # YOU AND THE PEOPLE WHO ACCEPTED YOU. Worked out at read time from
            # the friends table, so removing somebody stops their lines
            # appearing without anything having to go back and rewrite history.
            allowed = friend_ids(db, me)
            allowed.add(me)
            marks = ",".join("?" for _ in allowed)
            where += " AND user_id IN (%s)" % marks
            params += sorted(allowed)

        elif channel == "guild":
            seat = guild_membership(me)
            if seat is None:
                # No guild, nothing to read, and saying so plainly beats an
                # empty list the client has to guess the meaning of.
                return {
                    "messages": [],
                    # PRESENT HERE TOO, empty. Same rule as world_image_wait
                    # below: a field that exists on most answers and vanishes on
                    # one is how a client ends up reading a stale copy of it.
                    "removed": [],
                    "latest_id": 0,
                    "now": int(time.time()),
                    "channel": channel,
                    "available": False,
                    # SAYS WHERE TO GO, not only what is missing. The chat
                    # window shows this as it is; see _apply_read() in
                    # chatpanel.gd.
                    "notice": GUILD_CHAT_NO_GUILD,
                    # Present here too. A field that exists on most answers and
                    # vanishes on one is how a client ends up with a stale value
                    # it believes is current.
                    "world_image_wait": world_image_wait(g.user),
                    "muted": mute_state(g.user),
                }, 200

            # BY THE GUILD THE LINE WAS SENT TO, not by who is in the guild now.
            #
            # The obvious version filters on "authors who are currently in my
            # guild", the way the friends channel does - and it leaks. Somebody
            # who leaves guild A and joins guild B would carry every line they
            # ever said in A into B's window, because they are a current member
            # and their old messages match. Stamping the guild on the line at
            # post time means a message belongs to the room it was said in, for
            # good, whoever moves afterwards.
            where += " AND target_id = ?"
            params += [int(seat["guild_id"])]

        elif channel == "party":
            # A PARTY'S ROOM, by the party the line was said in (game 0.42.0).
            seat = party_membership(db, me)
            if seat is None:
                return {
                    "messages": [], "removed": [], "latest_id": 0,
                    "now": int(time.time()), "channel": channel, "available": False,
                    "notice": PARTY_CHAT_NO_PARTY,
                    "world_image_wait": world_image_wait(g.user),
                    "muted": mute_state(g.user),
                }, 200
            where += " AND target_id = ?"
            params += [int(seat["party_id"])]

        # NOBODY YOU IGNORE, in any channel. In the WHERE with everything else,
        # so the cursor, the tail and the removed list all agree about it.
        where += data.IGNORED_AUTHORS_CLAUSE
        params += [me]

        # Same two-shaped read as broadcasts: a first poll wants the tail of the
        # conversation, a returning poll wants only what it has not seen.
        if since == 0:
            rows = data.tail(db, where, params, CHAT_PAGE_LIMIT)
        else:
            rows = data.after(db, where, params, since, CHAT_PAGE_LIMIT)

        # THE NEWEST ID IN THIS CHANNEL, not in the table. A cursor taken from
        # the whole table would skip past everything said in the channel the
        # client is actually reading while another one was busy.
        newest = data.newest_id(db, where, params)

        # The pictures those lines point at, in one query rather than one each.
        wanted = sorted({r["image_id"] for r in rows if r["image_id"]})
        pictures = {image: services.image_answer(row) for image, row in data.image_meta(db, wanted).items()}

        # WHAT HAS BEEN TAKEN BACK DOWN, for the clients that already have it.
        #
        # THE SAME `where` AND THE SAME `params` the messages were read with.
        # That is the whole point of storing the deleted line's channel, user_id
        # and target_id: filtering deletions is not a second permission rule to
        # keep in step with the first one, it IS the first one. A line you could
        # never have read cannot produce a deletion you are told about, and
        # nobody has to remember that - it is the same SQL.
        #
        # BY TIME, NOT BY ID, because a deletion is an event and the message it
        # refers to is almost always BELOW the client's cursor, where `since`
        # can never reach. See CHAT_DELETION_WINDOW_SECONDS for why a window is
        # enough.
        removed_rows = data.removed_since(db, where, params, int(time.time()) - CHAT_DELETION_WINDOW_SECONDS)

        return {
            "messages": [services.message_answer(r, guild_tag, name_hue_of) for r in rows],
            "removed": [int(r["message_id"]) for r in removed_rows],
            "latest_id": int(newest or 0),
            # The server's clock, so the client can age a message without
            # trusting the machine it is running on. Same reason the staff list
            # returns it.
            "now": int(time.time()),
            "channel": channel,
            "with": partner,
            "available": True,
            "images": pictures,
            # HOW LONG BEFORE A PICTURE MAY GO TO WORLD, so the client can say
            # so BEFORE somebody picks a file, compresses it and uploads it,
            # rather than after. Always sent, on every channel, because the
            # answer does not depend on which channel is being read and a client
            # that has just switched tabs should not have a stale one.
            "world_image_wait": world_image_wait(g.user),
            # WHETHER YOU MAY SPEAK, so the box can say so before you type
            # rather than after. null, or {until, seconds_left, reason}.
            "muted": mute_state(g.user),
        }, 200

    def chat_relay_image():
        """
        Hand the server a link and get back a picture it has stored
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: link
            schema:
              type: object
              required: [url]
              properties:
                url:
                  type: string
                  description: "An http or https link to an image or a GIF"
        responses:
          200:
            description: The stored picture's id and how to draw it
          400:
            description: Not a link, not a picture, too big, or pointing inside the network
          401:
            description: Missing, invalid or expired token
          429:
            description: Relaying faster than the cooldown allows
          503:
            description: This server has no image support installed
        """
        if not services.PILLOW_AVAILABLE:
            return no_pictures()

        payload = request.get_json(silent=True) or {}
        url = str(payload.get("url", "")).strip()
        if url == "":
            return bad_request("url is required")
        if len(url) > 2000:
            return bad_request("that link is too long")

        refused = chat_image_cooldown(int(time.time()))
        if refused is not None:
            return refused

        raw, why = fetch_image(url)
        if raw is None:
            return bad_request(why)

        return keep_relayed_image(raw, url)

    # NOT /api/chat/image/upload, AND THE REASON IS WORTH KEEPING.
    #
    # It was that, and it produced a 405 that took a while to read. The path
    # matched `/api/chat/image/<image_id>` - "upload" is a perfectly good value
    # for image_id - and that rule is GET only, so a POST to it came back 405
    # Method Not Allowed. Werkzeug does prefer the static rule once BOTH exist,
    # so this worked the moment the new route was loaded; what it could not
    # survive was a server that had not been restarted. Then the only rule
    # matching that path was the GET one, and the client was told "method not
    # allowed" for a route that simply was not there yet.
    #
    # 405 and 404 are very different messages to the person reading them. 404
    # already says "restart the server, or something else has the port" - the
    # exact diagnosis - and 405 says nothing at all. Sitting outside the
    # `/api/chat/image/` prefix means a stale server gives the useful one.
    #
    # THE OLD PATH IS STILL REGISTERED, deliberately. Once a server HAS the
    # route, Werkzeug prefers the static rule and the old path works fine - the
    # collision only ever bit a server that had not been restarted. Keeping it
    # means a game build from before this move still posts pictures
    # successfully against a current server, which costs one line and removes
    # any window where the two halves have to be updated together.
    def chat_upload_image():
        """
        Send a picture straight from the player's machine
        ---
        tags:
          - Chat
        consumes:
          - application/octet-stream
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: header
            name: X-Picture-Name
            type: string
            required: false
            description: "The original filename, kept only so a moderator can see where it came from"
          - in: body
            name: picture
            required: true
            schema:
              type: string
              format: binary
              description: "The file itself as the raw request body - not JSON, not base64"
        responses:
          200:
            description: The stored picture's id and how to draw it
          400:
            description: Empty body, too big, or not a picture this server can read
          401:
            description: Missing, invalid or expired token
          413:
            description: Body over the server's hard request ceiling
          429:
            description: Uploading faster than the cooldown allows
          503:
            description: This server has no image support installed
        """
        # RAW BYTES, NOT BASE64, and that is the whole reason this is not just
        # another JSON route. Base64 inflates by a third, so a four-megabyte
        # photo arrives as five and a half and Werkzeug refuses it before this
        # function is ever called - the player would see "too big" for a file
        # that is not. Binary costs nothing and fits the limit that is actually
        # written down.
        if not services.PILLOW_AVAILABLE:
            return no_pictures()

        raw = request.get_data(cache=False)
        if not raw:
            return bad_request("send the picture as the body of the request")
        if len(raw) > IMAGE_MAX_BYTES:
            return bad_request("that picture is over %d MB" % (
                IMAGE_MAX_BYTES // (1024 * 1024)))

        refused = chat_image_cooldown(int(time.time()))
        if refused is not None:
            return refused

        return keep_relayed_image(raw, services.picture_label(request.headers.get("X-Picture-Name", "")))

    def chat_read_image(image_id):
        """
        The bytes of a relayed picture
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: path
            name: image_id
            type: string
            required: true
        responses:
          200:
            description: A PNG - one frame, or a spritesheet if it was animated
          401:
            description: Missing, invalid or expired token
          404:
            description: No such picture
        """
        # SIGNED IN TO LOOK, which keeps this from being a public file host run
        # by accident. The client already holds a token and sends it on every
        # other call; this is no different.
        clean = str(image_id or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", clean):
            return {"error": "Not Found", "message": "No such picture."}, 404

        # ONLY TO SOMEONE WHO COULD SEE IT - see may_see_image(). The same 404
        # as a missing row, so this cannot be used to learn which ids exist.
        db = get_db()
        if not may_see_image(db, g.user, clean):
            return {"error": "Not Found", "message": "No such picture."}, 404

        row = data.image_bytes(db, clean)
        if row is None:
            return {"error": "Not Found", "message": "No such picture."}, 404

        # THE TYPE IS WHAT WAS STORED, and the column defaults to png so every
        # row written before stills could be WebP still describes itself
        # correctly. Not sniffed from the bytes: nosniff is set below precisely
        # so the browser does not guess, and it would be odd to guess here
        # instead.
        kind = "webp" if str(row["format"] or "png") == "webp" else "png"

        # IMMUTABLE, AND IT REALLY IS: an id names these exact bytes for as long
        # as the row lives, so a client that has one can keep it. PRIVATE,
        # because who may fetch it now depends on who is asking - a shared cache
        # must never hand one player's answer to another.
        return bytes(row["data"]), 200, {
            "Content-Type": "image/%s" % kind,
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        }

    def chat_delete():
        """
        Take a chat message back down
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
          - in: body
            name: target
            schema:
              type: object
              required: [id]
              properties:
                id:
                  type: integer
        responses:
          200:
            description: The message is gone
          400:
            description: id was missing or not a whole number
          404:
            description: No such message, or the caller is not staff
        """
        payload = request.get_json(silent=True) or {}
        try:
            message_id = int(payload.get("id", 0))
        except (TypeError, ValueError):
            return bad_request("id must be a whole number")
        if message_id <= 0:
            return bad_request("id must be a whole number")

        db = get_db()
        row = data.line(db, message_id)
        if row is None:
            return {"error": "Not Found", "message": "No such message."}, 404

        # STAFF CANNOT DELETE ABOVE THEMSELVES. A mod taking down the owner's
        # line is the same permission question as a mod banning the owner, and
        # the same helper answers it. Deleting your own is allowed explicitly,
        # because can_act_on() is strictly-greater and so refuses it.
        author = find_user(row["username"])
        if author is not None and int(author["id"]) != int(g.user["id"]):
            if not can_act_on(g.user, author):
                return {"error": "Not Found", "message": "No such message."}, 404

        # AND TELL THE CLIENTS THAT ALREADY HAVE IT.
        #
        # Removing the row stops the message reaching anybody who has not read
        # it yet, which is the half that was working. The other half is everyone
        # who HAS: their feed is append-only, so the line stays on screen until
        # a hundred more push it off. See the chat_deletions table for why this
        # is a separate row rather than a column, and why it needs no second
        # cursor.
        now = int(time.time())
        data.take_down(db, row, now)

        # AND THE PICTURE, IF NOTHING ELSE STILL SHOWS IT.
        #
        # Deleting the MESSAGE used to leave the image row in place, and that
        # made this route the weaker half of a pair: a mod deleted an offensive
        # picture, the line vanished from the feed, and the bytes went on being
        # served to everyone who had already seen it.
        #
        # REFERENCE COUNTED, and that is not optional. data.store_image() uses
        # INSERT OR IGNORE on a content hash, so the same picture posted twice
        # is ONE row addressed by two messages - deleting the row on the first
        # message would blank the picture under a second, innocent line,
        # possibly in another channel. Count what is left and only drop it at
        # zero.
        image_id = str(row["image_id"] or "")
        image_dropped = False
        if image_id != "":
            if data.still_shown(db, image_id) == 0:
                data.drop_image(db, image_id)
                image_dropped = True

        log_staff_action(g.user, "chat_delete", row["username"], row["user_id"],
                         row["body"][:120])
        data.answer_reports_on(db, message_id, g.user["username"], now)
        db.commit()

        return {"id": message_id, "deleted": True, "image_dropped": image_dropped}, 200

    def list_ignores():
        """
        The players you have chosen not to hear
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
        responses:
          200:
            description: '{"ignored": [{"username", "since"}], "limit": 200}'
        """
        rows = data.ignored_by(get_db(), g.user["id"])
        return {"ignored": [{"username": r["username"], "since": int(r["created_at"])} for r in rows],
                "limit": ignore_limit()}, 200

    def add_ignore():
        """
        Stop hearing a player
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string}
        responses:
          200:
            description: Ignored - their lines are gone from every chat read of yours
          400:
            description: No name, or your own
          403:
            description: Staff cannot be ignored
          404:
            description: No such account
          409:
            description: The ignore list is full
        """
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("username", "")).strip()
        if not name:
            return bad_request("username is required")
        target = find_user(name)
        if target is None:
            return {"error": "Not Found", "message": "No such account."}, 404
        me = int(g.user["id"])
        if int(target["id"]) == me:
            return bad_request("You cannot ignore yourself.")
        if role_at_least(target, "mod"):
            return {"error": "Forbidden",
                    "message": "Staff cannot be ignored. If one of them is the problem, "
                               "report the message."}, 403
        db = get_db()
        if data.ignores(db, me, target["id"]):
            return {"username": target["username"], "ignored": True, "already": True}, 200
        most = ignore_limit()
        if data.ignoring_count(db, me) >= most:
            return {"error": "Conflict",
                    "message": "You are ignoring %d players, which is the most. "
                               "Stop ignoring somebody first." % most}, 409
        data.ignore(db, me, target["id"], int(time.time()))
        db.commit()
        return {"username": target["username"], "ignored": True}, 200

    def remove_ignore():
        """
        Hear a player again
        ---
        tags:
          - Chat
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [username]
              properties:
                username: {type: string}
        responses:
          200:
            description: Not ignored any more (or never was - was_ignored says which)
          404:
            description: No such account
        """
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("username", "")).strip()
        if not name:
            return bad_request("username is required")
        target = find_user(name)
        if target is None:
            return {"error": "Not Found", "message": "No such account."}, 404
        db = get_db()
        gone = data.unignore(db, g.user["id"], target["id"])
        db.commit()
        return {"username": target["username"], "ignored": False, "was_ignored": gone}, 200

    app.add_url_rule("/api/chat/send", endpoint="chat_send",
                     view_func=require_auth(chat_send), methods=["POST"])
    app.add_url_rule("/api/chat", endpoint="read_chat",
                     view_func=require_auth(read_chat), methods=["GET"])
    # The two picture doors do slow work - a fetch, a decode - before they
    # write, so they do not hold the write lock from their first read.
    app.add_url_rule("/api/chat/image", endpoint="chat_relay_image",
                     view_func=no_write_lock(require_auth(chat_relay_image)), methods=["POST"])
    upload = no_write_lock(require_auth(chat_upload_image))
    app.add_url_rule("/api/chat/upload", endpoint="chat_upload_image",
                     view_func=upload, methods=["POST"])
    app.add_url_rule("/api/chat/image/upload", endpoint="chat_upload_image",
                     view_func=upload, methods=["POST"])
    app.add_url_rule("/api/chat/image/<image_id>", endpoint="chat_read_image",
                     view_func=require_auth(chat_read_image), methods=["GET"])
    app.add_url_rule("/api/chat/delete", endpoint="chat_delete",
                     view_func=require_auth(require_role("mod")(chat_delete)), methods=["POST"])
    app.add_url_rule("/api/ignores", endpoint="list_ignores",
                     view_func=require_auth(list_ignores), methods=["GET"])
    app.add_url_rule("/api/ignores", endpoint="add_ignore",
                     view_func=require_auth(add_ignore), methods=["POST"])
    app.add_url_rule("/api/ignores/remove", endpoint="remove_ignore",
                     view_func=require_auth(remove_ignore), methods=["POST"])

    return SimpleNamespace(post_chat=post_chat, can_read_row=can_read_row,
                           may_see_image=may_see_image, ignores=data.ignores)
