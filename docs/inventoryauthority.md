# Moving the backpack to server authority

The server owns kills, loot rolls and loot bags. It does not own the backpack:
`PUT /api/character/inventory` takes whatever array the client sends and stores
it. A modified client can hand itself twenty stacks of anything real.

This is the plan for closing that. It is staged so every stage leaves the game
playable, because a half-migrated inventory is worse than an unmigrated one.

## The rule, for everything not yet built

**Anything that creates, destroys or moves an item is a server endpoint.**

Shops, fishing and cooking do not exist yet. Building them server-owned costs
almost nothing now and costs a rewrite each if they are built client-side first
and migrated after. This rule is the cheapest item on this page and the easiest
to forget, which is why it is at the top.

Concretely, when those get built:

- `POST /api/shop/buy` and `/sell` debit gold and write `carry_items` server
  side. The client sends "buy 3 of item X", never "here is my new backpack".
- `POST /api/fishing/catch` decides what was caught. The client sends that it
  fished, not what it got — a client that names its own catch is a client that
  catches whatever it likes.
- `POST /api/cooking/cook` consumes the inputs and produces the output in one
  transaction. Both halves server side, or a client can cook from nothing.

The test for whether an endpoint is shaped right: **could a malicious client
profit by lying in this request?** If the answer is yes, the decision is on the
wrong side of the wire.

## Where it already works

`POST /api/loot/take` is the model. It picks the item, writes `carry_items`
itself via `_add_to_backpack()`, deletes the bag entry in the same transaction,
and returns the resulting inventory as totals. The client renders the answer
rather than computing it. Copy this shape.

`POST /api/combat/kill` likewise owns the roll and the XP.

## Stages

### 0. Shadow mode — DONE

`_report_unexplained_gains()` compares each `PUT /api/character/inventory`
against what the server last recorded and logs `[LEDGER]` lines when the client
claims MORE than the server granted. It refuses nothing.

This exists because most disagreements are honest today — a potion eaten, a bank
withdrawal, a shop purchase — and a rule written before knowing which is which
would refuse real players. Play normally, then grep the Flask log for `[LEDGER]`:
whatever produces the most unexplained gain is the next thing to close.

Losses are not logged. A client holding less ate something or deposited it. A
client holding more got it from somewhere the server did not see, and that is
the entire question.

### 1. Bank transfers

`PUT /api/account/bank` is the same blanket push as the backpack. Replace with
`POST /api/bank/deposit` and `POST /api/bank/withdraw`, each moving quantities
between `carry_items` and `bank_items` in one transaction. Removes a whole class
of `[LEDGER]` noise and is self-contained.

### 2. Inventory mutations

The operations the client does locally and then pushes:

- `POST /api/inventory/move` — swap or move between cells
- `POST /api/inventory/use` — consume, apply the effect server side
- `POST /api/inventory/discard` — trash, with the quantity named
- `POST /api/inventory/split` — split a stack

Each returns the resulting inventory. After this the client never needs to send
a whole array for an ordinary action.

### 3. The three unbuilt systems

Shops, fishing, cooking — built to the rule at the top of this page.

### 4. Retire the blanket write

`PUT /api/character/inventory` becomes an error rather than a store. Do this
LAST and only once `[LEDGER]` has been quiet through a full play session,
because the day it starts refusing is the day every path that still needs it
breaks at once.

## What this does not fix

Anything the client is still trusted to report. Damage dealt, whether a hit
landed, how fast it attacks — none of that is in scope here, and a server that
owns the backpack but believes a client that says it killed a boss in one hit
has moved the problem rather than solved it.

Worth doing in this order anyway: items are the thing that persists, that other
players see, and that a trade system would let someone launder.
