#!/bin/bash
# ============================================================
# Patch tool YAMLs with your API Gateway URL
# ============================================================
# After deploying the AWS backend (./deploy.sh), the API Gateway
# URL is printed at the end. This script substitutes that URL
# into all the tool YAMLs so they point at YOUR backend.
#
# Usage:
#   API_GATEWAY_URL=https://abc123.execute-api.us-east-1.amazonaws.com/prod \
#     ./scripts/patch_tools.sh
# ============================================================

set -e

if [ -z "$API_GATEWAY_URL" ]; then
    echo "ERROR: API_GATEWAY_URL is required."
    echo "Usage: API_GATEWAY_URL=https://... ./scripts/patch_tools.sh"
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$REPO_ROOT/tools/v2"

# Strip trailing slash if present
API_GATEWAY_URL="${API_GATEWAY_URL%/}"

echo "Patching tool YAMLs to point at: $API_GATEWAY_URL"

count=0
for f in "$TOOLS_DIR"/eval_*.yaml; do
    if grep -q "API_GATEWAY_URL_PLACEHOLDER" "$f"; then
        sed -i.bak "s|API_GATEWAY_URL_PLACEHOLDER|$API_GATEWAY_URL|g" "$f"
        rm -f "$f.bak"
        count=$((count+1))
        echo "  patched: $(basename "$f")"
    fi
done

echo ""
echo "Patched $count tool YAML(s)."
echo ""
echo "Next: ./scripts/deploy_wxo.sh"
