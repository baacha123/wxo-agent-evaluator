#!/bin/bash

# WxO Agent Evaluator — AWS Deployment Script
# Deploys: 3 Lambdas + API Gateway + DynamoDB + Step Functions + S3 EventBridge
#
# Architecture:
#   Lambda 1: wxo-eval-api (API Gateway handler)
#   Lambda 2: wxo-eval-pipeline (Step Functions step dispatcher)
#   Lambda 3: wxo-eval-s3-trigger (S3 upload → Step Functions)
#   DynamoDB: wxo-eval-sessions (session state)
#   Step Functions: wxo-eval-pipeline-sfn
#   S3: wxo-eval-pipeline (data storage)
#   EventBridge: S3 upload notifications

set -e

# Configuration
API_LAMBDA="wxo-eval-api"
PIPELINE_LAMBDA="wxo-eval-pipeline"
S3_TRIGGER_LAMBDA="wxo-eval-s3-trigger"
API_NAME="wxo-eval-api-v2"
ROLE_NAME="wxo-eval-role"
SFN_NAME="wxo-eval-pipeline-sfn"
REDTEAM_SFN_NAME="wxo-eval-redteam-sfn"
SFN_ROLE_NAME="wxo-eval-sfn-role"
DYNAMO_TABLE="wxo-eval-sessions"
REGION="us-east-1"
RUNTIME="python3.12"
TIMEOUT_API=30         # API handler: 30s (WxO 40s timeout)
TIMEOUT_PIPELINE=900   # Pipeline steps: 15 min
TIMEOUT_TRIGGER=30     # S3 trigger: 30s
MEMORY=1024
STAGE_NAME="prod"
S3_BUCKET="wxo-eval-pipeline"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}  WxO Agent Evaluator - Deployment${NC}"
echo -e "${YELLOW}========================================${NC}"

# Check prerequisites
if ! command -v aws &> /dev/null; then
    echo -e "${RED}aws CLI not found. Please install it first.${NC}"
    exit 1
fi

# Load credentials
for EF in ".env" "../.env.aws" "../.env" "../../verint-mvp/config/.env"; do
    if [ -f "$EF" ]; then
        echo -e "${GREEN}Loading keys from $EF...${NC}"
        export $(sed 's/^[[:space:]]*//' "$EF" | grep -v '^\s*#' | grep -v '^$' | grep '=' | xargs)
    fi
done

export AWS_DEFAULT_REGION="${REGION}"

ACCOUNT_ID=$(aws sts get-caller-identity --query 'Account' --output text)
echo -e "${GREEN}Account: $ACCOUNT_ID${NC}"
echo -e "${GREEN}Region:  $REGION${NC}"

# ============================================================
# Step 1: S3 Bucket + EventBridge notifications
# ============================================================
echo -e "\n${YELLOW}Step 1: S3 bucket + EventBridge notifications...${NC}"

if aws s3api head-bucket --bucket $S3_BUCKET 2>/dev/null; then
    echo -e "${GREEN}Bucket exists: $S3_BUCKET${NC}"
else
    aws s3api create-bucket --bucket $S3_BUCKET --region $REGION
    echo -e "${GREEN}Created bucket: $S3_BUCKET${NC}"
fi

# Enable EventBridge notifications on S3
aws s3api put-bucket-notification-configuration \
    --bucket $S3_BUCKET \
    --notification-configuration '{
        "EventBridgeConfiguration": {}
    }' 2>/dev/null || echo -e "${YELLOW}EventBridge notifications may already be configured${NC}"

echo -e "${GREEN}S3 EventBridge notifications enabled${NC}"

# ============================================================
# Step 2: DynamoDB Table
# ============================================================
echo -e "\n${YELLOW}Step 2: DynamoDB table...${NC}"

if aws dynamodb describe-table --table-name $DYNAMO_TABLE 2>/dev/null; then
    echo -e "${GREEN}Table exists: $DYNAMO_TABLE${NC}"
