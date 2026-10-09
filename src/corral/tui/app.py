"""The corral terminal UI. A client of the gateway: it renders events and
relays decisions. It never holds keys and never touches a cell directly.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .. import config as config_mod
from ..client import GatewayClient, GatewayError
from ..text import one_line, sanitize
from .screens import ApprovalModal, ConfirmModal, PickerModal, ReviewScreen, TrustModal
from .theme import THEMES, palette
from .widgets import AssistantMsg, Composer, LogLine, Note, NoticeLine, Sidebar, ToolBlock, TopBar, UserMsg

COMMANDS: list[tuple[str, str, bool]] = [  # (name, description, takes arguments)
    ("/review", "review the cell's changes and apply the ones you want", False),
    ("/allow", "allow HTTPS to hosts, e.g. /allow pypi.org files.pythonhosted.org", True),
    ("/revoke", "remove hosts from the allowlist", True),
    ("/model", "switch model, e.g. /model claude-sonnet-5-5", True),
    ("/budget", "set this session's spend limit in USD, e.g. /budget 10", True),
    ("/trust", "read and trust this repo's AGENTS.md", False),
    ("/sessions", "switch to another session in this repo", False),
    ("/new", "start a new session in this repo", False),
    ("/close", "end this session and destroy its cell", False),
    ("/stop", "stop the current turn (esc)", False),
    ("/clear", "clear the screen; the session keeps its history", False),
    ("/theme", "switch between dusk and daylight", False),
    ("/quit", "leave; the session keeps running in the gateway", False),
]


CELL_KINDS = {"DockerCell": "docker", "LocalCell": "local, unisolated"}


def ago(ts: float) -> str:
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


class CorralApp(App):
    CSS_PATH = "corral.tcss"
    TITLE = "corral"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+r", "review", "Review", priority=True),
        Binding("ctrl+b", "sidebar", "Panel", priority=True),
        Binding("ctrl+t", "theme", "Theme"),
        Binding("escape", "escape", "Stop"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self, repo: Path, *, model: str | None = None, new: bool = False, attach: str | None = None,
                 review: str | None = None, client: GatewayClient | None = None):
        super().__init__()
        for theme in THEMES:  # before the stylesheet is parsed: it uses the zone variables
            self.register_theme(theme)
        self.theme = "dusk"
        self.repo = repo
        self.want_model = model
        self.force_new = new
        self.attach_sid = attach
        self.review_sid = review
        self.client = client or GatewayClient()
        self.cfg = config_mod.load()
        self.sid: str | None = None
        self.session_repo = str(repo)
        self.hosts: list[str] = []
        self.busy = False
        self.events: asyncio.Queue[dict] = asyncio.Queue()
        self.current_text: AssistantMsg | None = None
        self.current_note: Note | None = None
        self.tools: dict[str, ToolBlock] = {}
        self.pending_approvals: dict[str, dict] = {}
        self.approval_on_screen: str | None = None
        self.trust: tuple[str, str] | None = None
        self.last_blocked: str | None = None
        self.files: list[dict] = []

    # ------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield TopBar()
        with Horizontal(id="body"):
            yield VerticalScroll(id="transcript")
            yield Sidebar()
        with Vertical(id="bottom"):
            yield OptionList(id="suggest")
            yield Composer()
            yield Static(id="hints")

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    @property
    def bar(self) -> TopBar:
        return self.query_one(TopBar)

    @property
    def side(self) -> Sidebar:
        return self.query_one(Sidebar)

    @property
    def composer(self) -> Composer:
        return self.query_one(Composer)

    def on_mount(self) -> None:
        self.bar.set(repo=self.repo.name)
        self.update_hints()
        self.theme_changed_signal.subscribe(self, lambda _theme: self.update_hints())
        if self.size.width < 110:
            self.side.add_class("-hidden")
        self.consume_events()
        self.boot()

    def update_hints(self) -> None:
        c = palette(self)
        pairs = ([("esc", "stop"), ("ctrl+r", "review"), ("ctrl+b", "panel")] if self.busy else
                 [("enter", "send"), ("ctrl+j", "new line"), ("/", "commands"), ("ctrl+r", "review"),
                  ("ctrl+b", "panel"), ("ctrl+q", "quit")])
        t = Text(no_wrap=True, overflow="ellipsis")
        for key, label in pairs:
            t.append(key, style=c["gate"] if key in ("ctrl+r", "esc") else c["muted"])
            t.append(f" {label}   ", style=c["faint"])
        self.query_one("#hints", Static).update(t)

    # ------------------------------------------------------------ boot

    @work(exclusive=True, group="boot")
    async def boot(self) -> None:
        self.client.on_event = self.events.put_nowait
        self.client.on_disconnect = lambda: self.events.put_nowait({"event": "_disconnected"})
        try:
            await self.client.connect()
        except GatewayError as e:
            await self.add(NoticeLine("error", str(e)))
            self.bar.set(state="error")
            return
        if self.client.version_mismatch:
            await self.add(NoticeLine("warn", f"The gateway is version {self.client.server_version}; this client "
                                              "is newer. Restart it with `corral daemon restart`."))
        if self.review_sid:
            info = await self.client.request("session.attach", sid=self.review_sid, replay=False)
            self.sid = self.review_sid
            self.session_repo = info["repo"]
            await self.push_screen_wait(ReviewScreen(self.client, self.sid, Path(info["repo"]).name))
            self.exit()
            return
        if self.attach_sid:
            await self.attach(self.attach_sid)
            return
        choice = "new"
        if not self.force_new:
            try:
                sessions = await self.client.request("session.list", repo=str(self.repo))
            except GatewayError:
                sessions = []
            live = [s for s in sessions if s["state"] in ("starting", "ready", "running")]
            if live:
                choice = await self.push_screen_wait(self.session_picker(live)) or "new"
        if choice == "new":
            await self.open_new()
        else:
            await self.attach(choice)

    def session_picker(self, sessions: list[dict]) -> PickerModal:
        c = palette(self)
        options: list[tuple[str, Text | str]] = []
        for s in sessions:
            t = Text()
            t.append(s["sid"], style=c["cell"])
            t.append("  ")
            t.append(one_line(s.get("title") or "untitled", 46))
            meta = f"   {ago(s['created'])} · {s['model']}"
            if s.get("files"):
                meta += f" · {s['files']} changed"
            t.append(meta, style=c["faint"])
            options.append((s["sid"], t))
        options.append(("new", Text("New session", style=c["gate"])))
        return PickerModal("SESSIONS IN THIS REPO", options, "These are still running in the gateway.")

    async def open_new(self) -> None:
        await self.reset_view()
        try:
            info = await self.client.request("session.open", repo=str(self.repo),
                                              **({"model": self.want_model} if self.want_model else {}))
        except GatewayError as e:
            await self.add(NoticeLine("error", str(e)))
            self.bar.set(state="error")
            return
        self.adopt(info)
        self.composer.focus()

    async def attach(self, sid: str) -> None:
        await self.reset_view()
        self.sid = sid  # events from the replay arrive before the reply
        try:
            info = await self.client.request("session.attach", sid=sid)
        except GatewayError as e:
            await self.add(NoticeLine("error", str(e)))
            return
        self.adopt(info)
        self.composer.focus()

    def adopt(self, info: dict) -> None:
        self.sid = info["sid"]
        self.session_repo = info["repo"]
        self.hosts = info.get("hosts", [])
        self.bar.set(repo=Path(info["repo"]).name, model=info["model"], state=info["state"],
                     cell=info.get("cell") or "", hosts=len(self.hosts), budget=info.get("budget", 0),
                     cost=0.0 if info.get("stats") is None else info["stats"]["cost"])
        self.side.hosts = self.hosts
        self.side.cell = info.get("cell") or ""
        self.side.cell_kind = CELL_KINDS.get(info.get("cell_kind") or "", "")
        if info.get("stats"):
            self.side.stats = info["stats"]
        self.side.redraw()
        for a in info.get("pending_approvals", []):
            self.pending_approvals.setdefault(a["id"], {"id": a["id"], "hosts": a["hosts"], "reason": ""})
        self.call_after_refresh(self.show_next_approval)

    async def reset_view(self) -> None:
        await self.transcript.remove_children()
        self.current_text = None
        self.current_note = None
        self.tools.clear()
        self.pending_approvals.clear()
        self.files = []
        self.side.files, self.side.net, self.side.stats = [], self.side.net.__class__(maxlen=60), {}
        self.side.redraw()

    # ------------------------------------------------------------ transcript

    def following(self) -> bool:
        t = self.transcript
        return t.scroll_y >= t.max_scroll_y - 3

    async def add(self, widget) -> None:
        follow = self.following()
        await self.transcript.mount(widget)
        if follow:
            self.transcript.scroll_end(animate=False)

    def follow_later(self) -> None:
        if self.following():
            self.call_after_refresh(self.transcript.scroll_end, animate=False)

    async def end_blocks(self) -> None:
        if self.current_text:
            await self.current_text.finish()
            self.current_text = None
        if self.current_note:
            self.current_note.finish()
            self.current_note = None

    # ------------------------------------------------------------ gateway events

    @work(group="events")
    async def consume_events(self) -> None:
        while True:
            ev = await self.events.get()
            try:
                await self.handle(ev)
            except Exception as e:  # rendering must never kill the event loop
                self.log.error(f"event {ev.get('event')}: {e!r}")

    async def handle(self, ev: dict) -> None:
        kind = ev["event"]
        if kind == "_disconnected":
            await self.add(NoticeLine("error", "Lost connection to the gateway. Restart corral to reconnect; "
                                               "the session keeps running."))
            self.bar.set(state="error", busy=False)
            return
        if ev.get("sid") != self.sid:
            return
        match kind:
            case "session.state":
                state = ev["state"]
                self.bar.set(state=state, busy=state == "running")
                if state == "ready" and ev.get("cell"):
                    self.bar.set(cell=ev["cell"])
                    self.side.cell = ev["cell"]
                    self.side.cell_kind = CELL_KINDS.get(ev.get("cell_kind") or "", "")
                    self.side.redraw()
                elif state == "error":
                    await self.add(NoticeLine("error", f"Session failed: {ev.get('error')}"))
                elif state == "closed":
                    await self.add(NoticeLine("info", f"Session closed. Unapplied changes stay reviewable with "
                                                      f"`corral review {self.sid}`."))
            case "log":
                await self.add(LogLine(ev["kind"], ev["text"], ev.get("seconds")))
            case "user":
                await self.end_blocks()
                await self.add(UserMsg(ev["text"]))
                self.transcript.scroll_end(animate=False)
            case "turn.start":
                self.set_busy(True)
                self.last_blocked = None
            case "turn.end":
                await self.end_blocks()
                self.set_busy(False)
                if ev.get("stop") == "cancelled":
                    await self.add(NoticeLine("warn", "Stopped."))
            case "text.delta":
                if self.current_note:
                    self.current_note.finish()
                    self.current_note = None
                if not self.current_text:
                    self.current_text = AssistantMsg()
                    await self.add(self.current_text)
                await self.current_text.feed(sanitize(ev["text"]))
                self.follow_later()
            case "note.delta":
                if self.current_text:
                    await self.current_text.finish()
                    self.current_text = None
                if not self.current_note:
                    self.current_note = Note()
                    await self.add(self.current_note)
                self.current_note.append(ev["text"])
                self.follow_later()
            case "block.end":
                await self.end_blocks()
            case "tool.preparing":
                await self.end_blocks()
                if ev["call_id"] not in self.tools:
                    tb = ToolBlock(ev["call_id"], ev["name"])
                    self.tools[ev["call_id"]] = tb
                    await self.add(tb)
            case "tool.start":
                await self.end_blocks()
                tb = self.tools.get(ev["call_id"])
                if tb is None:
                    tb = ToolBlock(ev["call_id"], ev["name"])
                    self.tools[ev["call_id"]] = tb
                    await self.add(tb)
                tb.start(ev.get("title", ""))
            case "tool.output":
                tb = self.tools.get(ev["call_id"])
                if tb:
                    tb.output(ev["chunk"])
                    self.follow_later()
            case "tool.end":
                tb = self.tools.get(ev["call_id"])
                if tb:
                    tb.end(ev["ok"], ev.get("summary", ""), ev.get("detail", ""))
            case "usage":
                self.bar.set(cost=ev["cost"], budget=ev["budget"], priced=ev.get("priced", True),
                             ctx=ev.get("context_pct", 0), model=ev["model"])
                self.side.stats = ev
                self.side.redraw()
            case "changes":
                self.files = ev["files"]
                pending = [f for f in self.files if f["state"] != "applied"]
                self.bar.set(changes=len(pending))
                self.side.files = self.files
                self.side.redraw()
            case "applied":
                if ev["applied"]:
                    n = len(ev["applied"])
                    await self.add(LogLine("applied", f"{n} file{'s' if n != 1 else ''} to your repo: "
                                                      + ", ".join(ev["applied"][:6])))
                for r in ev["refused"]:
                    await self.add(LogLine("refused", f"{r['path']}: {r['reason']}"))
            case "network.allowlist":
                self.hosts = ev["hosts"]
                self.bar.set(hosts=len(self.hosts))
                self.side.hosts = self.hosts
                self.side.redraw()
                await self.add(LogLine("network", ("allowed " + ", ".join(self.hosts)) if self.hosts
                                       else "closed: no hosts allowed"))
            case "network.event":
                self.side.net.append(ev)
                self.side.redraw()
                if ev["decision"] == "deny" and ev["host"] != self.last_blocked:
                    self.last_blocked = ev["host"]
                    await self.add(LogLine("blocked", f"{ev['host'] or '?'}: {ev['reason']}"))
            case "approval.request":
                self.pending_approvals[ev["id"]] = ev
                self.call_after_refresh(self.show_next_approval)
            case "approval.resolved":
                self.pending_approvals.pop(ev["id"], None)
                if self.approval_on_screen == ev["id"] and isinstance(self.screen, ApprovalModal):
                    self.approval_on_screen = None
                    self.screen.dismiss(None)
                if not ev.get("allowed"):
                    await self.add(LogLine("blocked", "you denied the network request"))
            case "trust.needed":
                self.trust = (ev["file"], ev["text"])
                await self.add(NoticeLine("warn", f"{ev['file']} wasn't loaded because you haven't reviewed it. "
                                                  "Type /trust to read it and decide."))
            case "notice":
                await self.add(NoticeLine(ev.get("level", "info"), ev["text"]))
            case "model":
                self.bar.set(model=ev["model"])
                await self.add(LogLine("model", ev["model"]))

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.bar.set(busy=busy)
        self.composer.set_class(busy, "-busy")
        self.update_hints()

    def show_next_approval(self) -> None:
        if self.approval_on_screen or not self.pending_approvals:
            return
        aid, ev = next(iter(self.pending_approvals.items()))
        self.approval_on_screen = aid
        self.bell()

        def answered(allowed: list[str] | None) -> None:
            self.approval_on_screen = None
            if allowed is not None:
                self.respond_approval(aid, allowed)
            self.pending_approvals.pop(aid, None)
            self.call_after_refresh(self.show_next_approval)

        self.push_screen(ApprovalModal(ev), answered)

    @work(group="approval")
    async def respond_approval(self, aid: str, allowed: list[str]) -> None:
        try:
            await self.client.request("approval.respond", sid=self.sid, approval_id=aid, allowed=allowed)
        except GatewayError:
            pass  # answered elsewhere

    # ------------------------------------------------------------ input

    @on(Composer.Submitted)
    async def submitted(self, msg: Composer.Submitted) -> None:
        self.hide_suggestions()
        text = msg.text.strip()
        if text.startswith("/"):
            await self.run_command(text)
            return
        if not self.sid:
            self.notify("No session yet.", severity="warning")
            return
        try:
            await self.client.request("message.send", sid=self.sid, text=text)
        except GatewayError as e:
            await self.add(NoticeLine("error", str(e)))

    @on(Composer.Edited)
    def composer_edited(self, msg: Composer.Edited) -> None:
        text = msg.text
        if text.startswith("/") and " " not in text and "\n" not in text:
            matches = [c for c in COMMANDS if c[0].startswith(text)]
            if matches:
                self.show_suggestions(matches)
                return
        self.hide_suggestions()

    def show_suggestions(self, matches) -> None:
        c = palette(self)
        ol = self.query_one("#suggest", OptionList)
        ol.clear_options()
        for name, desc, _ in matches:
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(f"{name:<11}", style=c["gate"])
            t.append(desc, style=c["muted"])
            ol.add_option(Option(t, id=name))
        ol.highlighted = 0
        ol.add_class("-visible")
        self.composer.suggesting = True

    def hide_suggestions(self) -> None:
        ol = self.query_one("#suggest", OptionList)
        ol.remove_class("-visible")
        self.composer.suggesting = False

    @on(Composer.Nav)
    async def composer_nav(self, msg: Composer.Nav) -> None:
        ol = self.query_one("#suggest", OptionList)
        if msg.key == "up":
            ol.action_cursor_up()
            return
        if msg.key == "down":
            ol.action_cursor_down()
            return
        if ol.highlighted is None:
            return
        name = ol.get_option_at_index(ol.highlighted).id
        takes_args = next(c[2] for c in COMMANDS if c[0] == name)
        self.hide_suggestions()
        if msg.key == "tab" or takes_args:
            self.composer.load_text(name + " ")
            self.composer.move_cursor(self.composer.document.end)
        else:
            self.composer.clear()
            await self.run_command(name)

    async def run_command(self, line: str) -> None:
        name, _, rest = line.partition(" ")
        args = rest.split()
        if name not in {c[0] for c in COMMANDS}:
            await self.add(NoticeLine("warn", f"Unknown command {name}. Type / to see the list."))
            return
        if name == "/quit":
            self.action_quit()
            return
        if name == "/theme":
            self.action_theme()
            return
        if name == "/clear":
            await self.transcript.remove_children()
            self.current_text = None
            self.current_note = None
            return
        if name == "/new":
            await self.open_new()
            return
        if name == "/sessions":
            self.pick_session()
            return
        if not self.sid:
            self.notify("No session yet.", severity="warning")
            return
        try:
            match name:
                case "/review":
                    self.action_review()
                case "/stop":
                    await self.client.request("turn.cancel", sid=self.sid)
                case "/allow":
                    if not args:
                        await self.add(NoticeLine("warn", "Name the hosts: /allow pypi.org files.pythonhosted.org"))
                        return
                    await self.client.request("network.set", sid=self.sid, hosts=sorted(set(self.hosts) | set(args)))
                case "/revoke":
                    remaining = [h for h in self.hosts if h not in set(args)] if args else []
                    await self.client.request("network.set", sid=self.sid, hosts=remaining)
                case "/model":
                    if args:
                        await self.client.request("model.set", sid=self.sid, model=args[0])
                    else:
                        self.pick_model()
                case "/budget":
                    try:
                        usd = float(args[0].lstrip("$"))
                    except (IndexError, ValueError):
                        await self.add(NoticeLine("warn", "Give an amount in USD: /budget 10"))
                        return
                    await self.client.request("budget.set", sid=self.sid, usd=usd)
                    await self.add(LogLine("budget", f"${usd:.2f}"))
                case "/trust":
                    self.review_trust()
                case "/close":
                    self.confirm_close()
        except GatewayError as e:
            await self.add(NoticeLine("error", str(e)))

    @work(group="dialog")
    async def pick_session(self) -> None:
        try:
            sessions = await self.client.request("session.list", repo=self.session_repo)
        except GatewayError as e:
            self.notify(str(e), severity="error")
            return
        live = [s for s in sessions if s["state"] in ("starting", "ready", "running") and s["sid"] != self.sid]
        if not live:
            self.notify("No other sessions in this repo. /new starts one.")
            return
        choice = await self.push_screen_wait(self.session_picker(live))
        if choice == "new":
            await self.open_new()
        elif choice:
            await self.attach(choice)

    @work(group="dialog")
    async def pick_model(self) -> None:
        c = palette(self)
        options = [(m, Text(m, style=c["cell"] if m == self.bar.model else "")) for m in self.cfg.model_choices]
        choice = await self.push_screen_wait(PickerModal("MODEL", options,
                                                         "Or type /model <name>, e.g. /model ollama/qwen3:8b"))
        if choice:
            try:
                await self.client.request("model.set", sid=self.sid, model=choice)
            except GatewayError as e:
                await self.add(NoticeLine("error", str(e)))

    @work(group="dialog")
    async def review_trust(self) -> None:
        if not self.trust:
            self.notify("Nothing to review: no untrusted AGENTS.md in this session.")
            return
        file, text = self.trust
        if await self.push_screen_wait(TrustModal(file, text)):
            try:
                r = await self.client.request("trust.accept", sid=self.sid)
                self.trust = None
                self.notify(r["message"])
            except GatewayError as e:
                self.notify(str(e), severity="error")

    @work(group="dialog")
    async def confirm_close(self) -> None:
        ok = await self.push_screen_wait(ConfirmModal(
            "END SESSION",
            "The cell and everything in it is destroyed. Changes you haven't applied stay reviewable with "
            f"`corral review {self.sid}`.",
            "End session", gate=False))
        if ok:
            await self.client.request("session.close", sid=self.sid)

    # ------------------------------------------------------------ actions

    def action_review(self) -> None:
        if not self.sid or isinstance(self.screen, ReviewScreen):
            return
        self.push_screen(ReviewScreen(self.client, self.sid, Path(self.session_repo).name))

    def action_sidebar(self) -> None:
        self.side.toggle_class("-hidden")

    def action_theme(self) -> None:
        self.theme = "daylight" if self.theme == "dusk" else "dusk"

    async def action_escape(self) -> None:
        if self.composer.suggesting:
            self.hide_suggestions()
            return
        if self.busy and self.sid:
            try:
                await self.client.request("turn.cancel", sid=self.sid)
            except GatewayError:
                pass
            return
        if self.composer.text:
            self.composer.clear()

    async def action_quit(self) -> None:
        await self.client.close()
        self.exit(self.sid)

    @on(Sidebar.OpenReview)
    def open_review(self) -> None:
        self.action_review()

    @on(Markdown.LinkClicked)
    def link_clicked(self, event: Markdown.LinkClicked) -> None:
        # Links come from model output. Show them; don't open them.
        self.notify(f"Link (not opened): {one_line(event.href, 200)}", timeout=8)
