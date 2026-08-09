"""Live proof for scripts/zwnj_guard.py — WO-ZWNJ-0001.

Per the owner's standing rule ("reviewer must run the code" — a green unit
test is not proof, the reviewer must also give it one real execution), this
builds an actual throwaway git repo, commits a Persian file WITH half-spaces,
then commits an edit that drops them exactly the way the agy worker did on
fix/du-naming-and-interim-track, and asserts the guard script's real
subprocess exit code and printed receipt catch it. A second repo proves the
non-regression path (edit that keeps or adds ZWNJ) passes with exit 0.
"""
import subprocess
import sys
from pathlib import Path

ZWNJ = "‌"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "zwnj_guard.py"


def _git(repo: Path, *args):
    r = subprocess.run(  # noqa: PLW1510
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Branch name deliberately NOT "main"/"master": the machine's global
    # pre-commit hook (constitution rule 040) blocks direct commits to those
    # branch names even inside a disposable tmp repo, since it only looks at
    # the ref name, not whether the repo is "real".
    _git(repo, "init", "-q", "-b", "zwnj-guard-test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def test_guard_detects_real_zwnj_regression(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "comment.py"

    before = f"# می{ZWNJ}خواهم نیم{ZWNJ}فاصله حفظ شود\n"
    f.write_text(before)
    _git(repo, "add", "comment.py")
    _git(repo, "commit", "-q", "-m", "feat: initial persian comment")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    # Simulate exactly the observed bug: a worker "fix" that silently
    # replaces both ZWNJs with plain spaces.
    after = "# می خواهم نیم فاصله حفظ شود\n"
    f.write_text(after)
    _git(repo, "add", "comment.py")
    _git(repo, "commit", "-q", "-m", "fix: reword persian comment")

    result = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--base", base_sha],
        capture_output=True, text=True,
    )

    assert result.returncode == 1, (
        f"guard should FAIL on a real ZWNJ regression, got exit {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "regressions=1" in result.stdout
    assert "comment.py: before=2 after=0" in result.stdout
    assert "comment.py" in result.stderr
    assert "2 -> 0 (-2)" in result.stderr


def test_guard_passes_when_zwnj_preserved(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "comment.py"

    before = f"# می{ZWNJ}خواهم نیم{ZWNJ}فاصله حفظ شود\n"
    f.write_text(before)
    _git(repo, "add", "comment.py")
    _git(repo, "commit", "-q", "-m", "feat: initial persian comment")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    # A legitimate edit: adds a line, keeps both existing ZWNJs intact.
    after = before + f"# یک خط{ZWNJ}ی دیگر\n"
    f.write_text(after)
    _git(repo, "add", "comment.py")
    _git(repo, "commit", "-q", "-m", "feat: add another persian line")

    result = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--base", base_sha],
        capture_output=True, text=True,
    )

    assert result.returncode == 0, (
        f"guard should PASS when ZWNJ count does not drop, got exit "
        f"{result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "regressions=0" in result.stdout
    assert "comment.py: before=2 after=3" in result.stdout


def test_guard_reports_zero_files_when_nothing_touches_zwnj(tmp_path):
    repo = _init_repo(tmp_path)
    f = repo / "readme.md"
    f.write_text("# no persian here\n")
    _git(repo, "add", "readme.md")
    _git(repo, "commit", "-q", "-m", "docs: english only")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    f.write_text("# no persian here either\n")
    _git(repo, "add", "readme.md")
    _git(repo, "commit", "-q", "-m", "docs: tweak english")

    result = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--base", base_sha],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "files_with_zwnj=0" in result.stdout
    assert "regressions=0" in result.stdout


def test_guard_ignores_new_file_with_no_base_version(tmp_path):
    repo = _init_repo(tmp_path)
    (repo / "seed.py").write_text("# seed\n")
    _git(repo, "add", "seed.py")
    _git(repo, "commit", "-q", "-m", "chore: seed")
    base_sha = _git(repo, "rev-parse", "HEAD").strip()

    # New file, never existed at base — cannot be a "regression".
    (repo / "new_comment.py").write_text(f"# تازه{ZWNJ}ساز\n")
    _git(repo, "add", "new_comment.py")
    _git(repo, "commit", "-q", "-m", "feat: new persian file")

    result = subprocess.run(  # noqa: PLW1510
        [sys.executable, str(SCRIPT), "--repo", str(repo), "--base", base_sha],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "regressions=0" in result.stdout
