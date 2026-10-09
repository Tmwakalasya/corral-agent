"""Review screen and modal dialogs."""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, ListItem, ListView, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ..backend.policy import RISK_HELP
from ..client import GatewayError
from ..text import sanitize
from .theme import palette
from .widgets import on_theme_change

# ---------------------------------------------------------------- review


class FileItem(ListItem):
    def __init__(self, f: dict, selected: bool):
        super().__init__()
        self.f = f
        self.selected = selected
        self.label = Static()

    def compose(self) -> ComposeResult:
        yield self.label

    def on_mount(self) -> None:
        self.draw()
        on_theme_change(self, self.draw)

    @property
    def selectable(self) -> bool:
        return self.f["state"] == "pending" and not self.f["refused"]

    def draw(self) -> None:
        c = palette(self.app)
        f = self.f
        t = Text(no_wrap=True, overflow="ellipsis")
        if f["state"] == "applied":
            t.append("✓ ", style=c["cell"])
        elif not self.selectable:
            t.append("✗ ", style=c["blocked"])
        else:
            t.append("■ " if self.selected else "□ ", style=c["gate"] if self.selected else c["faint"])
        t.append(f"{f['status']} ", style={"A": c["cell"], "M": c["gate"], "D": c["blocked"]}.get(f["status"], ""))
        t.append(sanitize(f["path"]), style=c["faint"] if f["state"] == "applied" else None)
        t.append("  ")
        t.append("binary" if f["binary"] else f"+{f['adds']} −{f['dels']}", style=c["faint"])
        lines = [t]
        notes = []
        if f["state"] == "applied":
            notes.append(("applied", c["faint"]))
        elif f["state"] == "conflict":
            notes.append(("changed on your machine since the snapshot", c["blocked"]))
        if f["refused"]:
            notes.append((f"refused: {f['refused']}", c["blocked"]))
        for r in f["risks"]:
            notes.append((r, c["gate"]))
        if notes:
            n = Text("    ", no_wrap=True, overflow="ellipsis")
            for i, (label, style) in enumerate(notes):
                if i:
                    n.append(" · ", style=c["faint"])
                n.append(label, style=style)
            lines.append(n)
        self.label.update(Text("\n").join(lines))


