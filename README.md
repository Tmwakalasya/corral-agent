# corral

A coding agent that works inside a sealed, single-use sandbox and hands its work back for review.

The agent gets a copy of your repo in a container with no credentials, no host files and no network.
Everything it does stays there. You see what it changed as a diff and apply the files you want.
Network access, if it needs any, is per host and only after you approve it.

Works with Claude (default `claude-opus-5-5`), local models through Ollama, and any
OpenAI-compatible API (OpenAI, OpenRouter, Groq, Together, Fireworks, DeepSeek, xAI, Gemini).

```
corral                      # TUI for the repo you're in
corral run "fix the failing test" --allow pypi.org --allow files.pythonhosted.org
corral review <sid>         # review and apply a session's changes later
```

## Install

Needs Python 3.13+, git, and Docker (OrbStack or Docker Desktop).

```
uv tool install /path/to/corral
corral key set anthropic    # stored in the macOS Keychain; or set ANTHROPIC_API_KEY
corral doctor
```

The first session builds the cell image (`corral-cell:<version>`, a few minutes, once).

## How it's built

The layout follows Uber's GitFarm (gateway → backend → per-request sandboxes, one bare clone
per repo, warm pools), adapted so that the sandbox is the part nobody trusts.

```
 TUI / corral run  ──unix socket, 0600, peer uid checked──►  gateway  (corral daemon)
 renders, relays your decisions                               ├ API keys (Keychain), model calls
                                                              ├ budgets, approvals, audit log
                                                              ├ admission (max sessions), idle reaper
                                                              └ backend
                                                                 ├ one bare mirror per repo
                                                                 ├ warm pool of never-used cells
                                                                 └ cells ─┬ sandbox  (internal network only)
                                                                          └ proxy    (allowlist, HTTPS only)
```

A session:

1. **Snapshot.** The gateway fetches your HEAD into the repo's mirror and records your working
   tree (tracked and untracked-not-ignored files) as a baseline commit, using a throwaway index
   inside the mirror. Your index and files aren't written. Secret-looking files (`.env*`, keys,
   `credentials.json`, `.npmrc`, `*.tfstate`, …) are left out and listed.
2. **Cell.** A fresh cell comes from the warm pool. The baseline goes in as a git bundle,
   the only thing that crosses into the cell. The mirror never copies your `.git/config` or
   hooks, so tokens in remote URLs don't travel.
3. **Agent loop.** The gateway calls the model and runs each tool call inside the cell, one
   at a time, in the order the model issued them.
4. **Changes out.** After each turn the cell commits its work and streams back a bundle of
   `baseline..HEAD`. The gateway fetches it into the mirror with git's object checks on
   (a tree entry named `.git` is rejected) and a size cap. Diffs are computed on the host by
   your git, not reported by the cell.
5. **Apply.** You pick files. Each one is re-checked when written: the path is safe, no parent
   directory is a symlink, a symlink target stays inside the repo, submodules are refused, and
   your copy is still what the cell started from (if you've edited it since, it's a conflict
   and isn't overwritten).
6. **Close.** The cell is destroyed, never reused. Unapplied work stays reviewable from the mirror.

## What the sandbox enforces

| | |
|---|---|
| identity | uid 1000, every capability dropped, `no-new-privileges`, no sudo |
| filesystem | read-only root; writable only `/workspace` (per-cell volume), `/tmp` and `~` (size-capped tmpfs); no host paths mounted |
| resources | memory, CPU and pid limits per cell |
| network | `--internal` Docker network: no route out. One exit: the cell's own proxy |
| proxy | `CONNECT host:443` only, host must be on the allowlist; resolves DNS itself and refuses hosts that resolve to private, loopback or link-local addresses (metadata endpoints, your LAN); logs every decision |
| DNS | external names don't resolve inside the cell, so DNS can't carry data out |
| credentials | none. API keys stay in the gateway |

`tests/test_docker.py` checks each of these from inside a real cell.

## What you decide

- **Network.** The agent calls `request_network` with hosts and a reason. You see a dialog with
  the hosts (deselect any) and the reason, marked as unverified. `/allow` and `/revoke` work too.
  Blocked attempts show up in the transcript and the side panel.
- **Changes.** `ctrl+r` opens review: file list, diff, and labels for files that run code on
  your machine or in CI (workflows, git hooks, build scripts, editor config, lockfiles,
  executables, symlinks). Applying any of those asks again.
- **Project instructions.** `AGENTS.md` / `CLAUDE.md` load only after you've read them
  (`/trust`). Trust is pinned to the file's hash.

Everything above is recorded in `~/.corral/audit/<sid>.jsonl`, a hash-chained log
(`corral audit verify`).

## What it doesn't protect against

- **Kernel escapes.** Cells are containers. A kernel exploit from inside one reaches Docker's
  Linux VM, and OrbStack's VM can read your home directory by default. A VM per cell (Apple's
  `container`, Firecracker) or gVisor would close this; not supported yet.
- **Allowed hosts.** A host you allow can receive anything the cell sends it. There's no TLS
  inspection. Allow registries for installs, not general-purpose sites.
- **Your history.** The cell gets your git history so the agent can use `git log`. If secrets
  were ever committed, set `[cell] history = false`.
- **The model provider** sees the code the agent reads, as with any hosted model.
- **Bad code that looks fine.** Review is the control. Run what you apply the way you'd run a
  stranger's pull request.

## Using the TUI

| key | |
|---|---|
| enter / ctrl+j | send / new line |
| `/` | commands: `/review /allow /revoke /model /budget /trust /sessions /new /close /stop /clear /theme /quit` |
| ctrl+r | review changes |
| ctrl+b | side panel |
| esc | stop the current turn |
| ctrl+t | dusk / daylight theme |
| ctrl+q | quit; the session keeps running and `corral` in the repo picks it up again |

Colors mark where things happen: teal is inside the cell, amber is anything crossing the
fence, periwinkle is you, red is blocked.

Sessions you leave close themselves after 30 idle minutes.

## Config

`~/.corral/config.toml`, all optional:

```toml
model = "claude-opus-5-5"
effort = "high"
budget_usd = 5.0
pool_size = 2           # warm cells
max_sessions = 4
idle_close_minutes = 30

[cell]
memory = "4g"
cpus = 2.0
history = true

[network]
default_allow = []      # hosts every session starts with

[models]
choices = ["claude-opus-5-5", "claude-sonnet-5-5", "ollama/qwen3:8b"]

[providers.vllm]        # any OpenAI-compatible endpoint, used as vllm/<model>
base_url = "http://localhost:8000/v1"

[pricing."openrouter/moonshotai/kimi-k3"]
input = 0.6
output = 2.5
```

## Development

```
uv sync
uv run pytest                  # unit, git plumbing and gateway tests; Docker tests run if Docker is up
uv run pytest -m "not docker"
```

If the project lives in an iCloud-synced folder (Desktop, Documents), macOS marks the venv's
`.pth` files hidden and Python 3.13 skips them. Put the venv elsewhere:
`export UV_PROJECT_ENVIRONMENT=~/.venvs/corral`.
