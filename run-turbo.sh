#!/bin/bash

# ─────────────────────────────────────────────────────────────────────────────
# AnyBox → Obsidian Importer — Run Script
# Usage:
#   ./run_import.sh [START] [LIMIT] [WORKERS]
#
# All arguments are optional and fall back to the defaults below.
#
# Examples:
#   ./run_import.sh
#   ./run_import.sh 0 300
#   ./run_import.sh 0 300 8
#   ./run_import.sh 300 300 8
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION — edit these
# ─────────────────────────────────────────────────────────────────────────────

INPUT="./data/AnyBoxExport.json"
OUTPUT="./staging"
SKIP_EXISTING=true


# ─────────────────────────────────────────────────────────────────────────────
# ARGUMENTS
# $1 = START, $2 = LIMIT, $3 = WORKERS
# ─────────────────────────────────────────────────────────────────────────────

START="${1:-0}"
LIMIT="${2:-0}"
WORKERS="${3:-8}"


# ─────────────────────────────────────────────────────────────────────────────
# BUILD THE COMMAND
# ─────────────────────────────────────────────────────────────────────────────

CMD="python anybox_to_obsidian_cookies.py"
CMD="$CMD --input $INPUT"
CMD="$CMD --output $OUTPUT"
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