"""Tests for _cli_bin() / _cli_bin_search_dirs() external-binary resolution
in delegate.py (T-946). No network, no real system binaries — every
executable used here is a temp file created by the test itself, and the
fallback-dir scan is redirected into tmp_path via HOME.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d

NAME = "fakecli"
ENV_VAR = "AI_ROUTER_FAKECLI_BIN"


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """No env override, which() misses, fallback dirs point into tmp_path."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(d.shutil, "which", lambda name: None)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_exec(path, executable=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho hi\n")
    os.chmod(path, 0o755 if executable else 0o644)
    return path


def test_env_override_wins_over_which(isolated_env, monkeypatch, tmp_path):
    override = _make_exec(tmp_path / "override" / NAME)
    monkeypatch.setenv(ENV_VAR, str(override))
    monkeypatch.setattr(d.shutil, "which", lambda name: "/some/other/path/" + name)
    assert d._cli_bin(NAME) == str(override)


def test_env_override_ignored_when_missing_file(isolated_env, monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "does" / "not" / "exist"))
    assert d._cli_bin(NAME) is None


def test_env_override_ignored_when_not_executable(isolated_env, monkeypatch, tmp_path):
    non_exec = _make_exec(tmp_path / "override" / NAME, executable=False)
    monkeypatch.setenv(ENV_VAR, str(non_exec))
    assert d._cli_bin(NAME) is None


def test_which_wins_over_fallback_dirs(isolated_env, monkeypatch, tmp_path):
    _make_exec(tmp_path / ".local" / "bin" / NAME)
    monkeypatch.setattr(d.shutil, "which", lambda name: "/usr/bin/" + name)
    assert d._cli_bin(NAME) == "/usr/bin/" + NAME


def test_found_in_fallback_dir(isolated_env, tmp_path):
    expected = _make_exec(tmp_path / ".local" / "bin" / NAME)
    assert d._cli_bin(NAME) == str(expected)


def test_present_but_not_executable_in_fallback_dir_not_found(isolated_env, tmp_path):
    _make_exec(tmp_path / ".local" / "bin" / NAME, executable=False)
    assert d._cli_bin(NAME) is None


def test_nothing_found_returns_none(isolated_env):
    assert d._cli_bin(NAME) is None


def test_search_dirs_are_the_resolver_own_list(isolated_env):
    """The reported list must BE the list the resolver walks, not a copy of it."""
    assert d._cli_bin_search_dirs() == [os.path.expanduser(x) for x in d._CLI_FALLBACK_DIRS]
    assert len(d._cli_bin_search_dirs()) == 5


def test_require_cli_bin_raises_naming_env_var_and_every_dir(isolated_env):
    """Assert on the error the production code raises, not on a locally built string."""
    with pytest.raises(d.ProviderError) as exc:
        d._require_cli_bin(NAME)
    err = exc.value
    assert err.model == NAME
    assert err.status == "NOT_FOUND"
    assert ENV_VAR in err.short_reason
    for dir_path in d._cli_bin_search_dirs():
        assert dir_path in err.short_reason


def test_require_cli_bin_returns_the_resolved_path(isolated_env, tmp_path):
    target = _make_exec(tmp_path / ".local" / "bin" / NAME)
    assert d._require_cli_bin(NAME) == str(target)


def test_directory_named_like_the_binary_is_not_a_hit(isolated_env, tmp_path):
    """A dir is executable in the os.access sense; only a file may be returned."""
    (tmp_path / ".local" / "bin" / NAME).mkdir(parents=True)
    assert d._cli_bin(NAME) is None


def test_channel_table_uses_cli_bin(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(d, "DATA_DIR", tmp_path)
    monkeypatch.delenv("AI_ROUTER_DISABLE_CHANNELS", raising=False)
    monkeypatch.setattr(
        d, "_cli_bin",
        lambda name: f"/fake/resolved/path/{name}" if name in ("agy", "codewhale", "codex", "copilot") else None,
    )
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    d.cmd_channels()
    out = capsys.readouterr().out
    assert "/fake/resolved/path/agy" in out
