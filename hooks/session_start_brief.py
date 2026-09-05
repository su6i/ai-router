import json
import os
import re
import subprocess
import sys
import threading
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from delegate import _agent_projects_root, load_env
from rules_index import E5_REPO

# --- constants ---
TODO_CAP = 1200
RAG_CAP = 2500
INBOX_CAP = 600
TOTAL_CAP = 4500
RAG_TIMEOUT_S = 6.0
# A digest older than this, measured against the freshest chunk retrieved,
# describes state that has been superseded — see _rank_and_format.
STALE_AFTER_DAYS = 30

BOOST_TERMS = ("left open", "next work order", "open questions", "ready to test", "blocked")
PING_LINE_RE = re.compile(r'·\s+branch\s+|^\s*-\s*(last|session):')

def repo_slug(cwd: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            url = proc.stdout.strip()
            slug = url.split("/")[-1]
            if slug.endswith(".git"):
                slug = slug[:-4]
            return slug.lower()
    except Exception:
        pass

    try:
        proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0:
            return os.path.basename(proc.stdout.strip()).lower()
    except Exception:
        pass

    return os.path.basename(cwd).lower()

def _todo_open_items_block(slug: str) -> str:
    todo_path = _agent_projects_root() / "_memory" / "TODO.md"
    if not todo_path.is_file():
        return ""
    
    try:
        content = todo_path.read_text(errors="replace")
    except Exception:
        return ""
        
    lines = content.splitlines()
    collected = []
    in_section = False
    keep_continuation = False
    
    for line in lines:
        if line.lower().strip() == f"## {slug}":
            in_section = True
            continue
        elif in_section and re.match(r'^##\s+', line):
            break
            
        if in_section:
            if re.match(r'^\s*-\s*\[[ ~]\]', line):
                collected.append(line)
                keep_continuation = True
            elif keep_continuation and line.startswith((" ", "\t")) and line.strip():
                # TODO bullets wrap across lines; keeping only the first line
                # truncates most items mid-sentence and hides the branch name
                # or blocker that makes the item actionable.
                collected.append(line)
            else:
                keep_continuation = False
                
    joined = "\n".join(collected)
    if not joined:
        return ""
        
    return joined[:TODO_CAP]

def _model_is_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache
        res = try_to_load_from_cache(repo_id=E5_REPO, filename="onnx/model.onnx")
        return isinstance(res, str) and Path(res).is_file()
    except Exception:
        return False

def _is_ping_only_chunk(text: str) -> bool:
    lines = [L for L in text.splitlines() if L.strip()]
    if not lines:
        return True
    
    ping_count = sum(1 for L in lines if PING_LINE_RE.search(L))
    return (ping_count / len(lines)) >= 0.70

def _boost_score(heading: str, chunk: str) -> int:
    text = f"{heading}\n{chunk}".lower()
    return sum(1 for term in BOOST_TERMS if term in text)

def _rank_and_format(rows: list[tuple[str, str, str]]) -> str:
    valid_rows = []
    for heading, chunk, date in rows:
        h = heading if heading else ""
        c = chunk if chunk else ""
        d = date if date else ""
        if not _is_ping_only_chunk(c):
            valid_rows.append((h, c, d))
            
    if len(valid_rows) < 2:
        return "(RAG returned no usable continuity chunks)"
        
    valid_rows.sort(key=lambda r: r[2], reverse=True)
    valid_rows.sort(key=lambda r: _boost_score(r[0], r[1]), reverse=True)

    # A chunk that matches no resolution term is topical noise here — nearest
    # neighbour, but it states no open state. Keep it only when nothing better
    # survived, so the cap is spent on chunks that actually carry continuity.
    boosted = [r for r in valid_rows if _boost_score(r[0], r[1]) > 0]
    if len(boosted) >= 2:
        valid_rows = boosted

    # Staleness is not neutral here. An eight-week-old digest still says
    # "Left Open" about work that has long since shipped, and a brief that
    # opens a session with it is worse than one that stays quiet: the agent
    # re-raises closed items as if they were live. Keep only what is recent
    # relative to the freshest chunk retrieved, never an absolute date.
    dated = [r for r in valid_rows if r[2]]
    if dated:
        newest = max(r[2] for r in dated)
        try:
            cutoff = (dt.date.fromisoformat(str(newest)[:10])
                      - dt.timedelta(days=STALE_AFTER_DAYS)).isoformat()
        except ValueError:
            cutoff = ""
        if cutoff:
            fresh = [r for r in valid_rows if r[2] and str(r[2])[:10] >= cutoff]
            if fresh:
                valid_rows = fresh
    
    rendered = []
    for h, c, d in valid_rows:
        # Chunks usually begin with their own heading; prepending it again
        # spends the cap printing every heading twice.
        if h.strip() and not c.lstrip().startswith(h.strip()):
            rendered.append(f"{h}\n{c}")
        else:
            rendered.append(c)
            
    res = "\n---\n".join(rendered)
    return res[:RAG_CAP]

def _legacy_pointer_text(slug: str) -> str:
    todo = str(_agent_projects_root() / "_memory" / "TODO.md")
    hd = str(_agent_projects_root() / slug / "workspace" / "SESSION.md")
    handoffs_dir = _agent_projects_root() / "_memory" / "handoffs"
    
    last = "none"
    if handoffs_dir.is_dir():
        jsonls = list(handoffs_dir.glob("*.jsonl"))
        if jsonls:
            newest = max(jsonls, key=lambda p: p.stat().st_mtime)
            last = str(newest)
            
    return (f"Session continuity (rule 050): before starting, read your project section "
            f"(## {slug}) and the ## Cross-project section in {todo}, and this project's "
            f"log {hd}. Announce open items. Latest raw backup: {last}.")

def _search_session_chunks(slug: str, dsn: str) -> list[tuple]:
    import psycopg

    from rules_index import get_model
    model = get_model()
    q_emb = model.embed(["left open, next work order, open questions, unfinished tasks"], prefix="query: ")[0].tolist()
    
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # The session index also holds work orders, benchmark fixtures,
            # design docs and closed inbox notes. None of those are continuity;
            # retrieving them quotes the WO back at the agent that wrote it and
            # burns the cap. Allowlist the files that actually record state.
            cur.execute("""
                SELECT heading, chunk, date
                FROM session_chunks
                WHERE repo = %s AND (
                    path LIKE %s OR path LIKE %s OR path LIKE %s
                )
                ORDER BY embedding <=> %s::vector LIMIT 6
            """, (slug, "%/SESSION.md", "%/architect-memory.md",
                  "%/NEXT-SESSION.md", str(q_emb)))
            return cur.fetchall()

def _get_continuity_block(slug: str) -> str:
    try:
        load_env()
        dsn = os.environ.get("POSTGRES_DSN")
        if not dsn:
            return _legacy_pointer_text(slug)
            
        if not _model_is_cached():
            return _legacy_pointer_text(slug)
            
        res_container = []
        err_container = []
        
        def worker():
            try:
                res_container.append(_search_session_chunks(slug, dsn))
            except Exception as e:
                err_container.append(e)
                
        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(RAG_TIMEOUT_S)
        
        if t.is_alive() or err_container or not res_container:
            return _legacy_pointer_text(slug)
            
        return _rank_and_format(res_container[0])
    except Exception:
        return _legacy_pointer_text(slug)

def _inbox_block(slug: str) -> str:
    inbox_dir = _agent_projects_root() / slug / "workspace" / "inbox"
    if not inbox_dir.is_dir():
        return ""
        
    try:
        names = [p.name for p in inbox_dir.iterdir() if p.is_file()]
    except Exception:
        return ""
        
    if not names:
        return ""
        
    names.sort()
    inbox_list = " ".join(names[:10]) + " "
    
    res = (f"Inbox ({inbox_dir}): {inbox_list}— announce these notes and "
           f"triage/answer them this session, or state why not.")
    return res[:INBOX_CAP]

def _pointer_tail(slug: str) -> str:
    hd = str(_agent_projects_root() / slug / "workspace" / "SESSION.md")
    handoffs_dir = _agent_projects_root() / "_memory" / "handoffs"
    
    last = "none"
    if handoffs_dir.is_dir():
        jsonls = list(handoffs_dir.glob("*.jsonl"))
        if jsonls:
            newest = max(jsonls, key=lambda p: p.stat().st_mtime)
            last = str(newest)
            
    return (f"Fallback sources (read only if the brief above is insufficient): "
            f"{hd} (SESSION.md) and latest handoff {last}.")

def build_additional_context(cwd: str, data: dict | None = None) -> str:
    slug = repo_slug(cwd)
    block0 = _transcript_block(data or {}, cwd)
    block1 = _todo_open_items_block(slug)
    block2 = _get_continuity_block(slug)
    block3 = _inbox_block(slug)
    block4 = _pointer_tail(slug)
    
    blocks = [b for b in (block0, block1, block2, block3, block4) if b]
    joined = "\n\n".join(blocks)
    
    if len(joined) > TOTAL_CAP:
        overflow = len(joined) - TOTAL_CAP
        if block2:
            new_len = max(0, len(block2) - overflow)
            block2 = block2[:new_len]
            blocks = [b for b in (block0, block1, block2, block3, block4) if b]
            joined = "\n\n".join(blocks)
            
    return joined[:TOTAL_CAP]

def _transcript_block(data: dict, cwd: str) -> str:
    """Where THIS session's raw JSONL log lives, and the previous one's.

    After a /clear the agent keeps only what SESSION.md happened to capture.
    Everything else — the exact command that failed, the number nobody wrote
    down — is still in the raw transcript, so the path has to arrive in
    context automatically. Asking the agent to remember where it lives has
    already failed repeatedly; this block is the systemic fix.
    """
    path = data.get("transcript_path") or ""
    sid = data.get("session_id") or ""
    proj_dir = None

    if path:
        proj_dir = Path(path).parent
    else:
        # Fall back to the on-disk layout: ~/.claude/projects/<cwd with every
        # non-alphanumeric char replaced by ->/<session-id>.jsonl — verified
        # against the real directory, where "/@-" becomes "---".
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd or os.getcwd())
        proj_dir = Path.home() / ".claude" / "projects" / slug
        if sid:
            path = str(proj_dir / f"{sid}.jsonl")

    lines = ["## Raw transcript (not in SESSION.md — grep it before saying you don't know)"]
    if path:
        lines.append(f"this session : {path}")

    try:
        others = sorted((f for f in proj_dir.glob("*.jsonl") if str(f) != path),
                        key=lambda f: f.stat().st_mtime, reverse=True)
        if others:
            prev = others[0]
            when = dt.datetime.fromtimestamp(prev.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            lines.append(f"previous     : {prev}  ({when})")
    except Exception:
        pass

    if len(lines) == 1:
        return ""
    return "\n".join(lines)[:600]


def main():
    try:
        input_data = sys.stdin.read()
        try:
            data = json.loads(input_data)
        except Exception:
            data = {}
            
        cwd = data.get("cwd")
        if not cwd:
            cwd = os.getcwd()
            
        try:
            ctx = build_additional_context(cwd, data)
        except Exception:
            ctx = ""
            
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ctx
            }
        }
        print(json.dumps(out))
    except Exception:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": ""
            }
        }
        print(json.dumps(out))
    finally:
        sys.exit(0)

if __name__ == "__main__":
    main()
