#!/usr/bin/env python3
import sys
import os
import argparse
import re
from collections import defaultdict
import datetime
from pathlib import Path

try:
    from src.delegate import _agent_projects_root
except ImportError:
    def _agent_projects_root() -> Path:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
        return base / "agent-projects"

def get_vault_root() -> Path:
    vault_env = os.environ.get("OBSIDIAN_VAULT")
    if not vault_env:
        print("Error: OBSIDIAN_VAULT environment variable is not set.", file=sys.stderr)
        sys.exit(2)
    return Path(vault_env)

def setup_symlinks(vault_root: Path, memory_dir: Path):
    agents_dir = vault_root / "80-Agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    
    links = {
        "queue.md": memory_dir / "QUEUE.md",
        "todo.md": memory_dir / "TODO.md",
        "registry.md": memory_dir / "REGISTRY-IDS.md",
        "wo": memory_dir / "wo",
        "sessions": memory_dir
    }
    
    for link_name, target in links.items():
        link_path = agents_dir / link_name
        
        if not target.exists():
            continue
            
        if link_path.is_symlink():
            if link_path.resolve() == target.resolve():
                continue
            link_path.unlink()
        elif link_path.exists() and not link_path.is_dir():
            print(f"Warning: {link_path} exists and is not a symlink. Skipping.", file=sys.stderr)
            continue
        elif link_path.exists() and link_path.is_dir():
             print(f"Warning: {link_path} exists and is a directory. Skipping.", file=sys.stderr)
             continue
             
        link_path.symlink_to(target)

NOISE_CELL = re.compile(
    r'^(?:'
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}'      # a bare date
    r'|[TDNBW]-[0-9]{3}'               # a bare cross-reference
    r'|[+-][0-9]+/[+-][0-9]+'          # ahead/behind
    r'|~?[0-9]+[wdmh]'                 # an age
    r'|[—\-–]+'                        # an empty-marker dash
    r')$'
)


def _is_noise_cell(cell: str) -> bool:
    """True for cells that can never be a summary: dates, ids, counters, dashes."""
    stripped = re.sub(r'[*`_]', '', cell).strip()
    return len(stripped) < 4 or bool(NOISE_CELL.match(stripped))