else
    aws dynamodb create-table \
        --table-name $DYNAMO_TABLE \
        --attribute-definitions AttributeName=session_id,AttributeType=S \
        --key-schema AttributeName=session_id,KeyType=HASH \
        --billing-mode PAY_PER_REQUEST \
        --no-cli-pager

    echo -e "${YELLOW}Waiting for table to be active...${NC}"
    aws dynamodb wait table-exists --table-name $DYNAMO_TABLE

    # Enable TTL
    aws dynamodb update-time-to-live \
        --table-name $DYNAMO_TABLE \
        --time-to-live-specification "Enabled=true,AttributeName=expires_at" \
        --no-cli-pager

    echo -e "${GREEN}Created table: $DYNAMO_TABLE (with TTL)${NC}"
fi

# ============================================================
# Step 3: IAM Roles
# ============================================================
echo -e "\n${YELLOW}Step 3: IAM roles...${NC}"

# Lambda execution role
ROLE_ARN=$(aws iam get-role --role-name $ROLE_NAME --query 'Role.Arn' --output text 2>/dev/null || echo "")

if [ -z "$ROLE_ARN" ]; then
    echo -e "${YELLOW}Creating Lambda role: ${ROLE_NAME}...${NC}"

    cat > /tmp/eval-v2-trust.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF

    aws iam create-role \
        --role-name $ROLE_NAME \
        --assume-role-policy-document file:///tmp/eval-v2-trust.json \
        --no-cli-pager

    aws iam attach-role-policy --role-name $ROLE_NAME \
        --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
    aws iam attach-role-policy --role-name $ROLE_NAME \
        --policy-arn arn:aws:iam::aws:policy/AmazonS3FullAccess
    aws iam attach-role-policy --role-name $ROLE_NAME \
        --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess

    # Inline policy: Step Functions start execution
    cat > /tmp/eval-v2-sfn-policy.json << POLICY
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "states:StartExecution",
            "Resource": [
                "arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${SFN_NAME}",
                "arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${REDTEAM_SFN_NAME}"
            ]
        }
    ]
}
POLICY
    aws iam put-role-policy \
        --role-name $ROLE_NAME \
        --policy-name "StepFunctionsStart" \
        --policy-document file:///tmp/eval-v2-sfn-policy.json

    ROLE_ARN=$(aws iam get-role --role-name $ROLE_NAME --query 'Role.Arn' --output text)
    echo -e "${YELLOW}Waiting for role propagation (10s)...${NC}"
    sleep 10
    rm -f /tmp/eval-v2-trust.json /tmp/eval-v2-sfn-policy.json
fi

echo -e "${GREEN}Lambda role: $ROLE_ARN${NC}"

# Step Functions execution role
SFN_ROLE_ARN=$(aws iam get-role --role-name $SFN_ROLE_NAME --query 'Role.Arn' --output text 2>/dev/null || echo "")

if [ -z "$SFN_ROLE_ARN" ]; then
    echo -e "${YELLOW}Creating Step Functions role: ${SFN_ROLE_NAME}...${NC}"

    cat > /tmp/sfn-trust.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "states.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF

    aws iam create-role \
        --role-name $SFN_ROLE_NAME \
        --assume-role-policy-document file:///tmp/sfn-trust.json \
        --no-cli-pager

    cat > /tmp/sfn-policy.json << POLICY
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "lambda:InvokeFunction",
            "Resource": [
                "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PIPELINE_LAMBDA}",
                "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PIPELINE_LAMBDA}:*"
            ]
        }
    ]
}
POLICY
    aws iam put-role-policy \
        --role-name $SFN_ROLE_NAME \
        --policy-name "InvokePipelineLambda" \
        --policy-document file:///tmp/sfn-policy.json

    SFN_ROLE_ARN=$(aws iam get-role --role-name $SFN_ROLE_NAME --query 'Role.Arn' --output text)
    echo -e "${YELLOW}Waiting for role propagation (10s)...${NC}"
    sleep 10
    rm -f /tmp/sfn-trust.json /tmp/sfn-policy.json
fi

echo -e "${GREEN}SFN role: $SFN_ROLE_ARN${NC}"

# ============================================================
# Step 4: Build deployment packages
# ============================================================
echo -e "\n${YELLOW}Step 4: Building deployment packages...${NC}"

rm -rf package lambda-api.zip lambda-pipeline.zip lambda-trigger.zip
mkdir -p package

# Install dependencies
pip3 install -r requirements.txt -t package/ \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --python-version 3.12 \
    --only-binary=:all: \
    --quiet

# Common modules
cp storage.py package/
cp auth.py package/
cp session_store.py package/
cp -r pipeline package/

