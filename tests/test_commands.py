"""The CLI's commands end to end, offline: what a reader of the README runs first, with Token Factory, OSV.dev and the
package registries replaced by fakes."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from openport import cli, config, pricing
from openport.upgrade import osv, registry

ROOT = Path(__file__).resolve().parents[1]
SUPPORTDESK = str(ROOT / "examples" / "supportdesk")
AUTHKIT = str(ROOT / "examples" / "authkit")
MODELS = "nano=nvidia/nano-test,super=nvidia/super-test,ultra=nvidia/ultra-test"
runner = CliRunner()


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    """Runs land in a temporary folder, and no key is set, so nothing can reach a real service."""
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(pricing, "CACHE", tmp_path / "runs" / "prices.json")
    for key in ("NEBIUS_API_KEY", "TAVILY_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path / "runs"


def invoke(*args: str):
    return runner.invoke(cli.app, list(args), catch_exceptions=False)


def test_scan_finds_supportdesks_five_calls_and_saves_the_report(offline):
    result = invoke("scan", SUPPORTDESK)
    assert result.exit_code == 0, result.output
    saved = json.loads((offline / "scan-supportdesk.json").read_text(encoding="utf-8"))
    sites = {site["id"] for site in saved["findings"] if not site.get("in_tests")}
    assert sites == {"pii.extract_pii", "policy.review_case", "refunds.handle_refund_request", "replies.draft_reply",
                     "triage.triage_ticket"}
    assert "5 LLM calls" in result.output


def test_port_writes_a_copy_with_the_runtime_and_the_assigned_models(offline):
    result = invoke("port", SUPPORTDESK, "--models", MODELS, "--assign", "triage.triage_ticket=nano")
    assert result.exit_code == 0, result.output
    out = offline / "ports" / "supportdesk"
    assert (out / "supportdesk" / "openport_llm.py").exists()  # the runtime the Anthropic calls need
    assert '"nvidia/nano-test"' in (out / "supportdesk" / "triage.py").read_text(encoding="utf-8")
    assert '"nvidia/super-test"' in (out / "supportdesk" / "refunds.py").read_text(encoding="utf-8")  # the default tier
    diff = (offline / "port-supportdesk.diff").read_text(encoding="utf-8")
    assert "+++ b/supportdesk/triage.py" in diff
    original = (ROOT / "examples/supportdesk/supportdesk/triage.py").read_text(encoding="utf-8")
    assert "nvidia/nano-test" not in original  # the original is never modified


def test_port_refuses_a_call_site_that_doesnt_exist(offline):
    result = runner.invoke(cli.app, ["port", SUPPORTDESK, "--models", MODELS, "--assign", "nope.missing=nano"])
    assert result.exit_code == 1
    assert "No call site called nope.missing" in result.output


def test_port_refuses_an_unknown_tier(offline):
    result = runner.invoke(cli.app, ["port", SUPPORTDESK, "--models", MODELS, "--tier", "huge"])
    assert result.exit_code == 1
    assert "Unknown tier 'huge'" in result.output


def test_port_wont_overwrite_a_folder_it_didnt_make(offline, tmp_path):
    out = tmp_path / "mine"
    out.mkdir()
    (out / "notes.txt").write_text("someone's work", encoding="utf-8")
    result = runner.invoke(cli.app, ["port", SUPPORTDESK, "--models", MODELS, "--out", str(out)])
    assert result.exit_code == 1
    assert (out / "notes.txt").read_text(encoding="utf-8") == "someone's work"


def test_pr_writes_the_description_for_a_port(offline):
    assert invoke("port", SUPPORTDESK, "--models", MODELS).exit_code == 0
    prices = ["nvidia/super-test=0.10/0.50", "gpt-5-mini=0.25/2.00", "gpt-5=1.25/10.00", "claude-opus-5-5=5/25",
              "claude-sonnet-5-5=3/15"]
    result = invoke("pr", str(offline / "ports" / "supportdesk"), *[arg for price in prices for arg in ("--price", price)])
    assert result.exit_code == 0, result.output
    body = (offline / "pr-supportdesk.md").read_text(encoding="utf-8")
    assert body.startswith("## Move LLM calls to NVIDIA Nemotron on Nebius Token Factory")
    assert "`triage.triage_ticket`" in body and "nvidia/super-test" in body


def test_pr_needs_a_port_first(offline, tmp_path):
    result = runner.invoke(cli.app, ["pr", str(tmp_path)])
    assert result.exit_code == 1
    assert "run `openport port` first" in result.output


PYJWT_FLAW = {
    "id": "GHSA-ffqj-6fqr-9h24", "summary": "Key confusion through non-blocklisted public key formats",
    "aliases": ["CVE-2022-29217"],
    "affected": [{"package": {"ecosystem": "PyPI", "name": "pyjwt"},
                  "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "1.5.0"}, {"fixed": "2.4.0"}]}]}],
}


def test_upgrade_dry_run_lists_the_safe_version_and_changes_nothing(offline, monkeypatch):
    monkeypatch.setattr(osv, "_http_batch", lambda bodies: [["GHSA-ffqj-6fqr-9h24"] if b["package"]["name"] == "pyjwt" else []
                                                             for b in bodies])
    monkeypatch.setattr(osv, "_http_record", lambda vuln_id: PYJWT_FLAW)
    releases = {"1.7.1": "2018-12-06T00:00:00Z", "2.4.0": "2022-05-12T00:00:00Z", "2.5.0": "2022-09-14T00:00:00Z"}
    monkeypatch.setattr(registry, "_registry", lambda dep: {"releases": {v: [{"upload_time_iso_8601": t}] for v, t in releases.items()}})
    before = (ROOT / "examples/authkit/requirements.txt").read_text(encoding="utf-8")
    result = invoke("upgrade", AUTHKIT)
    assert result.exit_code == 0, result.output
    assert "1 vulnerable of 2 dependencies: 1 have a safe fix." in " ".join(result.output.split())
    assert "2.4.0" in result.output  # the earliest release that fixes it, long past the week's wait
    assert "Dry run" in result.output
    assert (ROOT / "examples/authkit/requirements.txt").read_text(encoding="utf-8") == before


def test_upgrade_says_so_when_nothing_is_vulnerable(offline, monkeypatch):
    monkeypatch.setattr(osv, "_http_batch", lambda bodies: [[] for _ in bodies])
    result = invoke("upgrade", AUTHKIT)
    assert result.exit_code == 0
    assert "No known vulnerabilities in 2 dependencies." in result.output


def test_upgrade_reports_an_unreachable_osv_as_a_failure_not_a_crash(offline, monkeypatch):
    def down(bodies):
        raise ConnectionError("osv.dev is down")

    monkeypatch.setattr(osv, "_http_batch", down)
    result = runner.invoke(cli.app, ["upgrade", AUTHKIT])
    assert result.exit_code == 1
    assert "Advisory lookup failed: ConnectionError: osv.dev is down" in result.output


def test_upgrade_open_needs_apply(offline):
    result = runner.invoke(cli.app, ["upgrade", AUTHKIT, "--open"])
    assert result.exit_code == 1
    assert "--open needs --apply" in result.output


def test_upgrade_without_lockfiles_says_so(offline, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["upgrade", str(tmp_path)])
    assert result.exit_code == 1
    assert "No lockfiles found" in result.output


def test_scan_prints_repo_text_literally_not_as_markup(offline, tmp_path):
    app = tmp_path / "app" / "[locale]"
    app.mkdir(parents=True)
    (app / "route.py").write_text(
        'from openai import OpenAI\n\nclient = OpenAI()\nMODELS = ["gpt-5"]\n\n\ndef answer(q):\n'
        '    return client.chat.completions.create(model=MODELS[0], messages=[{"role": "user", "content": q}])\n',
        encoding="utf-8")
    result = runner.invoke(cli.app, ["scan", str(tmp_path)], terminal_width=200)
    assert result.exit_code == 0, result.output
    assert "[locale]" in result.output and "MODELS[0]" in result.output
