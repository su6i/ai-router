"""Hook sync across every Claude Code config dir (multi-account enforcement)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync_hooks  # noqa: E402

CANON = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "bash ~/.claude/hooks/a.sh"}]}]}


def test_merge_adds_then_is_idempotent():
    merged, added = sync_hooks.merge({}, CANON)
    assert len(added) == 1
    again, added2 = sync_hooks.merge(merged, CANON)
    assert added2 == []
    assert again == merged


def test_tilde_and_absolute_are_the_same_hook():
    home = str(Path.home())
    existing = {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"bash {home}/.claude/hooks/a.sh"}]}]}
    _merged, added = sync_hooks.merge(existing, CANON)
    assert added == [], "an absolute-path hook must not be re-added in its ~ form"


def test_hook_found_in_a_second_group_with_the_same_matcher():
    existing = {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "other"}]},
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash ~/.claude/hooks/a.sh"}]},
    ]}
    _merged, added = sync_hooks.merge(existing, CANON)
    assert added == []


def test_account_local_hooks_and_other_settings_survive(tmp_path):
    cfg = tmp_path / "claude-acc9"
    cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]},
    }))
    n, _added, err = sync_hooks.sync_dir(cfg, CANON, apply=True)
    assert (n, err) == (1, None)
    data = json.loads((cfg / "settings.json").read_text())
    assert data["model"] == "opus"
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine.sh"
    assert sync_hooks.sync_dir(cfg, CANON, apply=True)[0] == 0


def test_unparseable_settings_is_skipped_not_overwritten(tmp_path):
    cfg = tmp_path / "claude-broken"
    cfg.mkdir()
    (cfg / "settings.json").write_text("{not json")
    n, _added, err = sync_hooks.sync_dir(cfg, CANON, apply=True)
    assert n == 0 and "unparseable" in err
    assert (cfg / "settings.json").read_text() == "{not json"


def test_discovery_finds_every_config_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    (tmp_path / ".config" / "claude-acc2").mkdir(parents=True)
    (tmp_path / ".config" / "claude-acc2" / "projects").mkdir()
    (tmp_path / ".config" / "claude-notmine").mkdir()  # no marker -> not a config dir
    found = {p.name for p in sync_hooks.discover_config_dirs()}
    assert found == {".claude", "claude-acc2"}


def test_canonical_file_renders_and_covers_the_guard_hooks():
    rendered = json.dumps(sync_hooks.render_canonical())
    assert "{REPO}" not in rendered and "{HOME}" not in rendered
    for guard in ("code_lookup_gate.py", "delegate_nudge.py", "layer_guard.py",
                  "session_start_brief.py", "rag-session-ingest.sh"):
        assert guard in rendered, f"{guard} missing from the canonical hook set"
