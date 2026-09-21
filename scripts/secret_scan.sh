#!/usr/bin/env bash
# Secret scan — fail closed. Scans tracked files for credentials/keys.
# Usage: scripts/secret_scan.sh [path]
set -uo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

# Only scan files that are (or would be) committed.
mapfile -t FILES < <(git ls-files -z 2>/dev/null | tr '\0' '\n' | grep -v -E '^$' || true)
if [ "${#FILES[@]}" -eq 0 ]; then
  mapfile -t FILES < <(find . -type f \
      -not -path './.git/*' -not -path '*/__pycache__/*' \
      -not -path './.venv/*' -not -name '.env' | sed 's|^\./||')
fi

# Patterns of real secrets (not placeholder names in templates/docs).
PATTERNS=(
  'sk-[A-Za-z0-9]{20,}'
  'ghp_[A-Za-z0-9]{20,}'
  'AKIA[0-9A-Z]{16}'
  'xox[baprs]-[A-Za-z0-9-]{10,}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  '0x[a-fA-F0-9]{64}'
  'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.'
  '(api[_-]?key|apikey|secret|passwd|password|token)["'"'"']?\s*[:=]\s*["'"'"'][A-Za-z0-9/_+\-]{16,}["'"'"']'
)

HITS=0
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  case "$f" in
    # Docs are scanned too: a real credential pasted into a design note is exactly
    # the leak this gate exists for. Example tokens are handled by the
    # REDACTED/PLACEHOLDER whitelist below instead of by skipping whole trees.
    .env.template|*.example|scripts/secret_scan.sh) continue ;;
  esac
  for p in "${PATTERNS[@]}"; do
    if grep -EIn --binary-files=without-match "$p" "$f" 2>/dev/null | grep -v -E 'REDACTED|PLACEHOLDER|your[_-]|<[a-z_]+>|xxx' >/dev/null; then
      echo "SECRET-SCAN: potential secret in $f (pattern: ${p:0:24}...)"
      HITS=$((HITS + 1))
    fi
  done
done

if [ "$HITS" -gt 0 ]; then
  echo "SECRET-SCAN: FAIL ($HITS finding(s)) — remove the secret, use env vars."
  exit 1
fi
echo "SECRET-SCAN: PASS (0 findings, ${#FILES[@]} files scanned)"
exit 0