# --- API Lambda package ---
cp api_handler.py package/
cd package
zip -r ../lambda-api.zip . -x "*.pyc" -x "__pycache__/*" --quiet
cd ..
rm package/api_handler.py

# --- Pipeline Lambda package ---
cp pipeline_handler.py package/
cd package
zip -r ../lambda-pipeline.zip . -x "*.pyc" -x "__pycache__/*" --quiet
cd ..
rm package/pipeline_handler.py

# --- S3 Trigger Lambda package ---
cp s3_trigger_handler.py package/
cd package
zip -r ../lambda-trigger.zip . -x "*.pyc" -x "__pycache__/*" --quiet
cd ..
rm package/s3_trigger_handler.py

echo -e "${GREEN}Packages built: lambda-api.zip, lambda-pipeline.zip, lambda-trigger.zip${NC}"

# ============================================================
# Step 5: Deploy Lambda functions
# ============================================================
echo -e "\n${YELLOW}Step 5: Deploying Lambda functions...${NC}"

ENV_VARS_BASE="S3_BUCKET=${S3_BUCKET},DYNAMODB_TABLE=${DYNAMO_TABLE},AWS_REGION_CUSTOM=${REGION}"
[ -n "$WXO_API_KEY" ] && ENV_VARS_BASE="${ENV_VARS_BASE},WXO_API_KEY=${WXO_API_KEY}"
[ -n "$WXO_INSTANCE_URL" ] && ENV_VARS_BASE="${ENV_VARS_BASE},WXO_INSTANCE_URL=${WXO_INSTANCE_URL}"
[ -n "$OPENAI_API_KEY" ] && ENV_VARS_BASE="${ENV_VARS_BASE},OPENAI_API_KEY=${OPENAI_API_KEY}"

deploy_lambda() {
    local FUNC_NAME=$1
    local ZIP_FILE=$2
    local HANDLER=$3
    local TIMEOUT=$4
    local EXTRA_ENV=$5

    local ENV_VARS="Variables={${ENV_VARS_BASE}"
    [ -n "$EXTRA_ENV" ] && ENV_VARS="${ENV_VARS},${EXTRA_ENV}"
    ENV_VARS="${ENV_VARS}}"

    if aws lambda get-function --function-name $FUNC_NAME 2>/dev/null; then
        echo -e "  Updating $FUNC_NAME..."
        aws lambda update-function-code \
            --function-name $FUNC_NAME \
            --zip-file fileb://$ZIP_FILE \
            --no-cli-pager > /dev/null

        aws lambda wait function-updated --function-name $FUNC_NAME

        aws lambda update-function-configuration \
            --function-name $FUNC_NAME \
            --timeout $TIMEOUT \
            --memory-size $MEMORY \
            --handler $HANDLER \
            --environment "$ENV_VARS" \
            --no-cli-pager > /dev/null
    else
        echo -e "  Creating $FUNC_NAME..."
        aws lambda create-function \
            --function-name $FUNC_NAME \
            --runtime $RUNTIME \
            --role $ROLE_ARN \
            --handler $HANDLER \
            --zip-file fileb://$ZIP_FILE \
            --timeout $TIMEOUT \
            --memory-size $MEMORY \
            --environment "$ENV_VARS" \
            --no-cli-pager > /dev/null

        aws lambda wait function-active --function-name $FUNC_NAME
    fi
    echo -e "  ${GREEN}✓ $FUNC_NAME deployed${NC}"
}

# Deploy all 3 Lambdas (SFN_ARN set after Step Functions creation)
deploy_lambda "$API_LAMBDA" "lambda-api.zip" "api_handler.lambda_handler" "$TIMEOUT_API" ""
deploy_lambda "$PIPELINE_LAMBDA" "lambda-pipeline.zip" "pipeline_handler.lambda_handler" "$TIMEOUT_PIPELINE" ""
deploy_lambda "$S3_TRIGGER_LAMBDA" "lambda-trigger.zip" "s3_trigger_handler.lambda_handler" "$TIMEOUT_TRIGGER" ""

PIPELINE_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PIPELINE_LAMBDA}"

# ============================================================
# Step 6: Step Functions State Machine
# ============================================================
echo -e "\n${YELLOW}Step 6: Step Functions state machine...${NC}"

