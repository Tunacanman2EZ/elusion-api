"""trade/schema.py - trade/'s tables:
trade_holds, trades and trade_items - a fresh find that cannot
be traded yet, a trade in progress, and what each side puts in.

Until 19 Oct these were in app.py's init_db(), and they moved word for word.

WHY HERE. A table lives with the one area that adds its rows; what the core
writes, or what several areas add rows to, stays in init_db() (ARCHITECTURE.md).
init_db() calls create() with the other packages', after the core's tables and
before any migration - the order, and why, is written out there.

upgrade() adds the columns that came later to a database made before
them; init_db() calls it after every create(), among its migrations.

INDENTED AS IT WAS IN init_db(), so the CREATE text SQLite keeps in the
database is the text it kept before the move.
"""

SCHEMA = """
        -- A FRESH FIND THAT CANNOT BE TRADED YET. One row per mythic or Perfect
        -- piece as it enters an account - taken from a loot bag or bought from
        -- the shop - lasting TRADE_HOLD_SECONDS. The server does not watch the
        -- fight (E-3), so a cheated kill can mint a mythic; the hold keeps it
        -- in the account that found it long enough for killwatch.py and staff
        -- to see it first. Counted per account and item id, not per cell:
        -- the item may move between characters through the bank.
        CREATE TABLE IF NOT EXISTS trade_holds (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            item_id     TEXT    NOT NULL,
            created_at  INTEGER NOT NULL,
            held_until  INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_trade_holds_user
            ON trade_holds(user_id, item_id, held_until);

        -- A TRADE IN PROGRESS. Two characters, what each is putting up, and
        -- whether each has said yes to what the other is showing.
        --
        -- NOTHING IS HELD IN ESCROW, and that is deliberate. The obvious design
        -- takes both sides' items out of their bags when they are offered and
        -- holds them here - but then a disconnect, a crash or an abandoned
        -- window leaves real items owned by a row instead of a player, and
        -- every one of those needs a sweeper to give them back. This table
        -- records INTENT; the items never leave the bag until the single
        -- transaction that crosses them, and that transaction re-checks that
        -- both sides still hold what they promised. The worst case is a trade
        -- that fails at the last moment, which is a refusal, not a lost sword.
        --
        -- state is the concurrency guard. Execution flips 'open' to 'done' with
        -- a conditional UPDATE and checks it changed a row, so two confirms
        -- arriving together can only fire once - see trade/routes.py, execute().
        CREATE TABLE IF NOT EXISTS trades (
            trade_id    TEXT PRIMARY KEY,
            a_user      INTEGER NOT NULL,
            a_slot      INTEGER NOT NULL,
            a_gold      INTEGER NOT NULL DEFAULT 0 CHECK (a_gold >= 0),
            a_confirmed INTEGER NOT NULL DEFAULT 0,
            b_user      INTEGER NOT NULL,
            b_slot      INTEGER NOT NULL,
            b_gold      INTEGER NOT NULL DEFAULT 0 CHECK (b_gold >= 0),
            b_confirmed INTEGER NOT NULL DEFAULT 0,
            state       TEXT    NOT NULL DEFAULT 'open',
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL,
            FOREIGN KEY (a_user) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (b_user) REFERENCES users(id) ON DELETE CASCADE
        );

        -- Finding a player's open trade is the single most common read here
        -- (the panel polls it), and it is asked from both directions.
        --
        -- updated_at ON THE END, so the same two indexes also answer "your
        -- last ten trades" in order instead of sorting every trade somebody
        -- has ever made. They replace idx_trades_a and idx_trades_b, which
        -- were these without the last column - see the MIGRATION section.
        CREATE INDEX IF NOT EXISTS idx_trades_a_recent ON trades(a_user, state, updated_at);
        CREATE INDEX IF NOT EXISTS idx_trades_b_recent ON trades(b_user, state, updated_at);

        -- The prune of old cancelled trades asks by state and age and nothing
        -- else. Without this it read every trade ever made, on every offer.
        --
        -- PARTIAL, ON PURPOSE, and the first version was not. A plain
        -- (state, updated_at) index was picked by the planner for "find my
        -- OPEN trade" - the read every trade window polls - because it
        -- matches state = 'open' with an equality and the planner has no
        -- statistics saying that is most of the table. That read went from
        -- two index lookups by player to a scan of every open trade in the
        -- world. test_trades.py caught it in the query plan. An index that
        -- only holds cancelled rows cannot be chosen for anything but the
        -- prune it exists for, and is smaller besides.
        CREATE INDEX IF NOT EXISTS idx_trades_cancelled ON trades(updated_at)
            WHERE state = 'cancelled';

        -- ONE ROW PER ITEM TYPE PER SIDE, not per backpack cell. The primary
        -- key does the merging: offering five arrows twice is one row of ten,
        -- not two rows that have to be summed before anything can be checked.
        -- Positions are the client's business; what crosses is a quantity.
        CREATE TABLE IF NOT EXISTS trade_items (
            trade_id TEXT    NOT NULL,
            side     TEXT    NOT NULL CHECK (side IN ('a', 'b')),
            item_id  TEXT    NOT NULL,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (trade_id, side, item_id),
            FOREIGN KEY (trade_id) REFERENCES trades(trade_id) ON DELETE CASCADE
        );
"""


def create(db):
    """Every table and index above, each IF NOT EXISTS, so nothing happens on a
    database that has them. An executescript, which commits first: init_db()
    calls this before its migrations begin."""
    db.executescript(SCHEMA)


def upgrade(db, add_column):
    """The columns added since these tables first shipped, for a database made
    before them. add_column(db, table, column, definition) is init_db()'s
    helper, handed in (a package never imports app.py): it adds a column only
    where it is missing. db.execute only - init_db() runs this inside its one
    migration transaction, which an executescript would commit early."""
    # TRADES, THREE COLUMNS (the fourth, saves.resync_trade, is on the core's
    # saves table, and init_db() adds it). trade/__init__.py has the rules each
    # one serves.
    #
    # revision: bumped by every change to either side's offer. A confirm names
    # the revision it saw, so "accept" can only ever mean the offer that was on
    # the screen - not one that was swapped underneath the button.
    add_column(db, "trades", "revision", "INTEGER NOT NULL DEFAULT 0")
    # What each side paid the kingdom, kept on the row. The ledger has it too,
    # but only as text in a detail column; a history panel should not be
    # parsing prose.
    add_column(db, "trades", "a_tax", "INTEGER NOT NULL DEFAULT 0")
    add_column(db, "trades", "b_tax", "INTEGER NOT NULL DEFAULT 0")

    # SUPERSEDED by idx_trades_a_recent / idx_trades_b_recent, which are these
    # with updated_at on the end. A prefix index beside its own extension is
    # a second write on every trade for nothing.
    db.execute("DROP INDEX IF EXISTS idx_trades_a")
    db.execute("DROP INDEX IF EXISTS idx_trades_b")
