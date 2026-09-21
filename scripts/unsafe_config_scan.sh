#!/usr/bin/env bash
# Unsafe-config scan — fail closed. Blocks the red lines of this repo.
# Usage: scripts/unsafe_config_scan.sh [path]
set -uo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

mapfile -t FILES < <(git ls-files -z 2>/dev/null | tr '\0' '\n' | grep -v -E '^$' || true)
if [ "${#FILES[@]}" -eq 0 ]; then
  mapfile -t FILES < <(find . -type f -not -path './.git/*' -not -path '*/__pycache__/*' | sed 's|^\./||')
fi

HITS=0
fail() { echo "UNSAFE-SCAN: $1 ($2)"; HITS=$((HITS + 1)); }

for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  case "$f" in
    # The gate's own test file must contain the violation patterns on purpose;
    # scanning it would make the gate fail on its fixtures forever.
    scripts/unsafe_config_scan.sh|tests/test_unsafe_config_scan.py|docs/*|reports/*|.tmp/*|*.tmp) continue ;;
  esac
  # 1. Live trading / paying money switches must never be turned on in this repo.
  #    Quoted keys (JSON/dict form) count too: {'dry_run': true} is the same switch.
  #    '^[^(]*' keeps keyword arguments out: scheduler.tick(dry_run=True) is a call,
  #    not a configuration switch, and flagging it would make this gate noise.
  if grep -EIn "^[^(]*['\"]?(dry_run|live_trading|payments_live|auto_payout|production_payments)['\"]?[[:space:]]*[:=][[:space:]]*['\"]?(true|True|1)['\"]?" "$f" >/dev/null 2>&1; then
    fail "live-money switch enabled" "$f"
  fi
  # 2. Shuffling a time-ordered log/table destroys auditability.
  if grep -EIn "^[^(]*['\"]?shuffle['\"]?[[:space:]]*[:=][[:space:]]*['\"]?(true|True|yes|1)['\"]?" "$f" >/dev/null 2>&1; then
    fail "shuffle=true on ordered data" "$f"
  fi
  # 3. Cardholder data must never be modelled/stored. Only a *field definition*
  #    (name followed by ':' or '=') is a finding: a security module that keeps a
  #    denylist of forbidden field names (audit.py PII rules) is the opposite of
  #    a violation, and flagging it would train the team to ignore this scan.
  #    \b keeps 'source_span:' / 'span =' out of the result: that was a false
  #    positive which would have taught the team to ignore this scan.
  #    Strict by default. A line may opt out only by carrying an explicit marker
  #    ("unsafe-scan: allow ...") with its reason, which keeps the exception
  #    greppable and reviewable instead of quietly widening the pattern.
  if grep -EIn '\b(card_number|cvv|cvc|cvn|expiry_month|security_code|pan)[[:space:]]*[:=][[:space:]]*' "$f" \
       | grep -v "unsafe-scan: allow" >/dev/null 2>&1; then
    fail "cardholder data field defined" "$f"
  fi
  # 4. Committed env files with real values.
  if [ "$(basename "$f")" = ".env" ]; then
    fail "committed .env file" "$f"
  fi
done

if [ "$HITS" -gt 0 ]; then
  echo "UNSAFE-SCAN: FAIL ($HITS finding(s))"
  exit 1
fi
echo "UNSAFE-SCAN: PASS (0 findings, ${#FILES[@]} files scanned)"
exit 0
