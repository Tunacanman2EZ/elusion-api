#!/usr/bin/env python3
"""
canary.py - a read-only watch over the Elusion economy's invariants.

    python3 canary.py                    # check $ELUSION_DB (or elusion.db)
    python3 canary.py --db /path/elusion.db --quiet    # silent unless something broke

WHAT IT WATCHES, AND WHY IT IS SEPARATE FROM security_bot.py
------------------------------------------------------------
security_bot.py attacks a server BEFORE players are let onto it - a gate you run
by hand. This is the opposite watch: it reads the LIVE database on a schedule and
checks that the numbers still add up, so a slow leak, a bad deploy, or a tamper
that got past everything else shows up as an alert the same day, not as a player
noticing their gold is wrong.

It re-derives the same invariants the bot does, straight from ground truth:

  * gold conservation    SUM(gold_ledger.delta) == SUM(saves.gold)+SUM(bank_gold)
  * lusion conservation  SUM(lusion_ledger.delta) == SUM(accounts.lusions)
  * no negative balances anywhere
  * no skill above the cap (99)

Any drift here means gold or lusions entered or left the world without going
through the one function allowed to move them - which is either a bug or a
breach, and either way something to look at now.

READ-ONLY BY CONSTRUCTION. Opens the database mode=ro, so the watcher can never
be the thing that changed what it is watching. Same discipline as the bot.

EXIT CODE is 0 only if every invariant holds; non-zero otherwise. A scheduler
(cron, Task Scheduler) that checks it turns a broken number into an alert -
mail it, page it, drop it in a channel, whatever you run.
"""

import argparse
import os
import sqlite3
import sys


def check_all(db):
    def one(query):
        return int(db.execute(query).fetchone()[0] or 0)

    checks = []

    # GOLD CONSERVATION. The equation the whole gold_ledger exists to hold: every
    # coin held was minted through gold_delta() and recorded there.
    recorded = one("SELECT COALESCE(SUM(delta), 0) FROM gold_ledger")
    held = (one("SELECT COALESCE(SUM(gold), 0) FROM saves")
            + one("SELECT COALESCE(SUM(bank_gold), 0) FROM accounts"))
    checks.append((
        "gold conservation (ledger == held)",
        held == recorded,
        "recorded %d, held %d, drift %d" % (recorded, held, held - recorded),
    ))

    # LUSION CONSERVATION. gold_ledger's twin, its own currency and its own sum.
    l_recorded = one("SELECT COALESCE(SUM(delta), 0) FROM lusion_ledger")
    l_held = one("SELECT COALESCE(SUM(lusions), 0) FROM accounts")
    checks.append((
        "lusion conservation (ledger == held)",
        l_held == l_recorded,
        "recorded %d, held %d, drift %d" % (l_recorded, l_held, l_held - l_recorded),
    ))

    # NO NEGATIVE BALANCES. A negative subtracted is a positive minted; the DB
    # trigger and the gold_delta guard both forbid it, so a negative here means a
    # path found around both.
    min_gold = one("SELECT COALESCE(MIN(gold), 0) FROM saves")
    min_bank = one("SELECT COALESCE(MIN(bank_gold), 0) FROM accounts")
    min_lus = one("SELECT COALESCE(MIN(lusions), 0) FROM accounts")
    checks.append((
        "no negative balances",
        min_gold >= 0 and min_bank >= 0 and min_lus >= 0,
        "min carried %d, min banked %d, min lusions %d" % (min_gold, min_bank, min_lus),
    ))

    # NO SKILL ABOVE THE CAP. The one number a modified client most wants to lift.
    over_cap = one("SELECT COUNT(*) FROM skills WHERE level > 99")
    checks.append((
        "no skill above the cap (99)",
        over_cap == 0,
        "skill rows over the cap: %d" % over_cap,
    ))

    return checks


def main():
    ap = argparse.ArgumentParser(description="Read-only invariant watch over the Elusion economy.")
    ap.add_argument("--db", default=os.environ.get("ELUSION_DB", "elusion.db"),
                    help="database to check (default: $ELUSION_DB or elusion.db)")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing unless an invariant broke (for cron)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("[CANARY] FAIL: no database at %s" % args.db, file=sys.stderr)
        sys.exit(2)

    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    try:
        checks = check_all(con)
    except sqlite3.Error as exc:
        print("[CANARY] FAIL: could not read the database: %s" % exc, file=sys.stderr)
        sys.exit(2)
    finally:
        con.close()

    broken = [c for c in checks if not c[1]]

    if broken or not args.quiet:
        print("=" * 64)
        print("  ELUSION CANARY - %s" % args.db)
        print("=" * 64)
        for name, ok, detail in checks:
            print("  [ %-6s] %s" % ("hold" if ok else "BROKEN", name))
            print("           %s" % detail)
        print("=" * 64)
        print("  " + ("FAIL - %d invariant(s) broke; investigate now." % len(broken)
                      if broken else "PASS - every number adds up."))
        print("=" * 64)

    sys.exit(1 if broken else 0)


if __name__ == "__main__":
    main()
