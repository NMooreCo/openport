"""Tests for Phase 2 `openport upgrade`: reading installed versions from lockfiles."""

import json

from openport.upgrade import lockfiles
from openport.upgrade.lockfiles import Dependency, read_lockfiles


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
