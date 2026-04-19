#!/usr/bin/env bash
set -euo pipefail

# Ingest notes into the knowledge base via the containerized CLI.
# Called by the ColinCee/notes GitHub Actions workflow on push to main.

NOTES_DIR="${1:-/home/colin/code/notes}"
HOMELAB_DIR="/home/colin/code/homelab"
COMPOSE_FILE="${HOMELAB_DIR}/stacks/knowledge/compose.yaml"

cd "$NOTES_DIR"
git pull --ff-only

cd "$HOMELAB_DIR"
NOTES_DIR="$NOTES_DIR" docker compose -f "$COMPOSE_FILE" --profile ingest run --rm ingest ingest --dir /notes
