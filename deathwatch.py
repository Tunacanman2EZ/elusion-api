#!/usr/bin/env python3
"""
deathwatch.py - a read-only look at why the kingdom board says nobody has died.

    python deathwatch.py                       # read $ELUSION_DB (or elusion.db)
    python deathwatch.py --db C:\\path\\elusion.db

WHAT THIS IS FOR
----------------
The board's deaths column is fed by ONE statement in app.py:

    if "hp" in updates and int(row["hp"]) > 0 and int(updates["hp"]) <= 0:
        UPDATE users SET deaths = deaths + 1

A death is a TRANSITION, not a state - the stored hp was above zero and the
arriving one is not. That is deliberate and it is tested (test_economy.py, "a
death is counted" / "staying dead is not five more deaths"), and a direct
repro against this very app.py counts it for both body shapes the client
sends. So when the board reads zero, the question is never "is the counter
broken". It is **did a zero ever cross the wire while the stored hp was above
it**, and this script answers that from the database rather than by guessing.

THE THREE ANSWERS IT CAN GIVE
-----------------------------
1. REVIVES BUT NO DEATHS, and the revives are old.
   The `deaths` column was added by _migrate_add_column() with DEFAULT 0 and
   nothing backfills it - there is nothing to backfill FROM, because a death
   left no row before the counter existed. Every revive older than the column
   is a death that happened and was never countable. The coffers still show
   the gold, because the LEDGER always had it. Nothing is broken; the counter
   simply starts the day it was added.

2. REVIVES BUT NO DEATHS, and a revive is recent.
   Then a zero really is failing to arrive, and the client half is where to
   look: take_damage() returns at `if hp <= 0` before the call that saves, so
   the fatal hit is the one hit that never saved. _start_death_sequence() now
   calls save_character_state() + flush_save() for exactly this reason.

3. A CHARACTER STORED AT hp 0 AND deaths STILL 0.
   The character is dead on the server, so a zero DID arrive - but the stored
   hp was already zero when it did, so there was no transition to count. That
   is the counter working as designed, on a death that was recorded before it
   could be counted.

READ-ONLY, and opened `mode=ro` so it cannot be anything else. It prints
usernames, hp and timestamps. It never touches password hashes, tokens or
email addresses, so its output is safe to paste.
"""

import argparse
import datetime
import os
import sqlite3
import sys


def when(stamp):
    if not stamp:
        return "never"
    return datetime.datetime.fromtimestamp(int(stamp)).strftime("%Y-%m-%d %H:%M")


