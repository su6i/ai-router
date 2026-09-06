import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from repo_identity import validate_repo_name, reject_flag_like, InvalidRepoIdentity  # noqa: E402


def test_rejects_empty():
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("")


def test_rejects_dot_and_dotdot():
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name(".")
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("..")


def test_rejects_leading_dash():
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("--help")
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("-x")


def test_rejects_path_separator():
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("foo/bar")
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("foo\\bar")
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("../escape")


def test_shape_only_accepts_plain_name_without_roots():
    assert validate_repo_name("ai-router") == "ai-router"


def test_roots_reject_nonexistent_directory(tmp_path):
    real_dir = tmp_path / "real-repo"
    real_dir.mkdir()
    with pytest.raises(InvalidRepoIdentity):
        validate_repo_name("does-not-exist", roots=[tmp_path])


def test_roots_accept_real_directory(tmp_path):
    real_dir = tmp_path / "real-repo"
    real_dir.mkdir()
    assert validate_repo_name("real-repo", roots=[tmp_path]) == "real-repo"


def test_reject_flag_like_passes_normal_value():
    assert reject_flag_like("T-956") == "T-956"
    assert reject_flag_like("ai-router") == "ai-router"


def test_reject_flag_like_rejects_dash_prefixed():
    with pytest.raises(argparse.ArgumentTypeError):
        reject_flag_like("--help")
    with pytest.raises(argparse.ArgumentTypeError):
        reject_flag_like("-x")
