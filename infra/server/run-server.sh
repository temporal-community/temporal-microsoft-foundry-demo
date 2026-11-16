#!/bin/sh
# Start the custom server (in-memory SQLite) with the worker controller enabled.
#   ./run-server.sh /tmp/foundry/build/temporal-server-darwin-arm64
# The bin/ directory goes on PATH because the subprocess provider shells out to timeout(1).
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
PATH="$HERE/bin:$PATH" exec "$1" --config-file config/development-sqlite.yaml --allow-no-auth start
