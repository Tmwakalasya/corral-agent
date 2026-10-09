import json

from corral import audit
from corral.agent.tools import normalize_host, validate
from corral.backend import policy
from corral.text import sanitize, truncate_middle


def test_withheld_patterns():
    for p in [".env", "config/.env.production", "certs/server.pem", "id_ed25519", "deploy/.ssh/config",
              "service-account-prod.json", "infra/terraform.tfstate", ".npmrc"]:
        assert policy.is_withheld(p), p
    for p in [".env.example", "keys/id_ed25519.pub", "src/env.py", "README.md", "docs/key.md"]:
        assert not policy.is_withheld(p), p


def test_unsafe_paths():
    assert policy.unsafe_path_reason("../x") == "path traversal"
    assert policy.unsafe_path_reason("/etc/passwd") == "absolute path"
    for p in [".git/hooks/pre-commit", "sub/.GIT/config", ".git./x", ".g‌it/hooks/x", "a/git~1/x"]:
        assert policy.unsafe_path_reason(p) == "writes into .git", p
    assert policy.unsafe_path_reason("src/app.py") is None
    assert policy.unsafe_path_reason(".github/workflows/ci.yml") is None


def test_risk_labels():
    assert "CI pipeline" in policy.risks(".github/workflows/ci.yml", "000000", "100644")
    assert "build or install script" in policy.risks("package.json", "100644", "100644")
    assert "executable" in policy.risks("bin/run", "100644", "100755")
    assert "symlink" in policy.risks("link", "000000", "120000")
    assert "submodule" in policy.risks("vendor/x", "000000", "160000")
    assert policy.risks("src/app.py", "100644", "100644") == []


def test_symlink_escape(tmp_path):
    assert policy.symlink_escapes(tmp_path, "a/link", "../../etc")
    assert policy.symlink_escapes(tmp_path, "link", "/etc/passwd")
    assert policy.symlink_escapes(tmp_path, "link", ".git/config")
    assert not policy.symlink_escapes(tmp_path, "a/link", "../b/file")


def test_sanitize_strips_terminal_control():
    evil = "ok\x1b[2J\x1b]0;pwned\x07\x1b[31mred\x1b[0m\x07 and ‮evil\nstep 1\rprogress 100%\n"
    out = sanitize(evil)
    assert "\x1b" not in out and "\x07" not in out and "‮" not in out
    assert out.startswith("okred and evil\n")
    assert out.endswith("\nprogress 100%\n")


def test_truncate_middle():
    s = "a" * 100 + "b" * 100
    t = truncate_middle(s, 60)
    assert t.startswith("a" * 40) and t.endswith("b" * 20) and "omitted" in t


def test_audit_chain(tmp_path):
    p = tmp_path / "a.jsonl"
    log = audit.AuditLog(p)
    log.write("user", text="hi")
    log.write("tool", name="bash", input={"command": "ls"})
    # Reopen and continue the chain.
    audit.AuditLog(p).write("apply", applied=["x"])
    assert audit.verify(p).ok and audit.verify(p).records == 3
    lines = p.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["data"]["input"]["command"] = "rm -rf /"
    lines[1] = json.dumps(rec)
    p.write_text("\n".join(lines) + "\n")
    v = audit.verify(p)
    assert not v.ok and "modified" in v.error
    p.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not audit.verify(p).ok


def test_tool_input_validation():
    assert validate("bash", {"command": "ls"}) is None
    assert validate("bash", {}) == "missing required field 'command'"
    assert validate("bash", {"command": "ls", "timeout_s": True}) == "field 'timeout_s' must be integer"
    assert validate("bash", {"command": "ls", "extra": 1}) == "unexpected field 'extra'"
    assert validate("nope", {}) == "unknown tool 'nope'"
    assert validate("write_file", {"__invalid_json__": "{"}) == "input was not valid JSON"


def test_normalize_host():
    assert normalize_host("https://PyPI.org/simple/") == "pypi.org"
    assert normalize_host("*.npmjs.org") == "*.npmjs.org"
    assert normalize_host("localhost") is None
    assert normalize_host("evil.com; rm -rf /") is None
    assert normalize_host("a..b") is None