class ReviewScreen(Screen):
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("space", "toggle", "Select", priority=True),
        Binding("a", "all", "All / none"),
        Binding("enter", "apply", "Apply", priority=True),
        Binding("r", "reload", "Refresh"),
    ]

    def __init__(self, client, sid: str, repo: str, focus_path: str | None = None):
        super().__init__()
        self.client = client
        self.sid = sid
        self.repo = repo
        self.files: list[dict] = []
        self.selected: set[str] = set()
        self.focus_path = focus_path
        self.head = Static(id="review-head")
        self.list = ListView(id="files")
        self.risk = Static(id="risk")
        self.diff = Static(id="diff")
        self._first_load = True

    def compose(self) -> ComposeResult:
        yield self.head
        with Horizontal(id="review-body"):
            yield self.list
            with VerticalScroll(id="diff-wrap"):
                yield self.risk
                yield self.diff
        yield Static(id="review-hints")

    def on_mount(self) -> None:
        self.paint_hints()
        on_theme_change(self, self.repaint)
        self.load()

    def repaint(self) -> None:
        self.paint_hints()
        self.draw_head()
        item = self._current()
        if item:
            self.show_diff(item.f)

    def paint_hints(self) -> None:
        c = palette(self.app)
        hints = Text()
        for key, label in (("space", "select"), ("a", "all / none"), ("enter", "apply selected"),
                           ("r", "refresh"), ("esc", "back")):
            hints.append(key, style=c["gate"])
            hints.append(f" {label}   ", style=c["faint"])
        self.query_one("#review-hints", Static).update(hints)

    @work(exclusive=True, group="load")
    async def load(self) -> None:
        try:
            r = await self.client.request("changes.get", sid=self.sid)
        except GatewayError as e:
            self.notify(str(e), severity="error")
            return
        self.files = r["files"]
        pending = {f["path"] for f in self.files if f["state"] == "pending" and not f["refused"]}
        if self._first_load:
            self.selected = set(pending)
            self._first_load = False
        else:
            self.selected &= pending
        await self.list.clear()
        for f in self.files:
            await self.list.append(FileItem(f, f["path"] in self.selected))
        self.draw_head()
        if not self.files:
            self.diff.update(Text("No changes in the cell yet.", style=palette(self.app)["muted"]))
            return
        index = 0
        if self.focus_path:
            index = next((i for i, f in enumerate(self.files) if f["path"] == self.focus_path), 0)
        self.list.index = index
        self.list.focus()
        self.show_diff(self.files[index])

    def draw_head(self) -> None:
        c = palette(self.app)
        adds = sum(f["adds"] for f in self.files)
        dels = sum(f["dels"] for f in self.files)
        t = Text()
        t.append("REVIEW", style=f"bold {c['gate']}")
        t.append(f"  {self.repo}\n", style=c["muted"])
        t.append(f"{len(self.files)} file{'s' if len(self.files) != 1 else ''} changed in the cell", style="bold")
        t.append(f"   +{adds} −{dels}", style=c["faint"])
        t.append("   ")
        n = len(self.selected)
        t.append(f"{n} selected to apply" if n else "nothing selected", style=c["gate"] if n else c["faint"])
        self.head.update(t)

    @on(ListView.Highlighted, "#files")
    def highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, FileItem):
            self.show_diff(event.item.f)

    @work(exclusive=True, group="diff")
    async def show_diff(self, f: dict) -> None:
        c = palette(self.app)
        risks = [r for r in f["risks"]]
        if risks or f["refused"] or f["state"] == "conflict":
            t = Text()
            for r in risks:
                t.append(r, style=f"bold {c['gate']}")
                t.append(f"  {RISK_HELP.get(r, '')}\n", style=c["muted"])
            if f["state"] == "conflict":
                t.append("conflict", style=f"bold {c['blocked']}")
                t.append("  You changed this file after the session started. corral won't overwrite it.\n",
                         style=c["muted"])
            if f["refused"]:
                t.append("refused", style=f"bold {c['blocked']}")
                t.append(f"  {f['refused']}\n", style=c["muted"])
            self.risk.update(t)
            self.risk.add_class("-visible")
        else:
            self.risk.remove_class("-visible")
        try:
            r = await self.client.request("changes.diff", sid=self.sid, path=f["path"])
        except GatewayError as e:
            self.diff.update(Text(str(e), style=c["blocked"]))
            return
        self.diff.update(render_diff(r["diff"], c))
        self.query_one("#diff-wrap").scroll_home(animate=False)

    def _current(self) -> FileItem | None:
        item = self.list.highlighted_child
        return item if isinstance(item, FileItem) else None

    def action_toggle(self) -> None:
        item = self._current()
        if not item or not item.selectable:
            return
        item.selected = not item.selected
        (self.selected.add if item.selected else self.selected.discard)(item.f["path"])
        item.draw()
        self.draw_head()

    def action_all(self) -> None:
        items = [i for i in self.list.children if isinstance(i, FileItem) and i.selectable]
        on_ = not all(i.selected for i in items)
        for i in items:
            i.selected = on_
            (self.selected.add if on_ else self.selected.discard)(i.f["path"])
            i.draw()
        self.draw_head()

    def action_reload(self) -> None:
        self.load()

    def action_back(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        paths = [f["path"] for f in self.files if f["path"] in self.selected]
        if not paths:
            self.notify("Select files with space first.", severity="warning")
            return
        risky = [f for f in self.files if f["path"] in self.selected and f["risks"]]
        if risky:
            c = palette(self.app)
            body = Text()
            body.append("These files can run code on your machine or in CI once applied:\n\n")
            for f in risky:
                body.append(sanitize(f["path"]), style="bold")
                body.append(f"  {', '.join(f['risks'])}\n", style=c["gate"])
            body.append("\nYou've seen their diffs on this screen. Apply anyway?", style=c["muted"])
            self.app.push_screen(
                ConfirmModal("APPLY", body, f"Apply {len(paths)} file{'s' if len(paths) != 1 else ''}"),
                lambda ok: self.do_apply(paths) if ok else None,
            )
        else:
            self.do_apply(paths)

    @work(group="apply")
    async def do_apply(self, paths: list[str]) -> None:
        try:
            r = await self.client.request("changes.apply", sid=self.sid, paths=paths)
        except GatewayError as e:
            self.notify(str(e), severity="error")
            return
        if r["applied"]:
            n = len(r["applied"])
            self.notify(f"Applied {n} file{'s' if n != 1 else ''} to {self.repo}")
        for item in r["refused"]:
            self.notify(f"Not applied: {item['path']}: {item['reason']}", severity="error", timeout=8)
        self.load()


def render_diff(diff: str, c: dict) -> Text:
    t = Text(no_wrap=False)
    lines = diff.splitlines()
    for line in lines[:6000]:
        if line.startswith(("diff --git", "index ", "new file mode", "deleted file mode", "old mode", "new mode")):
            continue
        if line.startswith(("+++", "---")):
            t.append(line + "\n", style=c["faint"])
        elif line.startswith("@@"):
            t.append("\n" + line + "\n", style=c["cell"])
        elif line.startswith("+"):
            t.append(line + "\n", style=c["success"])
        elif line.startswith("-"):
            t.append(line + "\n", style=c["blocked"])
        elif line.startswith("Binary files"):
            t.append(line + "\n", style=c["muted"])
        else:
            t.append(line + "\n")
    if len(lines) > 6000:
        t.append(f"\n… {len(lines) - 6000} more lines", style=c["faint"])
    if not lines:
        t.append("No textual diff (empty file or mode change).", style=c["muted"])
    return t


# ---------------------------------------------------------------- modals


class ApprovalModal(ModalScreen[list[str]]):
    BINDINGS = [Binding("y", "allow", "Allow"), Binding("n", "deny", "Deny"), Binding("escape", "deny", "Deny")]

    def __init__(self, approval: dict):
        super().__init__()
        self.approval = approval

    def compose(self) -> ComposeResult:
        hosts = self.approval["hosts"]
        with Vertical(classes="dialog -gate"):
            yield Static("NETWORK REQUEST", classes="eyebrow")
            yield Static(
                "The agent wants HTTPS access from the cell to the hosts below. A host you allow can receive "
                "data from the cell for the rest of this session.",
                classes="body",
            )
            yield SelectionList[str](*[(sanitize(h), h, True) for h in hosts], id="hosts")
            yield Static("Reason given by the agent (not verified)", classes="muted")
            yield Static(Text(sanitize(self.approval.get("reason") or "none given")), classes="reason")
            with Horizontal(classes="buttons"):
                yield Button("Deny  n", id="deny")
                yield Button("Allow selected  y", id="allow", classes="-gate")

    def on_mount(self) -> None:
        self.query_one("#hosts").focus()

    def action_allow(self) -> None:
        self.dismiss(list(self.query_one("#hosts", SelectionList).selected))

    def action_deny(self) -> None:
        self.dismiss([])

    @on(Button.Pressed, "#allow")
    def _allow(self) -> None:
        self.action_allow()

    @on(Button.Pressed, "#deny")
    def _deny(self) -> None:
        self.action_deny()


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "ok", "Confirm")]

    def __init__(self, eyebrow: str, body: Text | str, confirm: str, gate: bool = True):
        super().__init__()
        self.eyebrow, self.body, self.confirm, self.gate = eyebrow, body, confirm, gate

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog -gate" if self.gate else "dialog"):
            yield Static(self.eyebrow, classes="eyebrow")
            yield Static(self.body, classes="body")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(f"{self.confirm}  y", id="ok", classes="-gate" if self.gate else "-primary")

    def on_mount(self) -> None:
        self.query_one("#cancel").focus()

    def action_ok(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "ok")


class TrustModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Not now")]

    def __init__(self, file: str, text: str):
        super().__init__()
        self.file, self.text = file, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog -gate"):
            yield Static("PROJECT INSTRUCTIONS", classes="eyebrow")
            yield Static(
                f"{self.file} in this repository becomes part of the agent's instructions once you trust it. "
                "Read it first: anyone who can commit to the repo can write to it. Trust is remembered until "
                "the file changes.",
                classes="body",
            )
            with VerticalScroll(id="notes-text"):
                yield Static(Text(self.text))
            with Horizontal(classes="buttons"):
                yield Button("Not now", id="no")
                yield Button("Trust this version", id="yes", classes="-gate")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class PickerModal(ModalScreen[str | None]):
    """A titled list of options. Returns the chosen option id."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, eyebrow: str, options: list[tuple[str, Text | str]], note: str = ""):
        super().__init__()
        self.eyebrow, self.options, self.note = eyebrow, options, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.eyebrow, classes="eyebrow")
            if self.note:
                yield Static(self.note, classes="body muted")
            yield OptionList(*[Option(label, id=oid) for oid, label in self.options])

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
