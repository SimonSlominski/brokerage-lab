#!/bin/sh
set -eu

# Stop startup if migration or demo initialization fails.
alembic upgrade head
python -m brokerage_lab.demo seed

# Forward termination signals directly to the API process.
exec "$@"
