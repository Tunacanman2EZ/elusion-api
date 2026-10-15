"""
codemap.py - an index of app.py, so 10,000 lines stay navigable.

    venv\\Scripts\\python.exe codemap.py

Writes CODEMAP.md beside app.py: every section banner, every route (with the
rank it demands), and every top-level function, each with the line number to
jump to. Re-run it whenever the file grows - it is generated, never edited.

FEATURE PACKAGES (game 0.47.0): a folder beside app.py with a routes.py - the
BE105 layout, playerstats/ the first - is listed too, its routes found by
their add_url_rule() lines and its layers (routes, services, data) named.

WHY: app.py is past ten thousand lines and grepping for "def login" finds the
docstring, the call site and the definition. An index answers the two questions
actually being asked - "where is X" and "what does this server even expose" -
without reading any of it.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "app.py")
OUTPUT = os.path.join(HERE, "CODEMAP.md")

ROUTE = re.compile(r'^@app\.(get|post|put|delete|patch)\("([^"]+)"\)')
DECORATOR = re.compile(r"^@(require_auth|require_owner|require_role\([^)]*\))")
FUNCTION = re.compile(r"^def ([A-Za-z_][A-Za-z0-9_]*)\(")
BANNER = re.compile(r"^# =+\s*$")
CONSTANT = re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*=")
# One add_url_rule call, which may run over several lines (events/ in 0.48.0
# wraps them), up to its methods - and never into the next call's.
PACKAGE_ROUTE = re.compile(r'add_url_rule\(\s*"([^"]+)"(?:(?!add_url_rule).)*?methods=\[\s*"([A-Z]+)"', re.S)
PACKAGE_LAYERS = ("routes.py", "services.py", "data.py")


def feature_packages():
    """[(folder, [(line, method, path)], [layer files present])] for every
    folder beside app.py that app.py imports and that holds a routes.py - a
    practice folder or a stray copy lying beside the code is not the server."""
    with open(SOURCE, encoding="utf-8") as handle:
        imported = set(re.findall(r"^import ([A-Za-z_][A-Za-z0-9_]*)", handle.read(), re.M))
    found = []
    for name in sorted(os.listdir(HERE)):
        if name not in imported:
            continue
        folder = os.path.join(HERE, name)
        routes_py = os.path.join(folder, "routes.py")
        if not os.path.isdir(folder) or not os.path.exists(routes_py):
            continue
        with open(routes_py, encoding="utf-8") as handle:
            text = handle.read()
        rows = [(text.count("\n", 0, match.start()) + 1, match.group(2), match.group(1))
                for match in PACKAGE_ROUTE.finditer(text)]
        layers = [layer for layer in PACKAGE_LAYERS if os.path.exists(os.path.join(folder, layer))]
        found.append((name, rows, layers))
    return found


def main():
    if not os.path.exists(SOURCE):
        print("no app.py beside this script")
        return 1

    with open(SOURCE, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    sections = []
    routes = []
    functions = []
    constants = []

    pending_guards = []
    pending_route = None

    for index, line in enumerate(lines, start=1):
        # ---- section banners: # ====  /  # TITLE  /  # ==== ----------------
        if BANNER.match(line) and index < len(lines):
            title = lines[index]           # the line after this one
            if title.startswith("# ") and not BANNER.match(title):
                if index + 1 <= len(lines) and BANNER.match(lines[index + 1]):
                    sections.append((index, title[2:].strip()))

        match = ROUTE.match(line)
        if match:
            pending_route = (index, match.group(1).upper(), match.group(2))
            pending_guards = []
            continue

        if pending_route is not None:
            guard = DECORATOR.match(line)
            if guard:
                pending_guards.append(guard.group(1))
                continue
            handler = FUNCTION.match(line)
            if handler:
                line_no, method, path = pending_route
                routes.append({
                    "line": line_no, "method": method, "path": path,
                    "handler": handler.group(1), "guards": list(pending_guards),
                })
                pending_route = None
                pending_guards = []
                continue

        handler = FUNCTION.match(line)
        if handler:
            functions.append((index, handler.group(1)))

        constant = CONSTANT.match(line)
        if constant:
            constants.append((index, constant.group(1)))

    route_handlers = {r["handler"] for r in routes}

    def guard_label(guards):
        for guard in guards:
            if guard == "require_owner":
                return "owner"
            if guard.startswith("require_role"):
                inner = re.search(r'"([^"]+)"', guard)
                return (inner.group(1) if inner else "staff") + "+"
        return "logged in" if "require_auth" in guards else "public"

    out = []
    out.append("# app.py - code map\n")
    out.append("Generated by `codemap.py`. Do not edit; re-run it instead.\n")
    out.append("`%d lines - %d routes - %d functions`\n"
               % (len(lines), len(routes), len(functions)))

    out.append("\n## Routes\n")
    out.append("Who may call it, and where it lives.\n")
    out.append("\n| Line | Method | Path | Needs | Handler |")
    out.append("|-----:|--------|------|-------|---------|")
    for r in sorted(routes, key=lambda r: r["path"]):
        out.append("| %d | %s | `%s` | %s | `%s()` |"
                   % (r["line"], r["method"], r["path"], guard_label(r["guards"]), r["handler"]))

    packages = feature_packages()
    if packages:
        out.append("\n## Feature packages\n")
        out.append("Folders beside app.py, each in layers (BE105): routes, services, data.\n")
        out.append("\n| Package | Layers | Line | Method | Path |")
        out.append("|---------|--------|-----:|--------|------|")
        for name, rows, layers in packages:
            for line_no, method, path in rows:
                out.append("| `%s/` | %s | %d | %s | `%s` |" % (name, ", ".join(layers), line_no, method, path))

    out.append("\n## Sections\n")
    for line_no, title in sections:
        out.append("- **%d** - %s" % (line_no, title))

    out.append("\n## Functions (not routes)\n")
    plain = [(n, name) for n, name in functions if name not in route_handlers]
    out.append("\n| Line | Function |")
    out.append("|-----:|----------|")
    for line_no, name in plain:
        out.append("| %d | `%s()` |" % (line_no, name))

    out.append("\n## Tunable constants\n")
    out.append("\n| Line | Name |")
    out.append("|-----:|------|")
    for line_no, name in constants:
        out.append("| %d | `%s` |" % (line_no, name))

    with open(OUTPUT, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")

    print("wrote %s" % OUTPUT)
    print("  %d lines, %d routes, %d sections, %d functions, %d constants"
          % (len(lines), len(routes), len(sections), len(plain), len(constants)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
