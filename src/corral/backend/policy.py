"""What may cross the boundary from a cell back to the host, and what to flag.

Two kinds of output:

- ``refuse_reason`` — hard rules. A change that fails one is never applied.
- ``risks`` — labels shown in review for files that can run code on the
  user's machine or in CI once applied. They don't block; they make sure the
  reviewer looks.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import unicodedata
from pathlib import Path, PurePosixPath

# Files that never go into a cell. Matched against the basename and against
# each directory component.
WITHHELD_NAMES = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".git-credentials",
    ".htpasswd",
    "credentials.json",
    "service-account*.json",
    "*.tfstate",
    "*.tfstate.*",
]
WITHHELD_DIRS = {".ssh", ".aws", ".gnupg", ".docker", ".kube"}
# Templates people commit on purpose.
WITHHELD_EXCEPTIONS = [".env.example", ".env.sample", ".env.template", ".env.dist", "*.pub"]

_RISK_RULES: list[tuple[list[str], str]] = [
    (
        [".github/workflows/*", ".gitlab-ci.yml", ".circleci/*", "Jenkinsfile", "azure-pipelines.yml",
         ".buildkite/*", "bitbucket-pipelines.yml", ".travis.yml"],
        "CI pipeline",
    ),
    (
        [".husky/*", ".githooks/*", ".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml"],
        "git hook",
    ),
    (
        [".envrc", ".vscode/*", ".idea/*", ".devcontainer/*", "*.code-workspace", ".tool-versions", ".mise.toml"],
        "editor or shell config",
    ),
    (
        ["package.json", "setup.py", "setup.cfg", "pyproject.toml", "Makefile", "*.mk", "justfile", "Justfile",
         "build.rs", "build.gradle", "build.gradle.kts", "CMakeLists.txt", "Rakefile", "Gemfile", "conftest.py",
         "noxfile.py", "tox.ini", "Dockerfile", "docker-compose.yml", "compose.yaml"],
        "build or install script",
    ),
    (
        ["package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "Pipfile.lock",
         "Cargo.lock", "go.sum", "requirements*.txt", "Gemfile.lock", "bun.lock", "bun.lockb"],
        "dependency lockfile",
    ),
]

RISK_HELP = {
    "CI pipeline": "Runs in CI with your repository's secrets once pushed.",
    "git hook": "Runs on your machine during git commands.",
    "editor or shell config": "Can run code when the folder is opened in an editor or shell.",
    "build or install script": "Can run code on your machine when you build, install or test.",
    "dependency lockfile": "Check that package sources and hashes are ones you expect.",
    "executable": "New or changed executable bit.",
    "symlink": "Symbolic link. Applied only if it points inside the repository.",
    "secret file path": "This path matches a secrets pattern and was withheld from the cell.",
    "submodule": "Submodule changes are never applied.",
}

_IGNORABLE = {
    "​", "‌", "‍", "‎", "‏", "‪", "‫", "‬", "‭", "‮",
    "⁪", "⁫", "⁬", "⁭", "⁮", "⁯", "﻿",
}


def _matches(path: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p) for p in patterns)


def is_withheld(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if any(part in WITHHELD_DIRS for part in parts[:-1]):
        return True
    if _matches(path, WITHHELD_EXCEPTIONS):
        return False
    return _matches(path, WITHHELD_NAMES)


def risks(path: str, old_mode: str, new_mode: str) -> list[str]:
    out = [label for patterns, label in _RISK_RULES if _matches(path, patterns)]
    if new_mode == "100755" and old_mode != "100755":
        out.append("executable")
    if new_mode == "120000":
        out.append("symlink")
    if new_mode == "160000" or old_mode == "160000":
        out.append("submodule")
    if is_withheld(path):
        out.append("secret file path")
    return out


def _is_dotgit(component: str) -> bool:
    # Case-insensitive file systems, trailing dots/spaces (NTFS) and
    # ignorable code points (HFS+) can all make a name act like ".git".
    cleaned = "".join(ch for ch in unicodedata.normalize("NFC", component) if ch not in _IGNORABLE)
    cleaned = cleaned.rstrip(". ").lower()
    return cleaned == ".git" or cleaned.startswith("git~")


def unsafe_path_reason(rel: str) -> str | None:
    if not rel or "\x00" in rel or "\\" in rel:
        return "invalid path"
    p = PurePosixPath(rel)
    if p.is_absolute():
        return "absolute path"
    for part in p.parts:
        if part in ("", ".", ".."):
            return "path traversal"
        if _is_dotgit(part):
            return "writes into .git"
    return None


def check_parents(root: Path, rel: str) -> str | None:
    """Refuse to write through any existing symlinked directory under root."""
    cur = root
    for part in PurePosixPath(rel).parts[:-1]:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return None  # the rest will be created fresh
        if stat.S_ISLNK(st.st_mode):
            return f"parent directory {cur.relative_to(root)} is a symlink"
        if not stat.S_ISDIR(st.st_mode):
            return f"{cur.relative_to(root)} is not a directory"
    return None


def symlink_escapes(root: Path, rel: str, target: str) -> bool:
    if os.path.isabs(target):
        return True
    resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
    return resolved.startswith("..") or resolved == "." or _is_dotgit(PurePosixPath(resolved).parts[0])
