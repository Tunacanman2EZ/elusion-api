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
    "vsync": True,
    "window_width": 1280,
    "window_height": 720,
    "damage_numbers": True,
}

WINDOW_SIZES = [(1280, 720), (1600, 900), (1920, 1080), (2560, 1440)]


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
        return value

    def get_value(self, key):
        if key not in DEFAULTS:
            self.errors.append(key)
            return None
        return self.values.get(key, DEFAULTS[key])

    def set_value(self, key, value):
        if key not in DEFAULTS:
            self.errors.append(key)
            return
        typed = self.coerce(value, DEFAULTS[key])
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
