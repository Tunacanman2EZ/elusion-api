"""
A transcription of settings.gd, so its rules can be exercised without Godot.

    Settings.get_value / set_value / reset / _coerce / load_settings
    optionsscreen.refresh()'s window-size lookup

ConfigFile is stood in for by a dict, because what is being checked is the
schema discipline - unknown keys refused, types pinned by the default, a
missing file meaning defaults - not Godot's INI writer.
"""

DEFAULTS = {
    "volume_master": 1.0,
    "volume_music": 0.7,
    "volume_sfx": 1.0,
    "fullscreen": False,
    "vsync": "on",        # a MODE now: off / on / adaptive / fast
    "frame_cap": 0,       # 0 = no cap
    "window_width": 1280,
    "window_height": 720,
    "render_resolution": "screen",  # screen / low (1280x720, scaled up)
    "lighting": "full",             # full / simple (lights off, darkness lifted)
    "background_fps_limit": True,
    "damage_numbers": True,
}

# THIS LIST DRIFTED ONCE. It carried four sizes while settings.gd carried
# three, and test_settings.py stayed green against a picker the game did not
# have - "index((1920, 1080)) == 2" was checking a row that no longer
# existed. A transcription is only worth having while it matches; the
# comment at the top of this file is the contract and this line is where it
# was broken.
WINDOW_SIZES = [(1280, 720), (2560, 1440), (3840, 2160)]

VSYNC_MODES = ["off", "on", "adaptive", "fast"]
FRAME_CAPS = [0, 30, 60, 120, 144, 165, 240, 360]

RENDER_RESOLUTIONS = ["screen", "low"]
LIGHTING_MODES = ["full", "simple"]
BACKGROUND_FPS = 15


def normalise_vsync(value):
    """settings.gd normalise_vsync(): old bools and unknown strings."""
    if isinstance(value, bool):
        return "on" if value else "off"
    s = str(value).lower()
    if s == "true":
        return "on"
    if s == "false":
        return "off"
    return s if s in VSYNC_MODES else "on"


def normalise_frame_cap(value):
    return max(0, int(value))


def normalise_choice(value, allowed, fallback):
    """settings.gd normalise_choice(): case-folded, unknown -> default."""
    s = str(value).lower()
    return s if s in allowed else fallback


def fps_cap_for(frame_cap, focused, background_limit):
    """settings.gd fps_cap_for(): the one place max_fps is worked out."""
    cap = max(0, int(frame_cap))
    if focused or not background_limit:
        return cap
    return BACKGROUND_FPS if cap == 0 else min(cap, BACKGROUND_FPS)


class Settings:
    def __init__(self):
        self.values = {}
        self.errors = []
        self.applied = []
        self.saves = 0
        self.emitted = []
        self._loading = False

    # --- _coerce ---
    @staticmethod
    def coerce(value, like):
        if isinstance(like, bool):
            return bool(value)
        if isinstance(like, int):
            return int(value)
        if isinstance(like, float):
            return float(value)
        if isinstance(like, str):
            return str(value)
        return value

    @staticmethod
    def normalise(key, typed):
        if key == "vsync":
            return normalise_vsync(typed)
        if key == "frame_cap":
            return normalise_frame_cap(typed)
        if key == "render_resolution":
            return normalise_choice(typed, RENDER_RESOLUTIONS, "screen")
        if key == "lighting":
            return normalise_choice(typed, LIGHTING_MODES, "full")
        return typed

    def get_value(self, key):
        if key not in DEFAULTS:
            self.errors.append(key)
            return None
        return self.values.get(key, DEFAULTS[key])

    def set_value(self, key, value):
        if key not in DEFAULTS:
            self.errors.append(key)
            return
        typed = self.normalise(key, self.coerce(value, DEFAULTS[key]))
        if self.values.get(key, None) == typed and not self._loading:
            return
        self.values[key] = typed
        self.applied.append(key)
        if not self._loading:
            self.saves += 1
            self.emitted.append((key, typed))

    def reset(self):
        for key in DEFAULTS:
            self.set_value(key, DEFAULTS[key])

    def load_settings(self, stored=None):
        self._loading = True
        stored = stored or {}
        for key in DEFAULTS:
            self.set_value(key, stored.get(key, DEFAULTS[key]))
        self._loading = False
        self.saves += 1
