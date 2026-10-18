"""saves - a character itself: its slot, its whole read, and deleting it (a
package since 18 Oct; the routes are as they were in app.py).

    GET  /api/save              the account's characters, slot by slot
    PUT  /api/save              make a character, or write what the game may
                                still write: its name, class, area, the pet
                                out and the explored map
    GET  /api/character         everything one character is, in one call -
                                identity, vitals, carry, equipment, skills
    POST /api/character/delete  delete one, for good, its name typed first

THE SAVE WRITES LESS THAN IT LOOKS. The level is never taken from the body - a
new character starts at 1 and an old one keeps what the kill route granted.
Equipment and the hotbar are the server's too (the equip routes, and carried
cells past the bag), so a game that still sends them is answered with the field
named in `ignored` rather than refused: an un-updated game saving honestly must
not start failing. So is a pet the account does not hold. A missing key keeps
what is stored and an explicit empty one clears it, because not every caller
that writes a slot knows which pet is out or where the character stands.

A NEW CHARACTER IS FULL, AN OLD ONE IS NEVER FILLED. The maxima are derived on
every save from class, level and what is worn (app.py's _derived_stats()); a
new character gets full pools, an existing one only has its pools brought down
to a ceiling that fell. Refilling on every save would heal for free.

DELETING TAKES THE CHARACTER'S ROWS AND NOTHING OF THE ACCOUNT'S: its save,
carry, skills, heal grants, save history, kill record and loot bags go; the
bank, the lusions, friends and the guild stay. The carried gold leaves through
the ledger, so the supply still adds up, and a copy of what was deleted is kept
(character_deletions, the last few an account made).

Built like carry/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: the slot parser and the 400 every route
shares, _derived_stats() and the JSON-column reader, the save history
(take_snapshot()), the text rule, the trade lookup, the ledgers, and the two
deletion constants a suite reads (CHARACTER_DELETIONS_KEPT,
CHARACTER_DELETE_REASON).
"""
from .routes import register  # noqa: F401
from .services import MAX_EXPLORED_BYTES, MAX_EXPLORED_TILES, VALID_CLASSES  # noqa: F401
