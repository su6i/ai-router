"""
dashboards.py — Telegram dashboards generator for ai-router

This module implements two pinned, edit-in-place Telegram dashboards:
1. Inbox digest (aggregates unread notes across all project inboxes)
2. Open tasks digest (parses QUEUE.md for open tasks)

It handles rendering the content, applying the 4096-char Telegram limits,
and pushing/editing the messages via the Telegram Bot API.
"""
import sys
import os
import json
import hashlib
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delegate as d
import httpx
import rag_ingest

def _state_file() -> Path:
    """Resolved on every call (never cached at import time) so tests can
    isolate it by monkeypatching delegate.DATA_DIR, same lever used by
    _rag_freshness_line()."""
    return d.DATA_DIR / "telegram_dashboards.json"


def _load_state() -> dict:
    try:
        f = _state_file()
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


def _save_state(state: dict) -> None:
    f = _state_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _escape_html(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"


def _age_str(iso_timestamp: str) -> str:
    try:
        ts = dt.datetime.fromisoformat(iso_timestamp)
        now = dt.datetime.now().astimezone()
        delta = now - ts
        seconds = delta.total_seconds()
        if seconds < 3600:
            return f"{int(seconds // 60)}m"
        elif seconds < 86400:
            return f"{int(seconds // 3600)}h"
        else:
            return f"{int(seconds // 86400)}d"
    except Exception:  # noqa: BLE001
        return "?"


# NOTE: _rag_freshness_line is for the audit log ingest. Do not confuse it with
# the newer RAG semantic index ingest (_rag_index_freshness_line).
def _rag_index_freshness_line() -> str:
    try:
        fresh = rag_ingest.rag_freshness()
        parts = []
        for coll, label, key in (("rules", "docs", "docs"), ("sessions", "chunks", "chunks"), ("code", "chunks", "chunks")):
            info = fresh.get(coll, {})
            last_ingest = info.get("last_ingest")
            stale = info.get("stale", True)
            count = info.get(key, 0)
            
            if last_ingest is None:
                token = "⚠️ never run"
            else:
                age = _age_str(last_ingest)
                token = f"⚠️ stale {age}" if stale else f"{age} ago"
                
            parts.append(f"{coll} {token} · {count} {label}")
        return "🧠 RAG: " + " | ".join(parts)
    except Exception:  # noqa: BLE001
        return "🧠 RAG: unknown"


def _rag_freshness_line() -> str:
    try:
        f = d.DATA_DIR / "last_ingest.json"
        if not f.exists():
            return "RAG ingest: ⚠️ never run"
        data = json.loads(f.read_text(encoding="utf-8"))
        ts_str = data.get("ts", "")
        rows = data.get("rows", 0)
        age = _age_str(ts_str)
        line = f"RAG ingest: {age} ago ({rows} rows)"
        try:
            ts = dt.datetime.fromisoformat(ts_str)
            if (dt.datetime.now().astimezone() - ts).total_seconds() > 86400:
                line += " ⚠️ stale"
        except Exception:  # noqa: BLE001
            pass
        return line
    except Exception:  # noqa: BLE001
        return "RAG ingest: ⚠️ never run"


def _cap_telegram_length(header: str, item_blocks: list[str], footer: str, more_label: str) -> str:
    body = ""
    omitted = 0
    for i, block in enumerate(item_blocks):
        test_body = body + block
        left = len(item_blocks) - i - 1
        more_str = f"\n… +{left} more (see {more_label})" if left > 0 else ""
        if len(header + test_body + more_str + "\n\n" + footer) <= 4096:
            body = test_body
        else:
            omitted = len(item_blocks) - i
            break
            
    if omitted > 0:
        body += f"\n… +{omitted} more (see {more_label})"
        
    return header + body + "\n\n" + footer


def render_inbox() -> str:
    total_unread = 0
    item_blocks = []
    if d.AGENT_PROJECTS.exists() and d.AGENT_PROJECTS.is_dir():
        for p in sorted(d.AGENT_PROJECTS.iterdir()):
            if not p.is_dir():
                continue
            project_name = p.name
            try:
                notes = d.list_notes(project_name, unread_only=True, peek=True)
            except Exception:  # noqa: BLE001
                continue
                
            if not notes:
                continue
                
            total_unread += len(notes)
            
            first_note = True
            for f_path, meta, body in notes:
                prio = meta.get("priority")
                if prio == "high":
                    icon = "🔴"
                elif prio == "low":
                    icon = "·"
                else:
                    icon = "▪️"
                    
                sender = _escape_html(meta.get("from", "unknown"))
                
                subject = meta.get("subject", "").strip()
                if not subject:
                    lines = body.strip().split("\n")
                    subject = lines[0] if lines else ""
                subject = _escape_html(_truncate(subject, 60))
                
                age = _age_str(meta.get("created", ""))
                
                line = f"{icon} {subject} — {sender} · {age}\n"
                if first_note:
                    item_blocks.append(f"\n<b>{_escape_html(project_name)}</b>\n" + line)
                    first_note = False
                else:
                    item_blocks.append(line)

    now_str = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    header = f"📥 <b>Inbox</b>: {total_unread} unread ({now_str})\n"
    
    if total_unread == 0:
        item_blocks = ["\nno unread notes\n"]
        
    footer = _rag_freshness_line()
    raw = _cap_telegram_length(header, item_blocks, footer, "inbox")
    return d._redact(raw)


def render_tasks() -> str:
    queue_file = d.AGENT_PROJECTS / "_memory" / "QUEUE.md"
    now_str = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    header = f"📋 <b>Open Tasks</b> ({now_str})\n"
    footer = _rag_freshness_line() + "\n" + _rag_index_freshness_line()
    
    if not queue_file.exists():
        raw = f"📋 Open Tasks\n\nQUEUE.md not found.\n\n{footer}"
        return d._redact(raw)

    lines = queue_file.read_text(encoding="utf-8").split("\n")
    start_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("## ") and "ترتیبِ اجرا" in line:
            start_idx = i + 1
            break
            
    if start_idx == -1:
        raw = f"{header}\nno open tasks\n\n{footer}"
        return d._redact(raw)

    end_idx = len(lines)
    for i in range(start_idx, len(lines)):
        if lines[i].startswith("## "):
            end_idx = i
            break
            
    section_lines = lines[start_idx:end_idx]
    
    item_blocks = []
    row_count = 0
    valid_task_count = 0
    for line in section_lines:
        if line.startswith("|"):
            row_count += 1
            stripped_chars = set(line.strip().replace("|", "").replace(" ", ""))
            if stripped_chars and stripped_chars.issubset({"-", ":"}):
                continue
                
            parts = line.split("|")
            if len(parts) >= 2 and parts[0].strip() == "":
                parts = parts[1:]
            if len(parts) >= 1 and parts[-1].strip() == "":
                parts = parts[:-1]
                
            cells = [p.strip().replace("**", "") for p in parts]
            if "Tier" in cells or "#" in cells:
                continue
                
            if len(cells) < 5:
                item_blocks.append(f"\n⚠️ QUEUE.md row {row_count} unparsed\n")
                continue
                
            num = cells[0]
            tier = cells[1]
            repo = cells[2]
            status_blocker = cells[4]
            
            if "✅" in tier:
                continue
                
            valid_task_count += 1
            l1 = f"#{_escape_html(num)} · {_escape_html(tier)} · {_escape_html(repo)}"
            l2 = _escape_html(_truncate(status_blocker, 90))
            item_blocks.append(f"\n{l1}\n{l2}\n")

    if valid_task_count == 0:
        item_blocks = ["\nno open tasks\n"]
        
    raw = _cap_telegram_length(header, item_blocks, footer, "QUEUE.md")
    return d._redact(raw)


def push_dashboard(kind: str) -> str:
    if kind not in ("inbox", "tasks"):
        raise ValueError(f"invalid kind: {kind}")
        
    text = render_inbox() if kind == "inbox" else render_tasks()
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    
    state = _load_state()
    entry = state.get(kind)
    
    if entry and entry.get("hash") == h:
        return f"unchanged (message_id={entry['message_id']})"
        
    token = os.environ.get("AI_ROUTER_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("AI_ROUTER_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not in env")
        
    def _raise_api_err(prefix, desc):
        desc_redacted = d._redact(str(desc)).replace(token, "<redacted>")
        raise RuntimeError(f"{prefix}: {desc_redacted}")
        
    if not entry:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            resp = httpx.post(url, json=payload, timeout=30.0)
        except Exception as e:  # noqa: BLE001
            _raise_api_err("sendMessage network error", str(e))
            
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            _raise_api_err("sendMessage failed", f"HTTP {resp.status_code} (non-JSON response)")
            
        msg_id = data.get("result", {}).get("message_id")
        if not data.get("ok") or msg_id is None:
            _raise_api_err("Telegram refused sendMessage", data.get("description", "no description"))
            
        pin_url = f"https://api.telegram.org/bot{token}/pinChatMessage"
        pin_payload = {
            "chat_id": chat_id,
            "message_id": msg_id,
            "disable_notification": True
        }
        try:
            pin_resp = httpx.post(pin_url, json=pin_payload, timeout=30.0)
            try:
                pin_data = pin_resp.json()
                if not pin_data.get("ok"):
                    d.logger.warning(f"Telegram pinChatMessage failed: {d._redact(pin_data.get('description', ''))}")
            except Exception:  # noqa: BLE001
                d.logger.warning(f"Telegram pinChatMessage failed with HTTP {pin_resp.status_code}")
        except Exception as e:  # noqa: BLE001
            d.logger.warning(f"Telegram pinChatMessage network error: {d._redact(str(e))}")
            
        state[kind] = {"message_id": msg_id, "hash": h}
        _save_state(state)
        return f"created (message_id={msg_id})"
        
    else:
        url = f"https://api.telegram.org/bot{token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": entry["message_id"],
            "text": text,
            "parse_mode": "HTML"
        }
        try:
            resp = httpx.post(url, json=payload, timeout=30.0)
        except Exception as e:  # noqa: BLE001
            _raise_api_err("editMessageText network error", str(e))
            
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            _raise_api_err("editMessageText failed", f"HTTP {resp.status_code} (non-JSON response)")
            
        if data.get("ok"):
            state[kind]["hash"] = h
            _save_state(state)
            return f"edited (message_id={entry['message_id']})"
            
        desc = data.get("description", "").lower()
        if "message is not modified" in desc:
            state[kind]["hash"] = h
            _save_state(state)
            return f"unchanged (message_id={entry['message_id']})"
            
        if "message to edit not found" in desc or "message can't be edited" in desc:
            del state[kind]
            _save_state(state)
            result = push_dashboard(kind)
            return result.replace("created", "recreated", 1)
            
        _raise_api_err("Telegram refused editMessageText", data.get("description", "no description"))

def send_note_ping(text: str) -> str:
    token = os.environ.get("AI_ROUTER_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
    if not token or not chat_id:
        raise ValueError("AI_ROUTER_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not in env")

    def _raise_api_err(prefix, desc):
        desc_redacted = d._redact(str(desc)).replace(token, "<redacted>")
        raise RuntimeError(f"{prefix}: {desc_redacted}")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": d._redact(text)
    }
    
    try:
        resp = httpx.post(url, json=payload, timeout=30.0)
    except Exception as e:  # noqa: BLE001
        _raise_api_err("sendMessage network error", str(e))
        
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        _raise_api_err("sendMessage failed", f"HTTP {resp.status_code} (non-JSON response)")
        
    msg_id = data.get("result", {}).get("message_id")
    if not data.get("ok") or msg_id is None:
        _raise_api_err("Telegram refused sendMessage", data.get("description", "no description"))
        
    return f"message_id={msg_id}"


def send_note_ping_deduped(text: str, dedupe_key: str, window_seconds: int = 900) -> str:
    state = _load_state()
    pings = state.get("note_pings", {})
    now = dt.datetime.now().astimezone()

    to_delete = []
    for k, v in pings.items():
        try:
            ts = dt.datetime.fromisoformat(v.get("ts", ""))
            if (now - ts).total_seconds() > 86400:
                to_delete.append(k)
        except Exception:  # noqa: BLE001
            to_delete.append(k)
    for k in to_delete:
        pings.pop(k, None)

    entry = pings.get(dedupe_key)
    if entry:
        try:
            entry_ts = dt.datetime.fromisoformat(entry.get("ts", ""))
            age = (now - entry_ts).total_seconds()
        except Exception:  # noqa: BLE001
            age = window_seconds + 1

        if age <= window_seconds:
            msg_id = entry.get("message_id")
            base_text = entry.get("text", text)
            count = entry.get("count", 1) + 1
            new_text = f"{base_text}\n… and {count} more (see the pinned inbox dashboard)"

            token = os.environ.get("AI_ROUTER_BOT_TOKEN")
            chat_id = os.environ.get("TELEGRAM_OWNER_CHAT_ID")
            if not token or not chat_id:
                raise ValueError("AI_ROUTER_BOT_TOKEN or TELEGRAM_OWNER_CHAT_ID not in env")

            def _raise_api_err(prefix, desc):
                desc_redacted = d._redact(str(desc)).replace(token, "<redacted>")
                raise RuntimeError(f"{prefix}: {desc_redacted}")

            url = f"https://api.telegram.org/bot{token}/editMessageText"
            payload = {
                "chat_id": chat_id,
                "message_id": msg_id,
                "text": d._redact(new_text)
            }

            try:
                resp = httpx.post(url, json=payload, timeout=30.0)
            except Exception as e:  # noqa: BLE001
                _raise_api_err("editMessageText network error", str(e))

            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                _raise_api_err("editMessageText failed", f"HTTP {resp.status_code} (non-JSON response)")

            if data.get("ok"):
                entry["count"] = count
                entry["ts"] = now.isoformat()
                state["note_pings"] = pings
                _save_state(state)
                return f"coalesced (message_id={msg_id}, count={count})"

            desc = data.get("description", "").lower()
            if "message to edit not found" in desc or "message can't be edited" in desc:
                pings.pop(dedupe_key, None)
                state["note_pings"] = pings
                _save_state(state)
            else:
                _raise_api_err("Telegram refused editMessageText", data.get("description", "no description"))

    ping_res = send_note_ping(text)
    msg_id = None
    if ping_res.startswith("message_id="):
        try:
            msg_id = int(ping_res.split("=", 1)[1])
        except ValueError:
            pass

    pings[dedupe_key] = {
        "message_id": msg_id,
        "ts": now.isoformat(),
        "count": 1,
        "text": text
    }
    state["note_pings"] = pings
    _save_state(state)
    return ping_res

