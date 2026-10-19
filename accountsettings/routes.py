"""accountsettings/routes.py - the edge: the recovery address's three routes
and the name colour, as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth          the token check (puts the account on g.user)
    no_write_lock         marks a route that hashes (scrypt) and so does not
                          take the write lock (test_concurrency.py holds the
                          list)
    get_db                the request's database connection
    bad_request           a message -> app.py's 400
    parse_stat            a raw number -> an int 0 or more, or None
    hue_max               NAME_HUE_MAX: the last hue on the wheel
    clean_email           a raw address -> normalised, or "" for none
    mask_email            an address -> t****n@example.com
    row_int               (row, column) -> the column as an int, 0 for none
    record_login_attempt  (db, username, ip, ok, reason) - the login log
    client_ip             () -> the caller's address
    mail_can_send         () -> whether mail can go out at all
    send_mail_async       (to, subject, body) - sent off the request
    new_code              () -> six digits from the cryptographic generator
    store_code            (db, user_id, purpose, code, target, now) - one live
                          code per account and purpose
    consume_code          (db, user_id, purpose, code, now) -> (ok, reason)
    code_ttl              () -> RESET_CODE_TTL_SECONDS, how long a code lives

Everything but the hue's ceiling is asked for when a request lands: the mail
is what the suites stand in for, and the code and address helpers are shared
with the recovery routes and the staff login.

THE ENDPOINT NAMES ARE app.py's - get_account_email, set_account_email,
verify_account_email, set_name_colour - and routes.lock holds them.
"""
import time

from flask import g, request
from werkzeug.security import check_password_hash

from . import data, services


def register(app, *, require_auth, no_write_lock, get_db, bad_request, parse_stat, hue_max, clean_email,
             mask_email, row_int, record_login_attempt, client_ip, mail_can_send, send_mail_async, new_code,
             store_code, consume_code, code_ttl):

    def get_account_email():
        """
        The recovery address on this account, masked
        ---
        tags:
          - Account
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
        responses:
          200:
            description: Masked address and whether it has been verified
          401:
            description: Missing, invalid or expired token
        """
        row = data.recovery_address(get_db(), g.user["id"])
        address = "" if row is None else str(row["email"] or "")
        return services.address_view(address, row is not None and row_int(row, "email_verified"),
                                     mask_email(address)), 200

    def set_account_email():
        """
        Set or change the recovery address (needs the current password)
        ---
        tags:
          - Account
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
              required: [email, password]
              properties:
                email:    {type: string}
                password: {type: string, description: "the account's current password"}
        responses:
          200:
            description: Saved, unverified, and a code is on its way to it
          400:
            description: Not an address
          401:
            description: Wrong password, or no token
        """
        payload = request.get_json(silent=True) or {}
        address = clean_email(payload.get("email"))
        if address == "":
            return bad_request("That does not look like an email address.")

        password = payload.get("password")
        if not isinstance(password, str) or password == "":
            return bad_request("password is required to change the recovery address.")

        db = get_db()
        now = int(time.time())
        row = data.account(db, g.user["id"])
        if row is None:
            return {"error": "Unauthorized", "message": "No such account."}, 401

        # THE CURRENT PASSWORD IS THE WHOLE POINT, and this is the most important
        # line in the recovery feature.
        #
        # The recovery address is a key to the account. If a stolen SESSION could
        # change it, an attacker with a token would point recovery at their own
        # inbox, reset the password, and lock the real owner out permanently -
        # turning a temporary theft into a permanent one. Knowing the password is
        # the one thing a session thief does not have, so it is what this asks for.
        if not check_password_hash(row["password_hash"], password):
            record_login_attempt(db, row["username"], client_ip(), False, "bad-password-on-email")
            db.commit()
            return {"error": "Unauthorized", "message": "That password is not correct."}, 401

        # SAID, NOT SWALLOWED. With no mail set up the code went nowhere and this
        # answered 200 "Check that inbox" all the same - a player waited for an
        # email that could never come. After every check a request can fail on its
        # own, and before anything is stored.
        if not mail_can_send():
            return {"error": "Service Unavailable",
                    "message": "The server cannot send email right now, so an address "
                               "cannot be confirmed yet. Please try again later."}, 503

        # UNVERIFIED UNTIL A CODE COMES BACK FROM IT. Until then this address
        # cannot receive a reset, so a typo here costs a re-entry, not an account.
        data.set_unverified(db, row["id"], address)

        code = new_code()
        store_code(db, row["id"], "verify", code, address, now)
        db.commit()

        send_mail_async(address, *services.confirmation_mail(code, code_ttl()))

        return {"email": mask_email(address), "verified": False,
                "message": "Check that inbox for a confirmation code."}, 200

    def verify_account_email():
        """
        Confirm the recovery address with the emailed code
        ---
        tags:
          - Account
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
              required: [code]
              properties:
                code: {type: string}
        responses:
          200:
            description: Verified - this address can now recover the account
          400:
            description: Wrong or expired code
          401:
            description: Missing, invalid or expired token
        """
        payload = request.get_json(silent=True) or {}
        code = str(payload.get("code", "")).strip()
        if code == "":
            return bad_request("code is required.")

        db = get_db()
        ok, _reason = consume_code(db, g.user["id"], "verify", code, int(time.time()))
        if not ok:
            db.commit()
            return bad_request("That code is wrong or has expired.")

        data.mark_verified(db, g.user["id"])
        db.commit()
        return {"verified": True, "message": "Recovery address confirmed."}, 200

    def set_name_colour():
        """
        Choose the colour your name is drawn in, everywhere
        ---
        tags:
          - Account
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
              required: [hue]
              properties:
                hue: {type: integer, description: "0-359, a position on the colour wheel"}
        responses:
          200:
            description: The hue as stored
          400:
            description: Not a whole number 0-359
          401:
            description: Missing, invalid or expired token
        """
        # EVERYBODY, STAFF INCLUDED. The rule used to be that staff names were
        # painted in their rank colour and the Options slider did nothing for them;
        # rank is shown by a badge now (the owner's crown, MOD and DEV), which a
        # player cannot choose, so the colour is free for everybody to choose.
        #
        # A HUE, NOT A COLOUR. The client fixes saturation and brightness so every
        # choice reads against the world; accepting a whole colour would let a
        # patched client store black-on-black.
        payload = request.get_json(silent=True) or {}
        hue = parse_stat(payload.get("hue"))
        if hue is None or hue > hue_max:
            return bad_request("hue must be a whole number 0-%d" % hue_max)

        db = get_db()
        data.set_hue(db, g.user["id"], hue)
        db.commit()
        return {"name_hue": hue}, 200

    app.add_url_rule("/api/account/email", endpoint="get_account_email",
                     view_func=require_auth(get_account_email), methods=["GET"])
    app.add_url_rule("/api/account/email", endpoint="set_account_email",
                     view_func=no_write_lock(require_auth(set_account_email)), methods=["POST"])
    app.add_url_rule("/api/account/email/verify", endpoint="verify_account_email",
                     view_func=no_write_lock(require_auth(verify_account_email)), methods=["POST"])
    app.add_url_rule("/api/account/name-colour", endpoint="set_name_colour",
                     view_func=require_auth(set_name_colour), methods=["PUT"])
