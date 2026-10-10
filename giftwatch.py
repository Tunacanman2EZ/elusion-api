#!/usr/bin/env python3
"""
giftwatch.py - what the owner (and a co-owner) has given away, and what it comes to. Read only.

    python3 giftwatch.py                          # read $ELUSION_DB (or elusion.db)
    python3 giftwatch.py --db /var/lib/elusion/elusion.db
    python3 giftwatch.py --db /var/lib/elusion/elusion.db --player AllMind

WHY IT EXISTS
-------------
The owner, after he gave the student 10,990 Piles of Gold by accident and
decided to keep it: "do not roll back but make a ledger for anthing i give to
players so its accounted for if i ever ask how much did i inflate my server".

Every gift the owner makes is one row of owner_gifts (app.py, THE GIFTS
LEDGER): a give to a player, a grant to himself, gold put straight into his
own purse or bank. Each row keeps what the gift was worth ON THE DAY - the
gold a pile pays when it is used, the lusions a pile of lusions cashes into,
an item's catalogue price and what the shop pays for it - so a price changed
later never rewrites the past. Gifts from before the ledger were read back
from the staff log and the gold ledger the first time the API started with
it, and are marked "log".

This prints the same answer as GET /api/staff/gifts (the GM panel's "Gifts
ledger"), straight from the database, for the times the game is not open: the
totals, to players and to the givers themselves apart, who gave it (the owner,
and since 0.21.0 a co-owner), by player, the biggest gifts, the newest, and the
gold in every purse and bank now beside it.

The SQL is gift_report()'s, written out again here rather than imported:
importing app.py runs its start-up, and its start-up writes. If one changes,
change the other - test_gifts.py GL-8 holds them to the same numbers.

READ ONLY, opened mode=ro so it cannot be anything else. It prints usernames,
item names, amounts and dates - no passwords, tokens or email addresses - so
its output is safe to paste.
"""

import argparse
import datetime
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    # Only for the items' names. gamedata reads gamedata.json and nothing else.
    import gamedata
except Exception:                     # a missing catalogue only costs the names
    gamedata = None


def number(n):
    return "{:,}".format(int(n or 0))


def when(stamp):
    if not stamp:
        return "-"
    moment = datetime.datetime.fromtimestamp(int(stamp), datetime.timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def item_name(item_id):
    if not item_id:
        return "gold"
    row = gamedata.item_row(item_id) if gamedata is not None else None
    return str(row.get("display_name") or item_id) if row else str(item_id)


def worth(gold, lusions, value):
    """'24,975,000 gold', '50 lusions', 'items worth 1,200' - only the parts
    there are."""
    parts = []
    if gold:
        parts.append("%s gold" % number(gold))
    if lusions:
        parts.append("%s lusions" % number(lusions))
    if value:
        parts.append("items worth %s" % number(value))
    return ", ".join(parts) if parts else "nothing"


def sums(con, where, params):
    row = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(gold), 0), COALESCE(SUM(lusions), 0),"
        " COALESCE(SUM(value), 0), COALESCE(SUM(sells_for), 0), MIN(at), MAX(at)"
        " FROM owner_gifts" + where, params).fetchone()
    return {"gifts": int(row[0]), "gold": int(row[1]), "lusions": int(row[2]),
            "item_value": int(row[3]), "item_sells_for": int(row[4]),
            "first_at": row[5], "last_at": row[6]}


def line(label, s):
    return "  %-13s %s   (%s %s)" % (label, worth(s["gold"], s["lusions"], s["item_value"]),
                                     number(s["gifts"]), "gift" if s["gifts"] == 1 else "gifts")


