#!/bin/sh
# Build the Temporal server with the Azure Foundry compute provider linked in:
# go.temporal.io/server at a pinned commit of main, with go.temporal.io/auto-scaled-workers
# replaced by a checkout of the branch that carries the provider, plus the dev-only
# subprocess provider for running the demo locally.
#
#   ./build-server.sh [workdir] [GOOS] [GOARCH]
#   ./build-server.sh /tmp/foundry linux amd64      # for a VM or container in Azure
set -eu
WORK="${1:-/tmp/foundry}"
export GOOS="${2:-$(go env GOOS)}" GOARCH="${3:-$(go env GOARCH)}" CGO_ENABLED=0
SERVER_SHA=03ecd1d228d2b8d9d433da55785afc9aeeead918   # temporalio/temporal main, 2026-11-10
# temporalio/temporal-auto-scaled-workers, branch stefan/foundry as tested on 2026-11-16:
# the provider, plus the commit that raises its wait for Foundry from 5 s to 30 s.
# Set PROVIDER_SHA to build another commit of that branch.
PROVIDER_SHA="${PROVIDER_SHA:-fe532885f9a6dd34062f22fe0ded4e674db61ccb}"
mkdir -p "$WORK/build"
[ -d "$WORK/tasw/.git" ] || git clone --quiet https://github.com/temporalio/temporal-auto-scaled-workers "$WORK/tasw"
# Ask for the pinned revision by ID as well, so the build still works if the branch moves.
git -C "$WORK/tasw" fetch --quiet origin
git -C "$WORK/tasw" cat-file -e "$PROVIDER_SHA" 2>/dev/null || git -C "$WORK/tasw" fetch --quiet origin "$PROVIDER_SHA"
git -C "$WORK/tasw" checkout --quiet --detach "$PROVIDER_SHA"
[ -d "$WORK/temporal-src/.git" ] || git clone --quiet --filter=blob:none --no-checkout https://github.com/temporalio/temporal "$WORK/temporal-src"
git -C "$WORK/temporal-src" checkout --quiet --force "$SERVER_SHA"
cd "$WORK/temporal-src"
go mod edit -replace go.temporal.io/auto-scaled-workers="$WORK/tasw"
OUT="$WORK/build/temporal-server-$GOOS-$GOARCH"
go build -mod=mod -tags disable_grpc_modules,computeprovider_subprocess -o "$OUT" ./cmd/server
echo "built $OUT (provider at $(git -C "$WORK/tasw" rev-parse --short HEAD))"
