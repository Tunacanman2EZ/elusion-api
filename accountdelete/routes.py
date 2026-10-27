"""accountdelete/routes.py - the edge: POST /api/account/delete.

register() is handed app.py's own pieces - it never imports app.py:

    require_auth          the token check (puts the account on g.user)
    get_db                the request's database connection
    bad_request           a message -> app.py's 400
    protected             (username) -> True for the owner and the co-owners,
                          whose accounts cannot be deleted
    find_open_trade       (db, user_id) -> the open trade naming them, or None
    gold_delta            (db, user_id, slot, delta, reason, detail) - a purse
    bank_gold_delta       (db, user_id, delta, reason, detail) - the bank
    lusion_delta          (db, user_id, slot, delta, reason, detail)
    fix_party             (db, party_id) - parties/'s fix(): a party of one goes,
                          two or more keep one leader
    drop_guild            (db, guild_id) - guilds/'s: a guild its last member left
    log_guild_event       (db, guild_id, kind, actor) - guilds/'s history writer
    take_down_line        (db, row, now) - chat/'s take_down(): a line off every
                          screen (the deletion the feed carries) and out
    record_login_attempt  (db, username, ip, ok, reason) - the login log
    client_ip             () -> the caller's address
    log_staff_action      (actor, action, target_name, target_id, detail)

THE WRITE LOCK IS TAKEN (it is a POST): the burns read balances and then move
them, which is what the lock is for. The password hash inside it costs the
other writers a few milliseconds once per deleted account.
"""
import time

from flask import g, request
from werkzeug.security import check_password_hash

from . import data, services


def register(app, *, require_auth, get_db, bad_request, protected, find_open_trade, gold_delta,
             bank_gold_delta, lusion_delta, fix_party, drop_guild, log_guild_event, take_down_line,
             record_login_attempt, client_ip, log_staff_action):

    def delete_account():
        """
        Delete your account, for good
        ---
        tags:
          - Account
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
              required: [confirm, password]
              properties:
                confirm:  {type: string, example: tunacan, description: "The account's name, typed. Any case."}
                password: {type: string, description: "The account's password"}
        responses:
          200:
            description: Gone - every character, the bank, friends, chat lines and sign-ins; the name is free
          400:
            description: The name typed is not the account's, or no password
          401:
            description: The password is not correct (counted as a failed login)
          403:
            description: The owner's and the co-owners' accounts cannot be deleted
          409:
            description: An open trade names the account, or it leads a guild with other members
        """
        payload = request.get_json(silent=True) or {}
        user = g.user
        user_id = int(user["id"])
        username = str(user["username"])
        if not services.confirmed(payload.get("confirm"), username):
            return bad_request("Type your account's name to delete it.")
        password = payload.get("password")
        if not isinstance(password, str) or password == "" or len(password) > services.PASSWORD_MOST:
            return bad_request("Your password is needed to delete your account.")
        if protected(username):
            return {"error": "Forbidden",
                    "message": "The owner's and the co-owners' accounts cannot be deleted."}, 403

        db = get_db()
        row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        # THE PASSWORD, as the email route asks it: a stolen session must not be
        # able to throw an account away. A wrong one counts as a failed login.
        if row is None or not check_password_hash(row["password_hash"], password):
            record_login_attempt(db, username, client_ip(), False, "bad-password-on-delete")
            db.commit()
            return {"error": "Unauthorized", "message": "That password is not correct."}, 401

        if find_open_trade(db, user_id) is not None:
            return {"error": "Conflict",
                    "message": "You are in a trade. Finish or cancel it first."}, 409
        seat = data.guild_seat(db, user_id)
        if seat is not None and services.guild_blocks(seat[1], seat[2]):
            return {"error": "Conflict",
                    "message": "You lead a guild. Make somebody else the leader, or disband it, first."}, 409

        # ---- everything above this line is validation; everything below commits --
        now = int(time.time())
        stand_in = services.stand_in_name(user_id)

        # THE MONEY THROUGH THE LEDGERS, before the rows go: SUM(gold_ledger)
        # must still equal every purse and bank afterwards (test_economy.py).
        burned = 0
        for character in data.characters(db, user_id):
            gold = int(character["gold"] or 0)
            if gold > 0:
                gold_delta(db, user_id, int(character["slot"]), -gold, services.DELETE_REASON,
                           "carried gold of a deleted account's %s" % character["class_id"])
                burned += gold
        acct = data.account(db, user_id)
        lusions = 0
        if acct is not None:
            bank = int(acct["bank_gold"] or 0)
            if bank > 0:
                bank_gold_delta(db, user_id, -bank, services.DELETE_REASON, "bank of a deleted account")
                burned += bank
            lusions = int(acct["lusions"] or 0)
            if lusions > 0:
                lusion_delta(db, user_id, None, -lusions, services.DELETE_REASON, "a deleted account's")

        # THE GUILD AND THE PARTY are left as leaving would leave them.
        if seat is not None:
            db.execute("DELETE FROM guild_members WHERE user_id = ?", (user_id,))
            remaining = db.execute("SELECT COUNT(*) FROM guild_members WHERE guild_id = ?",
                                   (seat[0],)).fetchone()[0]
            if remaining == 0:
                drop_guild(db, seat[0])
            else:
                log_guild_event(db, seat[0], "left", stand_in)
        party = data.party_of(db, user_id)
        if party is not None:
            db.execute("DELETE FROM party_members WHERE user_id = ?", (user_id,))
            fix_party(db, party)

        # THEIR CHAT LINES, off every screen that still shows them, as a mod's
        # delete takes one down (chat/'s own writer), and the pictures they
        # showed or put up, to be weighed once the rows are gone.
        images = data.held_images(db, user_id)
        for line in data.chat_lines(db, user_id):
            if line["image_id"]:
                images.add(line["image_id"])
            take_down_line(db, line, now)
        # One line in the staff log, before the name goes: that an account was
        # deleted, by its owner, and what it took with it.
        log_staff_action({"id": user_id, "username": stand_in}, "account_deleted", stand_in, user_id,
                         "deleted by its owner; %d gold and %d lusions burned" % (burned, lusions))
        data.empty(db, user_id, username, stand_in, now)
        dropped = data.drop_orphan_images(db, images)
        db.commit()
        return {"deleted": True, "gold_burned": burned, "lusions_burned": lusions, "pictures_removed": dropped,
                "message": "Your account is deleted. Thank you for playing."}, 200

    app.add_url_rule("/api/account/delete", endpoint="delete_account",
                     view_func=require_auth(delete_account), methods=["POST"])
