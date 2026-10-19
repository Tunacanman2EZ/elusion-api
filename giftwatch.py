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

THE NUMBERS ARE THE ROUTE'S OWN, not a copy of them: it asks
ownertools/data.py's gift_report() - the function GET /api/staff/gifts answers
with - on a read-only connection, and only prints. It used to carry its own
copy of that SQL, because the report lived in app.py and importing app.py runs
its start-up, which writes; since 19 Oct the report is a package's, and its
data.py imports nothing, so it is loaded from its file alone (not through the
package, whose routes need Flask - this runs on the server's plain python3).
test_gifts.py GL-8 checks it is that function, and the numbers it prints.

READ ONLY, opened mode=ro so it cannot be anything else. It prints usernames,
item names, amounts and dates - no passwords, tokens or email addresses - so
its output is safe to paste.
"""

import argparse
import datetime
import importlib.util
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

# app.py's OWNER_GIFTS_SINCE_KEY: the setting that says when the ledger began
# (the boot backfill writes it). test_gifts.py GL-8 holds the two the same.
GIFTS_SINCE_KEY = "owner_gifts_since"


def _report_function():
    """ownertools/data.py's gift_report(), loaded from that one file: it
    imports nothing, so loading it runs nothing and needs nothing installed."""
    spec = importlib.util.spec_from_file_location(
        "_giftwatch_ownertools_data", os.path.join(_HERE, "ownertools", "data.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.gift_report


gift_report = _report_function()


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

        user_id, who = None, None
        if args.player.strip():
            found = con.execute("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
                                (args.player.strip(),)).fetchone()
            if found is None:
                print("There is no account called %s." % args.player.strip())
                return 2
            user_id, who = int(found[0]), str(found[1])

        # EVERY NUMBER BELOW IS THE ROUTE'S: one call to the function GET
        # /api/staff/gifts answers with, on this read-only connection.
        report = gift_report(con, GIFTS_SINCE_KEY, user_id, max(1, min(100, int(args.recent))))
        totals = report["totals"]

        print()
        print("THE GIFTS LEDGER" + (" - WHAT %s WAS GIVEN" % who.upper() if who else ""))
        if report["ledger_since"] is not None:
            print("  counted from %s, the first gift the logs remember" % when(report["ledger_since"]))
        print(line("everything", totals))
        print(line("to players", report["to_players"]))
        print(line("to themselves", report["to_yourself"]))
        if totals["item_value"]:
            print("  the items would sell to the shop for %s gold" % number(totals["item_sells_for"]))

        # -- the economy now, to set it beside ---------------------------------
        # The report gives the gold in purses and banks together; the purses
        # alone are read here, so the line can say how it splits.
        economy = report["economy"]
        gold_now = economy["gold_now"]
        purses = int(con.execute("SELECT COALESCE(SUM(gold), 0) FROM saves").fetchone()[0])
        print()
        print("THE GOLD IN THE GAME NOW")
        print("  %s gold in purses and banks (%s carried, %s banked)"
              % (number(gold_now), number(purses), number(gold_now - purses)))
        print("  %s lusions in accounts" % number(economy["lusions_now"]))
        print("  %s gold made, ever, by everything (loot, sales, piles used)" % number(economy["gold_ever_made"]))
        if gold_now > 0 and totals["gold"] > gold_now:
            print("  the gifts%s come to more than all of it: a pile still in a bag is"
                  " counted as given, and is in nobody's purse until it is used" % (" to %s" % who if who else ""))
        elif gold_now > 0 and totals["gold"]:
            print("  the gifts%s come to %.1f%% of the gold there is now"
                  % (" to %s" % who if who else "", 100.0 * totals["gold"] / gold_now))
            print("  (a pile still in a bag is counted as given, but is not in a purse until it is used)")

        # -- who gave it -------------------------------------------------------
        givers = report["by_giver"]
        if len(givers) > 1:
            print()
            print("WHO GAVE IT")
            for p in givers:
                print("  %-20s %s   (%s %s)"
                      % (p["username"], worth(p["gold"], p["lusions"], p["item_value"]), number(p["gifts"]),
                         "gift" if p["gifts"] == 1 else "gifts"))

        # -- by player ---------------------------------------------------------
        if not who:
            people = report["by_player"]
            print()
            print("WHO GOT IT, THE MOST GOLD FIRST")
            if not people:
                print("  (nobody yet)")
            for p in people:
                print("  %-20s %s   (%s %s, last %s)"
                      % (p["username"], worth(p["gold"], p["lusions"], p["item_value"]), number(p["gifts"]),
                         "gift" if p["gifts"] == 1 else "gifts", when(p["last_at"])))

        # -- by item -----------------------------------------------------------
        items = report["by_item"]
        print()
        print("BY ITEM, THE BIGGEST FIRST")
        if not items:
            print("  (nothing yet)")
        for i in items:
            label = item_name(i["item_id"]) if i["item_id"] else "gold straight in"
            amount = "" if not i["item_id"] else "%s x " % number(i["quantity"])
            print("  %-34s %s" % (amount + label, worth(i["gold"], i["lusions"], i["item_value"])))

        # -- the newest --------------------------------------------------------
        recent = report["recent"]
        print()
        print("THE NEWEST")
        if not recent:
            print("  (nothing yet)")
        for g in recent:
            what = ("%s x %s" % (number(g["quantity"]), item_name(g["item_id"]))) if g["item_id"] else "gold"
            print("  %s  %s -> %-16s %-30s %s%s"
                  % (when(g["at"]), g["by"], g["username"], what, worth(g["gold"], g["lusions"], g["item_value"]),
                     "   (from the logs)" if g["source"] == "log" else ""))
        print()
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
