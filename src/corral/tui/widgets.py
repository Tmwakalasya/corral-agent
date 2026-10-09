"""Widgets for the main screen. Everything from the gateway is rendered as
plain text or Markdown built from sanitized strings, never as markup, so a file
the agent reads can't style or rewrite the screen.
"""

from __future__ import annotations

import time
from collections import deque

from rich.console import Group
from rich.table import Table
from rich.text import Text
from textual import events
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Markdown, Static, TextArea

from ..text import sanitize
from .theme import palette

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def on_theme_change(widget, callback) -> None:
    """Rich renderables bake colors in, so re-render them when the theme changes."""
    widget.app.theme_changed_signal.subscribe(widget, lambda _theme: callback())


def markdown_parser():
    # CommonMark plus tables and strikethrough. No linkify: "invoice.py" is a
    # file name, not a link to Paraguay.
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").enable("table").enable("strikethrough")


def human_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ---------------------------------------------------------------- top bar


class TopBar(Static):
    def __init__(self):
        super().__init__()
        self.repo = ""
        self.model = ""
        self.state = "connecting"
        self.cell = ""
        self.hosts = 0
        self.changes = 0
        self.cost: float | None = None
        self.budget = 0.0
        self.priced = True
        self.ctx = 0.0
        self.busy = False
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)
        on_theme_change(self, self.refresh_bar)

    def _tick(self) -> None:
        if self.busy or self.state in ("starting", "connecting"):
            self._frame += 1
            self.refresh_bar()

    def set(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)
        self.refresh_bar()

    def refresh_bar(self) -> None:
        c = palette(self.app)
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append("╫ ", style=c["gate"])
        left.append("corral", style="bold")
        if self.repo:
            left.append("  /  ", style=c["faint"])
            left.append(self.repo, style="bold")
        if self.model:
            left.append("   ")
            left.append(self.model, style=c["muted"])

        right = Text(no_wrap=True, justify="right")
        state_style = {
            "ready": c["cell"], "running": c["cell"], "starting": c["gate"], "connecting": c["gate"],
            "error": c["blocked"], "closed": c["faint"],
        }.get(self.state, c["faint"])
        glyph = SPINNER[self._frame % len(SPINNER)] if (self.busy or self.state in ("starting", "connecting")) else "●"
        label = "working" if self.busy else self.state
        right.append(f"{glyph} {label}", style=state_style)

        def seg(name: str, value: str, style: str | None = None) -> None:
            right.append("   ")
            right.append(name + " ", style=c["faint"])
            right.append(value, style=style or c["foreground"])

        if self.cell:
            seg("cell", self.cell)
        seg("net", f"{self.hosts} host{'s' if self.hosts != 1 else ''}" if self.hosts else "closed",
            c["gate"] if self.hosts else c["cell"])
        seg("changes", str(self.changes), c["gate"] if self.changes else None)
        if self.cost is not None:
            spent = f"${self.cost:.2f}" if self.priced else "unpriced"
            seg("spend", f"{spent} / ${self.budget:.2f}",
                c["blocked"] if self.priced and self.cost >= self.budget * 0.9 else None)
        seg("ctx", f"{self.ctx:.0f}%")

        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(no_wrap=True, justify="right")
        grid.add_row(left, right)
        self.update(grid)


# ---------------------------------------------------------------- transcript items


class UserMsg(Static):
    def __init__(self, text: str):
        super().__init__(Text(sanitize(text)))


