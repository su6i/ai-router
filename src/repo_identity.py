"""Shared repo-identity validation for every ingest boundary (T-956).

A row's `repo` column must be a real directory name, not any string a CLI
happened to be handed. `session_chunks` picked up a row with `repo =
'--help'` because nothing validated the identity `sessions_index.py`
derived from walking the vault tree, and a mistyped invocation had already
created an actual `--help/` directory there. This module is the single
place that decides "valid repo identity, yes/no" so every ingest path
(and the one-shot cleanup) agrees on one rule instead of each re-deriving
its own.
"""
from __future__ import annotations

import argparse
from pathlib import Path


class InvalidRepoIdentity(ValueError):
    """Raised when a candidate repo identity fails validation.

    A ValueError subclass on purpose: a caller that only catches
    ValueError still catches this, while `except InvalidRepoIdentity`
    lets a caller be precise when it wants to log-and-skip one bad
    file/row rather than let the exception abort a whole batch. Raising
    is the point — a caller cannot silently swallow an invalid identity
    into "return None / skip without a trace", which is exactly how the
    `--help` row got into `session_chunks`.
    """


def validate_repo_name(name: str, roots: list[Path] | None = None) -> str:
    """Validate `name` as a real repo identity; return it unchanged on success.

    Always rejected: empty string, `.`, `..`, anything starting with `-`
    (the shape of a CLI flag swallowed as a positional value — e.g. a
    stray `--` end-of-options marker handing `--help` straight to a
    positional), and anything containing a path separator.

    When `roots` is given, `name` must additionally be an existing
    directory directly under at least one of them — the stronger check,
    appropriate where the identity is derived by walking a known root
    (sessions). Omit `roots` where the identity instead comes from an
    already-vetted source (a configured code-repo root, or `Path.cwd()`)
    and only the shape needs re-checking.
    """
    if not name:
        raise InvalidRepoIdentity("repo identity is empty")
    if name in (".", ".."):
        raise InvalidRepoIdentity(f"{name!r} is not a real repo directory name")
    if name.startswith("-"):
        raise InvalidRepoIdentity(f"{name!r} looks like a CLI flag, not a repo name")
    if "/" in name or "\\" in name or Path(name).name != name:
        raise InvalidRepoIdentity(f"{name!r} contains a path separator")
    if roots is not None:
        if not any((Path(root) / name).is_dir() for root in roots):
            raise InvalidRepoIdentity(
                f"{name!r} is not a directory under any configured root: "
                f"{[str(r) for r in roots]}"
            )
    return name


def reject_flag_like(value: str):
    """argparse `type=` callable: reject a positional value starting with `-`.

    A positional normally can't receive a `-`-prefixed token — argparse
    treats it as an option and fails with "unrecognized arguments" — EXCEPT
    after a literal `--` end-of-options marker, which hands the token
    straight through (`prog cmd -- --help` sets the positional to the
    literal string `--help`). That is the T-956 failure shape: a mistyped
    invocation's flag consumed as a positional. Guard for it explicitly,
    with a message that names the likely typo instead of letting the
    value flow through silently.
    """
    if value.startswith("-"):
        raise argparse.ArgumentTypeError(
            f"{value!r} looks like a flag, not the intended value here — "
            f"check for a missing argument or a stray '--' before it"
        )
    return value
