"""The options screen's rules, checked. Run: python3 test_settings.py"""
from sim_settings import Settings, DEFAULTS, WINDOW_SIZES, VSYNC_MODES, FRAME_CAPS

passed = failed = 0


def normalise_ok(mode):
    s = Settings()
    s.set_value("vsync", mode)
    return s.get_value("vsync") == mode


def check(label, ok, detail=None):
    global passed, failed
    if ok:
        passed += 1
        print("  ok   %s" % label)
    else:
        failed += 1
        print("  FAIL %s   %r" % (label, detail))


def section(t):
    print("\n%s\n%s" % (t, "-" * len(t)))


section("THE SCHEMA")
s = Settings()
check("a fresh install reads every default",
      all(s.get_value(k) == DEFAULTS[k] for k in DEFAULTS))
check("and knows nine settings", len(DEFAULTS) == 9, sorted(DEFAULTS))

# A KEY THAT IS NOT IN DEFAULTS CANNOT BE SET. The alternative is a typo that
# stores fine, reads back as null, and becomes "my setting will not save".
s.set_value("volume_musc", 0.5)
check("a misspelled key is refused, loudly", s.errors == ["volume_musc"], s.errors)
check("and stores nothing", "volume_musc" not in s.values, s.values)
s.errors.clear()
check("reading one is refused too", s.get_value("nonsense") is None)

section("TYPES ARE PINNED BY THE DEFAULT")
# ConfigFile round-trips 1.0 as a float and 1 as an int. A slider landing
# exactly on 1 would otherwise store an int where a float belongs and read
# back as one forever.
s = Settings()
s.set_value("volume_master", 1)
check("a slider that lands on 1 is still a float",
      isinstance(s.get_value("volume_master"), float), s.get_value("volume_master"))
s.set_value("window_width", 1600.0)
check("a window width is always a whole number",
      s.get_value("window_width") == 1600
      and isinstance(s.get_value("window_width"), int))
s.set_value("fullscreen", 1)
check("a truthy value becomes a real bool",
      s.get_value("fullscreen") is True, s.get_value("fullscreen"))

section("WRITING ONLY WHEN SOMETHING CHANGED")
# value_changed fires continuously while a slider is dragged. Writing the file
# on every step of a drag across the whole bar would be a hundred writes.
s = Settings()
s.saves = 0
s.set_value("volume_sfx", 0.5)
check("a real change saves once", s.saves == 1, s.saves)
s.set_value("volume_sfx", 0.5)
check("setting the same value again saves nothing", s.saves == 1, s.saves)
s.set_value("volume_sfx", 0.51)
check("moving on saves again", s.saves == 2, s.saves)

section("LOADING")
s = Settings()
s.load_settings()
check("a missing file is the normal case, not a failure", s.errors == [], s.errors)
check("and leaves a complete file behind", s.saves == 1, s.saves)
check("loading emits nothing at anything listening", s.emitted == [], s.emitted)

# A file written by an older build has some keys and not others.
s = Settings()
s.load_settings({"volume_music": 0.2, "gone_from_defaults": 7})
check("keys the file has are honoured", s.get_value("volume_music") == 0.2)
check("keys it lacks fall back to defaults",
      s.get_value("volume_sfx") == DEFAULTS["volume_sfx"])
check("a key the game no longer has is ignored, not an error",
      "gone_from_defaults" not in s.values and s.errors == [], s.errors)

s = Settings()
s.load_settings({"fullscreen": "true", "window_width": "1920"})
check("strings out of a hand-edited file are coerced",
      s.get_value("fullscreen") is True and s.get_value("window_width") == 1920,
      [s.get_value("fullscreen"), s.get_value("window_width")])

section("RESET")
s = Settings()
for k, v in [("volume_master", 0.1), ("fullscreen", True),
             ("damage_numbers", False), ("window_width", 2560)]:
    s.set_value(k, v)
check("four settings changed", len(s.values) == 4, s.values)
s.reset()
check("reset returns every one of the nine to its default",
      all(s.get_value(k) == DEFAULTS[k] for k in DEFAULTS), s.values)
check("including ones that were never touched",
      s.get_value("vsync") == DEFAULTS["vsync"])