# Replace placeholder in ASL definition
SFN_DEF=$(cat infrastructure/sfn_definition.json | sed "s|\${PipelineLambdaArn}|${PIPELINE_LAMBDA_ARN}|g")

SFN_ARN=$(aws stepfunctions list-state-machines \
    --query "stateMachines[?name=='${SFN_NAME}'].stateMachineArn" \
    --output text 2>/dev/null || echo "")

if [ -n "$SFN_ARN" ] && [ "$SFN_ARN" != "None" ]; then
    echo -e "${GREEN}Updating existing state machine...${NC}"
    aws stepfunctions update-state-machine \
        --state-machine-arn "$SFN_ARN" \
        --definition "$SFN_DEF" \
        --role-arn "$SFN_ROLE_ARN" \
        --no-cli-pager > /dev/null
else
    SFN_ARN=$(aws stepfunctions create-state-machine \
        --name "$SFN_NAME" \
        --definition "$SFN_DEF" \
        --role-arn "$SFN_ROLE_ARN" \
        --type STANDARD \
        --query 'stateMachineArn' \
        --output text)
fi

echo -e "${GREEN}Step Functions ARN: $SFN_ARN${NC}"

# --- Red Team Step Function ---
echo -e "${YELLOW}Creating Red Team state machine...${NC}"

REDTEAM_SFN_DEF=$(cat infrastructure/sfn_redteam_definition.json | sed "s|\${PipelineLambdaArn}|${PIPELINE_LAMBDA_ARN}|g")

REDTEAM_SFN_ARN=$(aws stepfunctions list-state-machines \
    --query "stateMachines[?name=='${REDTEAM_SFN_NAME}'].stateMachineArn" \
    --output text 2>/dev/null || echo "")

if [ -n "$REDTEAM_SFN_ARN" ] && [ "$REDTEAM_SFN_ARN" != "None" ]; then
    echo -e "${GREEN}Updating existing red team state machine...${NC}"
    aws stepfunctions update-state-machine \
        --state-machine-arn "$REDTEAM_SFN_ARN" \
        --definition "$REDTEAM_SFN_DEF" \
        --role-arn "$SFN_ROLE_ARN" \
        --no-cli-pager > /dev/null
else
    REDTEAM_SFN_ARN=$(aws stepfunctions create-state-machine \
        --name "$REDTEAM_SFN_NAME" \
        --definition "$REDTEAM_SFN_DEF" \
        --role-arn "$SFN_ROLE_ARN" \
        --type STANDARD \
        --query 'stateMachineArn' \
        --output text)
fi

echo -e "${GREEN}Red Team SFN ARN: $REDTEAM_SFN_ARN${NC}"

# Update Lambdas with SFN_ARN and REDTEAM_SFN_ARN
for FUNC in "$API_LAMBDA" "$S3_TRIGGER_LAMBDA"; do
    aws lambda update-function-configuration \
        --function-name $FUNC \
        --environment "Variables={${ENV_VARS_BASE},SFN_ARN=${SFN_ARN},REDTEAM_SFN_ARN=${REDTEAM_SFN_ARN}}" \
        --no-cli-pager > /dev/null 2>&1 || true
done
echo -e "${GREEN}Updated Lambda env vars with SFN_ARN and REDTEAM_SFN_ARN${NC}"

# ============================================================
# Step 7: EventBridge Rule (S3 → Lambda trigger)
# ============================================================
echo -e "\n${YELLOW}Step 7: EventBridge rule for S3 uploads...${NC}"

RULE_NAME="sample-eval-s3-upload"

aws events put-rule \
    --name "$RULE_NAME" \
    --event-pattern "{
        \"source\": [\"aws.s3\"],
        \"detail-type\": [\"Object Created\"],
        \"detail\": {
            \"bucket\": {\"name\": [\"${S3_BUCKET}\"]},
            \"object\": {\"key\": [{\"prefix\": \"uploads/\"}]}
        }
    }" \
    --no-cli-pager > /dev/null

S3_TRIGGER_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${S3_TRIGGER_LAMBDA}"

aws events put-targets \
    --rule "$RULE_NAME" \
    --targets "Id=s3-trigger,Arn=${S3_TRIGGER_ARN}" \
    --no-cli-pager > /dev/null

