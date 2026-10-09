"""Per-repository bare mirrors, and the git plumbing that moves work in and out of cells.

The flow (borrowed from GitFarm: one bare clone per repo, checkouts
materialized on demand):

1. ``snapshot`` fetches the host repo's HEAD into the mirror and records the
   working tree (tracked + untracked-not-ignored files, minus withheld
   secrets) as a *baseline* commit. It uses a throwaway index inside the
   mirror, so the user's index and working tree are never written.
2. ``bundle_for_cell`` packs the baseline as a git bundle. That bundle is the
   only thing that goes into a cell.
3. The cell commits its work and streams back a bundle of ``base..HEAD``.
   ``receive_bundle`` fetches it into the mirror with object checks on.
4. ``changes`` and ``apply`` diff baseline against work with the host's own
   git, and write files into the user's repo one by one under ``policy``.

The mirror never copies the host's ``.git/config`` or hooks, so credentials in
remote URLs and hook scripts don't travel.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import proc
from . import policy

# Isolated config for anything that touches the mirror: no system or global
# config means no filter drivers, textconv, fsmonitor or credential helpers.
_ISOLATED_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "corral",
    "GIT_AUTHOR_EMAIL": "corral@localhost",
    "GIT_COMMITTER_NAME": "corral",
    "GIT_COMMITTER_EMAIL": "corral@localhost",
    "GIT_ATTR_NOSYSTEM": "1",
}
_SAFE = ["-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false"]
_FSCK = ["-c", "transfer.fsckObjects=true", "-c", "fetch.fsckObjects=true", "-c", "receive.fsckObjects=true"]

MAX_FILE_BYTES = 50 * 1024 * 1024


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_ISOLATED_ENV)
    env.update(extra)
    return env


async def git(*args: str, cwd: Path | None = None, input: bytes | None = None, env: dict[str, str] | None = None,
              check: bool = True) -> proc.Completed:
    return await proc.run(["git", *_SAFE, *args], cwd=cwd, input=input, env=env or _env(), check=check)


async def _m(mirror: Path, *args: str, input: bytes | None = None, env: dict[str, str] | None = None,
             check: bool = True) -> proc.Completed:
    return await git(f"--git-dir={mirror}", *args, input=input, env=env, check=check)


@dataclass
class Snapshot:
    base: str  # baseline commit sha
    ref: str  # ref holding the baseline in the mirror
    head: str | None  # host HEAD at snapshot time
    files: int
    withheld: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # (path: reason)


async def is_git_repo(path: Path) -> Path | None:
    if path.is_file():
        path = path.parent
    r = await git("-C", str(path), "rev-parse", "--show-toplevel", check=False)
    if r.returncode != 0:
        return None
    return Path(r.text.strip())


async def ensure_mirror(mirror: Path) -> None:
    if (mirror / "HEAD").exists():
        return
    mirror.parent.mkdir(parents=True, exist_ok=True)
    await git("init", "--bare", "-q", str(mirror))
    for key, value in (("gc.auto", "0"), ("core.logAllRefUpdates", "false"), ("receive.fsckObjects", "true")):
        await _m(mirror, "config", key, value)


async def snapshot(repo: Path, mirror: Path, sid: str, *, history: bool = True) -> Snapshot:
    await ensure_mirror(mirror)
    head = None
    r = await git("-C", str(repo), "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
    if r.returncode == 0 and history:
        head = r.text.strip()
        await _m(mirror, *_FSCK, "fetch", "--no-tags", "-q", str(repo), f"+{head}:refs/corral/host/{sid}")

    listing = await git("-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    seen: set[str] = set()
    include: list[str] = []
    withheld: list[str] = []
    skipped: list[str] = []
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", errors="surrogateescape")
        if rel in seen:
            continue
        seen.add(rel)
        if policy.is_withheld(rel):
            withheld.append(rel)
            continue
        try:
            st = os.lstat(repo / rel)
        except FileNotFoundError:
            continue  # deleted in the working tree
        if stat.S_ISDIR(st.st_mode):
            skipped.append(f"{rel}: submodule")
            continue
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
            skipped.append(f"{rel}: not a regular file")
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size > MAX_FILE_BYTES:
            skipped.append(f"{rel}: larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
            continue
        include.append(rel)

    with tempfile.TemporaryDirectory(prefix="corral-idx-") as tmp:
        env = _env(GIT_INDEX_FILE=str(Path(tmp) / "index"), GIT_WORK_TREE=str(repo), GIT_DIR=str(mirror))
        if include:
            payload = b"\0".join(p.encode("utf-8", errors="surrogateescape") for p in include) + b"\0"
            await git("update-index", "--add", "-z", "--stdin", input=payload, env=env)
        tree = (await git("write-tree", env=env)).text.strip()
    parents = ["-p", head] if head else []
    base = (await _m(mirror, "commit-tree", tree, *parents, "-m", f"corral baseline {sid}")).text.strip()
    ref = f"refs/corral/{sid}/base"
    await _m(mirror, "update-ref", ref, base)
    return Snapshot(base=base, ref=ref, head=head, files=len(include), withheld=withheld, skipped=skipped)


async def bundle_for_cell(mirror: Path, snap: Snapshot, dest: Path) -> None:
    await _m(mirror, "bundle", "create", "-q", str(dest), snap.ref)


async def receive_bundle(mirror: Path, sid: str, bundle: Path) -> str:
    """Fetch a bundle produced inside a cell into refs/corral/<sid>/work. Returns its sha."""
    await _m(mirror, *_FSCK, "bundle", "verify", "-q", str(bundle))
    ref = f"refs/corral/{sid}/work"
    await _m(mirror, *_FSCK, "fetch", "--no-tags", "-q", str(bundle), f"+HEAD:{ref}")
    return (await _m(mirror, "rev-parse", "--verify", f"{ref}^{{commit}}")).text.strip()


async def set_work(mirror: Path, sid: str, sha: str) -> None:
    await _m(mirror, "update-ref", f"refs/corral/{sid}/work", sha)


async def resolve(mirror: Path, ref: str) -> str | None:
    r = await _m(mirror, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    return r.text.strip() if r.returncode == 0 else None


# ---------------------------------------------------------------- changes


@dataclass
class FileChange:
    path: str
    status: str  # A, M, D, T
    old_mode: str
    new_mode: str
    old_sha: str
    new_sha: str
    adds: int = 0
    dels: int = 0
    binary: bool = False
    risks: list[str] = field(default_factory=list)
    state: str = "pending"  # pending | applied | conflict
    refused: str | None = None  # hard-policy reason this can't be applied

    def to_dict(self) -> dict:
        return {
            "path": self.path, "status": self.status, "adds": self.adds, "dels": self.dels,
            "binary": self.binary, "risks": self.risks, "state": self.state, "refused": self.refused,
            "old_mode": self.old_mode, "new_mode": self.new_mode,
        }


ZERO = "0" * 40


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def host_blob(root: Path, rel: str) -> str | None:
    """Git blob id of the host's current version, or None if absent."""
    full = root / rel
    try:
        st = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(st.st_mode):
        return _blob_sha(os.fsencode(os.readlink(full)))
    if stat.S_ISREG(st.st_mode):
        return _blob_sha(full.read_bytes())
    return "not-a-file"


