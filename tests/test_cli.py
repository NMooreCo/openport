"""Tests for the `openport` entry point."""

import pytest

from openport import cli


def test_windows_relaunches_in_utf8_mode(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_needs_utf8_relaunch", lambda: True)
    monkeypatch.setattr(cli.subprocess, "call", lambda args: calls.append(args) or 3)
    monkeypatch.setattr(cli.sys, "argv", ["openport", "tune", "examples/supportdesk"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 3  # the relaunched run's exit code comes through
    assert calls[0][1:] == ["-X", "utf8", "-m", "openport.cli", "tune", "examples/supportdesk"]


def test_no_relaunch_when_already_utf8(monkeypatch):
    ran = []
    monkeypatch.setattr(cli, "_needs_utf8_relaunch", lambda: False)
    monkeypatch.setattr(cli, "app", lambda **kwargs: ran.append(kwargs))
    cli.main()
    assert ran == [{"prog_name": "openport"}]  # help says `openport`, not `python -m openport.cli`
