#!/usr/bin/env bash
# SIPsmith CI check script — lint, tests, air-gap lint.
# Must pass before any commit. Run from repo root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && cd .. && pwd)"
cd "${ROOT}"

PASS=0
FAIL=0

run_step() {
  local name="$1"; shift
  echo ""
  echo "━━━ ${name} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  if "$@"; then
    echo "  ✓ ${name} passed"
    PASS=$(( PASS + 1 ))
  else
    echo "  ✗ ${name} FAILED"
    FAIL=$(( FAIL + 1 ))
  fi
}

# ── Ruff lint ─────────────────────────────────────────────────────────────
run_step "ruff lint" python3 -m ruff check sipsmith/ plugins/ tests/ alembic/

# ── Ruff format check ─────────────────────────────────────────────────────
run_step "ruff format" python3 -m ruff format --check sipsmith/ plugins/ tests/ alembic/

# ── Pytest ────────────────────────────────────────────────────────────────
run_step "pytest" python3 -m pytest tests/ -q

# ── Air-gap lint: no external URL references in templates/CSS/JS ──────────
run_step "air-gap lint" bash scripts/airgap-lint.sh

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Passed: ${PASS}  Failed: ${FAIL}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[[ "${FAIL}" -eq 0 ]]
