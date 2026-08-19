#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# AnyBox → Obsidian Importer — Run Script
# Usage:
#   ./run-turbo.sh [START] [LIMIT] [WORKERS]
#
# All arguments are optional and fall back to the defaults below.
#
# Examples:
#   ./run-turbo.sh
#   ./run-turbo.sh 0 300
#   ./run-turbo.sh 0 300 8
#   ./run-turbo.sh 300 300 8
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these
# ─────────────────────────────────────────────────────────────────────────────

SKIP_EXISTING=true


# ─────────────────────────────────────────────────────────────────────────────
# HELP
# ─────────────────────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
AnyBox → Obsidian Importer
Converts an AnyBox JSON export into Obsidian-ready Markdown notes.

USAGE:
    ./run-turbo.sh [START] [LIMIT] [WORKERS]
    ./run-turbo.sh -h | --help

ARGUMENTS (all optional, positional):
    START      0-based index of the first item to process.   (default: 0)
    LIMIT      Number of items to process; 0 means all.       (default: 0 = all)
    WORKERS    Number of parallel download workers.           (default: 8)

EXAMPLES:
    ./run-turbo.sh                 # process every item, 8 workers
    ./run-turbo.sh 0 300           # first 300 items
    ./run-turbo.sh 0 300 8         # first 300 items, 8 workers
    ./run-turbo.sh 300 300 8       # items 300–599 (next batch of 300)

NOTES:
    - Run this from the project root:
        /Users/milko/Local/Accounts/Obsidian/anybox-to-obsidian
    - SKIP_EXISTING is set to "$SKIP_EXISTING" inside this script; when true,
      items whose output Markdown file already exists are skipped.
    - This wrapper activates the bundled Python env (anybox-obsidian-env) and
      calls: python anybox_to_obsidian_cookies.py --workers W --start S [--limit L] [--skip-existing]
    - For the full option list (--input, --output, etc.), run:
        python anybox_to_obsidian_cookies.py --help
EOF
}

case "$1" in
    -h|--help|help)
        usage
        exit 0
        ;;
esac


# ─────────────────────────────────────────────────────────────────────────────
# ARGUMENTS
# $1 = START, $2 = LIMIT, $3 = WORKERS
# ─────────────────────────────────────────────────────────────────────────────

START="${1:-0}"
LIMIT="${2:-0}"
WORKERS="${3:-8}"


# ─────────────────────────────────────────────────────────────────────────────
# ACTIVATE PYTHON ENVIRONMENT
# ─────────────────────────────────────────────────────────────────────────────

source "$(dirname "$0")/anybox-obsidian-env/bin/activate"


# ─────────────────────────────────────────────────────────────────────────────
# BUILD THE COMMAND
# ─────────────────────────────────────────────────────────────────────────────

CMD="python anybox_to_obsidian_cookies.py"
CMD="$CMD --workers $WORKERS"
CMD="$CMD --start $START"

# Only pass --limit if greater than 0
if [ "$LIMIT" -gt 0 ]; then
    CMD="$CMD --limit $LIMIT"
fi

# Only pass --skip-existing if true
if [ "$SKIP_EXISTING" = "true" ]; then
    CMD="$CMD --skip-existing"
fi


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

echo "Running: $CMD"
echo ""
$CMD