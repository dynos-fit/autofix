#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
TARGET="${BIN_DIR}/autofix"

mkdir -p "${BIN_DIR}"
cat > "${TARGET}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="${ROOT_DIR}:\${PYTHONPATH:-}"
exec python3 -m autofix "\$@"
EOF
chmod +x "${TARGET}"

echo "Installed autofix wrapper to ${TARGET}"
echo "Add ${BIN_DIR} to PATH if needed."
