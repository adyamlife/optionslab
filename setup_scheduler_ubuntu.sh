#!/usr/bin/env bash
# setup_scheduler_ubuntu.sh
# Installs cron jobs for the paper trade engine on Ubuntu 14.04+
#
# Usage:
#   bash setup_scheduler_ubuntu.sh
#
# What it does:
#   - Morning scan  : Mon-Fri 10:00 AM America/New_York  → scripts/morning_scan.py
#   - Evening check : Mon-Fri  5:00 PM America/New_York  → scripts/evening_check.py
#   - Logs to data/morning_scan.log and data/evening_check.log
#   - DST is handled automatically via TZ=America/New_York in each cron command
#
# Prerequisites (install once):
#   sudo apt-get install -y python3 python3-pip
#   pip3 install yfinance requests backports.zoneinfo   # backports.zoneinfo if Python < 3.9
#
# To remove the jobs later:
#   crontab -e   # delete the two OptionLab lines

set -euo pipefail

# ── Resolve project root (directory that contains this script) ──────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR"

# ── Detect Python 3 (prefer virtualenv if present) ──────────────────────────
PYTHON=""
for candidate in \
    "$PROJECT_ROOT/venv/bin/python3" \
    "$PROJECT_ROOT/env/bin/python3" \
    "$PROJECT_ROOT/.venv/bin/python3" \
    "$(which python3 2>/dev/null || true)" \
    "$(which python 2>/dev/null || true)"
do
    if [[ -x "$candidate" ]]; then
        PYTHON="$candidate"
        break
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "ERROR: python3 not found. Install it with:"
    echo "  sudo apt-get install -y python3 python3-pip"
    exit 1
fi

PYTHON_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
echo "Using Python $PYTHON_VER at: $PYTHON"

# ── Warn if Python is older than 3.7 (yfinance minimum) ─────────────────────
PYTHON_MAJOR="$("$PYTHON" -c 'import sys; print(sys.version_info[0])')"
PYTHON_MINOR="$("$PYTHON" -c 'import sys; print(sys.version_info[1])')"
if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 7 ]]; }; then
    echo "WARNING: Python $PYTHON_VER detected. yfinance requires Python >= 3.7."
    echo "  Consider using pyenv: https://github.com/pyenv/pyenv"
    echo "  Continuing anyway — you can fix the Python path in the crontab later."
fi

# ── Paths ────────────────────────────────────────────────────────────────────
MORNING_SCRIPT="$PROJECT_ROOT/scripts/morning_scan.py"
EVENING_SCRIPT="$PROJECT_ROOT/scripts/evening_check.py"
LOG_DIR="$PROJECT_ROOT/data"

# Verify scripts exist
for f in "$MORNING_SCRIPT" "$EVENING_SCRIPT"; do
    if [[ ! -f "$f" ]]; then
        echo "ERROR: Script not found: $f"
        echo "  Make sure you are running this from the project root."
        exit 1
    fi
done

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# Make scripts executable (optional but convenient for direct invocation)
chmod +x "$MORNING_SCRIPT" "$EVENING_SCRIPT" 2>/dev/null || true

# ── Build the two cron lines ─────────────────────────────────────────────────
# TZ=America/New_York is passed inline via env so it doesn't affect other jobs.
# Ubuntu 14.04 Vixie cron supports "env VAR=val command" in the command field.
# Mon-Fri = day-of-week field 1-5.

CRON_MARK="# OptionLab paper trade engine (added by setup_scheduler_ubuntu.sh)"

CRON_MORNING="0 10 * * 1-5 env TZ=America/New_York $PYTHON $MORNING_SCRIPT >> $LOG_DIR/morning_scan.log 2>&1"
CRON_EVENING="0 17 * * 1-5 env TZ=America/New_York $PYTHON $EVENING_SCRIPT >> $LOG_DIR/evening_check.log 2>&1"

# ── Install into crontab ──────────────────────────────────────────────────────
# Approach: read current crontab, strip any old OptionLab lines, append new ones.
EXISTING_CRONTAB="$(crontab -l 2>/dev/null | grep -v 'morning_scan\|evening_check\|OptionLab paper trade' || true)"

NEW_CRONTAB="${EXISTING_CRONTAB}
${CRON_MARK}
${CRON_MORNING}
${CRON_EVENING}"

echo "$NEW_CRONTAB" | crontab -

# ── Verify ───────────────────────────────────────────────────────────────────
echo ""
echo "Cron jobs installed. Current OptionLab entries:"
echo "─────────────────────────────────────────────────────────────────────────"
crontab -l | grep -E 'morning_scan|evening_check|OptionLab'
echo "─────────────────────────────────────────────────────────────────────────"
echo ""
echo "Schedule (America/New_York — DST handled automatically):"
echo "  Morning Scan  : Mon-Fri 10:00 AM ET  →  $MORNING_SCRIPT"
echo "  Evening Check : Mon-Fri  5:00 PM ET  →  $EVENING_SCRIPT"
echo ""
echo "Logs:"
echo "  $LOG_DIR/morning_scan.log"
echo "  $LOG_DIR/evening_check.log"
echo ""
echo "To view scheduled jobs : crontab -l"
echo "To edit / remove jobs  : crontab -e"
echo ""

# ── Dependency check (informational, not fatal) ───────────────────────────────
echo "Checking Python dependencies..."
MISSING=()
for pkg in yfinance flask toml; do
    if ! "$PYTHON" -c "import $pkg" 2>/dev/null; then
        MISSING+=("$pkg")
    fi
done

# backports.zoneinfo needed only for Python < 3.9
if [[ "$PYTHON_MAJOR" -lt 3 ]] || { [[ "$PYTHON_MAJOR" -eq 3 ]] && [[ "$PYTHON_MINOR" -lt 9 ]]; }; then
    if ! "$PYTHON" -c "import backports.zoneinfo" 2>/dev/null; then
        MISSING+=("backports.zoneinfo")
    fi
fi

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    echo "WARNING: Missing Python packages: ${MISSING[*]}"
    echo "  Install with:"
    echo "    pip3 install ${MISSING[*]}"
else
    echo "All required Python packages found."
fi

echo ""
echo "Done."
