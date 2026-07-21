# r() — thin shell wrapper around ai-router's delegate (src/delegate.py).
# Design: vault workspace R-WRAPPER-DESIGN.md (WO3). All routing, caching,
# budget and audit logic stays in delegate.py; this file only assembles argv.
#
# Activate once from your shell rc (bash or zsh):
#   source /Users/su6i/@-github/ai-router/shell/r.sh
#
# Usage:
#   r <model> <prompt words...>      chat: words are joined into one -p prompt
#   r <model> --<delegate flags...>  raw passthrough (worker mode, --session, ...)
#   r audit                          print the delegation ledger
#
# Env overrides:
#   AI_ROUTER_REPO    path to the ai-router repo (default below)
#   AI_ROUTER_PYTHON  interpreter (default: python3; delegate is stdlib-only)

# code/rules need the project venv (psycopg, tree-sitter, onnxruntime) which
# the stdlib-only `python3` used for chat/audit/worker does NOT have — so from
# any other directory `r code` would crash with ModuleNotFoundError. Run those
# two subcommands under `uv run` inside the repo, which resolves the deps.
_r_heavy() {  # _r_heavy <module> <args...>
  local repo="${AI_ROUTER_REPO:-/Users/su6i/@-github/ai-router}"
  if command -v uv >/dev/null 2>&1; then
    ( cd "$repo" && uv run --quiet python -m "$@" )
  else
    ( cd "$repo" && "${AI_ROUTER_PYTHON:-python3}" -m "$@" )
  fi
}

r() {
  local repo="${AI_ROUTER_REPO:-/Users/su6i/@-github/ai-router}"
  local py="${AI_ROUTER_PYTHON:-python3}"

  if [ "$#" -eq 0 ]; then
    echo 'usage: r <model> <prompt...> | r <model> --<delegate flags...> | r audit' >&2
    return 2
  fi

  if [ "$1" = "audit" ]; then
    "$py" "$repo/src/delegate.py" --audit
    return "$?"
  fi

  if [ "$1" = "rules" ]; then
    shift
    if [ "$1" = "--reindex" ]; then
      _r_heavy src.rules_index reindex
    else
      _r_heavy src.rules_index search "$@"
    fi
    return "$?"
  fi

  if [ "$1" = "sessions" ]; then
    shift
    if [ "$1" = "--reindex" ]; then
      _r_heavy src.sessions_index reindex
    else
      _r_heavy src.sessions_index search "$@"
    fi
    return "$?"
  fi

  if [ "$1" = "code" ]; then
    shift
    if [ "$1" = "--reindex" ]; then
      _r_heavy src.code_index reindex
    elif [ "$1" = "--rebuild" ]; then
      _r_heavy src.code_index reindex --rebuild
    else
      _r_heavy src.code_index search "$@"
    fi
    return "$?"
  fi

  if [ "$1" = "cost" ]; then
    shift
    "$py" "$repo/src/delegate.py" --cost "$@"
    return "$?"
  fi


  if [ "$1" = "note" ]; then
    shift
    local to_proj="$1"
    shift
    if [ "$#" -eq 0 ]; then
      echo 'usage: r note <project> <message...>' >&2
      return 2
    fi
    "$py" "$repo/src/delegate.py" --note "$to_proj" -p "$*"
    return "$?"
  fi

  if [ "$1" = "inbox" ]; then
    shift
    if [ "$1" = "--peek" ]; then
      "$py" "$repo/src/delegate.py" --inbox --peek
    else
      "$py" "$repo/src/delegate.py" --inbox "$@"
    fi
    return "$?"
  fi

  if [ "$1" = "agent" ]; then
    shift
    if [ "$#" -eq 0 ]; then
      echo 'usage: r agent <task...> | r agent --<delegate flags...>' >&2
      return 2
    fi
    case "$1" in
      -*) "$py" "$repo/src/delegate.py" --agent "$@" ;;
      *)  "$py" "$repo/src/delegate.py" --agent -p "$*" ;;
    esac
    return "$?"
  fi

  local model="$1"
  shift

  # A model with nothing after it would reach the provider with an empty
  # prompt — refuse before any paid call is possible.
  if [ "$#" -eq 0 ]; then
    echo 'usage: r <model> <prompt...> | r <model> --<delegate flags...> | r audit' >&2
    return 2
  fi

  case "$1" in
    -*) "$py" "$repo/src/delegate.py" --model "$model" "$@" ;;
    *)  "$py" "$repo/src/delegate.py" --model "$model" -p "$*" ;;
  esac
}
