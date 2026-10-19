"""auth/routes.py - the edge: making an account, logging in and out, the
heartbeat, a remembered login carried into a new game, the password typed again,
changing it, and recovering it by email - as they were in app.py.

register() is handed app.py's own pieces - it never imports app.py. WHAT EVERY
REQUEST PASSES THROUGH STAYS THERE and is handed in: the token check, the
sessions it reads and the tokens it issues, the throttles and the login log,
the bans and the evasion checks, the install binding, the mail.

  the edge
    require_auth            the token check (puts the account on g.user)
    no_write_lock           marks a route that hashes before it writes
    get_db                  the request's database connection
    bad_request             a message -> app.py's 400 (a lookup: defined later)
    client_ip               the caller's address, behind the proxy or not
  passwords
    validate_credentials    a register/login body -> (ok, errors)
    hash_password           werkzeug's generate_password_hash, as app.py uses it
    check_password          and its check_password_hash
    dummy_hash              _TIMING_DUMMY_HASH: what a missing account is
                            checked against, so both paths cost the same
    min_password_length     MIN_PASSWORD_LENGTH
  the gates
    maintenance_refusal     a username -> the closed server's 503, or None
    maintenance_public      () -> the closing notice, or None
    prune_login_attempts    (db, now) - the login log kept to its window
    prune_sessions          (db, now) - expired sessions gone
    ip_throttle_state       (db, ip, now) -> (reason, retry) while an address
                            is spraying, else None
    register_throttle_state the same, for an address probing names
    record_login_attempt    (db, username, ip, ok, reason) - the login log
    register_failed_login   (db, row, now) -> True when this miss locked it
    wait_words              seconds -> "15 minutes"
    ip_window               () -> IP_WINDOW_SECONDS (a lookup: suites turn it)
    lockout_seconds         () -> LOGIN_LOCKOUT_SECONDS
    ban_state               a users row -> its ban, or None
    ban_evasion_state       (db, ip, now) -> a banned account on this address
    install_evasion_state   (db, install_hash) -> a banned account on this
                            computer
    hash_install            the game's install id -> its hash (INSTALL IDS)
    record_account_ip       (db, user_id, ip, now)
    record_account_install  (db, user_id, install_hash, now)
  sessions and tokens (TOKEN LIFECYCLE)
    issue_token             (user_id, [expires_at], install_hash=, hardened=,
                            staff=, confirmed=) -> (token, expires_at)
    login_hardening         () -> (hardened, the install to bind to)
    end_other_sessions      (db, user_id, token, rotated_token=) - one login
                            at a time
    end_for_theft           (db, user_id, why) - every session, as theft
    hash_token              a token -> the hash it is stored by
    bearer_token            () -> the token this request came with
    stamp_presence          (db) - the heartbeat's stamp
    step_up_seconds, confirm_max_misses, token_stolen_message
                            STEP_UP_SECONDS, CONFIRM_MAX_MISSES and
                            TOKEN_STOLEN_MESSAGE
  who somebody is
    role_for, default_role, has_owner_powers, role_at_least, name_hue_of, row_int
  codes and mail (ACCOUNT RECOVERY, STAFF LOGIN CODES, in app.py)
    needs_recovery_email    a users row (or None) -> whether to ask for an address
    staff_login_protected   a users row -> whether its logins take a code
    mask_email, make_code, store_code, consume_code, clean_email
                            app.py's code helpers, shared with the address
                            routes
    send_mail_async         (to, subject, body) - asked on every call: the
                            suites replace it
    reset_code_ttl          RESET_CODE_TTL_SECONDS
    recover_message         RECOVER_GENERIC_MESSAGE: the one answer
    reset_cooldown          RESET_REQUEST_COOLDOWN_SECONDS
    reset_max_per_ip, reset_ip_window
                            RESET_MAX_REQUESTS_PER_IP, RESET_IP_WINDOW_SECONDS

THE ENDPOINT NAMES ARE app.py's - register, login, session_info,
resume_session, confirm_password, logout, logout_all, change_password,
request_recovery, complete_recovery - and routes.lock holds them, with which
take a token and which keep the write lock off. (The register view is named
register_account here, so it does not shadow this function.)
"""
import secrets
import sqlite3
import time

from flask import g, request

from . import data as store
from . import services


