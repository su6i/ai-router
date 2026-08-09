"""ZWNJ (U+200C, نیم‌فاصله) diagnostic — WO-ZWNJ-0001.

Owner-reported symptom: when the agy worker edits Persian text, half-spaces
disappear or get replaced with a plain space (real incident: branch
fix/du-naming-and-interim-track in ApplyForge, restored by hand). The
mandate was "root-cause, not just cure": is U+200C lost in the model's OWN
output, or in OUR normalization/encoding layer (the delegate_worker pipe,
patch apply, file write)?

This file answers that question mechanically. It feeds ZWNJ-bearing text
through the exact functions the worker pipeline calls after the model
responds — parse_worker_response() (sentinel-line parser), _write_files()
(===FILE=== writer), _apply_patches() (===PATCH=== writer) — and asserts the
U+200C count survives byte-for-byte. `_norm()` (NFC-normalize + whitespace
collapse) is checked separately: it exists ONLY for cache-key hashing, never
touches a file on disk, and is exercised here to prove it doesn't leak into
the write path either.

If every test in this file passes, the loss is NOT in src/delegate.py — it
happens upstream, in the worker model's own text generation, and the only
defence available at this layer is detection: scripts/zwnj_guard.py (see
tests/test_zwnj_guard_script.py for its own live proof).
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import delegate as d  # noqa: E402

ZWNJ = "‌"

# Real Persian words that rely on ZWNJ for correct spelling — each has 1 ZWNJ.
SAMPLE_TEXT = (
    f"این کامنت فارسی است و می{ZWNJ}خواهد نشان دهد که نیم{ZWNJ}فاصله حفظ "
    f"می{ZWNJ}شود، نه این{ZWNJ}که به فاصله{ZWNJ}ی معمولی تبدیل شود یا حذف شود."
)
SAMPLE_ZWNJ_COUNT = SAMPLE_TEXT.count(ZWNJ)


def test_sample_text_actually_contains_zwnj():
    # Guard against a typo silently making the rest of this file vacuous.
    assert SAMPLE_ZWNJ_COUNT == 5


def test_parse_worker_response_preserves_zwnj_in_file_block():
    worker_output = (
        "===FILE: fa_comment.py===\n"
        f"# {SAMPLE_TEXT}\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "added a Persian comment\n"
        "===END SUMMARY===\n"
    )
    files, patches, summary = d.parse_worker_response(worker_output)
    assert patches == []
    assert len(files) == 1
    path, content = files[0]
    assert path == "fa_comment.py"
    assert content.count(ZWNJ) == SAMPLE_ZWNJ_COUNT
    assert SAMPLE_TEXT in content


def test_parse_worker_response_preserves_zwnj_in_patch_block():
    worker_output = (
        "===PATCH: fa_comment.py===\n"
        "===OLD===\n"
        "# placeholder\n"
        "===NEW===\n"
        f"# {SAMPLE_TEXT}\n"
        "===END PATCH===\n"
    )
    files, patches, summary = d.parse_worker_response(worker_output)
    assert files == []
    assert len(patches) == 1
    path, old, new = patches[0]
    assert new.count(ZWNJ) == SAMPLE_ZWNJ_COUNT
    assert SAMPLE_TEXT in new


def test_write_files_byte_identical_zwnj(tmp_path):
    content = f"# {SAMPLE_TEXT}\n"
    written, rejected = d._write_files(
        [("fa_comment.py", content)], tmp_path, ["*.py"],
    )
    assert rejected == []
    assert len(written) == 1
    on_disk = (tmp_path / "fa_comment.py").read_text()
    assert on_disk == content  # byte-identical, not just "same count"
    assert on_disk.count(ZWNJ) == SAMPLE_ZWNJ_COUNT


def test_apply_patches_byte_identical_zwnj(tmp_path):
    target = tmp_path / "fa_comment.py"
    target.write_text("# placeholder\n")
    new_line = f"# {SAMPLE_TEXT}\n"
    applied, rejected = d._apply_patches(
        [("fa_comment.py", "# placeholder\n", new_line)], tmp_path, ["*.py"],
    )
    assert rejected == []
    assert len(applied) == 1
    on_disk = target.read_text()
    assert on_disk == new_line
    assert on_disk.count(ZWNJ) == SAMPLE_ZWNJ_COUNT


def test_full_pipeline_round_trip_byte_identical(tmp_path):
    """End-to-end: fake worker text -> parse -> write -> read back, no model call."""
    worker_output = (
        "===FILE: fa_comment.py===\n"
        f"# {SAMPLE_TEXT}\n"
        "===END FILE===\n"
        "===SUMMARY===\n"
        "s\n"
        "===END SUMMARY===\n"
    )
    files, patches, _summary = d.parse_worker_response(worker_output)
    written, rejected = d._write_files(files, tmp_path, ["*.py"])
    assert rejected == []
    on_disk = (tmp_path / "fa_comment.py").read_text()
    assert on_disk.count(ZWNJ) == SAMPLE_ZWNJ_COUNT
    assert f"# {SAMPLE_TEXT}\n" == on_disk


def test_norm_used_only_for_cache_key_does_not_strip_zwnj():
    # _norm() NFC-normalizes + collapses whitespace for cache-key hashing.
    # U+200C is not whitespace and is NFC-stable, so it must survive too —
    # but more importantly this function is never called on the write path
    # (grep-verified below: only cache_make_key() calls it), so even if it
    # DID strip ZWNJ it could not be this bug's cause.
    normalized = d._norm(SAMPLE_TEXT)
    assert normalized.count(ZWNJ) == SAMPLE_ZWNJ_COUNT


def test_norm_is_not_referenced_by_any_write_path_function():
    """Static proof _norm() cannot be the leak: it's wired only to cache
    hashing, never to parse_worker_response/_write_files/_apply_patches."""
    src = (Path(__file__).resolve().parent.parent / "src" / "delegate.py").read_text()
    calls = re.findall(r"(?<!def )_norm\(", src)  # exclude the "def _norm(" definition itself
    # cache_make_key() calls _norm() twice (system, prompt); that's the ONLY caller.
    fn_start = src.index("def cache_make_key")
    fn_end = src.index("\n\n\n", fn_start)
    calls_inside_cache_make_key = src[fn_start:fn_end].count("_norm(")
    assert len(calls) == calls_inside_cache_make_key


def test_zwnj_guard_script_importable_as_module():
    """scripts/zwnj_guard.py is imported directly (not just subprocessed) by
    test_zwnj_guard_script.py — a quick sanity import here keeps the failure
    local to this file if the module itself is broken (syntax error etc.),
    rather than surfacing as an opaque subprocess non-zero exit elsewhere."""
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    result = subprocess.run(  # noqa: PLW1510
        [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import zwnj_guard",
         str(scripts_dir)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
