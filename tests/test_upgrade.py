"""Tests for Phase 2 `openport upgrade`: reading installed versions from lockfiles."""

import json
from datetime import datetime, timedelta, timezone

from openport.upgrade import lockfiles, osv, plan_upgrades
from openport.upgrade.lockfiles import Dependency, read_lockfiles
from openport.upgrade.osv import Advisory, find_advisories, osv_name, parse_vuln, query_body
from openport.upgrade.registry import parse_npm_releases, parse_pypi_releases
from openport.upgrade.select import choose_target, fix_floor
from packaging.version import Version


def names(deps):
    return {(d.name, d.version) for d in deps}


def test_parse_requirements():
    text = "\n".join([
        "# a comment",
        "requests==2.31.0",
        "Django[argon2]==4.2.1 ; python_version >= '3.10'",
        "-r other.txt",
        "-e .",
        "git+https://github.com/x/y.git#egg=y",
        "flask>=2.0  # not an exact pin",
        "numpy==1.26.4  --hash=sha256:abc",
    ])
    deps = lockfiles.parse_requirements(text)
    assert names(deps) == {("requests", "2.31.0"), ("Django", "4.2.1"), ("numpy", "1.26.4")}
    assert all(d.ecosystem == "PyPI" for d in deps)


def test_parse_toml_lock():
    text = '[[package]]\nname = "requests"\nversion = "2.31.0"\n\n[[package]]\nname = "idna"\nversion = "3.6"\n'
    deps = lockfiles.parse_toml_lock(text, "uv.lock")
    assert names(deps) == {("requests", "2.31.0"), ("idna", "3.6")}
    assert deps[0].source == "uv.lock"


def test_parse_package_lock_v3():
    data = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "app", "version": "1.0.0"},
            "node_modules/axios": {"version": "1.6.0"},
            "node_modules/axios/node_modules/follow-redirects": {"version": "1.15.3"},
            "node_modules/@scope/pkg": {"version": "2.0.1"},
        },
    }
    deps = lockfiles.parse_package_lock(json.dumps(data))
    assert names(deps) == {("axios", "1.6.0"), ("follow-redirects", "1.15.3"), ("@scope/pkg", "2.0.1")}
    assert all(d.ecosystem == "npm" for d in deps)


def test_parse_package_lock_v1():
    data = {"lockfileVersion": 1, "dependencies": {"lodash": {"version": "4.17.20", "dependencies": {"nested": {"version": "1.0.0"}}}}}
    deps = lockfiles.parse_package_lock(json.dumps(data))
    assert names(deps) == {("lodash", "4.17.20"), ("nested", "1.0.0")}


def test_parse_pnpm_lock():
    text = "packages:\n"
    text += "  /axios@1.6.0:\n    dev: false\n"
    text += "  '/@scope/pkg@2.0.1(react@18.2.0)':\n    dev: false\n"
    text += "  /old-style/1.2.3:\n    dev: false\n"
    deps = lockfiles.parse_pnpm_lock(text)
    assert names(deps) == {("axios", "1.6.0"), ("@scope/pkg", "2.0.1"), ("old-style", "1.2.3")}


def test_read_lockfiles_dedup(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "requests"\nversion = "2.99.0"\n', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text(
        json.dumps({"packages": {"": {"name": "app"}, "node_modules/axios": {"version": "1.6.0"}}}), encoding="utf-8"
    )
    deps = read_lockfiles(tmp_path)
    # requests appears in both Python lockfiles but is kept once (first wins: requirements.txt);
    # axios comes from the npm lockfile.
    assert names(deps) == {("requests", "2.31.0"), ("axios", "1.6.0")}
    assert next(d for d in deps if d.name == "requests").source == "requirements.txt"


def test_read_lockfiles_empty(tmp_path):
    assert read_lockfiles(tmp_path) == []


# --- OSV.dev advisory lookup -------------------------------------------------

VULN = {
    "id": "GHSA-xxxx",
    "summary": "Requests leaks credentials",
    "aliases": ["CVE-2024-1234"],
    "affected": [
        {
            "package": {"ecosystem": "PyPI", "name": "requests"},
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "2.31.0"}]}],
        },
        {  # a different package in the same advisory must be ignored
            "package": {"ecosystem": "PyPI", "name": "urllib3"},
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "9.9.9"}]}],
        },
    ],
}


def test_osv_name_normalizes_pypi():
    assert osv_name(Dependency("Django", "4.2", "PyPI", "r")) == "django"
    assert osv_name(Dependency("typing_extensions", "4.0", "PyPI", "r")) == "typing-extensions"
    assert osv_name(Dependency("ruamel.yaml", "0.18", "PyPI", "r")) == "ruamel-yaml"
    assert osv_name(Dependency("@scope/Pkg", "1.0", "npm", "r")) == "@scope/Pkg"