# Permission for EventBridge to invoke Lambda
aws lambda add-permission \
    --function-name $S3_TRIGGER_LAMBDA \
    --statement-id eventbridge-s3 \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" \
    --no-cli-pager 2>/dev/null || true

echo -e "${GREEN}EventBridge rule created: uploads/ → ${S3_TRIGGER_LAMBDA}${NC}"

# ============================================================
# Step 8: API Gateway
# ============================================================
echo -e "\n${YELLOW}Step 8: API Gateway...${NC}"

API_LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${API_LAMBDA}"

EXISTING_API_ID=$(aws apigateway get-rest-apis --query "items[?name=='$API_NAME'].id" --output text 2>/dev/null || echo "")

if [ -n "$EXISTING_API_ID" ] && [ "$EXISTING_API_ID" != "None" ]; then
    API_ID=$EXISTING_API_ID
    echo -e "${GREEN}Using existing API: $API_ID${NC}"
else
    API_ID=$(aws apigateway create-rest-api \
        --name "$API_NAME" \
        --description "WxO Agent Evaluator API" \
        --endpoint-configuration types=REGIONAL \
        --query 'id' --output text)
    echo -e "${GREEN}Created API: $API_ID${NC}"
fi

ROOT_ID=$(aws apigateway get-resources \
    --rest-api-id $API_ID \
    --query 'items[?path==`/`].id' \
    --output text)

# Create /eval resource
EVAL_ID=$(aws apigateway create-resource \
    --rest-api-id $API_ID \
    --parent-id $ROOT_ID \
    --path-part "eval" \
    --query 'id' --output text 2>/dev/null || \
    aws apigateway get-resources --rest-api-id $API_ID \
    --query 'items[?path==`/eval`].id' --output text)

# Create /eval/session resource
SESSION_ID=$(aws apigateway create-resource \
    --rest-api-id $API_ID \
    --parent-id $EVAL_ID \
    --path-part "session" \
    --query 'id' --output text 2>/dev/null || \
    aws apigateway get-resources --rest-api-id $API_ID \
    --query 'items[?path==`/eval/session`].id' --output text)

setup_method() {
    local RESOURCE_ID=$1
    local RESOURCE_PATH=$2

    echo -e "  ${RESOURCE_PATH}..."

    aws apigateway put-method \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method POST \
        --authorization-type NONE \
        --no-cli-pager 2>/dev/null || true

    aws apigateway put-integration \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method POST \
        --type AWS_PROXY \
        --integration-http-method POST \
        --uri "arn:aws:apigateway:${REGION}:lambda:path/2015-03-31/functions/${API_LAMBDA_ARN}/invocations" \
        --no-cli-pager > /dev/null

    aws apigateway put-method \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --authorization-type NONE \
        --no-cli-pager 2>/dev/null || true

    aws apigateway put-integration \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --type MOCK \
        --request-templates '{"application/json": "{\"statusCode\": 200}"}' \
        --no-cli-pager > /dev/null

    aws apigateway put-method-response \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --status-code 200 \
        --response-parameters '{"method.response.header.Access-Control-Allow-Headers": true, "method.response.header.Access-Control-Allow-Methods": true, "method.response.header.Access-Control-Allow-Origin": true}' \
        --no-cli-pager 2>/dev/null || true

    aws apigateway put-integration-response \
        --rest-api-id $API_ID \
        --resource-id $RESOURCE_ID \
        --http-method OPTIONS \
        --status-code 200 \
        --response-parameters '{"method.response.header.Access-Control-Allow-Headers": "'"'"'Content-Type,Authorization'"'"'", "method.response.header.Access-Control-Allow-Methods": "'"'"'POST,OPTIONS'"'"'", "method.response.header.Access-Control-Allow-Origin": "'"'"'*'"'"'"}' \
        --no-cli-pager 2>/dev/null || true
}

# Session sub-resources
for ENDPOINT in start config; do
    RES_ID=$(aws apigateway create-resource \
        --rest-api-id $API_ID \
        --parent-id $SESSION_ID \
        --path-part "$ENDPOINT" \
        --query 'id' --output text 2>/dev/null || \
        aws apigateway get-resources --rest-api-id $API_ID \
        --query "items[?path==\`/eval/session/${ENDPOINT}\`].id" --output text)
    setup_method $RES_ID "/eval/session/${ENDPOINT}"
done