class AssistantMsg(Markdown):
    def __init__(self):
        super().__init__("", open_links=False, parser_factory=markdown_parser)
        self._stream = None

    async def feed(self, text: str) -> None:
        # Not `append`: MarkdownStream calls Markdown.append itself.
        if self._stream is None:
            self._stream = Markdown.get_stream(self)
        await self._stream.write(text)

    async def finish(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
            self._stream = None


class Note(Static):
    """Reasoning or progress notes. Short notes stay visible; long reasoning
    (open models stream all of it) shows its tail while it streams, then folds
    to one line you can click open."""

    SHORT = 320

    def __init__(self):
        super().__init__("")
        self.buf = ""
        self.done = False
        self.expanded = False
        self.started = time.monotonic()
        self.seconds = 0.0

    def on_mount(self) -> None:
        on_theme_change(self, self.paint)

    def append(self, text: str) -> None:
        self.buf += sanitize(text)
        self.paint()

    def finish(self) -> None:
        if not self.done:
            self.done = True
            self.seconds = time.monotonic() - self.started
            self.paint()

    def paint(self) -> None:
        c = palette(self.app)
        text = self.buf.strip()
        if len(text) <= self.SHORT or self.expanded:
            self.update(Text(text))
        elif not self.done:
            tail = text[-self.SHORT:]
            self.update(Text("… " + tail[tail.find(" ") + 1:]))
        else:
            t = Text()
            t.append("▸ ", style=c["faint"])
            t.append(f"thought for {self.seconds:.0f}s · {len(text.split())} words", style=c["muted"])
            t.append("  click to read", style=c["faint"])
            self.update(t)

    def on_click(self) -> None:
        if self.done and len(self.buf.strip()) > self.SHORT:
            self.expanded = not self.expanded
            self.paint()


class LogLine(Static):
    def __init__(self, kind: str, text: str, seconds: float | None = None):
        super().__init__()
        self.kind, self.text_, self.seconds = kind, text, seconds

    def on_mount(self) -> None:
        self.paint()
        on_theme_change(self, self.paint)

    def paint(self) -> None:
        c = palette(self.app)
        style = c["blocked"] if self.kind in ("blocked", "refused") else c["faint"]
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append(f"{self.kind:<10}", style=style)
        left.append(sanitize(self.text_), style=c["muted"])
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(no_wrap=True, justify="right")
        grid.add_row(left, Text(f"{self.seconds:.2f}s" if self.seconds is not None else "", style=c["faint"]))
        self.update(grid)


class NoticeLine(Static):
    def __init__(self, level: str, text: str):
        super().__init__()
        self.level, self.text_ = level, text

    def on_mount(self) -> None:
        self.paint()
        on_theme_change(self, self.paint)

    def paint(self) -> None:
        c = palette(self.app)
        color = {"error": c["blocked"], "warn": c["gate"]}.get(self.level, c["cell"])
        t = Text()
        t.append("▍", style=color)
        t.append(sanitize(self.text_), style=color if self.level == "error" else None)
        self.update(t)


class ToolBlock(Vertical):
    """One tool call: a one-line header, and output that folds away when done."""

    MAX_LINES = 2000

    def __init__(self, call_id: str, name: str):
        super().__init__()
        self.call_id = call_id
        self.tool = name
        self.title = ""
        self.state = "preparing"
        self.summary = ""
        self.detail = ""
        self.lines: deque[str] = deque(maxlen=self.MAX_LINES)
        self._partial = ""
        self.started = time.monotonic()
        self._frame = 0
        self.head = Static(classes="tool-head")
        self.body = Static(classes="tool-body")
        self._tail: int | None = 8

    def compose(self):
        yield self.head
        yield self.body

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick)
        self._render_head()
        on_theme_change(self, self.paint)

    def paint(self) -> None:
        self._render_head()
        self._render_body(tail=self._tail)

    def _tick(self) -> None:
        if self.state in ("preparing", "running"):
            self._frame += 1
            self._render_head()
        else:
            self._timer.stop()

    def start(self, title: str) -> None:
        self.title = sanitize(title)
        self.state = "running"
        self.started = time.monotonic()
        self._render_head()

    def output(self, chunk: str) -> None:
        text = self._partial + chunk
        parts = text.split("\n")
        self._partial = parts.pop()
        self.lines.extend(parts)
        if self.state == "running":
            self.add_class("-open")
            self._render_body(tail=8)

    def end(self, ok: bool, summary: str, detail: str) -> None:
        if self._partial:
            self.lines.append(self._partial)
            self._partial = ""
        self.state = "ok" if ok else "failed"
        self.summary = sanitize(summary)
        self.detail = sanitize(detail or "")
        self._render_head()
        if self.tool == "edit_file" and self.detail:
            self.add_class("-open")
            self._render_body(tail=None)
        elif not ok and self.lines:
            self.add_class("-open")
            self._render_body(tail=8)
        else:
            self.remove_class("-open")
            self._render_body(tail=None)

    def _render_head(self) -> None:
        c = palette(self.app)
        if self.state in ("preparing", "running"):
            glyph, gstyle = SPINNER[self._frame % len(SPINNER)], c["cell"]
        elif self.state == "ok":
            glyph, gstyle = "✓", c["cell"]
        else:
            glyph, gstyle = "✗", c["blocked"]
        name_style = f"bold {c['gate']}" if self.tool == "request_network" else "bold"
        left = Text(no_wrap=True, overflow="ellipsis")
        left.append(f"{glyph} ", style=gstyle)
        left.append(self.tool, style=name_style)
        left.append("  ")
        left.append(self.title or ("preparing…" if self.state == "preparing" else ""), style=c["muted"])
        if self.state in ("preparing", "running"):
            summary = f"{time.monotonic() - self.started:.0f}s" if self.state == "running" else ""
            sstyle = c["faint"]
        else:
            summary = self.summary
            sstyle = c["blocked"] if self.state == "failed" else c["faint"]
        more = "  ▾" if (self.lines or self.detail) and not self.has_class("-open") else ""
        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(no_wrap=True, justify="right")
        grid.add_row(left, Text(summary + more, style=sstyle))
        self.head.update(grid)

    def _render_body(self, tail: int | None) -> None:
        self._tail = tail
        c = palette(self.app)
        if self.tool == "edit_file" and self.detail:
            t = Text()
            for i, line in enumerate(self.detail.splitlines()[:60]):
                style = c["blocked"] if line.startswith("- ") else c["success"] if line.startswith("+ ") else None
                t.append(("\n" if i else "") + line, style=style)
            self.body.update(t)
            return
        lines = list(self.lines)
        if self.detail and not lines:
            lines = self.detail.splitlines()
        if tail is not None and not self.has_class("-expanded"):
            hidden = max(0, len(lines) - tail)
            lines = lines[-tail:]
        else:
            hidden = max(0, len(lines) - 400)
            lines = lines[-400:]
        t = Text()
        if hidden:
            t.append(f"… {hidden} earlier lines\n", style=c["faint"])
        t.append("\n".join(lines))
        self.body.update(t)

    def on_click(self, event: events.Click) -> None:
        if not (self.lines or self.detail):
            return
        if self.has_class("-open") and self.has_class("-expanded"):
            self.remove_class("-open", "-expanded")
        else:
            self.add_class("-open", "-expanded")
        self._render_body(tail=None)
        self._render_head()