def test_parse_vuln_extracts_fixed_versions_for_the_package():
    adv = parse_vuln(VULN, "PyPI", "requests")
    assert adv.id == "GHSA-xxxx"
    assert adv.fixed_versions == ["2.31.0"]  # urllib3's fix is not attributed to requests
    assert adv.aliases == ["CVE-2024-1234"]


def test_query_body_uses_normalized_name_and_version():
    assert query_body(Dependency("Flask", "2.0.1", "PyPI", "r")) == {
        "version": "2.0.1",
        "package": {"name": "flask", "ecosystem": "PyPI"},
    }


def test_find_advisories_returns_only_vulnerable_deps():
    deps = [
        Dependency("requests", "2.20.0", "PyPI", "r"),
        Dependency("safe-pkg", "1.0.0", "PyPI", "r"),
    ]

    def fake_fetch(body):
        return [VULN] if body["package"]["name"] == "requests" else []

    results = find_advisories(deps, fetch=fake_fetch)
    assert len(results) == 1
    dep, advisories = results[0]
    assert dep.name == "requests" and advisories[0].fixed_versions == ["2.31.0"]


# --- safe-version selection --------------------------------------------------

NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def days_ago(n):
    return NOW - timedelta(days=n)


def adv(*fixed):
    return Advisory(id="GHSA", summary="", fixed_versions=list(fixed))


def test_fix_floor_takes_max_of_each_advisorys_lowest_fix():
    assert fix_floor([adv("2.31.0"), adv("2.20.0", "2.25.0")]) == Version("2.31.0")


def test_fix_floor_none_when_an_advisory_has_no_fix():
    assert fix_floor([adv("2.31.0"), adv()]) is None


def test_choose_target_picks_earliest_old_enough_fix():
    releases = {
        "2.30.0": days_ago(90),  # below the fix floor
        "2.31.0": days_ago(60),  # the earliest fix that is old enough
        "2.32.0": days_ago(1),   # fixes, but too new to trust
        "3.0.0": days_ago(100),
    }
    target = choose_target("2.20.0", [adv("2.31.0")], releases, min_age_days=7, now=NOW)
    assert target is not None and target.version == "2.31.0" and target.age_days == 60


def test_choose_target_none_when_only_fix_is_too_new():
    target = choose_target("2.20.0", [adv("2.31.0")], {"2.31.0": days_ago(2)}, min_age_days=7, now=NOW)
    assert target is None


def test_choose_target_skips_prereleases():
    releases = {"2.31.0rc1": days_ago(90), "2.31.0": days_ago(30)}
    target = choose_target("2.20.0", [adv("2.31.0")], releases, min_age_days=7, now=NOW)
    assert target.version == "2.31.0"


def test_choose_target_none_when_no_fix_available():
    assert choose_target("2.20.0", [adv()], {"9.9.9": days_ago(90)}, now=NOW) is None


# --- registry release dates --------------------------------------------------

def test_parse_pypi_releases():
    data = {
        "releases": {
            "2.31.0": [{"upload_time_iso_8601": "2024-05-29T12:00:00.000000Z"}],
            "2.32.0": [],  # no files (fully yanked): skipped
        }
    }
    out = parse_pypi_releases(data)
    assert set(out) == {"2.31.0"} and out["2.31.0"].year == 2024


def test_parse_npm_releases():
    data = {"time": {"created": "2020-01-01T00:00:00Z", "modified": "2024-01-01T00:00:00Z", "1.6.0": "2023-10-01T00:00:00.000Z"}}
    out = parse_npm_releases(data)
    assert set(out) == {"1.6.0"} and out["1.6.0"].year == 2023


# --- plan_upgrades orchestration ---------------------------------------------

def test_plan_upgrades_reports_a_status_per_vulnerable_dep():
    requests = Dependency("requests", "2.20.0", "PyPI", "r")
    leftpad = Dependency("leftpad", "1.0.0", "npm", "r")
    nofix = Dependency("nofix", "1.0.0", "PyPI", "r")

    def find(_deps):
        return [(requests, [adv("2.31.0")]), (leftpad, [adv("2.0.0")]), (nofix, [adv()])]

    def releases_for(dep):
        return {
            "requests": {"2.31.0": days_ago(60)},
            "leftpad": {"2.0.0": days_ago(1)},  # fixes, but too new
        }.get(dep.name, {})

    plans = plan_upgrades([requests, leftpad, nofix], find=find, releases_for=releases_for, now=NOW)
    status = {p.dep.name: p.status for p in plans}
    assert status == {"requests": "upgrade", "leftpad": "held: fix too new", "nofix": "no fix available"}
    assert next(p for p in plans if p.dep.name == "requests").target.version == "2.31.0"