def ago(stamp, now):
    if not stamp:
        return ""
    seconds = max(0, now - int(stamp))
    if seconds < 3600:
        return "%d min ago" % (seconds // 60)
    if seconds < 86400:
        return "%d hours ago" % (seconds // 3600)
    return "%d days ago" % (seconds // 86400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("ELUSION_DB", "elusion.db"),
                    help="database to read (default: $ELUSION_DB or elusion.db)")
    ap.add_argument("--revives", type=int, default=12,
                    help="how many recent revives to list (default 12)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("no such database: %s" % args.db)
        return 2

    con = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    con.row_factory = sqlite3.Row
    now = int(__import__("time").time())

    # -- the counter itself ---------------------------------------------------
    print("\nDEATHS PER ACCOUNT")
    rows = con.execute(
        "SELECT id, username, deaths FROM users ORDER BY deaths DESC, username").fetchall()
    total = 0
    for r in rows:
        total += int(r["deaths"] or 0)
        print("  %-20s deaths %d" % (r["username"], int(r["deaths"] or 0)))
    print("  %-20s %d   <- this is the board's total_deaths" % ("TOTAL", total))

    # -- what the saves say right now ----------------------------------------
    #
    # STORED hp IS THE OTHER HALF OF THE CONDITION. A character sitting at 0 is
    # a character whose next reported 0 cannot be a transition, which is the
    # one case that looks like a broken counter and is not.
    print("\nCHARACTERS, AS THE SERVER HOLDS THEM")
    saves = con.execute(
        "SELECT users.username AS username, saves.slot AS slot, saves.name AS name,"
        "       saves.hp AS hp, saves.max_hp AS max_hp, saves.updated_at AS updated_at "
        "FROM saves JOIN users ON users.id = saves.user_id "
        "ORDER BY users.username, saves.slot").fetchall()
    if not saves:
        print("  (no characters)")
    for s in saves:
        state = "DEAD" if int(s["hp"] or 0) <= 0 else "alive"
        print("  %-20s slot %d  %-14s hp %4d/%-4d  %-5s  last write %s (%s)"
              % (s["username"], int(s["slot"]), str(s["name"])[:14],
                 int(s["hp"] or 0), int(s["max_hp"] or 0), state,
                 when(s["updated_at"]), ago(s["updated_at"], now)))

    # -- revives, which are deaths by another name ---------------------------
    #
    # A revive is the one thing in the ledger that PROVES a death: the route
    # refuses a character the server does not hold at hp 0. So revives are a
    # lower bound on deaths, and the gap between the two counts is the thing
    # worth explaining.
    print("\nREVIVES (every one of these was a death)")
    revives = []
    for table, unit in (("gold_ledger", "gold"), ("lusion_ledger", "lusions")):
        try:
            found = con.execute(
                "SELECT %s.at AS at, users.username AS username, %s.delta AS delta "
                "FROM %s LEFT JOIN users ON users.id = %s.user_id "
                "WHERE %s.reason = 'revive' ORDER BY %s.at DESC"
                % (table, table, table, table, table, table)).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in found:
            revives.append((int(row["at"]), row["username"] or "(deleted)",
                            abs(int(row["delta"])), unit))

    revives.sort(reverse=True)
    if not revives:
        print("  none recorded")
    for at, username, amount, unit in revives[:args.revives]:
        print("  %s (%-12s) %-20s paid %s %s"
              % (when(at), ago(at, now), username, "{:,}".format(amount), unit))
    if len(revives) > args.revives:
        print("  ... and %d older" % (len(revives) - args.revives))

    # -- the verdict ----------------------------------------------------------
    #
    # ONE PAYMENT IS NOT ONE DEATH. A revive can take gold AND lusions, and the
    # gold half can come out of the purse and the bank as two rows. So the
    # count of rows is an upper bound on deaths and the newest row is the fact
    # that actually decides this - which is why the verdict below leans on the
    # TIMESTAMP and not on the arithmetic.
    print("\nWHAT THAT MEANS")
    if total > 0:
        print("  The counter is moving. %d death%s recorded."
              % (total, "" if total == 1 else "s"))
    elif not revives:
        print("  No deaths and no revives. Nothing has died since this database")
        print("  was made - there is nothing here for the counter to have missed.")
    else:
        newest = revives[0][0]
        print("  %d revive payment%s recorded and 0 deaths counted."
              % (len(revives), "" if len(revives) == 1 else "s"))
        print("  Most recent revive: %s (%s)." % (when(newest), ago(newest, now)))
        print("")
        if now - newest > 86400:
            print("  That is over a day old. The deaths column was added with")
            print("  DEFAULT 0 and nothing backfills it, so every death older than")
            print("  the column is invisible by construction. Die once now and the")
            print("  board should move; if it does not, that is a real bug.")
        else:
            print("  That is recent, so a zero should have crossed the wire and")
            print("  did not. Check the client half: god mode short-circuits")
            print("  take_damage() before hp changes, and a character already")
            print("  stored at hp 0 produces no transition to count.")

    dead_now = [s for s in saves if int(s["hp"] or 0) <= 0]
    if dead_now and total == 0:
        print("")
        print("  NOTE: %d character%s stored at hp 0 right now. A zero DID arrive"
              % (len(dead_now), "" if len(dead_now) == 1 else "s"))
        print("  for %s. The next one cannot count, because the stored hp it"
              % ", ".join(str(s["name"]) for s in dead_now))
        print("  would have to cross is already there.")

    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
