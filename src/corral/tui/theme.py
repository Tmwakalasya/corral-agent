"""Colors that mean where something happens.

- cell     teal           inside the sandbox: the agent, its tools, its output
- gate     lantern amber  anything crossing the fence: network approvals, review, apply
- you      periwinkle     your messages and your machine
- blocked  red            denied, failed, refused

"dusk" is the default; ctrl+t switches to "daylight".
"""

from textual.theme import Theme

DUSK = Theme(
    name="dusk",
    primary="#5cc8ac",     # cell
    secondary="#9da8ff",   # you
    accent="#f2b544",      # gate
    warning="#f2b544",
    error="#ff6a4d",
    success="#9bd17a",
    foreground="#e9e4d4",  # hay
    background="#0f1117",
    surface="#151821",
    panel="#1c2030",
    boost="#222739",
    dark=True,
    variables={
        "cell": "#5cc8ac",
        "gate": "#f2b544",
        "you": "#9da8ff",
        "blocked": "#ff6a4d",
        "rule": "#262b3b",
        "faint": "#5b6175",
        "muted": "#9a9fb2",
        "border": "#2a3044",
        "border-blurred": "#232838",
        "footer-key-foreground": "#f2b544",
        "input-selection-background": "#5cc8ac40",
        "scrollbar": "#262b3b",
        "scrollbar-hover": "#343a50",
        "scrollbar-active": "#5cc8ac",
        "scrollbar-background": "#0f1117",
        "scrollbar-corner-color": "#0f1117",
    },
)

DAYLIGHT = Theme(
    name="daylight",
    primary="#13806a",
    secondary="#4752c4",
    accent="#b2730c",
    warning="#b2730c",
    error="#c43c1d",
    success="#4d7f25",
    foreground="#1b1e2b",
    background="#f5f1e6",
    surface="#ece6d6",
    panel="#e2dbc8",
    boost="#d9d1bb",
    dark=False,
    variables={
        "cell": "#13806a",
        "gate": "#b2730c",
        "you": "#4752c4",
        "blocked": "#c43c1d",
        "rule": "#d6cdb6",
        "faint": "#8a8573",
        "muted": "#5f5b4e",
        "border": "#cfc6ae",
        "border-blurred": "#d6cdb6",
        "footer-key-foreground": "#b2730c",
        "input-selection-background": "#13806a33",
        "scrollbar": "#d6cdb6",
        "scrollbar-hover": "#c2b89c",
        "scrollbar-active": "#13806a",
        "scrollbar-background": "#f5f1e6",
        "scrollbar-corner-color": "#f5f1e6",
    },
)

THEMES = (DUSK, DAYLIGHT)


_cache: dict[str, dict[str, str]] = {}


def palette(app) -> dict[str, str]:
    """Zone colors for Rich renderables, which can't read CSS variables."""
    name = app.theme
    if name not in _cache:
        v = app.get_css_variables()
        _cache[name] = {k: v[k] for k in ("cell", "gate", "you", "blocked", "rule", "faint", "muted",
                                          "foreground", "success", "background", "surface", "boost")}
    return _cache[name]
