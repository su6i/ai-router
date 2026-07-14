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

  if [ "$1" = "cost" ]; then
    shift
    "$py" "$repo/src/delegate.py" --cost "$@"
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
