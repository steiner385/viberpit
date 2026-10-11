import pytest

from server.permission_rules.patterns import (
    PatternError,
    describe,
    matches_any,
    parse,
    suggest,
)

CWD = "/w/repo"


@pytest.mark.parametrize("pat,tool,inp,ok", [
    ("Bash(npm test)", "Bash", {"command": "npm  test"}, True),
    ("Bash(npm test)", "Bash", {"command": "npm test --watch"}, False),
    ("Bash(npm test:*)", "Bash", {"command": "npm test --watch"}, True),
    ("Bash(npm test:*)", "Bash", {"command": "npm testing"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm run build"}, True),
    ("Bash(npm *)", "Bash", {"command": "npx foo"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test; rm -rf ~"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test && curl x | sh"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test $(whoami)"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test `id`"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test > ~/.bashrc"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test 2>&1"}, True),
    ("Bash(npm *)", "Bash", {"command": "npm test <(id)"}, False),
    ("Bash(npm [ab]*)", "Bash", {"command": "npm a"}, False),
    ("Bash(npm ?*)", "Bash", {"command": "npm x"}, False),
    ("Edit(src/**)", "Edit", {"file_path": "/w/repo/src/a/b.ts"}, True),
    ("Edit(src/**)", "Write", {"file_path": "/w/repo/src/x.ts"}, True),
    ("Edit(src/**)", "Edit", {"file_path": "src/x.ts"}, True),
    ("Edit(src/**)", "Edit", {"file_path": "/etc/passwd"}, False),
    ("Edit(src/**)", "Edit", {"file_path": "/w/repo/../other/src/x"}, False),
    ("Edit(src/*.ts)", "Edit", {"file_path": "/w/repo/src/a/b.ts"}, False),
    ("Edit(**/*.md)", "Edit", {"file_path": "/w/repo/README.md"}, True),
    ("Write(src/**)", "Edit", {"file_path": "/w/repo/src/x"}, False),
    ("Read(docs/**)", "Read", {"file_path": "/w/repo/docs/a.md"}, True),
    ("Read(docs/**)", "Edit", {"file_path": "/w/repo/docs/a.md"}, False),
    ("Edit", "Write", {"file_path": "/w/repo/x"}, True),
    ("Edit", "Write", {"file_path": "/etc/x"}, False),
    ("WebFetch(domain:example.com)", "WebFetch", {"url": "https://api.example.com/x"}, True),
    ("WebFetch(domain:example.com)", "WebFetch", {"url": "https://evilexample.com/"}, False),
    ("mcp__gh__create_issue", "mcp__gh__create_issue", {}, True),
    ("mcp__gh", "mcp__gh__create_issue", {}, True),
    ("mcp__gh", "mcp__ghx__create_issue", {}, False),
    ("WebSearch", "WebSearch", {"query": "x"}, True),
])
def test_matches(pat, tool, inp, ok):
    assert (matches_any([parse(pat)], tool, inp, CWD) is not None) is ok


def test_compound_needs_every_segment_covered():
    pats = [parse("Bash(npm test:*)"), parse("Bash(tee:*)")]
    assert matches_any(pats, "Bash", {"command": "npm test 2>&1 | tee log"}, CWD) is not None
    assert matches_any(pats[:1], "Bash", {"command": "npm test | tee log"}, CWD) is None


def test_compound_returns_pattern_covering_first_segment():
    pats = [parse("Bash(tee:*)"), parse("Bash(npm test:*)")]
    assert matches_any(pats, "Bash", {"command": "npm test | tee log"}, CWD) == pats[1]


def test_path_rule_without_cwd_never_matches():
    assert matches_any([parse("Edit(src/**)")], "Edit", {"file_path": "/w/repo/src/x"}, None) is None
    assert matches_any([parse("Edit(src/**)")], "Edit", {"file_path": "src/x"}, None) is None


@pytest.mark.parametrize("bad", ["", "Bash(", "Bash()", "Foo(bar)", "WebFetch(example.com)", "1Bash"])
def test_parse_rejects(bad):
    with pytest.raises(PatternError):
        parse(bad)


def test_parse_kinds():
    assert parse("Bash").kind == "bare"
    assert parse("mcp__a").kind == "mcp"
    assert parse("Bash(a  b:*)").spec == "a b"
    assert parse("Bash(a *)").kind == "glob"
    assert parse("Bash(a b)").kind == "exact"


def test_suggest_bash():
    assert suggest("Bash", {"command": "npm test --watch"}, CWD) == [
        "Bash(npm test --watch)", "Bash(npm test:*)", "Bash(npm:*)"]


def test_suggest_bash_single_word_and_compound():
    assert suggest("Bash", {"command": "ls"}, CWD) == ["Bash(ls)", "Bash(ls:*)"]
    assert suggest("Bash", {"command": "npm test && ls"}, CWD) == []


def test_suggest_edit():
    assert suggest("Write", {"file_path": "/w/repo/src/a/b.ts"}, CWD) == [
        "Edit(src/a/b.ts)", "Edit(src/a/**)", "Edit(src/**)"]


def test_suggest_outside_cwd_is_empty():
    assert suggest("Edit", {"file_path": "/etc/x"}, CWD) == []


def test_suggest_mcp_and_web():
    assert suggest("mcp__gh__create_issue", {}, CWD) == ["mcp__gh__create_issue", "mcp__gh"]
    assert suggest("WebFetch", {"url": "https://docs.python.org/3/"}, CWD) == ["WebFetch(domain:docs.python.org)"]
    assert suggest("ExitPlanMode", {}, CWD) == []
    assert suggest("AskUserQuestion", {}, CWD) == []
    assert suggest("WebSearch", {}, CWD) == ["WebSearch"]


def test_describe():
    assert describe("Bash(npm test:*)") == "Any Bash command that is `npm test` or starts with `npm test `"
    assert describe("Edit(src/**)") == "File edits/writes under src/ in the session's worktree"
    assert describe("mcp__s") == "Every tool on the MCP server s"
    assert describe("Bash(npm *)") == "Any Bash command matching `npm *` (`*` = anything)"
    assert describe("Bash(npm test)") == "Exactly the Bash command `npm test`"
    assert describe("Edit(src/*.ts)") == "File edits/writes matching src/*.ts in the session's worktree"
    assert describe("Read(x)") == "File reads matching x in the session's worktree"
    assert describe("WebFetch(domain:example.com)") == "Web fetches from example.com and its subdomains"
    assert describe("mcp__s__t") == "The MCP tool mcp__s__t"
    assert describe("WebSearch") == "Every WebSearch call"


@pytest.mark.parametrize("cmd", [
    "npm test\nrm -rf ~",
    "npm test & rm -rf ~",
    "npm test |& tee x",
    "npm test\r\nrm x",
    "npm test >/dev/nullx",
    "npm test 2>&12",
])
def test_bash_separator_and_redirect_bypasses_rejected(cmd):
    assert matches_any([parse("Bash(npm test:*)")], "Bash", {"command": cmd}, CWD) is None


def test_pipe_ampersand_covered_when_both_present():
    pats = [parse("Bash(npm test:*)"), parse("Bash(tee:*)")]
    assert matches_any(pats, "Bash", {"command": "npm test |& tee x"}, CWD) is not None
    assert matches_any(pats, "Bash", {"command": "npm test\ntee x"}, CWD) is not None


def test_redirect_then_chain_needs_both_covered():
    cmd = {"command": "npm test 2>&1 && npm run lint"}
    assert matches_any([parse("Bash(npm test:*)")], "Bash", cmd, CWD) is None
    both = [parse("Bash(npm test:*)"), parse("Bash(npm run lint)")]
    assert matches_any(both, "Bash", cmd, CWD) is not None


@pytest.mark.parametrize("cmd", ["echo $(id)", "echo `id`", "cat <(id)", "echo hi > f", "a & b >x"])
def test_suggest_unmatchable_bash_is_empty(cmd):
    assert suggest("Bash", {"command": cmd}, CWD) == []


def test_symlink_escape_does_not_match(tmp_path):
    wt = tmp_path / "wt"
    outside = tmp_path / "outside"
    (wt / "src").mkdir(parents=True)
    outside.mkdir()
    (wt / "src" / "link").symlink_to(outside)
    pats = [parse("Edit(src/**)")]
    esc = {"file_path": str(wt / "src" / "link" / "authorized_keys")}
    assert matches_any(pats, "Edit", esc, str(wt)) is None
    assert suggest("Edit", esc, str(wt)) == []


def test_nonexistent_file_under_real_dir_matches(tmp_path):
    (tmp_path / "src").mkdir()
    inp = {"file_path": str(tmp_path / "src" / "new" / "x.ts")}
    assert matches_any([parse("Edit(src/**)")], "Write", inp, str(tmp_path)) is not None


def test_read_rules_never_match_outside_worktree():
    for pat in ("Read", "Read(**)", "Read(docs/**)"):
        assert matches_any([parse(pat)], "Read", {"file_path": "/home/u/.ssh/id_rsa"}, CWD) is None
    assert matches_any([parse("Read")], "Read", {"file_path": "/w/repo/a"}, CWD) is not None


def test_suggest_multi_segment():
    assert suggest("Bash", {"command": "npm test && npm run lint"}, CWD) == ["Bash(npm:*)"]
    assert suggest("Bash", {"command": "npm test && make"}, CWD) == []


@pytest.mark.parametrize("tool,inp", [
    ("Bash", {"command": "npm test --watch"}),
    ("Bash", {"command": "ls"}),
    ("Bash", {"command": "npm test 2>&1 && npm run lint"}),
    ("Bash", {"command": "git status && git diff"}),
    ("Write", {"file_path": "/w/repo/src/a/b.ts"}),
    ("Read", {"file_path": "docs/a.md"}),
    ("WebFetch", {"url": "https://docs.python.org/3/"}),
    ("mcp__gh__create_issue", {}),
    ("WebSearch", {"query": "x"}),
])
def test_every_suggestion_covers_its_request(tool, inp):
    sugg = suggest(tool, inp, CWD)
    assert sugg
    for c in sugg:
        assert matches_any([parse(c)], tool, inp, CWD) is not None


@pytest.mark.parametrize(
    "tool,pat,rel",
    [
        ("Edit", "Edit(**)", ".git/hooks/pre-commit"),
        ("Edit", "Edit(**)", ".claude/settings.json"),
        ("Edit", "Edit(**)", ".env"),
        ("Read", "Read(**)", ".aws/credentials"),
        ("Edit", "Edit", "a/.env.local"),
        ("Edit", "Edit(**)", "certs/x.pem"),
        ("Edit", "Edit(**)", ".GIT/hooks/x"),
        ("Edit", "Edit(**)", ".Claude/settings.json"),
        ("Edit", "Edit(**)", ".ENV"),
        ("Edit", "Edit(**)", ".git"),
        ("Edit", "Edit(**)", "a/.git/hooks/pre-commit"),
        ("Edit", "Edit(**)", "pkg/.husky/x"),
        ("Edit", "Edit(**)", "x/.vscode/settings.json"),
        ("Edit", "Edit(**)", "x/.idea/y"),
        ("Read", "Read(**)", "x/.aws/credentials"),
        ("Edit", "Edit(**)", "x/.gnupg/y"),
        ("Edit", "Edit(**)", "x/.mcp.json"),
        ("Edit", "Edit(**)", "x/.netrc"),
        ("Edit", "Edit(**)", "x/.pypirc"),
        ("Edit", "Edit(**)", "x/.ssh/y"),
        ("Edit", "Edit(**)", "sub/.git"),
    ],
)
def test_protected_paths_never_match(tmp_path, tool, pat, rel):
    cwd = str(tmp_path)
    assert matches_any([parse(pat)], tool, {"file_path": f"{cwd}/{rel}"}, cwd) is None
    assert matches_any([parse(pat)], tool, {"file_path": f"{cwd}/src/ok.py"}, cwd) is not None
