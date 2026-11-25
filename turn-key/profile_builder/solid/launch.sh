#!/usr/bin/env bash
# Start the Turn-Key Profile Builder dev server
# Usage: ./launch.sh

set -e

cd "$(dirname "$0")"

# Kill any existing vite on port 5173
lsof -ti:5173 2>/dev/null | xargs kill -9 2>/dev/null || true

echo "Starting Turn-Key Profile Builder..."
echo "Open http://127.0.0.1:5173 in your browser"
echo ""

npx vite --port 5173 --host 0.0.0.0