# Eval sub-resources
for ENDPOINT in upload start status results explain reanalyze redteam; do
    RES_ID=$(aws apigateway create-resource \
        --rest-api-id $API_ID \
        --parent-id $EVAL_ID \
        --path-part "$ENDPOINT" \
        --query 'id' --output text 2>/dev/null || \
        aws apigateway get-resources --rest-api-id $API_ID \
        --query "items[?path==\`/eval/${ENDPOINT}\`].id" --output text)
    setup_method $RES_ID "/eval/${ENDPOINT}"
done

# Red team sub-resources under /eval/redteam
REDTEAM_PARENT_ID=$(aws apigateway get-resources --rest-api-id $API_ID \
    --query 'items[?path==`/eval/redteam`].id' --output text)

for ENDPOINT in list start results; do
    RES_ID=$(aws apigateway create-resource \
        --rest-api-id $API_ID \
        --parent-id $REDTEAM_PARENT_ID \
        --path-part "$ENDPOINT" \
        --query 'id' --output text 2>/dev/null || \
        aws apigateway get-resources --rest-api-id $API_ID \
        --query "items[?path==\`/eval/redteam/${ENDPOINT}\`].id" --output text)
    setup_method $RES_ID "/eval/redteam/${ENDPOINT}"
done

# Lambda permission for API Gateway
aws lambda add-permission \
    --function-name $API_LAMBDA \
    --statement-id apigateway-eval-v2 \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${REGION}:${ACCOUNT_ID}:${API_ID}/*" \
    --no-cli-pager 2>/dev/null || true

# ============================================================
# Step 9: Deploy API
# ============================================================
echo -e "\n${YELLOW}Step 9: Deploying to ${STAGE_NAME} stage...${NC}"

aws apigateway create-deployment \
    --rest-api-id $API_ID \
    --stage-name $STAGE_NAME \
    --no-cli-pager > /dev/null

API_URL="https://${API_ID}.execute-api.${REGION}.amazonaws.com/${STAGE_NAME}"

# ============================================================
# Done!
# ============================================================
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  V2 Deployment Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e ""
echo -e "${GREEN}API URL:       ${API_URL}${NC}"
echo -e "${GREEN}API Lambda:    ${API_LAMBDA}${NC}"
echo -e "${GREEN}Pipeline:      ${PIPELINE_LAMBDA}${NC}"
echo -e "${GREEN}S3 Trigger:    ${S3_TRIGGER_LAMBDA}${NC}"
echo -e "${GREEN}Step Functions: ${SFN_ARN}${NC}"
echo -e "${GREEN}Red Team SFN:  ${REDTEAM_SFN_ARN}${NC}"
echo -e "${GREEN}DynamoDB:      ${DYNAMO_TABLE}${NC}"
echo -e "${GREEN}S3 Bucket:     ${S3_BUCKET}${NC}"
echo -e ""
echo -e "${YELLOW}Test with:${NC}"
echo -e "  curl -X POST ${API_URL}/eval/session/start -H 'Content-Type: application/json' -d '{}'"
echo -e ""
echo -e "${YELLOW}Update tool YAMLs:${NC}"
echo -e "  sed -i '' 's|PLACEHOLDER_API_URL|${API_URL}|g' tools/v2/eval_*.yaml"
echo -e ""
echo -e "${YELLOW}Deploy to WxO (multi-agent):${NC}"
echo -e "  # 1. Import tools"
echo -e "  for f in tools/v2/eval_*.yaml; do orchestrate tools import -k openapi -f \"\$f\"; done"
echo -e ""
echo -e "  # 2. Import collaborator agents FIRST (supervisor references them)"
echo -e "  orchestrate agents import -f agents/eval_pipeline_agent.yaml"
echo -e "  orchestrate agents import -f agents/eval_analyze_agent.yaml"
echo -e "  orchestrate agents import -f agents/eval_redteam_agent.yaml"
echo -e ""
echo -e "  # 3. Import supervisor (routes to collaborators)"
echo -e "  orchestrate agents import -f agents/eval_supervisor_agent.yaml"
echo -e ""
echo -e "  # Rollback to monolithic agent if needed:"
echo -e "  # orchestrate agents import -f agents/sample_eval_agent_v2.yaml"
echo -e ""

# Cleanup
rm -rf package

echo -e "${GREEN}Done! Ready to import into WxO.${NC}"