async def changes(repo: Path, mirror: Path, base: str, work: str) -> list[FileChange]:
    if base == work:
        return []
    raw = await _m(mirror, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", "--raw", "--no-abbrev",
                   base, work)
    out: dict[str, FileChange] = {}
    fields = raw.stdout.split(b"\0")
    i = 0
    while i < len(fields) - 1:
        meta = fields[i].decode()
        path = fields[i + 1].decode("utf-8", errors="surrogateescape")
        i += 2
        if not meta.startswith(":"):
            continue
        old_mode, new_mode, old_sha, new_sha, status = meta[1:].split(" ")
        out[path] = FileChange(path, status[0], old_mode, new_mode, old_sha, new_sha)
    num = await _m(mirror, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "-z", "--numstat", base, work)
    for entry in num.stdout.split(b"\0"):
        if not entry:
            continue
        parts = entry.decode("utf-8", errors="surrogateescape").split("\t", 2)
        if len(parts) != 3 or parts[2] not in out:
            continue
        ch = out[parts[2]]
        if parts[0] == "-":
            ch.binary = True
        else:
            ch.adds, ch.dels = int(parts[0]), int(parts[1])
    for ch in out.values():
        ch.risks = policy.risks(ch.path, ch.old_mode, ch.new_mode)
        ch.refused = policy.unsafe_path_reason(ch.path)
        if ch.refused is None and "submodule" in ch.risks:
            ch.refused = "submodule"
        current = host_blob(repo, ch.path)
        want = None if ch.status == "D" else ch.new_sha
        had = None if ch.old_sha == ZERO else ch.old_sha
        if current == want:
            ch.state = "applied"
        elif current != had:
            ch.state = "conflict"
    return sorted(out.values(), key=lambda c: c.path)


async def file_diff(mirror: Path, base: str, work: str, path: str) -> str:
    r = await _m(mirror, "diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--no-color", base, work, "--", path)
    return r.stdout.decode("utf-8", errors="replace")


@dataclass
class ApplyResult:
    applied: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)


async def apply(repo: Path, mirror: Path, base: str, work: str, paths: list[str]) -> ApplyResult:
    """Write the selected changed files into the user's repo.

    Each file is checked again right before it's written: the path must be
    safe, parent directories must not be symlinks, and the host's copy must
    still be what the cell started from (otherwise it's a conflict).
    """
    result = ApplyResult()
    current = {c.path: c for c in await changes(repo, mirror, base, work)}
    root = repo.resolve()
    for rel in paths:
        ch = current.get(rel)
        if ch is None:
            result.refused.append((rel, "not a changed file"))
            continue
        if ch.state == "applied":
            continue
        reason = ch.refused or policy.unsafe_path_reason(rel) or policy.check_parents(root, rel)
        if reason is None and ch.state == "conflict":
            reason = "changed on your machine since the session started"
        if reason is None and ch.new_mode == "120000":
            target = (await _m(mirror, "cat-file", "blob", ch.new_sha)).stdout.decode("utf-8", errors="replace")
            if policy.symlink_escapes(root, rel, target):
                reason = "symlink points outside the repository"
        if reason:
            result.refused.append((rel, reason))
            continue
        full = root / rel
        if ch.status == "D":
            if full.is_dir() and not full.is_symlink():
                result.refused.append((rel, "is a directory on your machine"))
                continue
            full.unlink(missing_ok=True)
            result.applied.append(rel)
            continue
        data = (await _m(mirror, "cat-file", "blob", ch.new_sha)).stdout
        if full.is_dir() and not full.is_symlink():
            result.refused.append((rel, "is a directory on your machine"))
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        tmp = full.parent / f".corral-{os.getpid()}-{full.name}.tmp"
        if ch.new_mode == "120000":
            os.symlink(data.decode(), tmp)
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            mode = 0o755 if ch.new_mode == "100755" else 0o644
            os.chmod(tmp, mode & ~_umask())
        os.replace(tmp, full)
        result.applied.append(rel)
    return result


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask
