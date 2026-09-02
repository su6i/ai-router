import argparse
import datetime
import fcntl
import os
import re
import sys

ALLOWED_PREFIXES = {"D", "T", "N", "B", "R"}
ID_REGEX = re.compile(r"\b([DTNBR])-(\d+)\b")
# An ID counts only when it is the identifier OF a registry row, never when it is
# merely mentioned inside one. Prose examples live in blockquotes ("> ... `T-0900`")
# and sentences, so anchoring to the row shape is what kills the poisoning (N-031).
#
# Two real row shapes exist in REGISTRY-IDS.md, verified against the live file:
#   - T-919 — ...                     bare id first
#   - WO-ARX-0072 / T-925 — ...       an external work-order label, then the id
# The second shape carries 9 real allocations. Missing them is worse than the bug
# this fix replaces: an unseen maximum makes the allocator re-issue a live id, the
# exact collision class the registry records for D-211 and D-037/D-038.
#
# The optional label is deliberately narrow — one bare token plus a slash. It cannot
# span a sentence, so it cannot walk into prose. Only the FIRST id on the row is
# taken: rows like "- T-919 — VOID, duplicate of T-916 (pattern of T-901)" must
# yield T-919 and must not re-admit the voided ids quoted in their own body.
_LABEL = r"(?:[A-Za-z0-9][A-Za-z0-9-]*\s*/\s*)?"
LIST_ROW_REGEX = re.compile(r"^-\s+" + _LABEL + r"([DTNBR])-(\d+)\b")
TABLE_ROW_REGEX = re.compile(r"^\|\s*" + _LABEL + r"([DTNBR])-(\d+)\s*\|")

def get_base_dir():
    return os.environ.get("AGENT_MEMORY_DIR", os.path.expanduser("~/.local/share/agent-projects/_memory"))

def get_registry_path():
    return os.path.join(get_base_dir(), "REGISTRY-IDS.md")

def get_ledger_path():
    return os.path.join(get_base_dir(), "ID-LEDGER.tsv")

def parse_registry():
    registry_path = get_registry_path()
    found = {}
    if not os.path.exists(registry_path):
        return found
    with open(registry_path, "r", encoding="utf-8") as f:
        for line in f:
            match = LIST_ROW_REGEX.match(line)
            if not match:
                match = TABLE_ROW_REGEX.match(line)
            if match:
                prefix, num_str = match.groups()
                num = int(num_str)
                full_id = f"{prefix}-{num_str}"
                if full_id not in found:
                    found[full_id] = (prefix, num)
    return found

def parse_ledger(f):
    f.seek(0)
    ledger_ids = set()
    max_nums = {p: 0 for p in ALLOWED_PREFIXES}
    duplicates = set()
    lines = f.read().splitlines()
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if not parts:
            continue
        full_id = parts[0]
        match = ID_REGEX.fullmatch(full_id)
        if match:
            prefix, num_str = match.groups()
            num = int(num_str)
            if full_id in ledger_ids:
                duplicates.add(full_id)
            ledger_ids.add(full_id)
            if num > max_nums[prefix]:
                max_nums[prefix] = num
    return ledger_ids, max_nums, duplicates

def cmd_next(args):
    prefix = args.prefix
    if prefix not in ALLOWED_PREFIXES:
        sys.exit(2)
    
    intent = args.intent
    if not intent:
        sys.exit(2)
        
    who = args.who or "unknown"
    
    os.makedirs(get_base_dir(), exist_ok=True)
    ledger_path = get_ledger_path()
    
    registry_ids = parse_registry()
    registry_max = 0
    for full_id, (p, num) in registry_ids.items():
        if p == prefix and num > registry_max:
            registry_max = num
            
    with open(ledger_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            ledger_ids, max_nums, _ = parse_ledger(f)
            ledger_max = max_nums.get(prefix, 0)
            
            next_num = max(registry_max, ledger_max) + 1
            next_id = f"{prefix}-{next_num:03d}"
            
            now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = f"{next_id}\t{now_str}\t{who}\t{intent}\n"
            
            f.seek(0, os.SEEK_END)
            f.write(row)
            f.flush()
            os.fsync(f.fileno())
            
            print(next_id)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

MAX_GAPS_SHOWN = 10

def cmd_check(args):
    ledger_path = get_ledger_path()
    registry_ids = parse_registry()
    
    if not os.path.exists(ledger_path):
        ledger_ids = set()
        duplicates = set()
        max_nums = {p: 0 for p in ALLOWED_PREFIXES}
    else:
        with open(ledger_path, "r", encoding="utf-8") as f:
            ledger_ids, max_nums, duplicates = parse_ledger(f)
            
    manual = []
    for full_id in registry_ids:
        if full_id not in ledger_ids:
            manual.append(full_id)
            
    # A gap is a number nobody ever took. An ID that lives in the registry but not
    # yet in the ledger is already reported as "manually assigned" above, so it must
    # not be counted a second time here -- otherwise a fresh ledger reports every
    # historical number as missing and buries the two findings that matter.
    known = set(ledger_ids) | set(registry_ids)
    gaps = []
    for p in ALLOWED_PREFIXES:
        nums = sorted(int(ID_REGEX.fullmatch(k).group(2)) for k in known if k.startswith(p + "-"))
        if not nums:
            continue
        gaps.extend(f"{p}-{m:03d}" for m in range(1, max(nums) + 1) if m not in set(nums))
            
    exit_code = 0
    print(
        f"ID Allocator Health Check: {len(ledger_ids)} in ledger, "
        f"{len(manual)} manually assigned, {len(duplicates)} duplicated, "
        f"{len(gaps)} unallocated numbers"
    )
    if manual:
        for m in sorted(manual):
            print(f"manually assigned: {m}")
        exit_code = 1
    if duplicates:
        for d in sorted(duplicates):
            print(f"duplicate in ledger: {d}")
        exit_code = 1
    # Informational only, and capped: an unallocated number is never an error, so it
    # must never be able to push the real findings off the top of a SessionStart line.
    for g in gaps[:MAX_GAPS_SHOWN]:
        print(f"gap in sequence: {g}")
    if len(gaps) > MAX_GAPS_SHOWN:
        print(f"... and {len(gaps) - MAX_GAPS_SHOWN} more unallocated numbers")
            
    sys.exit(exit_code)

def cmd_seed(args):
    registry_ids = parse_registry()
    os.makedirs(get_base_dir(), exist_ok=True)
    ledger_path = get_ledger_path()
    
    with open(ledger_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            ledger_ids, _, _ = parse_ledger(f)
            now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            for full_id in registry_ids:
                if full_id not in ledger_ids:
                    row = f"{full_id}\t{now_str}\tseed\tseeded from registry\n"
                    f.seek(0, os.SEEK_END)
                    f.write(row)
                    ledger_ids.add(full_id)
                    
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def main():
    parser = argparse.ArgumentParser(prog="id_alloc")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    parser_next = subparsers.add_parser("next")
    parser_next.add_argument("prefix")
    parser_next.add_argument("--intent", required=True)
    parser_next.add_argument("--who", default="unknown")
    
    subparsers.add_parser("check")
    
    subparsers.add_parser("seed")
    
    args = parser.parse_args()
    
    if args.command == "next":
        cmd_next(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "seed":
        cmd_seed(args)

if __name__ == "__main__":
    main()
