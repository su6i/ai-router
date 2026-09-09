#!/usr/bin/env python3
"""sync_hooks.py — keep the global hook set installed in EVERY Claude Code config dir.

The failure this closes (measured 2026-09-09): Claude Code reads user settings
from ``$CLAUDE_CONFIG_DIR/settings.json``, not from ``~/.claude`` unconditionally.
A second account (``~/.config/claude-acc2``) therefore ran with an empty
``hooks`` block — no ``code_lookup`` gate, no ``delegate_nudge``, no
``layer_guard``, no RAG session brief, no instant RAG ingest — while the
primary account had all of them. Nothing was broken and nothing was logged;
the enforcement layer simply did not exist for that account, so agents fell
straight back to grep/cat.

Fix shape: one canonical hooks block in the repo (``hooks/settings.hooks.json``,
with ``{REPO}`` / ``{CLAUDE_HOME}`` / ``{HOME}`` tokens), and this script
merging it into every config dir that exists on the machine — including ones
created after this script was written, since the dirs are discovered by glob,
never listed by hand. Wired to LaunchAgent ``com.ai-router.hooks-sync``
(RunAtLoad + every 30 min), so a config dir born at 14:00 is enforced by 14:30
without anyone remembering it exists.

The merge is strictly additive: an entry is keyed by (event, matcher, command)
and only ever appended. An account-local hook this file does not know about is
never removed, and re-running changes nothing (idempotent).

Usage:
    python scripts/sync_hooks.py            # dry run: report drift, exit 1 if any
    python scripts/sync_hooks.py --apply    # write the missing entries
    python scripts/sync_hooks.py --print-plist
"""
import argparse
import glob
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CANONICAL = REPO / "hooks" / "settings.hooks.json"
PLIST_LABEL = "com.ai-router.hooks-sync"


def discover_config_dirs() -> list[Path]:
    """Every Claude Code config dir on this machine.

    A dir counts if it holds a ``settings.json`` or the runtime dirs Claude Code
    creates on first launch (``projects``/``sessions``/``history.jsonl``) — that
    keeps unrelated ``~/.config/claude-*`` directories (other tools) out while
    still catching a fresh account whose settings.json does not exist yet.
    """
    home = Path.home()
    candidates = [home / ".claude", *(Path(p) for p in glob.glob(str(home / ".config" / "claude*")))]
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    out, seen = [], set()
    for c in candidates:
        try:
            rc = c.resolve()
        except OSError:
            continue
        if rc in seen or not rc.is_dir():
            continue
        markers = ("settings.json", "projects", "sessions", "history.jsonl")
        if not any((rc / m).exists() for m in markers):
            continue
        seen.add(rc)
        out.append(rc)
    return sorted(out)


def render_canonical() -> dict:
    text = CANONICAL.read_text("utf-8")
    text = text.replace("{REPO}", str(REPO))
    text = text.replace("{CLAUDE_HOME}", str(Path.home() / ".claude"))
    text = text.replace("{HOME}", str(Path.home()))
    return json.loads(text)


def _norm_cmd(command: str | None) -> str:
    """Comparison key for a hook command.

    ``bash ~/.claude/hooks/x.sh`` and ``bash /Users/me/.claude/hooks/x.sh`` are
    the same hook to the shell but different strings; comparing raw strings
    would re-"install" every tilde-written hook on the primary account on every
    run, duplicating it. Expand ``~`` and collapse whitespace before comparing.
    """
    if not command:
        return ""
    return " ".join(command.replace("~/", str(Path.home()) + "/").split())


def _matcher_key(group: dict):
    """Missing matcher and an explicit "*" are different things to Claude Code
    (SessionEnd/Stop groups carry no matcher at all), so they must not collapse
    onto one key — otherwise the sync would consider a group "already there"
    when the runtime sees a different one."""
    return group["matcher"] if "matcher" in group else None


def merge(existing: dict, canonical: dict) -> tuple[dict, list[str]]:
    """Add every canonical (event, matcher, command) missing from `existing`.

    Returns the merged block and a list of human-readable additions. Never
    deletes, never reorders what is already there.
    """
    merged = json.loads(json.dumps(existing)) if existing else {}
    added = []

    for event, groups in canonical.items():
        merged.setdefault(event, [])
        for group in groups:
            key = _matcher_key(group)
            # One event can carry SEVERAL groups with the same matcher (the
            # primary account has two PostToolUse "*" groups). Presence must be
            # checked across all of them, or a hook living in the second group
            # looks missing and gets appended to the first one on every run.
            siblings = [g for g in merged[event] if _matcher_key(g) == key]
            if siblings:
                target = siblings[0]
            else:
                target = {k: v for k, v in group.items() if k != "hooks"}
                target["hooks"] = []
                merged[event].append(target)
                siblings = [target]
            have = {_norm_cmd(h.get("command")) for g in siblings for h in g.get("hooks", [])}
            for hook in group.get("hooks", []):
                if _norm_cmd(hook.get("command")) in have:
                    continue
                target.setdefault("hooks", []).append(json.loads(json.dumps(hook)))
                added.append(f"{event}[{key or '-'}] {hook.get('command', '')[:70]}")
    return merged, added


def sync_dir(cfg_dir: Path, canonical: dict, apply: bool) -> tuple[int, list[str], str | None]:
    """Returns (n_added, additions, error). A settings.json that does not parse
    is reported and skipped — never overwritten, since replacing an unreadable
    file would destroy account settings this script cannot see."""
    settings_path = cfg_dir / "settings.json"
    data = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return 0, [], f"unparseable settings.json: {e}"
        if not isinstance(data, dict):
            return 0, [], "settings.json is not a JSON object"

    merged_hooks, added = merge(data.get("hooks", {}), canonical)
    if not added:
        return 0, [], None

    if apply:
        if settings_path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak-hooks-{stamp}"))
        data["hooks"] = merged_hooks
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")
        os.replace(tmp, settings_path)

    return len(added), added, None


def print_plist() -> None:
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    print(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{uv}</string>
        <string>run</string>
        <string>--directory</string>
        <string>{REPO}</string>
        <string>python</string>
        <string>scripts/sync_hooks.py</string>
        <string>--apply</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>StandardOutPath</key>
    <string>/tmp/{PLIST_LABEL}.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/{PLIST_LABEL}.err</string>
</dict>
</plist>""")


def main() -> None:
    ap = argparse.ArgumentParser(description="Install the canonical hook set into every Claude Code config dir")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--print-plist", action="store_true", help="emit the LaunchAgent plist and exit")
    args = ap.parse_args()

    if args.print_plist:
        print_plist()
        return

    canonical = render_canonical()
    dirs = discover_config_dirs()
    if not dirs:
        print("no Claude Code config dir found", file=sys.stderr)
        sys.exit(2)

    drift = failed = 0
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    for cfg in dirs:
        n, added, err = sync_dir(cfg, canonical, args.apply)
        if err:
            failed += 1
            print(f"[{stamp}] {cfg}: SKIPPED — {err}", file=sys.stderr)
            continue
        if n == 0:
            print(f"[{stamp}] {cfg}: ok (hooks complete)")
            continue
        drift += n
        verb = "installed" if args.apply else "MISSING"
        print(f"[{stamp}] {cfg}: {verb} {n} hook(s)")
        for a in added:
            print(f"    - {a}")

    if failed:
        sys.exit(2)
    if drift and not args.apply:
        sys.exit(1)


if __name__ == "__main__":
    main()