section("THE WINDOW SIZE PICKER")
# Every offered size is at or above the project's own 1280x720 viewport, so
# nothing here ever scales the UI DOWN - which is the direction that makes
# text unreadable, and the open #148.
check("every offered size is 16:9",
      all(abs(w / h - 16 / 9) < 0.001 for w, h in WINDOW_SIZES), WINDOW_SIZES)
check("and none is smaller than the project viewport",
      all(w >= 1280 and h >= 720 for w, h in WINDOW_SIZES), WINDOW_SIZES)
check("the default is the viewport exactly",
      (DEFAULTS["window_width"], DEFAULTS["window_height"]) == WINDOW_SIZES[0])

# refresh() sets `selected` to find()'s answer, which is -1 for a size the
# list does not have. That selects nothing rather than snapping to the first
# entry - the player may have dragged the window corner, and reporting
# 1280x720 at that point would be the panel contradicting what they can see.
check("a size the list has selects that row",
      WINDOW_SIZES.index((2560, 1440)) == 1)
# 1920x1080 is NOT offered, deliberately - it is 1.5x the viewport and the
# integer scaler would draw it at 1x inside a black frame. This check used to
# assert it WAS at index 2, against a stale copy of the list. See sim_settings.
check("and 1080p is not in the list at all", (1920, 1080) not in WINDOW_SIZES)
check("a hand-dragged size selects nothing at all",
      ((1366, 768) in WINDOW_SIZES) is False)

section("V-SYNC IS A MODE, AND THE OLD FILE STORED A BOOL")
# Every options.cfg written before this change has vsync=true or vsync=false.
# ConfigFile hands those back as bools; they have to keep meaning what they
# meant, and anything the apply step does not know has to become the default
# rather than a value the match statement falls through.
s = Settings()
s.load_settings({"vsync": True})
check("an old vsync=true loads as 'on'", s.get_value("vsync") == "on", s.get_value("vsync"))
s.load_settings({"vsync": False})
check("and vsync=false as 'off'", s.get_value("vsync") == "off", s.get_value("vsync"))
s.load_settings({"vsync": "false"})
check("the string 'false' too - a hand-edited file", s.get_value("vsync") == "off")
s.set_value("vsync", "Adaptive")
check("case is not the player's problem", s.get_value("vsync") == "adaptive")
s.set_value("vsync", "banana")
check("nonsense becomes the default, loudly nothing", s.get_value("vsync") == "on")
check("every mode the picker offers survives a round trip",
      all(Settings().set_value("vsync", m) is None and normalise_ok(m) for m in VSYNC_MODES))

section("THE CAP IS THE PLAYER'S, OR NOTHING")
# The whole regression that reintroduced the tearing band was code choosing a
# cap by itself - ceil(refresh) - 1. The schema's default is the fix: 0, and
# nothing normalises it to anything else.
check("the default cap is no cap", DEFAULTS["frame_cap"] == 0)
check("and Unlimited is the first thing the picker offers", FRAME_CAPS[0] == 0)
s = Settings()
s.set_value("frame_cap", -5)
check("a negative cap is 0", s.get_value("frame_cap") == 0)
s.set_value("frame_cap", "144")
check("a string cap is a whole number", s.get_value("frame_cap") == 144 and isinstance(s.get_value("frame_cap"), int))
check("no offered cap sits within 2 fps of 60 Hz except 60 itself",
      all(c == 60 or abs(c - 60) > 2 for c in FRAME_CAPS if c), FRAME_CAPS)

section("WHAT APPLYING TOUCHES")
# _apply() dispatches on the key. Setting the window mode flickers the window,
# so doing it because the SFX slider moved would read as a bug.
s = Settings()
s.applied.clear()
s.set_value("volume_sfx", 0.3)
check("moving one slider applies one thing", s.applied == ["volume_sfx"], s.applied)
s.applied.clear()
s.set_value("fullscreen", True)
check("and toggling fullscreen applies one thing",
      s.applied == ["fullscreen"], s.applied)

print("\n" + "=" * 60)
print("  %d passed, %d failed" % (passed, failed))
print("=" * 60)
raise SystemExit(1 if failed else 0)
