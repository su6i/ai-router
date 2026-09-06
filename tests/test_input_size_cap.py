"""T-959 — the pre-call gate caps how many bytes a delegation sends to a worker.

The file-count cap alone does not bound a run: two files are enough to send
megabytes when one of them is a 157 KB module. These tests pin the byte cap's
behaviour, including that it never reads a file (stat only) and that a path the
worker is expected to create does not count against the cap.
"""
import pytest

import delegate as d


def _files(tmp_path, spec):
    """spec: {relative name: size in bytes}. Returns the --files argument."""
    for name, size in spec.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
    return ",".join(spec)


def test_over_cap_aborts_naming_the_largest_file(tmp_path):
    files = _files(tmp_path, {"big.py": 300_000, "small.py": 1_000})
    with pytest.raises(ValueError) as e:
        d._enforce_verify_and_cap(files, "src/**", "pytest -q", None, None,
                                  project_root=tmp_path, max_input_bytes=100_000)
    msg = str(e.value)
    assert "over the cap" in msg
    assert "big.py" in msg          # names the offender, not just a total
    assert "T-959" in msg


def test_under_cap_passes(tmp_path):
    files = _files(tmp_path, {"a.py": 1_000, "b.py": 2_000})
    d._enforce_verify_and_cap(files, "src/**", "pytest -q", None, None,
                              project_root=tmp_path, max_input_bytes=100_000)


def test_missing_file_does_not_count(tmp_path):
    """A path the worker will create contributes nothing to the input."""
    files = _files(tmp_path, {"a.py": 1_000}) + ",tests/test_new.py"
    d._enforce_verify_and_cap(files, "src/**", "pytest -q", None, None,
                              project_root=tmp_path, max_input_bytes=2_000)


def test_zero_disables_the_cap(tmp_path):
    files = _files(tmp_path, {"huge.py": 500_000})
    d._enforce_verify_and_cap(files, "src/**", "pytest -q", None, None,
                              project_root=tmp_path, max_input_bytes=0)


def test_env_var_supplies_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_ROUTER_MAX_INPUT_BYTES_PER_RUN", "5000")
    files = _files(tmp_path, {"a.py": 10_000})
    with pytest.raises(ValueError, match="over the cap"):
        d._enforce_verify_and_cap(files, "src/**", "pytest -q", None, None,
                                  project_root=tmp_path, max_input_bytes=None)


def test_gate_never_reads_the_files(tmp_path, monkeypatch):
    """stat() only — a gate that reads the files costs what it is saving."""
    files = _files(tmp_path, {"a.py": 1_000})
    import builtins
    real_open = builtins.open

    def boom(path, *a, **kw):
        if str(path).endswith("a.py"):
            raise AssertionError(f"gate opened {path}")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", boom)
    d._enforce_verify_and_cap(files, "src/**", "pytest -q", None, None,
                              project_root=tmp_path, max_input_bytes=100_000)


def test_cap_runs_after_the_verify_check(tmp_path):
    """A missing --verify must still be the error the caller sees first."""
    files = _files(tmp_path, {"huge.py": 500_000})
    with pytest.raises(ValueError, match="no --verify given"):
        d._enforce_verify_and_cap(files, "src/**", "", None, None,
                                  project_root=tmp_path, max_input_bytes=1_000)
