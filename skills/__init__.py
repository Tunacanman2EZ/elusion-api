"""skills - a character's six skills (a package since 18 Oct; the routes are as
they were in app.py).

    PUT  /api/character/skills  the whole set - validated, then dropped: every
                                skill is the server's to grant now
    POST /api/skill/train       report training for agility, defense and
                                magic, held to what the elapsed time allows

AND THE SERVER'S XP GRANT, which is why the package is registered before every
area that trains a skill: attack at the kill, fishing and cooking against the
items the server consumed, defense, agility and magic through the training
report - all through grant_xp() here, which register() hands back. app.py keeps
it callable as _grant_skill_xp(), and the skills read as skills_payload().

EVERY SKILL IS THE SERVER'S. The skills table was written only by the whole-set
PUT once, which replaced every row with whatever the client sent. Then each
skill got a server-observed event - the kill, the catch, the cook, the training
report - and the PUT stopped storing it. All six are SERVER_OWNED_SKILLS now,
so the PUT validates a body and throws it away: an un-updated game still sends
all six on every save, and a 400 would break saving for that build. Its 400s
are still the contract - an unknown skill name, a level of 0, a non-object.

TRAINING IS REPORTED, AND HELD TO THE CLOCK. Defense, agility and magic train
on continuous play, not on a discrete moment, so the client accumulates the raw
XP and reports it. The server cannot prove the play happened - no more than the
kill route can prove a fight did - but it caps the RATE (MAX_TRAIN_XP_PER_SEC x
the time since the last report, at most MAX_TRAIN_ELAPSED_SECONDS), which turns
"claim 99 instantly" into "the real hours", and applies the class specialty
itself.

Built like carry/ (ARCHITECTURE.md): routes.py (the edge), services.py (the
rules, pure), data.py (the SQL). It never imports app.py; register() is handed
what it needs. WHAT STAYS IN app.py: the skill set and the server's share of it
(VALID_SKILLS, SERVER_OWNED_SKILLS, TRAINABLE_SKILLS), the ceiling and the
training caps - the file's tunables, quoted at the top of it - the class
specialty (proficient_amount(), which the kill asks too), and whether a
character exists.
"""
from .routes import register  # noqa: F401