# ---------------------------------------------------------------- sidebar


class Sidebar(VerticalScroll):
    class OpenReview(Message):
        pass

    def __init__(self):
        super().__init__()
        self.files: list[dict] = []
        self.hosts: list[str] = []
        self.net: deque[dict] = deque(maxlen=60)
        self.stats: dict = {}
        self.cell = ""
        self.cell_kind = ""
        self.changes_w = Static(classes="section", id="changes-section")
        self.net_w = Static(classes="section")
        self.session_w = Static(classes="section")

    def compose(self):
        yield self.changes_w
        yield self.net_w
        yield self.session_w

    def on_mount(self) -> None:
        self.redraw()
        on_theme_change(self, self.redraw)

    def _label(self, text: str, count: str = "") -> Table:
        c = palette(self.app)
        g = Table.grid(expand=True)
        g.add_column(ratio=1)
        g.add_column(justify="right")
        g.add_row(Text(text.upper(), style=f"bold {c['faint']}"), Text(count, style=c["faint"]))
        return g

    def redraw(self) -> None:
        c = palette(self.app)
        # Changes
        rows = Table.grid(expand=True, padding=(0, 1))
        rows.add_column(width=1, no_wrap=True)
        rows.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        rows.add_column(no_wrap=True, justify="right")
        pending = [f for f in self.files if f["state"] != "applied"]
        for f in self.files[:30]:
            applied = f["state"] == "applied"
            status_style = {"A": c["cell"], "M": c["gate"], "D": c["blocked"]}.get(f["status"], c["muted"])
            stat = "bin" if f["binary"] else f"+{f['adds']} −{f['dels']}"
            path = Text(sanitize(f["path"]), style=c["faint"] if applied else None)
            if f["risks"] and not applied:
                path.append(" !", style=c["gate"])
            rows.add_row(Text(f["status"], style=c["faint"] if applied else status_style), path,
                         Text("applied" if applied else stat, style=c["faint"]))
        hint = Text("ctrl+r to review", style=c["faint"]) if pending else \
            Text("nothing to review" if not self.files else "all applied", style=c["faint"])
        self.changes_w.update(Group(self._label("changes", str(len(pending)) if pending else ""), rows, hint))

        # Network
        net = Table.grid(expand=True, padding=(0, 1))
        net.add_column(width=7, no_wrap=True)
        net.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        net.add_column(no_wrap=True, justify="right")
        if not self.hosts:
            net.add_row(Text("closed", style=c["cell"]), Text("no hosts allowed", style=c["muted"]), Text(""))
        for h in self.hosts:
            used = sum(e["up"] + e["down"] for e in self.net if e["host"] == h and e["decision"] == "allow")
            net.add_row(Text("open", style=c["gate"]), Text(h), Text(human_bytes(used) if used else "", style=c["faint"]))
        blocked: dict[str, int] = {}
        for e in self.net:
            if e["decision"] == "deny":
                blocked[e["host"]] = blocked.get(e["host"], 0) + 1
        for h, n in list(blocked.items())[-6:]:
            net.add_row(Text("blocked", style=c["blocked"]), Text(h or "?"), Text(f"×{n}", style=c["faint"]))
        self.net_w.update(Group(self._label("network", ""), net))

        # Session
        s = self.stats
        info = Table.grid(expand=True, padding=(0, 1))
        info.add_column(width=7, style=c["faint"])
        info.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        if s:
            info.add_row("model", s.get("model", ""))
            info.add_row("tokens", f"{human_tokens(s.get('input_tokens', 0))} in · "
                                   f"{human_tokens(s.get('output_tokens', 0))} out")
            if s.get("cache_read"):
                info.add_row("cached", human_tokens(s["cache_read"]))
            spent = f"${s.get('cost', 0):.2f}" if s.get("priced", True) else "unpriced"
            info.add_row("spend", f"{spent} of ${s.get('budget', 0):.2f}")
        if self.cell:
            info.add_row("cell", f"{self.cell} · {self.cell_kind}" if self.cell_kind else self.cell)
        self.session_w.update(Group(self._label("session"), info))

    def on_click(self, event: events.Click) -> None:
        if event.widget is self.changes_w:
            self.post_message(self.OpenReview())


