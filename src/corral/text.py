"""Text hygiene for anything that came out of a cell or a model.

Tool output is attacker-influenced. Before it reaches a terminal it loses ANSI
escape sequences and control characters, so a file can't repaint the screen,
fake an approval prompt or set the window title.
"""

from __future__ import annotations

import re

# CSI, OSC (terminated by BEL or ST), and other two-byte ESC sequences.
_ANSI = re.compile(
    r"""
    \x1b\[[0-?]*[ -/]*[@-~]        # CSI ... final byte
    | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?   # OSC ... BEL or ST
    | \x1b[PX^_][^\x1b]*(?:\x1b\\)?        # DCS / SOS / PM / APC
    | \x1b[@-Z\\-_]                # other Fe escapes
    | \x9b[0-?]*[ -/]*[@-~]        # 8-bit CSI
    """,
    re.VERBOSE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize(s: str) -> str:
    s = _ANSI.sub("", s)
    s = s.replace("\r\n", "\n")
    # Progress bars redraw a line with bare \r; keep what the line ended as.
    if "\r" in s:
        s = "\n".join(line.rsplit("\r", 1)[-1] for line in s.split("\n"))
    s = _CONTROL.sub("", s)
    # Bidi overrides can make reviewed text read differently than it runs.
    return s.translate({c: None for c in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069)})


def truncate_middle(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(s) - head - tail
    return f"{s[:head]}\n… [{omitted} characters omitted] …\n{s[-tail:]}"


def one_line(s: str, limit: int = 120) -> str:
    s = " ".join(sanitize(s).split())
    return s if len(s) <= limit else s[: limit - 1] + "…"
