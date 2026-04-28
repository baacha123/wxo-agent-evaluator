#!/bin/bash
# ============================================================
# Deploy WxO Agents & Tools
# ============================================================
# Imports all evaluator agents and tools to your active watsonx
# Orchestrate environment.
#
# Prerequisites:
#   - The `orchestrate` CLI installed and authenticated:
#       orchestrate env activate <your-env>
#   - The AWS backend already deployed (run ./deploy.sh first)
#   - The tool YAMLs updated to point at your API Gateway URL
#       (deploy.sh prints the URL — paste it into tools/v2/*.yaml or
#        set the API_GATEWAY_URL env var and run scripts/patch_tools.sh)
#
# Usage:
#   ./scripts/deploy_wxo.sh
# ============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v orchestrate &> /dev/null; then
    echo -e "${RED}orchestrate CLI not found.${NC}"
    echo "Install: pip install ibm-watsonx-orchestrate"
    exit 1
fi

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  Deploying WxO Evaluator${NC}"
echo -e "${YELLOW}========================================${NC}"

# ------------------------------------------------------------
# 1. Import all tools (must come before agents that reference them)
# ------------------------------------------------------------
echo -e "\n${YELLOW}Importing tools...${NC}"
for f in "$REPO_ROOT"/tools/v2/eval_*.yaml; do
    name=$(basename "$f")
    if grep -q "API_GATEWAY_URL_PLACEHOLDER" "$f"; then
        echo -e "${RED}  $name still has API_GATEWAY_URL_PLACEHOLDER.${NC}"
        echo -e "${RED}  Replace it with your API Gateway URL before importing.${NC}"
        exit 1
    fi
    echo -e "  Importing $name..."
    orchestrate tools import -k openapi -f "$f" 2>&1 | tail -1
done

# ------------------------------------------------------------
# 2. Import collaborator agents (must come before supervisor)
# ------------------------------------------------------------
echo -e "\n${YELLOW}Importing collaborator agents...${NC}"
orchestrate agents import -f "$REPO_ROOT/agents/eval_pipeline_agent.yaml" 2>&1 | tail -1
orchestrate agents import -f "$REPO_ROOT/agents/eval_analyze_agent.yaml"  2>&1 | tail -1
orchestrate agents import -f "$REPO_ROOT/agents/eval_redteam_agent.yaml"  2>&1 | tail -1

# ------------------------------------------------------------
# 3. Import supervisor (references the 3 collaborators above)
# ------------------------------------------------------------
echo -e "\n${YELLOW}Importing supervisor...${NC}"
orchestrate agents import -f "$REPO_ROOT/agents/eval_supervisor_agent.yaml" 2>&1 | tail -1

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  Done!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e ""
echo -e "Open your WxO chat UI and message the ${YELLOW}agent_evaluator${NC} agent:"
echo -e "  > Hi, I want to evaluate an agent"
