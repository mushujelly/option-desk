#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
npm --cache "$PWD/.state/npm-cache" ci
npm --cache "$PWD/.state/npm-cache" --prefix web ci
npm --prefix web run build
if [ ! -f .env ]; then cp .env.example .env; chmod 600 .env; fi
printf '%s\n' 'Setup complete. Run npm run dev. Configuration: .env'