def main():
    ap = argparse.ArgumentParser(description="What the owner has given away, read only.")
    ap.add_argument("--db", default=os.environ.get("ELUSION_DB", "elusion.db"),
                    help="database to read (default: $ELUSION_DB or elusion.db)")
    ap.add_argument("--player", default="",
                    help="only what this account was given")
    ap.add_argument("--recent", type=int, default=15,
                    help="how many of the newest gifts to list (default 15)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("There is no database at %s." % args.db)
        return 2
    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    try:
        has_ledger = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'owner_gifts'").fetchone()
        if not has_ledger:
            print("This database has no gifts ledger yet. It starts the first time the API runs"
                  " with the version that has it: pull, restart elusion-api, then ask again.")
            return 2

        where, params, who = "", (), None
        if args.player.strip():
            found = con.execute("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
                                (args.player.strip(),)).fetchone()
            if found is None:
                print("There is no account called %s." % args.player.strip())
                return 2
            where, params, who = " WHERE user_id = ?", (int(found[0]),), str(found[1])
        joiner = " AND " if where else " WHERE "

        since = con.execute("SELECT value FROM server_settings WHERE key = 'owner_gifts_since'").fetchone()
        totals = sums(con, where, params)
        to_players = sums(con, where + joiner + "(actor_id IS NULL OR user_id IS NOT actor_id)", params)
        to_yourself = sums(con, where + joiner + "user_id IS actor_id", params)

        print()
        print("THE GIFTS LEDGER" + (" - WHAT %s WAS GIVEN" % who.upper() if who else ""))
        if since is not None and str(since[0]).isdigit():
            print("  counted from %s, the first gift the logs remember" % when(since[0]))
        print(line("everything", totals))
        print(line("to players", to_players))
        print(line("to themselves", to_yourself))
        if totals["item_value"]:
            print("  the items would sell to the shop for %s gold" % number(totals["item_sells_for"]))

        # -- the economy now, to set it beside ---------------------------------
        purses = int(con.execute("SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0])
        banks = con.execute("SELECT COALESCE(SUM(bank_gold), 0), COALESCE(SUM(lusions), 0)"
                            " FROM accounts").fetchone()
        made = int(con.execute("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger WHERE delta > 0").fetchone()[0])
        gold_now = purses + int(banks[0])
        print()
        print("THE GOLD IN THE GAME NOW")
        print("  %s gold in purses and banks (%s carried, %s banked)"
              % (number(gold_now), number(purses), number(banks[0])))
        print("  %s lusions in accounts" % number(banks[1]))
        print("  %s gold made, ever, by everything (loot, sales, piles used)" % number(made))
        if gold_now > 0 and totals["gold"] > gold_now:
            print("  the gifts%s come to more than all of it: a pile still in a bag is"
                  " counted as given, and is in nobody's purse until it is used" % (" to %s" % who if who else ""))
        elif gold_now > 0 and totals["gold"]:
            print("  the gifts%s come to %.1f%% of the gold there is now"
                  % (" to %s" % who if who else "", 100.0 * totals["gold"] / gold_now))
            print("  (a pile still in a bag is counted as given, but is not in a purse until it is used)")

        # -- who gave it -------------------------------------------------------
        givers = con.execute(
            "SELECT actor_name, COUNT(*), SUM(gold), SUM(lusions), SUM(value)"
            " FROM owner_gifts" + where + " GROUP BY COALESCE(actor_id, -1), actor_name"
            " ORDER BY SUM(gold) DESC, SUM(value) DESC, actor_name LIMIT 20", params).fetchall()
        if len(givers) > 1:
            print()
            print("WHO GAVE IT")
            for p in givers:
                print("  %-20s %s   (%s %s)"
                      % (p[0], worth(p[2], p[3], p[4]), number(p[1]), "gift" if p[1] == 1 else "gifts"))

        # -- by player ---------------------------------------------------------
        if not who:
            people = con.execute(
                "SELECT username, COUNT(*), SUM(gold), SUM(lusions), SUM(value), MAX(at)"
                " FROM owner_gifts GROUP BY COALESCE(user_id, -1), username"
                " ORDER BY SUM(gold) DESC, SUM(value) DESC, username LIMIT 50").fetchall()
            print()
            print("WHO GOT IT, THE MOST GOLD FIRST")
            if not people:
                print("  (nobody yet)")
            for p in people:
                print("  %-20s %s   (%s %s, last %s)"
                      % (p[0], worth(p[2], p[3], p[4]), number(p[1]), "gift" if p[1] == 1 else "gifts",
                         when(p[5])))

        # -- by item -----------------------------------------------------------
        items = con.execute(
            "SELECT item_id, SUM(quantity), SUM(gold), SUM(lusions), SUM(value)"
            " FROM owner_gifts" + where + " GROUP BY item_id, kind"
            " ORDER BY SUM(gold) + SUM(value) + SUM(lusions) DESC LIMIT 15", params).fetchall()
        print()
        print("BY ITEM, THE BIGGEST FIRST")
        if not items:
            print("  (nothing yet)")
        for i in items:
            label = item_name(i[0]) if i[0] else "gold straight in"
            amount = "" if not i[0] else "%s x " % number(i[1])
            print("  %-34s %s" % (amount + label, worth(i[2], i[3], i[4])))

        # -- the newest --------------------------------------------------------
        recent = con.execute(
            "SELECT at, actor_name, username, item_id, quantity, gold, lusions, value, source"
            " FROM owner_gifts" + where + " ORDER BY at DESC, id DESC LIMIT ?",
            params + (max(1, min(100, int(args.recent))),)).fetchall()
        print()
        print("THE NEWEST")
        if not recent:
            print("  (nothing yet)")
        for g in recent:
            what = ("%s x %s" % (number(g[4]), item_name(g[3]))) if g[3] else "gold"
            print("  %s  %s -> %-16s %-30s %s%s"
                  % (when(g[0]), g[1], g[2], what, worth(g[5], g[6], g[7]),
                     "   (from the logs)" if g[8] == "log" else ""))
        print()
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
