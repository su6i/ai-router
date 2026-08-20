import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import vault_export

def test_vault_export(tmp_path, capsys):
    vault_root = tmp_path / "vault"
    agent_projects_root = tmp_path / "agent-projects"
    
    memory_dir = agent_projects_root / "_memory"
    memory_dir.mkdir(parents=True)
    
    registry_file = memory_dir / "REGISTRY-IDS.md"
    registry_file.write_text("""# REGISTRY
- T-150 — Test task for multi-line support. (ai-router، باز)
More details for T-150 on the second line.
And another mention of T-150.
- T-151 — Another task referring to T-150 inside its body. (Arix، بسته)
- T-152 — Dispatched via `wo/wo-0014-T-152-legacy-extraction.md` today. (@-github، باز)

| ID | تصمیم |
|---|---|
| D-001 | AVeryLongDecisionTextThatExceedsEightyCharactersByALotSoItMustBeTruncatedWithoutSpaces |
| W-998 | **✅ 🔴 My Task** |

| ID | Unknown |
|---|---|
| W-999 | Some fallback summary |
| W-997 | Row with Apple ID inside it to test false positive header detection |

## N — notes

| ID | فایل | موضوع |
|---|---|---|
| N-001 | a-long-note-filename-here | short subject |
| D-003 | A decision row misfiled under the N table header | 2026-08-13 |

## ✅ بسته‌شده
- D-002 — This should be closed implicitly.
""")
    
    # Run first time
    vault_export.export_notes(vault_root, agent_projects_root)
    captured = capsys.readouterr()
    
    assert "Warning: Unrecognized table header: | ID | Unknown |" in captured.err
    assert "written=11 unchanged=0" in captured.out
    
    t150_file = vault_root / "80-Agents" / "ids" / "T-150.md"
    assert t150_file.exists()
    content_t150 = t150_file.read_text()
    
    assert "repo: ai-router" in content_t150
    assert "More details for [[T-150]] on the second line." in content_t150
    assert "Test task for multi-line support." in content_t150
    
    t151_file = vault_root / "80-Agents" / "ids" / "T-151.md"
    content_t151 = t151_file.read_text()
    assert "Another task referring to [[T-150]] inside its body" in content_t151
    
    d001_file = vault_root / "80-Agents" / "ids" / "D-001.md"
    content_d001 = d001_file.read_text()
    assert "repo:" not in content_d001
    long_text = "AVeryLongDecisionTextThatExceedsEightyCharactersByALotSoItMustBeTruncatedWithoutSpaces"
    expected_title = long_text[:79] + "…"
    assert expected_title in content_d001
    
    w998_file = vault_root / "80-Agents" / "ids" / "W-998.md"
    content_w998 = w998_file.read_text()
    assert "# W-998 — My Task" in content_w998
    
    w999_file = vault_root / "80-Agents" / "ids" / "W-999.md"
    content_w999 = w999_file.read_text()
    assert "# W-999 — Some fallback summary" in content_w999

    w997_file = vault_root / "80-Agents" / "ids" / "W-997.md"
    content_w997 = w997_file.read_text()
    assert "# W-997 — Row with Apple ID inside it to test false positive header detection" in content_w997
    
    # Regression: an id that recurs later in its own line (inside a WO filename)
    # must not hijack the summary — the summary is what follows the FIRST em-dash.
    t152_file = vault_root / "80-Agents" / "ids" / "T-152.md"
    content_t152 = t152_file.read_text()
    assert "# T-152 — Dispatched via" in content_t152
    title_t152 = next(ln for ln in content_t152.splitlines() if ln.startswith("# "))
    assert title_t152.startswith("# T-152 — Dispatched via")

    # A row of one kind sitting under another kind's header must still get a real
    # title (not the date column) and must not borrow that header's aux columns.
    content_d003 = (vault_root / "80-Agents" / "ids" / "D-003.md").read_text()
    assert "# D-003 — A decision row misfiled under the N table header" in content_d003
    assert "file:" not in content_d003
    assert "rows of kind D- appear under the N- table header" in captured.err

    # A well-formed row still takes its named summary column, not the longest cell.
    content_n001 = (vault_root / "80-Agents" / "ids" / "N-001.md").read_text()
    assert "# N-001 — short subject" in content_n001

    d002_file = vault_root / "80-Agents" / "ids" / "D-002.md"
    content_d002 = d002_file.read_text()
    assert "status: بسته" in content_d002

    # Run second time
    vault_export.export_notes(vault_root, agent_projects_root)
    captured2 = capsys.readouterr()
    assert "written=0 unchanged=11" in captured2.out
