#!/usr/bin/env bash
#
# install.sh — set up the autofix CLI in a Python venv and verify the
# entry point resolves.
#
# Usage:
#   ./install.sh                      # base install (scan, replay, export-sarif, policy)
#   ./install.sh --with-watch         # + Watchman-backed watcher (needs `watchman` binary)
#   ./install.sh --with-dedup         # + sentence-transformers / hnswlib semantic dedup
#   ./install.sh --with-jsts          # + tree-sitter-typescript adapter
#   ./install.sh --with-go            # + tree-sitter-go adapter
#   ./install.sh --with-otlp          # + OpenTelemetry OTLP exporter
#   ./install.sh --dev                # + jsonschema (test extras)
#   ./install.sh --all                # everything except --dev
#   ./install.sh --venv .venv-autofix # use a different venv path (default: .venv)
#   ./install.sh --no-venv            # install into the current Python env
#
# Combine flags freely, e.g. `./install.sh --with-watch --with-dedup --dev`.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV_PATH=".venv"
USE_VENV=1
EXTRAS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-watch)  EXTRAS+=("watch") ;;
    --with-dedup)  EXTRAS+=("dedup") ;;
    --with-jsts)   EXTRAS+=("jsts") ;;
    --with-go)     EXTRAS+=("go") ;;
    --with-otlp)   EXTRAS+=("telemetry-otlp") ;;
    --dev)         EXTRAS+=("test") ;;
    --all)         EXTRAS+=("watch" "dedup" "jsts" "go" "telemetry-otlp") ;;
    --venv)        VENV_PATH="$2"; shift ;;
    --no-venv)     USE_VENV=0 ;;
    -h|--help)
      awk '/^#!/ {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
      exit 0
      ;;
    *)
      echo "install.sh: unknown flag: $1" >&2
      echo "Run './install.sh --help' for usage." >&2
      exit 2
      ;;
  esac
  shift
done

# Pick the python interpreter. Honor $PYTHON if set; otherwise prefer 3.11
# /3.12 because the current dep pins (tree-sitter-python<0.22) don't ship
# wheels for 3.13+. Override with PYTHON=python3.13 ./install.sh if needed.
if [[ -n "${PYTHON:-}" ]]; then
  PY="$PYTHON"
elif command -v python3.12 >/dev/null 2>&1; then PY=python3.12
elif command -v python3.11 >/dev/null 2>&1; then PY=python3.11
elif command -v python3.13 >/dev/null 2>&1; then PY=python3.13
elif command -v python3 >/dev/null 2>&1; then PY=python3
else
  echo "install.sh: python3 (>=3.11) not found on PATH" >&2
  exit 1
fi

# Verify the chosen python is >=3.11.
"$PY" - <<'PY' || { echo "install.sh: python >=3.11 required" >&2; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

echo "==> Using $($PY --version) at $(command -v "$PY")"

# Set up the venv unless --no-venv.
if [[ "$USE_VENV" -eq 1 ]]; then
  if [[ ! -d "$VENV_PATH" ]]; then
    echo "==> Creating venv at $VENV_PATH"
    "$PY" -m venv "$VENV_PATH"
  else
    echo "==> Reusing existing venv at $VENV_PATH"
  fi
  # shellcheck disable=SC1091
  source "$VENV_PATH/bin/activate"
  PY=python
fi

echo "==> Upgrading pip"
"$PY" -m pip install --upgrade pip >/dev/null

# Build the install spec: `autofix-scanner[ext1,ext2,...]` if any extras.
SPEC="."
if [[ "${#EXTRAS[@]}" -gt 0 ]]; then
  # de-dup the extras list
  IFS=$'\n' UNIQ=($(printf '%s\n' "${EXTRAS[@]}" | sort -u)); unset IFS
  JOINED="$(IFS=,; echo "${UNIQ[*]}")"
  SPEC=".[$JOINED]"
  echo "==> Installing extras: $JOINED"
fi

echo "==> pip install -e $SPEC"
"$PY" -m pip install -e "$SPEC"

# Watchman binary check when --with-watch was requested.
if [[ " ${EXTRAS[*]:-} " == *" watch "* ]]; then
  if ! command -v watchman >/dev/null 2>&1; then
    echo
    echo "WARNING: --with-watch installed pywatchman but the \`watchman\` daemon binary"
    echo "         is NOT on PATH. autofix watch will fail at invocation time"
    echo "         with a clear diagnostic. Install the daemon:"
    echo
    echo "           macOS:        brew install watchman"
    echo "           Debian/Ubuntu: apt install watchman"
    echo "           Other:         https://facebook.github.io/watchman/docs/install.html"
    echo
  fi
fi

# Smoke-test the console script.
echo "==> Verifying entry point"
autofix --help >/dev/null 2>&1 \
  && echo "    autofix: $(command -v autofix) ✓" \
  || { echo "    autofix entry point FAILED" >&2; exit 1; }

cat <<'NEXT'

==> Install complete.

Next steps:

  # (only when you used a venv) activate it in this shell:
  source .venv/bin/activate

  # one-off scan against a target repo:
  autofix scan --root /path/to/repo

  # inspect the policy (read-only):
  autofix policy --show --root /path/to/repo

  # long-running watcher (needs --with-watch + watchman daemon):
  autofix watch --root /path/to/repo --safety-sweep 30m

Docs:
  - README.md                     (overview)
  - CHANGELOG.md                  (recent cleanup notes)
  - docs/AGENTIC_LLM_BACKENDS.md  (LLM backend config)
  - docs/AUTOFIX_STANDALONE.md    (operations runbook)
NEXT