def parse_all_metadata(lines):
    id_pattern = re.compile(r'\b([TDNBW]-[0-9]{3})\b')
    id_meta = {}
    current_header_cols = {}
    current_header_line = ""
    in_closed_section = False
    section_kind = None
    warned_headers = set()

    for line in lines:
        if line.startswith("## ✅ بسته‌شده"):
            in_closed_section = True
        elif line.startswith("## ") and "بسته‌شده" not in line:
            in_closed_section = False
            m_sec = re.match(r'## ([TDNBW]) —', line)
            section_kind = m_sec.group(1) if m_sec else None
            
        if line.startswith("|"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) > 1 and parts[1] == "ID":
                current_header_line = line
                current_header_cols = {name: i for i, name in enumerate(parts) if name}
                
                summary_cols = ["تصمیم", "چه کاری در آن است", "تسک", "موضوع"]
                if not any(col in current_header_cols for col in summary_cols):
                    print(f"Warning: Unrecognized table header: {current_header_line}", file=sys.stderr)
                continue
            
        matches = id_pattern.findall(line)
        if not matches:
            continue
            
        for m in set(matches):
            if m not in id_meta:
                kind_map = {"T": "task", "D": "decision", "N": "note", "B": "branch", "W": "work"}
                id_meta[m] = {
                    "kind": kind_map.get(m[0], "unknown"),
                    "repo": None,
                    "tier": None,
                    "branch": None,
                    "blocker": None,
                    "file": None,
                    "summary": None,
                    "status": "بسته" if in_closed_section else "باز"
                }
            
            if line.startswith("|") and len(line.split("|")) > 1 and m in line.split("|")[1]:
                parts = [p.strip() for p in line.split("|")]

                # The registry has long runs of rows appended under a header that
                # belongs to another section (D- and B- rows sitting under the N
                # table). A name-based column mapping then files a decision's text
                # as "فایل" and titles the note with the date column. So the aux
                # fields are only read when the row's kind matches the section it
                # sits in — an empty field beats a confidently wrong one.
                row_fits_section = section_kind is not None and section_kind == m[0]
                if current_header_cols and row_fits_section:
                    field_mapping = {
                        "repo": "repo",
                        "Tier": "tier",
                        "branch": "branch",
                        "بلاکر": "blocker",
                        "فایل": "file"
                    }
                    for col_name, field in field_mapping.items():
                        if col_name in current_header_cols:
                            idx = current_header_cols[col_name]
                            if idx < len(parts) and parts[idx]:
                                id_meta[m][field] = parts[idx]
                elif current_header_cols and section_kind and current_header_line not in warned_headers:
                    warned_headers.add(current_header_line)
                    print(
                        f"Warning: rows of kind {m[0]}- appear under the "
                        f"{section_kind}- table header; aux columns skipped for them.",
                        file=sys.stderr,
                    )

                # The summary is the longest cell that is not itself a date, an id
                # or a status marker. That is independent of column position and
                # header naming, which is what this file actually requires.
                if not id_meta[m]["summary"]:
                    summary_cols = ["تصمیم", "چه کاری در آن است", "تسک", "موضوع"]
                    picked = None
                    if row_fits_section:
                        for col in summary_cols:
                            idx = current_header_cols.get(col)
                            if idx is not None and idx < len(parts) and parts[idx]:
                                picked = parts[idx]
                                break
                    if picked is None or _is_noise_cell(picked):
                        cells = [c for c in parts[2:] if c and not _is_noise_cell(c)]
                        picked = max(cells, key=len) if cells else None
                    if picked:
                        id_meta[m]["summary"] = picked

            elif line.startswith("- " + m) or line.startswith("> **" + m + "**"):
                # The summary is simply whatever follows the FIRST em-dash. Do not
                # re-search for the id inside that remainder: ids recur in the text
                # (e.g. inside a WO filename), and matching the second occurrence
                # captures a filename fragment instead of the summary. Length is
                # handled by the shared truncation pass below, so no sentence split
                # here either — "." shows up in filenames and dates.
                summary_val = line.split("—", 1)[-1].strip()
                    
                m_repo = re.search(r'\(([^)]+)\)$', line.strip())
                if m_repo:
                    sp = m_repo.group(1).split("،")
                    if len(sp) >= 1:
                        id_meta[m]["repo"] = sp[0].strip()
                        
                if not id_meta[m]["summary"] and summary_val and not _is_noise_cell(summary_val):
                    id_meta[m]["summary"] = summary_val
                    
            if "✅" in line or "بسته" in line:
                id_meta[m]["status"] = "بسته"
            elif "🔴" in line or "منتظر" in line or "⏳" in line:
                id_meta[m]["status"] = "منتظرِ مالک"

    for m, meta in id_meta.items():
        text = meta["summary"]
        if not text:
            meta["summary"] = "(بدون خلاصه در رجیستری)"
            continue
            
        text = re.sub(r'[*`_]', '', text)
        text = re.sub(r'^[\s✅🔴⚠️📌🚀❌]+', '', text).strip()
        
        if not text:
            meta["summary"] = "(بدون خلاصه در رجیستری)"
            continue
            
        if len(text) > 80:
            short = text[:79]
            last_space = short.rfind(' ')
            if last_space > 0:
                meta["summary"] = short[:last_space] + "…"
            else:
                meta["summary"] = short + "…"
        else:
            meta["summary"] = text
            
    return id_meta