def register(app, *, require_auth, no_write_lock, get_db, bad_request, client_ip, validate_credentials,
             hash_password, check_password, dummy_hash, min_password_length, maintenance_refusal,
             maintenance_public, prune_login_attempts, prune_sessions, ip_throttle_state, register_throttle_state,
             record_login_attempt, register_failed_login, wait_words, ip_window, lockout_seconds, ban_state,
             ban_evasion_state, install_evasion_state, hash_install, record_account_ip, record_account_install,
             issue_token, login_hardening, end_other_sessions, end_for_theft, hash_token, bearer_token,
             stamp_presence, step_up_seconds, confirm_max_misses, token_stolen_message, role_for, default_role,
             has_owner_powers, role_at_least, name_hue_of, row_int, needs_recovery_email, staff_login_protected,
             mask_email, make_code, store_code, consume_code, clean_email, send_mail_async, reset_code_ttl,
             recover_message, reset_cooldown, reset_max_per_ip, reset_ip_window):

    def trusted_device(db, row, raw, now):
        """True when `raw` is a device token this account proved, still in date,
        at the rank the account holds now. Marks it used."""
        raw = str(raw or "")
        if raw == "" or len(raw) > 128:
            return False
        found = store.device(db, hash_token(raw), row["id"])
        if found is None or row_int(found, "expires_at") <= now or found["role"] != role_for(row):
            return False
        store.device_used(db, hash_token(raw), row["id"], now)
        return True

    def trust_device(db, row, now):
        """A new device token for a login that just proved itself with a code.
        Returns the token; only its hash is kept. Caller commits."""
        raw = secrets.token_urlsafe(32)
        store.add_device(db, hash_token(raw), row["id"], role_for(row), now,
                         now + services.TRUSTED_DEVICE_DAYS * 86400, services.TRUSTED_DEVICES_KEPT)
        return raw

    def forget_devices(db, user_id):
        """Every computer this account proved has to prove itself again. Caller commits."""
        store.forget_devices(db, user_id)

    def staff_login_step(db, row, data, ip, now):
        """
        None when this login may have its token now; otherwise the answer to send
        instead. Called after the password and the ban check have both passed.
        """
        if not staff_login_protected(row):
            return None
        address = row["email"]
        sent_to = mask_email(address)
        # Spaces are how people copy "183 774" out of a mail; anything longer than
        # a code is refused before it reaches the hash.
        code = services.typed_code(data.get("code"))

        # A COMPUTER THAT HAS ALREADY PROVED ITSELF needs no code. See TRUSTED
        # DEVICES. Only when no code was sent: a code is always checked as a code.
        if not code and trusted_device(db, row, data.get("device"), now):
            db.commit()
            return None

        if code:
            ok, _reason = consume_code(db, row["id"], services.STAFF_LOGIN_CODE_PURPOSE, code, now)
            if ok:
                db.commit()
                # The login answers with a device token, so this computer is not
                # asked again. Only here: a code that was right, just now.
                g.staff_code_proved = True
                return None
            register_failed_login(db, row, now)
            record_login_attempt(db, row["username"], ip, False, "bad-code")
            # 400, NOT 401. The game reads a 401 as "wrong name or password", and
            # the password here was right.
            return {
                "error": "Bad Code",
                "code_required": True,
                "sent_to": sent_to,
                "message": "That code is not right, or it has run out. Use the newest "
                           "email, or clear the box and log in again for a new code.",
            }, 400

        pending = store.pending_code(db, row["id"], services.STAFF_LOGIN_CODE_PURPOSE)
        recent = (pending is not None and row_int(pending, "expires_at") > now
                  and now - row_int(pending, "created_at") < services.STAFF_LOGIN_CODE_RESEND_SECONDS)
        if not recent:
            new_code = make_code()
            store_code(db, row["id"], services.STAFF_LOGIN_CODE_PURPOSE, new_code, address, now)
            db.commit()
            send_mail_async(
                address, "Elusion RPG - your staff login code",
                "Your staff login code is %s\n\n"
                "It expires in %d minutes. Somebody - hopefully you - just typed the "
                "correct password for %s. If that was not you, change your password "
                "now: it is known, and this code is the only thing that stopped them."
                % (new_code, reset_code_ttl // 60, row["username"]))
        record_login_attempt(db, row["username"], ip, False, "code-sent")
        return {
            "code_required": True,
            "sent_to": sent_to,
            "expires_in": reset_code_ttl,
            "message": "Staff login: we emailed a code to %s." % sent_to,
        }, 202

    def register_account():
        """
        Create a new account
        ---
        tags:
          - Auth
        consumes:
          - application/json
        parameters:
          - in: body
            name: body
            required: true
            schema:
              type: object
              properties:
                username:
                  type: string
                password:
                  type: string
                install:
                  type: string
                  description: "This copy of the game's install id (64 hex). Optional; see INSTALL IDS"
        responses:
          201:
            description: Account created, returns a session token
            schema:
              type: object
              properties:
                user_id:
                  type: integer
                username:
                  type: string
                token:
                  type: string
                expires_at:
                  type: integer
          400:
            description: Validation error
            schema:
              type: object
              properties:
                error:
                  type: string
                message:
                  type: array
                  items:
                    type: string
          409:
            description: Username already taken
            schema:
              type: object
              properties:
                error:
                  type: string
                message:
                  type: string
        """
        data = request.get_json(silent=True)

        is_valid, errors = validate_credentials(data)
        if not is_valid:
            return {"error": "Bad Request", "message": errors}, 400

        username = data["username"]

        # A CLOSED SERVER TAKES NO NEW ACCOUNTS EITHER. Registering is a login
        # that creates its own account first - letting it through would be a
        # way in while the front door is shut.
        closed = maintenance_refusal(username)
        if closed is not None:
            return closed

        db = get_db()
        ip = client_ip()
        now = int(time.time())

        # THE GATES COME BEFORE THE HASH, and that ordering is the point of both of
        # them. See /login for the same arrangement and the same reasoning.
        #
        # WHY /register NEEDED THIS AT ALL. /login is careful never to say whether
        # a username exists: one message for both failures, and the same cost on
        # both paths. /register answers the identical question outright and has to
        # — you cannot ask someone to choose a name and refuse to say it is taken.
        # What it must not do is answer that question an unlimited number of times.
        # Unthrottled, the 409/201 split is a clean oracle for the whole user table,
        # and it made the care taken on /login pointless: an attacker enumerates
        # here instead, then brings the list back to the login throttle.
        #
        # AND THE HASH WAS THE SECOND PROBLEM. generate_password_hash ran BEFORE
        # the INSERT, so every probe — including every one that was about to be
        # refused as a duplicate — cost a full scrypt: 32MB and about 100ms of work
        # done on the attacker's behalf, at their chosen rate. That is exactly the
        # denial-of-service lever the per-IP gate on /login exists to avoid, and it
        # was sitting wide open one endpoint over. Nothing is hashed now until the
        # request has passed both gates.
        prune_login_attempts(db, now)

        # The login gate first: an address already spraying passwords does not get
        # to switch endpoints and carry on. This direction only — a clumsy signup
        # must not lock anyone out of logging in, which is why the register count
        # below is kept separate.
        ip_state = ip_throttle_state(db, ip, now)
        if ip_state is not None:
            reason, retry = ip_state
            record_login_attempt(db, username, ip, False, reason)
            return {
                "error": "Too Many Requests",
                "message": "Too many failed attempts from this address. Try again in %s." % wait_words(retry),
            }, 429

        probe_state = register_throttle_state(db, ip, now)
        if probe_state is not None:
            reason, retry = probe_state
            record_login_attempt(db, username, ip, False, reason)
            return {
                "error": "Too Many Requests",
                "message": "Too many registration attempts from this address. Try again in %s." % wait_words(retry),
            }, 429

        # BAN EVASION. The pattern this catches is the ordinary one: banned, makes
        # a new account, same connection, same minute.
        #
        # REGISTRATION ONLY - logins from this address are untouched, so the
        # sibling, the roommate and everyone behind the same mobile carrier keep
        # playing. Blocking their logins too would be the version that takes out a
        # household to stop one person, which is the trade _ip_throttle_state's
        # comment already refuses to make.
        #
        # 403, NOT 429. This is not "slow down", it is "not from here", and a retry
        # timer would invite exactly the retrying it is trying to stop. The banned
        # account's name is NOT returned: whoever is reading this either already
        # knows it, or is a stranger who should not be told who was banned on a
        # shared address.
        evader = ban_evasion_state(db, ip, now)
        if evader is not None:
            # ONE LOG LINE, NOT TWO. record_login_attempt() already writes the row
            # and warns, and a second dedicated warning here would double the noise
            # for the one event most likely to repeat - an evader retrying is
            # precisely what this refuses. The name of the banned account it
            # matched is not in the line because it does not need to be: the link
            # lives in account_ips permanently, where the staff view reads it, and
            # a log that rotates is the worse place to keep it.
            record_login_attempt(db, username, ip, False, "register-ban-evasion")
            return {
                "error": "Forbidden",
                "message": "New accounts cannot be created from this connection.",
            }, 403

        # AND FROM THE SAME COMPUTER, whatever its address - see INSTALL IDS. The
        # VPN half of the same check, refused the same way and logged under its
        # own reason, so staff can tell which of the two caught somebody.
        install_hash = hash_install((data or {}).get("install"))
        evader = install_evasion_state(db, install_hash)
        if evader is not None:
            record_login_attempt(db, username, ip, False, "register-ban-evasion-install")
            return {
                "error": "Forbidden",
                "message": "New accounts cannot be created from this computer.",
            }, 403

        password_hash = hash_password(data["password"])

        try:
            user_id = store.create_account(db, username, password_hash, int(time.time()))
            db.commit()
        except sqlite3.IntegrityError:
            # RECORDED, because a conflict is the only observable this endpoint
            # emits and a run of them is the signature of enumeration. The reason
            # is prefixed so _ip_throttle_state() can exclude it — see the note
            # there about why a name-picker must not trip the login spray gate.
            record_login_attempt(db, username, ip, False, "register-conflict")
            return {
                "error": "Conflict",
                "message": "That username is already taken.",
            }, 409

        # THE SIGNUP'S OWN FAILED LOGIN IS NOT EVIDENCE. The game has no separate
        # "create account" form: it logs in first and registers only after a 401,
        # so every new account begins with a 'no-such-user' row - which is on the
        # spray gate's allowlist. Six people signing up behind one address inside
        # ten minutes (a household, a school, a carrier's NAT, or EVERYONE behind a
        # proxy with ELUSION_TRUSTED_PROXIES at 0) tripped IP_MAX_USERNAMES and
        # locked that address out of logging in for a quarter of an hour.
        #
        # RELABELLED, NOT DELETED: the log keeps what happened, and only the rows
        # for THIS name, from THIS address, inside the window stop counting. That
        # name did not exist when it failed, so it guessed at nobody's password;
        # a spray against real accounts leaves 'bad-password' rows, untouched here.
        store.relabel_signup_login(db, ip, username, now - ip_window())
        db.commit()
        hardened, bound_to = login_hardening()
        token, expires_at = issue_token(user_id, install_hash=bound_to, hardened=hardened, confirmed=True)
        # The account's first address. Recorded here as well as on login, so an
        # account that registers and is banned before it ever logs in still carries
        # the link that makes its next sibling visible.
        record_account_ip(db, user_id, ip, now)
        record_account_install(db, user_id, install_hash, now)

        # THE SAME SHAPE AS LOGIN AND SESSION. This used to omit keys that those
        # two returned, so a client that read the response after registering got a
        # different object than the one it got after logging in - and the
        # difference was a missing key rather than a false value, which is the kind
        # that surfaces as a crash somewhere else entirely.
        #
        # A new account is always a player, but it can be the owner: registering
        # the account named by ELUSION_OWNER is exactly how a fresh server gets one.
        # IS_OWNER IS "HAS THE OWNER'S POWERS" (0.21.0): true for a co-owner while
        # the switch is on, and `role` says which of the two.
        return {
            "user_id": user_id,
            "username": username,
            "role": role_for({"username": username, "role": default_role}),
            "is_owner": has_owner_powers(username),
            # A new account has chosen nothing yet; the client draws its default.
            "name_hue": None,
            "token": token,
            "expires_at": expires_at,
            # An account one second old cannot have confirmed an address yet, so
            # this is true - unless the server cannot send mail at all, in which
            # case there is nothing to ask for (see needs_recovery_email()).
            # Sent rather than left out, so the client reads the same key on
            # register as it does on login.
            "email_verified": False,
            "needs_email": needs_recovery_email(None),
        }, 201

    def login():
        """
        Log in and receive a session token
        ---
        tags:
          - Auth
        consumes:
          - application/json
        parameters:
          - in: body
            name: body
            required: true
            schema:
              type: object
              properties:
                username:
                  type: string
                password:
                  type: string
                install:
                  type: string
                  description: "This copy of the game's install id (64 hex). Optional; see INSTALL IDS"
        responses:
          200:
            description: Logged in
            schema:
              type: object
              properties:
                user_id:
                  type: integer
                username:
                  type: string
                role:
                  type: string
                token:
                  type: string
                expires_at:
                  type: integer
          400:
            description: Validation error
          401:
            description: Bad username or password
            schema:
              type: object
              properties:
                error:
                  type: string
                message:
                  type: string
        """
        data = request.get_json(silent=True)

        is_valid, errors = validate_credentials(data)
        if not is_valid:
            return {"error": "Bad Request", "message": errors}, 400

        # THE DOOR. Before the throttle, before the user lookup, before any
        # hashing: a closed server should cost one indexed read and say so. The
        # owner is exempt, or the switch could lock its own operator out.
        closed = maintenance_refusal(data["username"])
        if closed is not None:
            return closed

        db = get_db()
        ip = client_ip()
        now = int(time.time())

        # THE PER-IP GATE COMES FIRST - before the user lookup, before the account
        # lockout, before any password hashing. A spray should cost the server one
        # indexed COUNT and nothing else; running scrypt for an attacker is doing
        # 32MB of work per guess ON THEIR BEHALF, which turns the good hash into a
        # denial-of-service lever.
        prune_login_attempts(db, now)
        # Expired sessions go here too. This is the one request every returning
        # player makes, which makes it the natural place to tidy a table that only
        # ever grows - and the delete is indexed on a column every row has.
        prune_sessions(db, now)
        ip_state = ip_throttle_state(db, ip, now)
        if ip_state is not None:
            reason, retry = ip_state
            record_login_attempt(db, (data or {}).get("username", ""), ip, False, reason)
            return {
                "error": "Too Many Requests",
                "message": "Too many failed attempts from this address. Try again in %s." % wait_words(retry),
            }, 429

        # SELECT * rather than a column list: ban_state() and role_for() both read
        # from this row, and a list here is a list that gets forgotten the next
        # time a column is added - which is exactly what happened when the ban
        # columns arrived. (data.py's account_named().)
        row = store.account_named(db, data["username"])

        # LOCKOUT comes before the password check on purpose: while an account is
        # frozen, no amount of guessing gets a verdict, correct or not. Only a real
        # row can be frozen, so a locked username is knowable - the accepted cost of
        # per-account lockout (see SECURITY_NOTES.md E-5).
        if row is not None and row_int(row, "lockout_until") > now:
            retry = row_int(row, "lockout_until") - now
            record_login_attempt(db, data["username"], ip, False, "account-locked")
            return {
                "error": "Too Many Requests",
                "message": "Too many failed attempts. Try again in %s." % wait_words(retry),
            }, 429

        # same response whether the user is missing or the password is wrong -
        # otherwise this endpoint tells an attacker which usernames exist.
        #
        # NOT `row is None or not check_password_hash(...)`, which is what this was.
        # `or` short-circuits, so a missing user skipped the hash entirely and came
        # back in about a millisecond while a real user with a wrong password spent
        # about a hundred in scrypt. The response was identical and the clock was
        # not, which enumerates usernames just as well. See app.py's _TIMING_DUMMY_HASH.
        #
        # The verify now ALWAYS runs — against the real hash when there is a row,
        # against the dummy when there is not — so both paths pay the same cost and
        # there is no early return for a stopwatch to find.
        stored_hash = row["password_hash"] if row is not None else dummy_hash
        password_ok = check_password(stored_hash, data["password"])

        if row is None or not password_ok:
            locked = row is not None and register_failed_login(db, row, now)
            # The REASON is recorded even though the RESPONSE cannot distinguish
            # them. The 401 has to stay identical or it enumerates usernames; the
            # log is on our side of that line, and "no-such-user" against forty
            # names is the signature of a spray.
            record_login_attempt(
                db, data["username"], ip, False,
                "no-such-user" if row is None else "bad-password",
            )
            # THE MISS THAT LOCKS IT SAYS SO. It used to answer the same 401 as the
            # seven before it, so the player learned about the lock only by typing
            # the RIGHT password next and being told to wait fifteen minutes. The
            # next attempt would have said "locked" anyway - this is the same
            # sentence one try sooner, not anything a stranger could not learn.
            if locked:
                return {
                    "error": "Too Many Requests",
                    "message": "Too many failed attempts. Try again in %s."
                               % wait_words(lockout_seconds()),
                }, 429
            return {
                "error": "Unauthorized",
                "message": "Incorrect username or password.",
            }, 401

        # AFTER the password check, deliberately. Telling someone their account is
        # banned before they have proved it is theirs would make this endpoint a
        # way to find out who is banned.
        #
        # 403 rather than 401, and the reason IS included - unlike the routes that
        # hide behind a 404, a banned player has every right to know they are
        # banned and why. Silence there reads as the game being broken.
        ban = ban_state(row)
        install_hash = hash_install(data.get("install"))
        if ban is not None:
            # THE COMPUTER IS RECORDED EVEN THOUGH THE LOGIN IS REFUSED. The
            # password was right, so this is the account's holder at their own
            # machine - and a player banned before install ids existed has no
            # computer on file until they try once more, which they always do.
            # The address is not recorded here; that rule is older and unchanged.
            record_account_install(db, row["id"], install_hash, now)
            record_login_attempt(db, row["username"], ip, False, "banned")
            return {
                "error": "Forbidden",
                "message": "This account is banned." if ban["permanent"]
                           else "This account is banned until further notice.",
                "ban": ban,
            }, 403

        # STAFF: the password is right, and it is still not enough. See STAFF
        # LOGIN CODES above.
        step = staff_login_step(db, row, data, ip, now)
        if step is not None:
            return step

        # A login that gets its token clears the streak - the throttle is about
        # CONSECUTIVE misses, so one success resets it. Only here, after the staff
        # step: a correct password followed by a wrong code is still a miss. Skip
        # the write when there is nothing to clear, which is the common case.
        if row_int(row, "failed_logins") or row_int(row, "lockout_until"):
            store.clear_misses(db, row["id"])
            db.commit()

        hardened, bound_to = login_hardening()
        token, expires_at = issue_token(row["id"], install_hash=bound_to, hardened=hardened,
                                        staff=role_at_least(row, "mod"), confirmed=True)
        # ONE LOGIN AT A TIME: only a login that got its token. A wrong password,
        # a ban or a staff code still to come ends nothing.
        end_other_sessions(db, row["id"], token)
        device_token = trust_device(db, row, now) if g.get("staff_code_proved") else ""
        db.commit()
        record_login_attempt(db, row["username"], ip, True, "")
        # ON SUCCESS ONLY, and after the ban check above - a banned account never
        # reaches here, so a ban cannot keep refreshing its own address history and
        # extending the block on everyone who shares the connection.
        record_account_ip(db, row["id"], ip, now)
        record_account_install(db, row["id"], install_hash, now)

        return {
            "user_id": row["id"],
            "username": row["username"],
            # THE OWNER IS ALWAYS THE OWNER, whatever the column says. A stored
            # rank is a row someone could revoke; the owner is configuration.
            # Locking the owner out of their own server with an UPDATE should not
            # be possible, so role_for() answers from ELUSION_OWNER first.
            "role": role_for(row),
            # The owner's powers: the owner, or a co-owner while the switch is on.
            "is_owner": has_owner_powers(row["username"]),
            # THE COLOUR COMES WITH THE LOGIN, so a second machine draws the name
            # the player chose on the first one.
            "name_hue": name_hue_of(row),
            "token": token,
            "expires_at": expires_at,
            # THE PROMPT SWITCH. The client shows the "secure your account" step
            # when this is true, which is how every existing account picks up a
            # recovery address without anybody having to go and ask them.
            "email_verified": not needs_recovery_email(row),
            "needs_email": needs_recovery_email(row),
            # A staff account that logged in on the password alone, because there
            # was nowhere to send a code. The HUD says so. See STAFF LOGIN CODES.
            "staff_unprotected": role_at_least(row, "mod") and not staff_login_protected(row),
            # Only after a staff code: this computer's proof, for the next login.
            # See TRUSTED DEVICES.
            **({"device_token": device_token} if device_token else {}),
        }, 200

    def session_info():
        """
        Validate a stored token
        ---
        tags:
          - Auth
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Token is valid
            schema:
              type: object
              properties:
                user_id:
                  type: integer
                username:
                  type: string
                role:
                  type: string
                expires_at:
                  type: integer
          401:
            description: Missing, invalid or expired token
        """
        # THIS IS ALSO THE HEARTBEAT. The game calls it every HEARTBEAT seconds
        # while a character is in the world, for two reasons:
        #
        #   a 401 here is how a kicked or banned player finds out. Both delete the
        #   session; without a regular call the client never asks, and keeps
        #   playing until it is restarted - which made a kick a suggestion.
        #
        #   the stamp below is how staff see who is actually playing. A session
        #   lasts thirty days, so "holds a session" is not "online".
        #
        # ONE UPDATE BY PRIMARY KEY. The token's hash is the row, so this touches exactly
        # the session asking and nothing else. expires_at is NOT extended: a
        # heartbeat proves the client is running, not that the login is fresher.
        db = get_db()
        stamp_presence(db)
        db.commit()

        # THE CLOSING NOTICE RIDES THE HEARTBEAT. This is the only call every
        # client in the world makes on a timer, which makes it the one place
        # that can tell everyone at once that the server is closing - and it is
        # how the client knows to flush its save while it still has a session.
        # None while the server is open, so the client can just test for it.
        return {
            "user_id": g.user["id"],
            "username": g.user["username"],
            "role": role_for(g.user),
            "is_owner": has_owner_powers(g.user["username"]),
            "name_hue": name_hue_of(g.user),
            "expires_at": g.user["expires_at"],
            "maintenance": maintenance_public(),
            "email_verified": not needs_recovery_email(g.user),
            "needs_email": needs_recovery_email(g.user),
        }, 200

    def resume_session():
        """
        Carry a remembered login into this game, on a new token
        ---
        tags:
          - Auth
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: What /api/auth/session answers, plus the token to use from now on
          401:
            description: Missing, invalid or expired token
        """
        # ONE LOGIN AT A TIME, for the game that never typed a password. A second
        # copy of the game opened on the same computer finds the same remembered
        # token, and GET /api/auth/session would let both run on one session - the
        # two-writer bug with nothing to end. So opening the game swaps the token:
        # this one gets a fresh session, and every other session on the account,
        # the one it came in on included, ends as if it had been signed in over.
        #
        # THE NEW TOKEN ENDS WHEN THE OLD ONE WOULD HAVE. A fresh thirty days
        # here would let a remembered login - or a stolen token - renew itself
        # forever without anybody typing a password. It is the same login, moved.
        db = get_db()
        hardened, bound_to = login_hardening()
        token, expires_at = issue_token(g.user["id"], int(g.user["expires_at"]), install_hash=bound_to,
                                        hardened=hardened, staff=role_at_least(g.user, "mod"))
        # THE TOKEN IT CAME IN ON IS "ROTATED" (TOKEN LIFECYCLE): back from another
        # copy of the game, it is theft.
        end_other_sessions(db, g.user["id"], token, rotated_token=bearer_token())
        db.commit()
        # A REMEMBERED LOGIN IS STILL A LOGIN from this computer and this address.
        # "Remember me" can carry a player for thirty days without a password, and
        # recording only on /login left that whole month out of the link view.
        now = int(time.time())
        record_account_ip(db, g.user["id"], client_ip(), now)
        record_account_install(db, g.user["id"],
                               hash_install((request.get_json(silent=True) or {}).get("install")),
                               now)
        return {
            "user_id": g.user["id"],
            "username": g.user["username"],
            "role": role_for(g.user),
            "is_owner": has_owner_powers(g.user["username"]),
            "name_hue": name_hue_of(g.user),
            "token": token,
            "expires_at": expires_at,
            "maintenance": maintenance_public(),
            "email_verified": not needs_recovery_email(g.user),
            "needs_email": needs_recovery_email(g.user),
        }, 200

    def confirm_password():
        """
        Type the password again, for the routes that ask (STEP-UP)
        ---
        tags:
          - Auth
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
              required: [password]
              properties:
                password: {type: string}
        responses:
          200:
            description: Confirmed for STEP_UP_SECONDS on this session
          400:
            description: No password given
          401:
            description: Missing, invalid or expired token - or, after CONFIRM_MAX_MISSES wrong ones, every session ended
          403:
            description: The wrong password
        """
        # TOKEN LIFECYCLE, STEP-UP. A wrong one is 403, never 401: the game reads
        # 401 as "signed out". CONFIRM_MAX_MISSES (app.py) wrong ones on one session is
        # somebody with the token guessing at the password, and is theft.
        payload = request.get_json(silent=True) or {}
        password = payload.get("password")
        if not isinstance(password, str) or not password:
            return bad_request("password is required")
        db = get_db()
        row = store.password_of(db, int(g.user["id"]))
        token_hash = hash_token(bearer_token())
        if row is None or not check_password(row["password_hash"], password):
            store.miss_confirm(db, token_hash)
            db.commit()
            misses = store.confirm_misses(db, token_hash)
            if misses is not None and int(misses["confirm_misses"]) >= confirm_max_misses:
                end_for_theft(db, int(g.user["id"]), "%d wrong passwords to confirm" % confirm_max_misses)
                return {"error": "Unauthorized", "message": token_stolen_message, "token_stolen": True}, 401
            return {"error": "Forbidden", "message": "That is not your password.", "confirm_password": True}, 403
        now = int(time.time())
        store.confirmed(db, token_hash, now)
        db.commit()
        return {"confirmed": True, "until": now + step_up_seconds}, 200

    def logout():
        """
        Invalidate the current token
        ---
        tags:
          - Auth
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          204:
            description: Token invalidated
          401:
            description: Missing, invalid or expired token
        """
        db = get_db()
        store.end_session(db, hash_token(bearer_token()))
        db.commit()
        return "", 204

    def logout_all():
        """
        Invalidate every session for the logged-in account
        ---
        tags:
          - Auth
        parameters:
          - in: header
            name: Authorization
            type: string
            required: true
            description: "Bearer <token>"
        responses:
          200:
            description: Sessions invalidated
            schema:
              type: object
              properties:
                revoked:
                  type: integer
          401:
            description: Missing, invalid or expired token
        """
        # WHY THIS EXISTS, given TOKEN_TTL is thirty days.
        #
        # A long token is the right call for a game - nobody wants to retype a
        # password to play for twenty minutes - but it is only defensible if there
        # is a way to END one early. Without this, a token that leaks stays valid
        # for up to a month and nothing anyone can do shortens that: changing a
        # password would not help, because /api/auth/logout only kills the token
        # doing the asking, and the attacker is holding a different one.
        #
        # So the answer to "30 days is a long time" is not a smaller number that
        # annoys every honest player. It is this: the window stays generous, and it
        # can be slammed shut on demand.
        #
        # DELETES THE CALLER'S TOKEN TOO. "Log out everywhere" that leaves the
        # device you typed it on still logged in is not what anyone means by it,
        # and if the reason you pressed it is that you think you were compromised,
        # the ambiguity is the last thing you need.
        db = get_db()
        revoked = store.end_sessions(db, g.user["id"])
        # And every computer's trust: see TRUSTED DEVICES (services.py).
        forget_devices(db, g.user["id"])
        db.commit()

        app.logger.warning(
            "all sessions revoked  user=%r  ip=%s  count=%d",
            g.user["username"], client_ip() or "?", revoked,
        )

        return {"revoked": revoked}, 200

    def change_password():
        """
        Change the logged-in account's password
        ---
        tags:
          - Auth
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
              properties:
                current_password:
                  type: string
                new_password:
                  type: string
        responses:
          200:
            description: Password changed; every session was revoked and a new one issued
            schema:
              type: object
              properties:
                token:
                  type: string
                expires_at:
                  type: integer
                revoked:
                  type: integer
          400:
            description: Validation error
          401:
            description: Missing token, or the current password is wrong
          429:
            description: Too many failed attempts
        """
        # THE OTHER HALF OF E-6. logout-all lets you end sessions; without this
        # there was no way to change the credential that leaked, so an attacker who
        # had the password simply logged back in and got a fresh 30-day token.
        # Revoking without rotating is a door you keep closing on someone holding
        # the key.
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return bad_request("Payload must be a JSON object.")

        current = data.get("current_password")
        new = data.get("new_password")

        if not isinstance(current, str) or not isinstance(new, str):
            return bad_request("current_password and new_password are required strings.")
        if len(new) < min_password_length:
            return bad_request(
                "Field 'new_password' must be at least %d characters." % min_password_length
            )
        if new == current:
            return bad_request("The new password must be different from the current one.")

        db = get_db()
        ip = client_ip()
        now = int(time.time())

        row = store.account(db, g.user["id"])
        if row is None:
            # The token resolved a moment ago, so this is a deleted account racing
            # its own session rather than anything the caller did.
            return {"error": "Unauthorized", "message": "No such account."}, 401

        # THE CURRENT PASSWORD IS REQUIRED, and it is the whole point. A valid token
        # is not proof of identity here - a stolen token is exactly the situation
        # this endpoint exists for, and letting one set a new password would hand
        # the account to the thief rather than take it back.
        #
        # THROTTLED LIKE LOGIN, for the same reason: this is a second place to guess
        # a password, and one that answers from an authenticated session. Leaving it
        # unthrottled would put the lock back on the front door and a window beside
        # it. Both ceilings apply, and a miss counts against both.
        ip_state = ip_throttle_state(db, ip, now)
        if ip_state is not None:
            reason, retry = ip_state
            record_login_attempt(db, row["username"], ip, False, reason)
            return {
                "error": "Too Many Requests",
                "message": "Too many failed attempts from this address. Try again in %s." % wait_words(retry),
            }, 429

        if row_int(row, "lockout_until") > now:
            retry = row_int(row, "lockout_until") - now
            record_login_attempt(db, row["username"], ip, False, "account-locked")
            return {
                "error": "Too Many Requests",
                "message": "Too many failed attempts. Try again in %s." % wait_words(retry),
            }, 429

        if not check_password(row["password_hash"], current):
            register_failed_login(db, row, now)
            record_login_attempt(db, row["username"], ip, False, "bad-password-on-change")
            return {
                "error": "Unauthorized",
                "message": "The current password is incorrect.",
            }, 401

        # ONE TRANSACTION: re-hash, revoke, re-issue. Splitting these leaves a window
        # where the password is new and the attacker's session is still live, which
        # is the exact state the endpoint exists to remove.
        #
        # EVERY session goes, including the caller's own, and then a fresh one is
        # issued to the caller. That ordering matters: "change my password" must not
        # be a way to log everyone out except whoever currently holds a stolen token.
        new_hash = hash_password(new)
        store.set_password(db, row["id"], new_hash)
        revoked = store.end_sessions(db, row["id"])
        forget_devices(db, row["id"])
        db.commit()

        hardened, bound_to = login_hardening()
        token, expires_at = issue_token(row["id"], install_hash=bound_to, hardened=hardened,
                                        staff=role_at_least(row, "mod"), confirmed=True)
        record_login_attempt(db, row["username"], ip, True, "password-changed")

        app.logger.warning(
            "password changed  user=%r  ip=%s  sessions_revoked=%d",
            row["username"], ip or "?", revoked,
        )

        return {"token": token, "expires_at": expires_at, "revoked": revoked}, 200

    def request_recovery():
        """
        Ask for a reset code by email
        ---
        tags:
          - Auth
        parameters:
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [email]
              properties:
                email: {type: string}
        responses:
          200:
            description: Always the same answer, whether or not that address exists
          429:
            description: Too many requests from this address
        """
        payload = request.get_json(silent=True) or {}
        address = clean_email(payload.get("email"))

        db = get_db()
        now = int(time.time())
        ip = client_ip()

        # A CEILING PER ADDRESS, so this cannot be pointed at a list of leaked
        # emails, or used to bomb one inbox from one machine. Counted before
        # anything else, because a throttled caller should cost one indexed read.
        recent = store.recovery_requests(db, ip, now - reset_ip_window)
        if recent >= reset_max_per_ip:
            return {
                "error": "Too Many Requests",
                "message": "Too many recovery requests from this address. Try again later.",
            }, 429

        record_login_attempt(db, "", ip, True, "recover-request")

        row = None
        if address != "":
            # VERIFIED ONLY. An address nobody has proved they own is not a way in.
            row = store.verified_account(db, address)

        if row is None:
            # SAME WORK, SAME ANSWER. Hashing a throwaway code here costs what the
            # real path costs, so the reply cannot be timed to learn whether the
            # address is on an account - the same trick app.py's _TIMING_DUMMY_HASH plays on
            # /login, for the same reason.
            hash_password(make_code())
            db.commit()
            return {"ok": True, "message": recover_message}, 200

        # ONE CODE A MINUTE PER ACCOUNT. Silently, because saying "you already have
        # one" would confirm the address exists.
        existing = store.reset_code(db, row["id"])
        if existing is not None and row_int(existing, "created_at") > now - reset_cooldown:
            db.commit()
            return {"ok": True, "message": recover_message}, 200

        code = make_code()
        store_code(db, row["id"], "reset", code, address, now)
        db.commit()

        send_mail_async(address, "Elusion RPG - your password reset code",
                        "Someone asked to reset the password for %s.\n\n"
                        "Your code is %s\n\n"
                        "It expires in %d minutes and can be used once. If this was not "
                        "you, ignore this email - your password has not changed."
                        % (row["username"], code, reset_code_ttl // 60))

        return {"ok": True, "message": recover_message}, 200

    def complete_recovery():
        """
        Set a new password using the emailed code
        ---
        tags:
          - Auth
        parameters:
          - in: body
            name: body
            required: true
            schema:
              type: object
              required: [email, code, new_password]
              properties:
                email:        {type: string}
                code:         {type: string}
                new_password: {type: string}
        responses:
          200:
            description: Password replaced, every session on the account destroyed
          400:
            description: Wrong or expired code, or a password that fails the rules
          429:
            description: Too many attempts from this address
        """
        payload = request.get_json(silent=True) or {}
        address = clean_email(payload.get("email"))
        code = str(payload.get("code", "")).strip()
        new = payload.get("new_password")

        if not isinstance(new, str) or len(new) < min_password_length:
            return bad_request(
                "Field 'new_password' must be at least %d characters." % min_password_length)

        db = get_db()
        now = int(time.time())
        ip = client_ip()

        # The login throttle covers this too: it is a place to guess a code, and an
        # unthrottled one would be a million tries from a loop.
        ip_state = ip_throttle_state(db, ip, now)
        if ip_state is not None:
            reason, retry = ip_state
            record_login_attempt(db, "", ip, False, reason)
            db.commit()
            return {
                "error": "Too Many Requests",
                "message": "Too many failed attempts from this address. Try again in %s." % wait_words(retry),
            }, 429

        row = None
        if address != "" and code != "":
            row = store.verified_account(db, address)

        if row is None:
            check_password(dummy_hash, code)
            record_login_attempt(db, "", ip, False, "reset-bad-code")
            db.commit()
            return bad_request("That code is wrong or has expired.")

        ok, _reason = consume_code(db, row["id"], "reset", code, now)
        if not ok:
            record_login_attempt(db, row["username"], ip, False, "reset-bad-code")
            db.commit()
            # ONE MESSAGE FOR EVERY FAILURE. Separating "wrong" from "expired" from
            # "you have run out of tries" tells an attacker how close they are.
            return bad_request("That code is wrong or has expired.")

        # EVERY SESSION GOES. This is the half that makes recovery worth having:
        # the player reaching for it usually believes somebody else is already in
        # their account, and a new password that left the intruder's token alive
        # would not have removed them.
        store.set_password(db, row["id"], hash_password(new))
        revoked = store.end_sessions(db, row["id"])
        forget_devices(db, row["id"])
        record_login_attempt(db, row["username"], ip, True, "password-reset")
        db.commit()

        app.logger.warning("password RESET by email  user=%r  ip=%s  sessions_revoked=%d",
                           row["username"], ip or "?", revoked)

        send_mail_async(address, "Elusion RPG - your password was changed",
                        "The password for %s was just reset using a code sent to this "
                        "address, and every device signed in to it has been signed out.\n\n"
                        "If that was not you, change your password again immediately."
                        % row["username"])

        return {"ok": True, "revoked": revoked,
                "message": "Password updated. You can sign in now."}, 200

    app.add_url_rule("/api/auth/register", endpoint="register",
                     view_func=no_write_lock(register_account), methods=["POST"])
    app.add_url_rule("/api/auth/login", endpoint="login",
                     view_func=no_write_lock(login), methods=["POST"])
    app.add_url_rule("/api/auth/session", endpoint="session_info",
                     view_func=require_auth(session_info), methods=["GET"])
    app.add_url_rule("/api/auth/resume", endpoint="resume_session",
                     view_func=require_auth(resume_session), methods=["POST"])
    app.add_url_rule("/api/auth/confirm", endpoint="confirm_password",
                     view_func=require_auth(confirm_password), methods=["POST"])
    app.add_url_rule("/api/auth/logout", endpoint="logout",
                     view_func=require_auth(logout), methods=["POST"])
    app.add_url_rule("/api/auth/logout-all", endpoint="logout_all",
                     view_func=require_auth(logout_all), methods=["POST"])
    app.add_url_rule("/api/auth/password", endpoint="change_password",
                     view_func=no_write_lock(require_auth(change_password)), methods=["POST"])
    app.add_url_rule("/api/auth/recover", endpoint="request_recovery",
                     view_func=no_write_lock(request_recovery), methods=["POST"])
    app.add_url_rule("/api/auth/reset", endpoint="complete_recovery",
                     view_func=no_write_lock(complete_recovery), methods=["POST"])
