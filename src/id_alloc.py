import argparse
import contextlib
import datetime
import fcntl
import os
import re
import sys
import tempfile

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

def get_lock_path():
    # A dedicated, never-replaced sidecar. The ledger itself is written via
    # temp+os.replace() (see _atomic_write_ledger), which swaps its inode out
    # from under any lock held on the old file descriptor -- a second writer
    # that opened-and-blocked on that old inode right before the replace
    # would, on waking, hold a lock that no longer protects the live file.
    # Locking a path that is never replaced sidesteps that TOCTOU entirely;
    # every writer serializes through the same stable inode for the whole
    # read-modify-replace cycle. Covered by test_concurrency's 8-way race.
    return get_ledger_path() + ".lock"

_WHO_CANONICAL = {
    # Three spellings of one actor seen in the live ledger/registry
    # (N-035/T-915). Canonicalized going forward only -- historical ledger
    # rows are never rewritten (ids and their rows are locked forever, owner
    # decree 2026-07-31).
    "manager@-github": "manager@-github",
    "manager @-github": "manager@-github",
    "manager-@-github": "manager@-github",
}

def canonicalize_who(who):
    return _WHO_CANONICAL.get(who, who)

def _read_ledger_text():
    path = get_ledger_path()
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def _atomic_write_ledger(new_text):
    """Replace the ledger's full content atomically: write to a temp file in
    the same directory, fsync it, then os.replace() it into place. Must be
    called while holding ledger_lock()."""
    base_dir = get_base_dir()
    os.makedirs(base_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".id-ledger-", suffix=".tmp", dir=base_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_f:
            tmp_f.write(new_text)
            tmp_f.flush()
            os.fsync(tmp_f.fileno())
        os.replace(tmp_path, get_ledger_path())
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

@contextlib.contextmanager
def ledger_lock():
    os.makedirs(get_base_dir(), exist_ok=True)
    with open(get_lock_path(), "a+", encoding="utf-8") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

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

def parse_ledger(text):
    ledger_ids = set()
    max_nums = {p: 0 for p in ALLOWED_PREFIXES}
    occurrences = {}
    lines = text.splitlines()
    for line in lines:
        if not line.strip():
            continue
        parts = line.split("\t")
        if not parts:
            continue
        full_id = parts[0]
        intent = parts[3] if len(parts) > 3 else ""
        match = ID_REGEX.fullmatch(full_id)
        if match:
            prefix, num_str = match.groups()
            num = int(num_str)
            if full_id not in occurrences:
                occurrences[full_id] = []
            occurrences[full_id].append(intent)
            ledger_ids.add(full_id)
            if num > max_nums[prefix]:
                max_nums[prefix] = num
                
    duplicates = set()
    for full_id, intents in occurrences.items():
        if len(intents) > 1:
            if intents[-1].startswith("VOID"):
                num_voids = sum(1 for i in intents if i.startswith("VOID"))
                num_actives = len(intents) - num_voids
                if num_actives - num_voids > 1:
                    duplicates.add(full_id)
            else:
                duplicates.add(full_id)
                
    return ledger_ids, max_nums, duplicates

def cmd_next(args):
    prefix = args.prefix
    if prefix not in ALLOWED_PREFIXES:
        sys.exit(2)

    intent = args.intent
    if not intent:
        sys.exit(2)

    who = canonicalize_who(args.who or "unknown")

    # max() comes only from the locked ledger (T-915 phase B/C). parse_registry()
    # is deliberately NOT consulted here any more: a live, unlocked markdown file
    # moving `max` out from under a concurrent `next` is the root cause the N-035/
    # D-173/D-174 forensics trace this whole hardening back to. This is only safe
    # because T-916's seed already backfilled every manually-assigned id from the
    # registry into the ledger, and the SessionStart/SessionEnd hooks keep it that
    # way -- parse_registry() still runs (via `check`) to catch anything that
    # slips through between seeds.
    with ledger_lock():
        text = _read_ledger_text()
        ledger_ids, max_nums, _ = parse_ledger(text)
        ledger_max = max_nums.get(prefix, 0)

        next_num = ledger_max + 1
        next_id = f"{prefix}-{next_num:03d}"

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = f"{next_id}\t{now_str}\t{who}\t{intent}\n"

        _atomic_write_ledger(text + row)

        print(next_id)

MAX_GAPS_SHOWN = 10

def cmd_check(args):
    registry_ids = parse_registry()
    text = _read_ledger_text()
    ledger_ids, max_nums, duplicates = parse_ledger(text)

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

    with ledger_lock():
        text = _read_ledger_text()
        ledger_ids, _, _ = parse_ledger(text)
        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        new_rows = "".join(
            f"{full_id}\t{now_str}\tseed\tseeded from registry\n"
            for full_id in registry_ids
            if full_id not in ledger_ids
        )
        if new_rows:
            _atomic_write_ledger(text + new_rows)

def cmd_void(args):
    full_id = args.id
    reason = args.reason
    who = canonicalize_who(args.who or "unknown")

    match = ID_REGEX.fullmatch(full_id)
    if not match:
        sys.exit(2)

    with ledger_lock():
        text = _read_ledger_text()
        ledger_ids, _, _ = parse_ledger(text)
        # An id that was never allocated cannot be voided: appending the row
        # anyway would put a number in the ledger that no allocation ever
        # produced and push `max` past it, which is the same "the ledger
        # gained a number nobody issued" failure N-035 documents. A typo in
        # the id must fail loudly instead.
        if full_id not in ledger_ids:
            print(f"cannot void {full_id}: not in ledger", file=sys.stderr)
            sys.exit(3)

        now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = f"{full_id}\t{now_str}\t{who}\tVOID \u2014 {reason}\n"

        _atomic_write_ledger(text + row)

def main():
    parser = argparse.ArgumentParser(prog="id_alloc")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    parser_next = subparsers.add_parser("next")
    parser_next.add_argument("prefix")
    parser_next.add_argument("--intent", required=True)
    parser_next.add_argument("--who", default="unknown")
    
    subparsers.add_parser("check")
    
    subparsers.add_parser("seed")

    parser_void = subparsers.add_parser("void")
    parser_void.add_argument("id")
    parser_void.add_argument("--reason", required=True)
    parser_void.add_argument("--who", default="unknown")
    
    args = parser.parse_args()
    
    if args.command == "next":
        cmd_next(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "seed":
        cmd_seed(args)
    elif args.command == "void":
        cmd_void(args)

if __name__ == "__main__":
    main()