def export_notes(vault_root: Path, agent_projects_root: Path):
    agents_dir = vault_root / "80-Agents"
    ids_dir = agents_dir / "ids"
    ids_dir.mkdir(parents=True, exist_ok=True)
    
    memory_dir = agent_projects_root / "_memory"
    registry_file = memory_dir / "REGISTRY-IDS.md"
    
    if not registry_file.exists():
        print(f"Error: Registry file not found at {registry_file}", file=sys.stderr)
        sys.exit(1)
        
    lines = registry_file.read_text().splitlines()
    id_pattern = re.compile(r'\b([TDNBW]-[0-9]{3})\b')
    id_lines = defaultdict(list)
    
    for line in lines:
        matches = id_pattern.findall(line)
        for m in set(matches):
            id_lines[m].append(line)
            
    parsed_metadata = parse_all_metadata(lines)
            
    written = 0
    unchanged = 0
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    for id_str, lines_for_id in id_lines.items():
        meta = parsed_metadata.get(id_str, {})
        kind = meta.get("kind", "unknown")
        status = meta.get("status", "باز")
        summary = meta.get("summary", "(بدون خلاصه در رجیستری)")
        
        body_lines = []
        for line in lines_for_id:
            line = re.sub(r'\[\[([TDNBW]-[0-9]{3})\]\]', r'\1', line)
            line = re.sub(r'\b([TDNBW]-[0-9]{3})\b', r'[[\1]]', line)
            line = re.sub(r'(?<!\.)\b_memory/wo/([a-zA-Z0-9_-]+\.md)', r'../wo/\1', line)
            line = re.sub(r'(?<!\.)\bwo/([a-zA-Z0-9_-]+\.md)', r'../wo/\1', line)
            body_lines.append(line)
            
        fm = [f"id: {id_str}", f"kind: {kind}"]
        for field in ["repo", "tier", "branch", "blocker", "file"]:
            if meta.get(field):
                fm.append(f"{field}: {meta.get(field)}")
        fm.append(f"status: {status}")
        fm.append(f"updated: {now_str}")
        fm.append("generated: true")
        
        content = "---\n" + "\n".join(fm) + "\n---\n"
        content += "> ⚠️ **هشدار:** این فایل بهطورِ خودکار تولید شده است. لطفاً دستی ویرایش نکنید.\n\n"
        content += f"# {id_str} — {summary}\n\n"
        content += "\n".join(body_lines) + "\n"
        
        out_file = ids_dir / f"{id_str}.md"
        
        if out_file.exists():
            existing_content = out_file.read_text()
            def strip_updated(t):
                return re.sub(r'^updated:.*$', '', t, flags=re.MULTILINE)
            
            if strip_updated(existing_content) == strip_updated(content):
                unchanged += 1
                continue
                
        out_file.write_text(content)
        written += 1
        
    index_content = ["# فهرست شناسهها\n"]
    grouped = defaultdict(lambda: defaultdict(list))
    for id_str in sorted(id_lines.keys()):
        meta = parsed_metadata.get(id_str, {})
        kind = meta.get("kind", "unknown")
        status = meta.get("status", "باز")
        summary = meta.get("summary", "(بدون خلاصه در رجیستری)")
        grouped[kind][status].append(f"- [[{id_str}]] — {summary}")
        
    for kind in sorted(grouped.keys()):
        index_content.append(f"## {kind.capitalize()}")
        for status in sorted(grouped[kind].keys()):
            index_content.append(f"### {status}")
            index_content.extend(grouped[kind][status])
            index_content.append("")
            
    index_text = "\n".join(index_content)
    index_file = ids_dir / "_index.md"
    
    if index_file.exists() and index_file.read_text() == index_text:
        unchanged += 1
    else:
        index_file.write_text(index_text)
        written += 1
        
    print(f"written={written} unchanged={unchanged}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true", help="Setup symlinks in vault")
    args = parser.parse_args()
    
    vault_root = get_vault_root()
    agent_projects_root = _agent_projects_root()
    
    if args.setup:
        setup_symlinks(vault_root, agent_projects_root / "_memory")
    else:
        export_notes(vault_root, agent_projects_root)

if __name__ == "__main__":
    main()
