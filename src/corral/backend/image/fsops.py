#!/usr/bin/env python3
"""File operations for the agent's tools, run inside the cell.

Reads one JSON request on stdin, writes one JSON response on stdout. Lives on
the cell's read-only root filesystem, so the agent can't change it. Its output
is still treated as untrusted by the gateway.
"""

import json
import os
import shutil
import subprocess
import sys

MAX_LINE = 2000
MAX_OUTPUT = 200_000


def _abs(path):
    return path if os.path.isabs(path) else os.path.join(os.getcwd(), path)


def _read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        raise ValueError(f"binary file ({len(data)} bytes)")
    return data.decode("utf-8", errors="replace")


def op_read(path, offset=1, limit=2000):
    text = _read_text(_abs(path))
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(offset))
    end = min(total, start + max(1, int(limit)) - 1)
    out = []
    size = 0
    for i in range(start, end + 1):
        line = lines[i - 1]
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + " …[line truncated]"
        entry = f"{i:>6}\t{line}"
        size += len(entry) + 1
        if size > MAX_OUTPUT:
            end = i - 1
            break
        out.append(entry)
    return {"content": "\n".join(out), "total_lines": total, "start": start, "end": end}


def op_write(path, content):
    full = _abs(path)
    existed = os.path.exists(full)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return {"created": not existed, "bytes": len(content.encode()), "lines": content.count("\n") + (0 if content.endswith("\n") or not content else 1)}


def op_edit(path, old_string, new_string, replace_all=False):
    full = _abs(path)
    text = _read_text(full)
    if old_string == "":
        raise ValueError("old_string is empty")
    count = text.count(old_string)
    if count == 0:
        raise ValueError("old_string not found in file")
    if count > 1 and not replace_all:
        raise ValueError(f"old_string appears {count} times; include more context or set replace_all")
    first = text.index(old_string)
    updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    with open(full, "w", encoding="utf-8", newline="") as f:
        f.write(updated)
    line = text.count("\n", 0, first) + 1
    new_lines = updated.splitlines()
    lo, hi = max(0, line - 3), min(len(new_lines), line + new_string.count("\n") + 3)
    snippet = "\n".join(f"{i + 1:>6}\t{new_lines[i][:MAX_LINE]}" for i in range(lo, hi))
    return {"replacements": count if replace_all else 1, "line": line, "snippet": snippet}


def _rg():
    return shutil.which("rg")


def op_list(path=".", pattern=None, limit=1000):
    full = _abs(path)
    if _rg():
        cmd = ["rg", "--files", "--hidden", "-g", "!.git", "--sort", "path"]
        if pattern:
            cmd += ["-g", pattern]
        cmd += ["--", full]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.splitlines()
    else:
        out = []
        for root, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d != ".git"]
            out += [os.path.join(root, f) for f in files]
    rel = [os.path.relpath(p, os.getcwd()) if not os.path.isabs(path) else p for p in out]
    return {"files": rel[:limit], "total": len(rel), "truncated": len(rel) > limit}


def op_search(pattern, path=".", glob=None, ignore_case=False, limit=200):
    if not _rg():
        raise ValueError("ripgrep is not installed in this cell")
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "--hidden", "-g", "!.git",
           "--max-columns", "300", "--max-columns-preview"]
    if ignore_case:
        cmd.append("-i")
    if glob:
        cmd += ["-g", glob]
    cmd += ["-e", pattern, "--", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode == 2:
        raise ValueError(proc.stderr.strip()[:500] or "search failed")
    lines = proc.stdout.splitlines()
    return {"matches": lines[:limit], "total": len(lines), "truncated": len(lines) > limit}


OPS = {"read": op_read, "write": op_write, "edit": op_edit, "list": op_list, "search": op_search}


def main():
    try:
        req = json.loads(sys.stdin.read())
        op = OPS[req.pop("op")]
        result = op(**req)
        sys.stdout.write(json.dumps({"ok": True, **result}))
    except FileNotFoundError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"no such file: {e.filename}"}))
    except IsADirectoryError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"is a directory: {e.filename}"}))
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    main()
