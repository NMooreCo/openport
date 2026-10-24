#!/usr/bin/env bash
# Deploy the OpenPort dashboard as a Nebius AI Cloud Serverless Endpoint, and print its URL.
#
# Needs the nebius CLI with a profile (`nebius profile create`), docker and jq. On Windows, run it in WSL
# with Docker Desktop's WSL integration on. From the repo root:
#
#   REGION=eu-north1 SUBNET_ID=<id> SECRET=openport-keys scripts/deploy_endpoint.sh
#
# SECRET is a MysteryBox secret holding the API keys, created once with your own values:
#
#   nebius mysterybox secret create --name openport-keys --secret-version-payload \
#     '[{"key": "NEBIUS_API_KEY", "string_value": "..."}, {"key": "TAVILY_API_KEY", "string_value": "..."}]'
#
# The keys reach the endpoint only from MysteryBox: they're never in the image or on this command line.
# The image runs with OPENPORT_RUN_REPO_CODE=vetted, so only the demo apps' code runs (see the README).
# The endpoint bills while it runs. When the demo is over: nebius ai endpoint delete <ID>
set -euo pipefail

REGION=${REGION:?set REGION to your project region, e.g. eu-north1}
SUBNET_ID=${SUBNET_ID:?set SUBNET_ID (find it with: nebius vpc subnet list)}
SECRET=${SECRET:?set SECRET to the MysteryBox secret with NEBIUS_API_KEY and TAVILY_API_KEY}
NAME=${NAME:-openport-dashboard}
PLATFORM=${PLATFORM:-cpu-d3}
PRESET=${PRESET:-4vcpu-16gb}
TAG=${TAG:-$(git rev-parse --short HEAD)}
BUDGET=${BUDGET:-2}  # USD a day that visitors' live runs may spend on Token Factory; recorded demo runs are free

# 1. A container registry named "openport", made on the first run.
REGISTRY_ID=$(nebius registry list --format json | jq -r '.items[]? | select(.metadata.name == "openport") | .metadata.id' | head -1)
if [ -z "$REGISTRY_ID" ]; then
  REGISTRY_ID=$(nebius registry create --name openport --format json | jq -r '.metadata.id')
fi
IMAGE="cr.$REGION.nebius.cloud/${REGISTRY_ID#registry-}/openport:$TAG"

# 2. Build and push the image. The credential helper signs Docker in with your nebius profile.
nebius registry configure-helper
docker build -t "$IMAGE" .
docker push "$IMAGE"
echo "Image: $IMAGE   (also what openport tune --jobs takes as --job-image)"

# 3. The endpoint: the dashboard on port 8000, open to anyone with the URL.
nebius ai endpoint create --name "$NAME" --image "$IMAGE" --container-port 8000 \
  --platform "$PLATFORM" --preset "$PRESET" --subnet-id "$SUBNET_ID" --env "OPENPORT_DAILY_BUDGET_USD=$BUDGET" \
  --env-secret "NEBIUS_API_KEY=$SECRET" --env-secret "TAVILY_API_KEY=$SECRET"
ENDPOINT_ID=$(nebius ai endpoint get-by-name --name "$NAME" --format jsonpath='{.metadata.id}')
echo "Endpoint: $ENDPOINT_ID"
until URL=$(nebius ai endpoint get "$ENDPOINT_ID" --format json | jq -r '.status.public_endpoints[]? | select(startswith("https://"))' | head -1) && [ -n "$URL" ]; do
  sleep 10  # the URL appears once the endpoint is running
done
echo "Dashboard: $URL"