# ---------------------------------------------------------------- composer


class Composer(TextArea):
    class Submitted(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    class Edited(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    class Nav(Message):
        """Up/down/tab while the command list is open."""

        def __init__(self, key: str):
            super().__init__()
            self.key = key

    def __init__(self):
        super().__init__(soft_wrap=True, show_line_numbers=False, tab_behavior="focus", compact=True,
                         highlight_cursor_line=False,
                         placeholder="Ask for a change. / for commands, ctrl+j for a new line.")
        self.sent: list[str] = []
        self._hist_pos: int | None = None
        self.suggesting = False

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if self.suggesting and key in ("up", "down", "tab"):
            event.stop()
            event.prevent_default()
            self.post_message(self.Nav(key))
            return
        if key == "enter":
            event.stop()
            event.prevent_default()
            if self.suggesting:
                self.post_message(self.Nav("enter"))
                return
            text = self.text
            if text.strip():
                self.sent.append(text)
                self._hist_pos = None
                self.clear()
                self.post_message(self.Submitted(text))
            return
        if key in ("ctrl+j", "shift+enter", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "up" and self.cursor_location[0] == 0 and self.sent:
            event.stop()
            event.prevent_default()
            self._hist_pos = len(self.sent) - 1 if self._hist_pos is None else max(0, self._hist_pos - 1)
            self.load_text(self.sent[self._hist_pos])
            self.move_cursor(self.document.end)
            return
        if key == "down" and self._hist_pos is not None and self.cursor_location[0] == self.document.line_count - 1:
            event.stop()
            event.prevent_default()
            self._hist_pos += 1
            if self._hist_pos >= len(self.sent):
                self._hist_pos = None
                self.clear()
            else:
                self.load_text(self.sent[self._hist_pos])
                self.move_cursor(self.document.end)
            return
        await super()._on_key(event)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.post_message(self.Edited(self.text))
