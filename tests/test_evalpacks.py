"""Tests for eval specs that point at fetched repos: `repo:` roots, adapter entries, shared clients, rules grading."""

import json
import sys
from pathlib import Path

import pytest

from openport.evals import graders
from openport.evals.runner import run_site
from openport.evals.spec import load_spec

from .fakes import FakeTokenFactory, completion

PREFLIGHT_PACK = Path(__file__).parents[1] / "examples" / "evalpacks" / "preflight"
PREFLIGHT_CLONE = Path(__file__).parents[1] / "runs" / "repos" / "jsliapark__preflight"


def test_rules_grader():
    output = {"critical": 2, "comments": 3, "title_length": 80, "conventional": True}
    assert graders.rules(output, {"critical >=": 1, "conventional ==": True}).passed
    result = graders.rules(output, {"title_length <=": 72, "missing ==": 1})
    assert not result.passed and result.failures == ["title_length: got 80, wanted <= 72", "missing: got None, wanted == 1"]


def test_shared_client_and_adapter_entry(tmp_path, monkeypatch):
    """An app whose agents share one client from another module, evaluated through an adapter."""
    app = tmp_path / "app"
    (app / "core").mkdir(parents=True)
    (app / "core" / "__init__.py").write_text("")
    (app / "core" / "client.py").write_text("_client = None\n\ndef get_client():\n    return _client\n")
    (app / "agent.py").write_text(
        "from core.client import get_client\n\n"
        "def review(changes):\n"
        "    text = get_client().chat.completions.create(model='gpt-5', messages=[{'role': 'user', 'content': changes.text}]).choices[0].message.content\n"
        "    return {'comments': len(text.split())}\n"
    )
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "adapter.py").write_text("from types import SimpleNamespace\nfrom agent import review\n\ndef run(diff):\n    return review(SimpleNamespace(text=diff))\n")
    (pack / "cases.jsonl").write_text(json.dumps({"id": "c1", "input": {"diff": "x"}, "expected": {"comments >=": 2}}) + "\n")
    (pack / "openport.evals.yaml").write_text(
        f"target: app\nsource_root: {app.as_posix()}\nextra_paths: ['.']\ncall_sites:\n"
        "  - {id: review, file: agent.py, function: review, module: adapter, entry: run, client_module: core.client,\n"
        "     client_attr: _client, sdk: openai, original_model: gpt-5, output: json_in_text, dataset: cases.jsonl, grader: {type: rules}}\n"
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = load_spec(pack)
    site = spec.call_sites[0]
    assert (site.entry_function, site.client_home, site.function) == ("run", "core.client", "review")
    fake = FakeTokenFactory(lambda request: completion(content="two words"))
    result = run_site(spec, site, "nano", "nvidia/nano-test", client=fake)
    assert result.cases[0].passed and fake.requests[0]["model"] == "nvidia/nano-test"
    assert sys.modules["core.client"]._client is None  # put back after the run
    for name in ("adapter", "agent", "core", "core.client"):
        sys.modules.pop(name, None)


def test_repo_source_root_fetches_the_repo(tmp_path, monkeypatch):
    fetched = []
    monkeypatch.setattr("openport.workspace.fetch_repo", lambda name: fetched.append(name) or (tmp_path, False))
    (tmp_path / "openport.evals.yaml").write_text("target: x\nsource_root: repo:owner/name\ncall_sites: []\n")
    assert load_spec(tmp_path).source_root == tmp_path.resolve() and fetched == ["owner/name"]


def test_a_pinned_repo_is_fetched_at_exactly_that_commit(tmp_path, monkeypatch):
    from openport import config, workspace

    commit = "7a00fa2928acea9fcddce9661417e2bd6e6c1403"
    calls = []
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(workspace.subprocess, "run", lambda args, check: calls.append(args))
    root, fresh = workspace.fetch_repo(f"jsliapark/preflight@{commit}")
    assert fresh and root == tmp_path / "repos" / commit[:12] / "jsliapark__preflight" and root.is_dir()
    assert [args[3:] for args in calls] == [
        ["init", "--quiet"],
        ["config", "core.longpaths", "true"],  # paths longer than Windows' 260 characters still check out
        ["remote", "add", "origin", "https://github.com/jsliapark/preflight.git"],  # so `openport pr` can find the repo
        ["fetch", "--depth", "1", "--quiet", "origin", commit],
        ["checkout", "--quiet", "--detach", "FETCH_HEAD"],
    ]
    assert workspace.fetch_repo(f"jsliapark/preflight@{commit}") == (root, False)  # reused, not fetched again
    assert workspace.is_remote(f"https://github.com/jsliapark/preflight@{commit}")
    assert not workspace.is_remote("jsliapark/preflight@main")  # only a full commit hash pins


def test_a_clone_that_fails_halfway_is_never_reused(tmp_path, monkeypatch):
    """git can leave a half-checked-out folder behind (a path too long for Windows): it mustn't pass for a clone."""
    import subprocess

    from openport import config, workspace

    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)

    def failing_clone(args, check):
        Path(args[-1]).mkdir(parents=True)  # what git leaves: the folder, without all the files
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr(workspace.subprocess, "run", failing_clone)
    with pytest.raises(subprocess.CalledProcessError):
        workspace.fetch_repo("acme/huge")
    assert not (tmp_path / "repos" / "acme__huge").exists()
    monkeypatch.setattr(workspace.subprocess, "run", lambda args, check: Path(args[-1]).mkdir(parents=True))
    root, fresh = workspace.fetch_repo("acme/huge")  # the next try clones afresh
    assert fresh and root.is_dir() and not root.with_name("acme__huge.partial").exists()


def test_preflight_pack_is_pinned_to_the_reviewed_commit():
    from openport.server.pipeline import eval_pack_for

    pack = eval_pack_for("jsliapark/preflight")
    assert pack.folder == PREFLIGHT_PACK and pack.commit == "7a00fa2928acea9fcddce9661417e2bd6e6c1403"


def test_preflight_pack_labels():
    cases = {path.stem: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] for path in PREFLIGHT_PACK.glob("*.jsonl")}
    assert {k: len(v) for k, v in cases.items()} == {"review": 42, "standards": 25, "diff_analyzer": 26, "pr_description": 26}
    for name, rows in cases.items():
        assert len({r["id"] for r in rows}) == len(rows), name
    assert all(r["input"]["review_pass"] in ("security", "correctness") for r in cases["review"])
    assert all(r["expected"]["critical =="] == 0 for r in cases["standards"])  # preflight's rule: standards is never critical
    assert {r["expected"]["intent"] for r in cases["diff_analyzer"]} == {"feature", "bugfix", "refactor"}
    flagged = {r["id"].removeprefix("security-") for r in cases["review"] if r["expected"] == {"critical >=": 1}}
    assert flagged == {"sql_injection", "hardcoded_secret", "command_injection", "insecure_deserialization", "password_logged", "eval_input"}


@pytest.mark.skipif(not PREFLIGHT_CLONE.exists(), reason="needs the preflight clone (openport scan jsliapark/preflight)")
def test_preflight_pack_diffs_parse_with_preflights_parser(monkeypatch):
    monkeypatch.setattr(sys, "path", [str(PREFLIGHT_CLONE), *sys.path])
    monkeypatch.setattr("openport.workspace.fetch_repo", lambda target: (PREFLIGHT_CLONE, False))  # offline: the local clone
    from core.diff_parser import parse_diff

    for site in load_spec(PREFLIGHT_PACK).call_sites:
        for case in site.cases():
            changes = parse_diff(case["input"]["diff"])
            assert changes.files and all(f.hunks for f in changes.files), case["id"]
