"""googlesignin/routes.py - the edge (game 0.101.0). The whole story is in
googlesignin/__init__.py.

register() is handed app.py's own pieces - it never imports app.py:

  the edge
    require_auth, require_fresh_password   the token check, and the password
                            asked again within STEP_UP_SECONDS (link, unlink)
    get_db, bad_request, client_ip
    client_id               () -> ELUSION_GOOGLE_CLIENT_ID, asked on every call
    request_install_hash    () -> the install id this request came with, hashed
  the gates the password login has
    username_ok             a name -> whether USERNAME_PATTERN takes it
    maintenance_refusal     a username -> the closed server's 503, or None
    prune_login_attempts, ip_throttle_state, register_throttle_state,
    record_login_attempt, wait_words
    ban_state               a users row -> its ban, or None
    ban_evasion_state, install_evasion_state, record_account_ip,
    record_account_install
  sessions
    issue_token, login_hardening, end_other_sessions
  who somebody is
    role_for, default_role, has_owner_powers, role_at_least, name_hue_of,
    needs_recovery_email, mask_email
    dummy_hash              a hash whose password was never kept: a Google
                            account's password, so nobody can sign in with one
"""
import sqlite3
import time

from flask import g, request

from . import data, services


def register(app, *, require_auth, require_fresh_password, get_db, bad_request, client_ip, client_id,
             request_install_hash, username_ok, maintenance_refusal, prune_login_attempts, ip_throttle_state,
             register_throttle_state, record_login_attempt, wait_words, ban_state, ban_evasion_state,
             install_evasion_state, record_account_ip, record_account_install, issue_token, login_hardening,
             end_other_sessions, role_for, default_role, has_owner_powers, role_at_least, name_hue_of,
             needs_recovery_email, mask_email, dummy_hash, trusted_device=lambda db, row, raws, now: False):
    """Add the nine routes."""

    def live_client_id():
        return services.clean_client_id(client_id())

    def is_staff(row):
        return role_at_least(row, "mod") or has_owner_powers(row["username"])

    def not_set_up():
        return {"error": "Service Unavailable", "message": services.NOT_SET_UP}, 503

    def new_code(db, purpose, user_id, now):
        """A code for this computer; the answer the game needs to go on."""
        install = request_install_hash()
        code = services.new_code()
        data.add_code(db, services.code_hash(code), install, client_ip(), purpose, user_id, now,
                      now + services.GOOGLE_CODE_SECONDS)
        db.commit()
        return {"code": code, "check": services.check_code(code),
                "url": services.page_url(request.scheme, request.host, code),
                "expires_in": services.GOOGLE_CODE_SECONDS, "poll_every": services.GOOGLE_POLL_SECONDS}

    def own_code(db, now):
        """(the code's row, the code) when this request names a live code of
        this computer's; (None, refusal) otherwise - the same 404 for a code
        that never was and one another computer began."""
        payload = request.get_json(silent=True)
        code = payload.get("code") if isinstance(payload, dict) else None
        if not services.code_ok(code):
            return None, bad_request("code must be the code from start")
        row = data.code(db, services.code_hash(code))
        install = request_install_hash()
        if row is None or install == "" or row["install_hash"] != install:
            return None, ({"error": "Not Found", "message": "No such sign-in."}, 404)
        if row["state"] == "used" or (row["state"] == "waiting" and now > int(row["expires_at"])) \
                or now > int(row["expires_at"]) + services.GOOGLE_CODE_SECONDS:
            return None, ({"error": "Gone", "message": services.RUN_OUT}, 410)
        return row, code

    def signed_in(row, token, expires_at, status=200):
        """The same answer as a password login (auth/'s login), for the game's
        one way in. Google is the way back in, so no recovery address is asked."""
        return {
            "user_id": row["id"],
            "username": row["username"],
            "role": role_for(row),
            "is_owner": has_owner_powers(row["username"]),
            "name_hue": name_hue_of(row),
            "token": token,
            "expires_at": expires_at,
            "email_verified": not needs_recovery_email(row),
            "needs_email": False,
            "staff_unprotected": False,
            "via": "google",
        }, status

    def google_start():
        """
        Begin signing in with Google - a code for this computer and the page to open
        ---
        tags:
          - Auth
        responses:
          200:
            description: "{code, check, url, expires_in, poll_every}: open url in a web browser, then poll with code"
          400:
            description: no install id (X-Elusion-Install) - an older game
          429:
            description: too many begun from this address
          503:
            description: Google sign-in is not set up on this server
        """
        if not live_client_id():
            return not_set_up()
        if request_install_hash() == "":
            return bad_request("Update your game to sign in with Google.")
        db = get_db()
        now = int(time.time())
        data.prune(db, now - services.GOOGLE_CODES_KEPT_SECONDS)
        if data.starts_since(db, client_ip(), now - services.GOOGLE_START_WINDOW) >= services.GOOGLE_STARTS_PER_WINDOW:
            db.commit()
            return {"error": "Too Many Requests",
                    "message": "Too many sign-ins begun from here. Try again in a few minutes."}, 429
        return new_code(db, "signin", None, now), 200

    def google_page():
        """
        The page a player signs in with Google on, for one code
        ---
        tags:
          - Auth
        parameters:
          - in: query
            name: c
            type: string
            required: true
        responses:
          200:
            description: an HTML page - Google's button and the check code, or why there is none
        """
        code = request.args.get("c", "")
        cid = live_client_id()
        words = ""
        if not cid:
            words = services.NOT_SET_UP
        elif not services.code_ok(code):
            words = services.RUN_OUT
        else:
            row = data.code(get_db(), services.code_hash(code))
            if row is None or row["state"] != "waiting" or int(time.time()) > int(row["expires_at"]):
                words = services.RUN_OUT
        body = services.page_html(code, cid, words)
        return body, 200, {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            # Google's button needs to be told which page it is on.
            "Referrer-Policy": "strict-origin-when-cross-origin",
        }

    def google_finish():
        """
        The page's: Google's signed token for a code, checked here
        ---
        tags:
          - Auth
        parameters:
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [code, credential]
              properties:
                code: {type: string}
                credential: {type: string, description: "Google's ID token"}
        responses:
          200:
            description: "{ok, message}: go back to the game"
          400:
            description: Google's token refused
          403:
            description: a staff account - they sign in with the password
          409:
            description: that Google account is linked to another account
          410:
            description: the code has run out or was used
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return bad_request("body must be a JSON object")
        cid = live_client_id()
        if not cid:
            return not_set_up()
        code = payload.get("code")
        if not services.code_ok(code):
            return bad_request("code must be the code from the page")
        # GOOGLE'S TOKEN IS CHECKED BEFORE THE DATABASE IS TOUCHED: fetching
        # Google's keys must never be done holding the write lock.
        who, why = services.verify_id_token(payload.get("credential"), cid)
        if who is None:
            app.logger.warning("[GOOGLE] token refused: %s  ip=%s", why, client_ip())
            message = "Could not reach Google. Try once more." if why == "no-keys" \
                else "Google's answer could not be checked. Press Sign in with Google in the game again."
            return {"error": "Bad Request", "message": message}, 400
        db = get_db()
        now = int(time.time())
        row = data.code(db, services.code_hash(code))
        if row is None or row["state"] != "waiting" or now > int(row["expires_at"]):
            return {"error": "Gone", "message": services.RUN_OUT}, 410
        linked = data.link_of_sub(db, who["sub"])
        if row["purpose"] == "link":
            account = data.user(db, row["user_id"]) if row["user_id"] is not None else None
            mine = data.link_of_user(db, row["user_id"]) if account is not None else None
            if account is None:
                data.finish_code(db, row["code_hash"], "refused", said=services.RUN_OUT)
                db.commit()
                return {"error": "Gone", "message": services.RUN_OUT}, 410
            if linked is not None and int(linked["user_id"]) != int(account["id"]):
                data.finish_code(db, row["code_hash"], "refused", said=services.LINKED_ELSEWHERE)
                db.commit()
                return {"error": "Conflict", "message": services.LINKED_ELSEWHERE}, 409
            if mine is None:
                data.add_link(db, account["id"], who["sub"], who["email"], now, False)
                data.give_recovery_email(db, account["id"], who["email"])
            data.finish_code(db, row["code_hash"], "done", who["sub"], who["email"], services.DONE_LINK)
            db.commit()
            app.logger.info("[GOOGLE] linked  user=%r", account["username"])
            return {"ok": True, "message": services.DONE_LINK}, 200
        # A STAFF ACCOUNT LINKED IN OPTIONS IS LET THROUGH HERE: whether this
        # computer may sign it in is the poll's question (sign_in()).
        account = data.user(db, linked["user_id"]) if linked is not None else None
        data.finish_code(db, row["code_hash"], "done", who["sub"], who["email"], services.DONE_SIGNIN,
                         user_id=account["id"] if account is not None else None, keep_user=False)
        db.commit()
        return {"ok": True, "message": services.DONE_SIGNIN}, 200

    def google_poll():
        """
        The game's: is this computer's sign-in done yet
        ---
        tags:
          - Auth
        parameters:
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
            description: "signed in (the login's answer), {needs_name: true}, or {linked: true}"
          202:
            description: "{waiting: true}: ask again"
          403:
            description: refused - a ban (with it), or a staff account
          404:
            description: no such sign-in from this computer
          410:
            description: run out
        """
        db = get_db()
        now = int(time.time())
        row, code = own_code(db, now)
        if row is None:
            return code
        if row["state"] == "waiting":
            return {"waiting": True, "retry_after_ms": services.GOOGLE_POLL_SECONDS * 1000}, 202
        if row["state"] == "refused":
            data.use_code(db, row["code_hash"])
            db.commit()
            return {"error": "Forbidden", "message": row["said"] or services.STAFF_REFUSED}, 403
        if row["purpose"] == "link":
            data.use_code(db, row["code_hash"])
            db.commit()
            return {"linked": True, "message": "Google is linked to your account."}, 200
        if row["user_id"] is None:
            # A GOOGLE ACCOUNT WE HAVE NOT SEEN - but its email may be an
            # account's confirmed recovery address: offer those first.
            same = data.accounts_with_email(db, row["email"])
            found = [a["username"] for a in same if not is_staff(a)]
            if not found and any(is_staff(a) for a in same):
                # ONLY A STAFF ACCOUNT HAS THIS EMAIL (the owner, signing in
                # with Google: "im the gm it should know"): say how, rather
                # than ask a name for a new account he never wanted. Never
                # offered by email: staff link Google in Options, with the
                # password.
                data.use_code(db, row["code_hash"])
                db.commit()
                return {"error": "Forbidden", "message": services.STAFF_LINK_FIRST}, 403
            return {"needs_name": True, "found": found}, 200
        account = data.user(db, row["user_id"])
        if account is None or data.link_of_sub(db, row["sub"]) is None:
            return {"error": "Gone", "message": services.RUN_OUT}, 410
        return sign_in(db, account, row, now)

    def claimable(db, row):
        """The accounts this sign-in may claim by its email: confirmed for
        that address, not linked, never staff."""
        return [a for a in data.accounts_with_email(db, row["email"]) if not is_staff(a)]

    def sign_in(db, account, row, now):
        """The code spent and `account` signed in, under every rule the
        password login has; or the refusal."""
        closed = maintenance_refusal(account["username"])
        if closed is not None:
            return closed
        ip = client_ip()
        install = request_install_hash()
        ban = ban_state(account)
        if ban is not None:
            data.use_code(db, row["code_hash"])
            record_account_install(db, account["id"], install, now)
            record_login_attempt(db, account["username"], ip, False, "banned")
            db.commit()
            return {"error": "Forbidden", "message": "This account is banned." if ban["permanent"]
                    else "This account is banned until further notice.", "ban": ban}, 403
        staff = is_staff(account)
        if staff and not trusted_device(db, account, services.sent_devices(request.get_json(silent=True)), now):
            # STAFF ONLY ON A COMPUTER ALREADY PROVED with the password and the
            # emailed code (auth/'s TRUSTED DEVICES): a stolen Google account
            # on another computer opens nothing.
            data.use_code(db, row["code_hash"])
            record_login_attempt(db, account["username"], ip, False, "google-new-computer")
            db.commit()
            return {"error": "Forbidden", "message": services.STAFF_NEW_COMPUTER}, 403
        if not data.use_code(db, row["code_hash"]):
            return {"error": "Gone", "message": services.RUN_OUT}, 410
        db.commit()
        hardened, bound_to = login_hardening()
        # NOT CONFIRMED FOR STAFF: Google is not the password, so every route
        # behind require_fresh_password (bans, gold, grants, the server's
        # switches) asks for it first - the owner: "i can still use my key for
        # admin stuff". A staff session is short, as after any staff login.
        token, expires_at = issue_token(account["id"], install_hash=bound_to, hardened=hardened, staff=staff,
                                        confirmed=not staff)
        end_other_sessions(db, account["id"], token)
        db.commit()
        record_login_attempt(db, account["username"], ip, True, "google")
        record_account_ip(db, account["id"], ip, now)
        record_account_install(db, account["id"], install, now)
        return signed_in(account, token, expires_at)

    def google_claim():
        """
        Sign in to an existing account whose confirmed recovery email is this Google account's
        ---
        tags:
          - Auth
        parameters:
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [code, username]
              properties:
                code: {type: string}
                username: {type: string, description: "one of the poll's found"}
        responses:
          200:
            description: linked and signed in (the login's answer)
          403:
            description: refused - a ban (with it)
          404:
            description: no such account among those this sign-in may claim
          410:
            description: run out
        """
        payload = request.get_json(silent=True)
        username = payload.get("username") if isinstance(payload, dict) else None
        if not isinstance(username, str) or username == "":
            return bad_request("username must be one of the accounts found")
        db = get_db()
        now = int(time.time())
        row, code = own_code(db, now)
        if row is None:
            return code
        if row["state"] != "done" or row["purpose"] != "signin" or row["user_id"] is not None:
            return {"error": "Conflict", "message": "That sign-in has no account to claim."}, 409
        # ONLY AN ACCOUNT WHOSE CONFIRMED ADDRESS IS GOOGLE'S VERIFIED ONE:
        # the same reach "Forgot password" already gives whoever reads that
        # inbox - nothing more.
        account = next((a for a in claimable(db, row) if a["username"].casefold() == username.casefold()), None)
        if account is None or data.link_of_sub(db, row["sub"]) is not None:
            return {"error": "Not Found", "message": "That account cannot be signed into with this Google account."}, 404
        try:
            data.add_link(db, account["id"], row["sub"], row["email"], now, False)
        except sqlite3.IntegrityError:  # two claims at once: the first won
            db.rollback()
            return {"error": "Gone", "message": services.RUN_OUT}, 410
        app.logger.info("[GOOGLE] claimed by its email  user=%r", account["username"])
        return sign_in(db, account, row, now)

    def google_register():
        """
        A new account for a new Google account, with the name the player chose
        ---
        tags:
          - Auth
        parameters:
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [code, username]
              properties:
                code: {type: string}
                username: {type: string}
        responses:
          201:
            description: made and signed in (the register's answer)
          400:
            description: a name the rules do not take
          403:
            description: from a banned player's address or computer
          409:
            description: the name is taken
          429:
            description: too many from this address
        """
        payload = request.get_json(silent=True)
        username = payload.get("username") if isinstance(payload, dict) else None
        if not isinstance(username, str) or not username_ok(username):
            return bad_request("Names are 3-20 letters, numbers and _ .")
        closed = maintenance_refusal(username)
        if closed is not None:
            return closed
        db = get_db()
        now = int(time.time())
        row, code = own_code(db, now)
        if row is None:
            return code
        if row["state"] != "done" or row["purpose"] != "signin" or row["user_id"] is not None:
            return {"error": "Conflict", "message": "That sign-in has no new account to make."}, 409
        ip = client_ip()
        prune_login_attempts(db, now)
        for gate, words in ((ip_throttle_state, "Too many failed attempts from this address."),
                            (register_throttle_state, "Too many registration attempts from this address.")):
            state = gate(db, ip, now)
            if state is not None:
                reason, retry = state
                record_login_attempt(db, username, ip, False, reason)
                return {"error": "Too Many Requests", "message": "%s Try again in %s." % (words, wait_words(retry))}, 429
        install = request_install_hash()
        if ban_evasion_state(db, ip, now) is not None:
            record_login_attempt(db, username, ip, False, "register-ban-evasion")
            return {"error": "Forbidden", "message": "New accounts cannot be created from this connection."}, 403
        if install_evasion_state(db, install) is not None:
            record_login_attempt(db, username, ip, False, "register-ban-evasion-install")
            return {"error": "Forbidden", "message": "New accounts cannot be created from this computer."}, 403
        if services.staff_name(role_for, default_role, username):
            return {"error": "Conflict", "message": "That name is taken. Pick another."}, 409
        if data.link_of_sub(db, row["sub"]) is not None:
            return {"error": "Conflict", "message": services.LINKED_ELSEWHERE}, 409
        try:
            user_id = data.make_account(db, username, dummy_hash, row["email"], now)
        except sqlite3.IntegrityError:
            db.rollback()
            record_login_attempt(db, username, ip, False, "register-conflict")
            return {"error": "Conflict", "message": services.NAME_TAKEN}, 409
        data.add_link(db, user_id, row["sub"], row["email"], now, True)
        data.use_code(db, row["code_hash"])
        db.commit()
        hardened, bound_to = login_hardening()
        token, expires_at = issue_token(user_id, install_hash=bound_to, hardened=hardened, confirmed=True)
        record_login_attempt(db, username, ip, True, "google")
        record_account_ip(db, user_id, ip, now)
        record_account_install(db, user_id, install, now)
        app.logger.info("[GOOGLE] new account  user=%r", username)
        return signed_in(data.user(db, user_id), token, expires_at, 201)

    def google_status():
        """
        Whether Google is linked to this account
        ---
        tags:
          - Account
        responses:
          200:
            description: "{available, linked, email (masked), made_with_google, staff}"
        """
        db = get_db()
        link = data.link_of_user(db, g.user["id"])
        return {"available": bool(live_client_id()), "linked": link is not None,
                "email": mask_email(link["email"]) if link is not None and link["email"] else "",
                "made_with_google": bool(link is not None and int(link["made_with_google"])),
                "staff": bool(is_staff(g.user)), "staff_may_link": True}, 200

    def google_link():
        """
        Link Google to this account - a code and the page to open, as at sign-in
        ---
        tags:
          - Account
        responses:
          200:
            description: "{code, check, url, expires_in, poll_every}: then poll"
          403:
            description: a staff account, or the password must be typed again (confirm_password)
          409:
            description: Google is linked already
          503:
            description: Google sign-in is not set up on this server
        """
        if not live_client_id():
            return not_set_up()
        if request_install_hash() == "":
            return bad_request("Update your game to link Google.")
        db = get_db()
        if data.link_of_user(db, g.user["id"]) is not None:
            return {"error": "Conflict", "message": "Google is linked to this account already."}, 409
        return new_code(db, "link", g.user["id"], int(time.time())), 200

    def google_unlink():
        """
        Take Google off this account
        ---
        tags:
          - Account
        responses:
          200:
            description: unlinked
          403:
            description: the password must be typed again (confirm_password)
          404:
            description: Google is not linked
          409:
            description: the account was made with Google, so Google stays
        """
        db = get_db()
        link = data.link_of_user(db, g.user["id"])
        if link is None:
            return {"error": "Not Found", "message": "Google is not linked to this account."}, 404
        if int(link["made_with_google"]):
            return {"error": "Conflict",
                    "message": "This account was made with Google, so Google stays linked to it."}, 409
        data.drop_link(db, g.user["id"])
        db.commit()
        return {"ok": True, "message": "Google is no longer linked."}, 200

    app.add_url_rule("/api/auth/google/start", endpoint="google_start", view_func=google_start, methods=["POST"])
    app.add_url_rule(services.PAGE_PATH, endpoint="google_page", view_func=google_page, methods=["GET"])
    app.add_url_rule(services.FINISH_PATH, endpoint="google_finish", view_func=google_finish, methods=["POST"])
    app.add_url_rule("/api/auth/google/poll", endpoint="google_poll", view_func=google_poll, methods=["POST"])
    app.add_url_rule("/api/auth/google/register", endpoint="google_register", view_func=google_register,
                     methods=["POST"])
    app.add_url_rule("/api/auth/google/claim", endpoint="google_claim", view_func=google_claim, methods=["POST"])
    app.add_url_rule("/api/account/google", endpoint="google_status", view_func=require_auth(google_status),
                     methods=["GET"])
    app.add_url_rule("/api/account/google/link", endpoint="google_link",
                     view_func=require_auth(require_fresh_password(google_link)), methods=["POST"])
    app.add_url_rule("/api/account/google/unlink", endpoint="google_unlink",
                     view_func=require_auth(require_fresh_password(google_unlink)), methods=["POST"])
