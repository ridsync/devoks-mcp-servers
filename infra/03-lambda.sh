#!/usr/bin/env bash
# TASK-057 · TASK-058 — Lambda 함수 + Function URL (FRD §10 Stage 2 Step 4~5)
#
# 전제: infra/01-ecr-and-github-oidc.sh (ECR 이미지), infra/02-secrets.sh (SSM).
#
# 2026-09-10에 아래 명령을 순차 실행해 실제로 구축했고, 라이브 검증까지 끝냈다.
# 재현용으로 정리한 파일이며 통째 실행 경로는 미검증이다(02-secrets.sh와 동일한
# 사유 — 샌드박스 분류기가 신규 스크립트 실행을 차단).
#
# ============================================================================
# 여기서 실제로 발목을 잡은 것 (문서·튜토리얼에 잘 안 나오는 것들)
#
# 1) Function URL 403 — 권한 statement가 **두 개** 필요하다.
#    거의 모든 가이드가 `lambda:InvokeFunctionUrl` 하나만 보여주는데, 공식
#    문서는 "If a function's resource-based policy doesn't grant
#    lambda:invokeFunctionUrl **and lambda:InvokeFunction** permissions, users
#    get a 403 Forbidden"이라고 명시한다. 하나만 붙이면 정책·URL 설정이 전부
#    정상으로 보이는데도 모든 요청이 403이고, 오류 메시지는 어느 액션이
#    빠졌는지 알려주지 않는다.
#    두 번째 statement는 `--invoked-via-function-url`(컨텍스트 키
#    `lambda:InvokedViaFunctionUrl`)로 **Function URL 경로에만** 한정해야
#    한다. 이 조건이 없으면 일반 Invoke API로도 누구나 호출할 수 있게 된다.
#
# 2) 환경변수 총량 4 KB (aggregate) 한계.
#    실측 2,252 B / 4,096 B (55%). PEM 1,674 B가 대부분이다. 기본값과 같은
#    선택 키(MCP_PORT·MCP_LOG_LEVEL·MCP_STATELESS_HTTP 등)를 넣지 않는 이유가
#    이 예산이다. RSA 4096비트 키(약 3,250 B)로 바꾸면 총량이 3,800 B대로
#    올라 여유가 거의 사라진다 — 그때는 앱이 SSM을 직접 읽는 방식으로
#    전환해야 한다.
#
# 3) MCP_PUBLIC_URL / MCP_ALLOWED_HOSTS 는 2단계 주입이다.
#    Function URL의 <url-id>는 생성 시점에 정해지므로 함수를 먼저 만들 수밖에
#    없다. config.py가 Fail-Fast라 값이 없으면 기동에 실패하므로, 1단계에서는
#    형식만 유효한 플레이스홀더(placeholder.invalid)를 넣고 URL 확보 후
#    2단계에서 덮는다. `update-function-configuration --environment`는 맵을
#    **전체 교체**하므로 3개만 보내면 나머지 6개가 사라진다.
#
# 4) 메모리 512 MB는 측정에 근거한 선택이다.
#    Init Duration 실측: 512MB 1,923ms / 1024MB 2,007ms / 1769MB 1,877ms —
#    메모리를 올려도 콜드스타트가 개선되지 않는다(노이즈 범위). Max Memory
#    Used는 116 MB. 따라서 여유만 두고 512에 머문다.
#    단, **새 이미지 첫 콜드스타트는 8,511ms**였다(63MB 이미지의 Lambda 내부
#    최적화 1회 비용). 이후 콜드스타트는 ~1.9초. CI 배포(TASK-061) 직후
#    첫 요청은 이 8.5초를 낸다.
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
FN=devoks-mcp-management
ROLE="$FN-lambda"
LOG_GROUP="/aws/lambda/$FN"
REPO_URI="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/devoks-mcp-management"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG=<커밋 SHA> 를 지정하라}"

# --- 1. 로그 그룹: 보존기간을 미리 박는다 --------------------------------
# 함수가 먼저 뜨면 Lambda가 "만료 없음"으로 자동 생성하므로, 여기서 먼저
# 만들어야 한다. 서울 수집 요율 $0.76/GB(실측)라 방치하면 누적된다.
aws logs create-log-group --log-group-name "$LOG_GROUP" \
  --tags Project=devoks-mcp,Component=management \
  --profile "$PROFILE" --region "$REGION" 2>/dev/null || true
aws logs put-retention-policy --log-group-name "$LOG_GROUP" \
  --retention-in-days 30 --profile "$PROFILE" --region "$REGION"

# --- 2. 실행 역할: 최소권한 ----------------------------------------------
# AWSLambdaBasicExecutionRole(관리형)을 쓰지 않는다 — 그것은 logs:* 를
# Resource "*" 로 준다. 아래는 자기 로그그룹과 자기 ECR 리포로 한정한다.
# SSM·KMS 권한은 **주지 않는다**: 시크릿은 환경변수로 주입되므로 런타임이
# SSM을 읽지 않는다(읽게 바꾸면 그때 추가하면 된다).
TRUST=$(mktemp); PERMS=$(mktemp); trap 'rm -f "$TRUST" "$PERMS"' EXIT
cat > "$TRUST" <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON
cat > "$PERMS" <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"WriteOwnLogsOnly","Effect":"Allow",
  "Action":["logs:CreateLogStream","logs:PutLogEvents"],
  "Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:$LOG_GROUP:*"},
 {"Sid":"RecreateOwnLogGroupIfDeleted","Effect":"Allow",
  "Action":"logs:CreateLogGroup",
  "Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:$LOG_GROUP"},
 {"Sid":"PullOwnContainerImage","Effect":"Allow",
  "Action":["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
  "Resource":"arn:aws:ecr:$REGION:$ACCOUNT:repository/devoks-mcp-management"}]}
JSON
# 같은 계정 ECR은 한쪽만 허용하면 된다(공식 문서) — 여기서는 실행 역할의
# identity-based 정책으로 주므로 ECR 리포지토리 정책은 건드리지 않는다.
aws iam create-role --role-name "$ROLE" \
  --assume-role-policy-document "file://$TRUST" \
  --tags Key=Project,Value=devoks-mcp Key=Component,Value=management \
  --profile "$PROFILE" >/dev/null 2>&1 || true
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name "$ROLE-inline" --policy-document "file://$PERMS" --profile "$PROFILE"

# --- 3. 함수 생성 (1단계 env = 플레이스홀더) -----------------------------
# lambda-env.json 은 SSM(시크릿) + .env(비밀 아닌 설정)에서 조립한다 —
# 조립 로직은 이 파일 하단의 주석 참고. 여기서는 이미 만들어진 파일을 쓴다.
ENV_JSON="${ENV_JSON:?ENV_JSON=<lambda-env.json 경로> 를 지정하라}"
aws lambda create-function --function-name "$FN" \
  --package-type Image --code "ImageUri=$REPO_URI:$IMAGE_TAG" \
  --role "arn:aws:iam::$ACCOUNT:role/$ROLE" \
  --architectures arm64 --memory-size 512 --timeout 60 \
  --environment "file://$ENV_JSON" \
  --tags Project=devoks-mcp,Component=management \
  --profile "$PROFILE" --region "$REGION" >/dev/null

aws lambda wait function-active-v2 --function-name "$FN" --profile "$PROFILE" --region "$REGION"

# --- 4. Function URL + 권한 2개 ------------------------------------------
# AuthType=NONE 인 이유: AWS_IAM 은 SigV4 를 Authorization 헤더에 쓰므로
# 우리 Bearer 토큰과 정면 충돌한다. 인증 경계는 Stage 1의 OAuth 2.1
# 리소스 서버(AC-002-*)다.
URL=$(aws lambda create-function-url-config --function-name "$FN" \
  --auth-type NONE --invoke-mode BUFFERED \
  --profile "$PROFILE" --region "$REGION" --query FunctionUrl --output text)

aws lambda add-permission --function-name "$FN" \
  --statement-id FunctionUrlPublicInvoke \
  --action lambda:InvokeFunctionUrl --principal "*" \
  --function-url-auth-type NONE \
  --profile "$PROFILE" --region "$REGION" >/dev/null

# ⚠️ 이 두 번째 statement 없이는 전부 403이다 (위 함정 1번).
aws lambda add-permission --function-name "$FN" \
  --statement-id FunctionUrlPublicInvokeFunction \
  --action lambda:InvokeFunction --principal "*" \
  --invoked-via-function-url \
  --profile "$PROFILE" --region "$REGION" >/dev/null

HOST="${URL#https://}"; HOST="${HOST%/}"
echo "Function URL : $URL"
echo "다음 단계     : MCP_ALLOWED_HOSTS=$HOST"
echo "               MCP_PUBLIC_URL=https://$HOST/mcp   (경로가 /mcp 로 끝나야 한다 — CTR-001)"
echo "               MCP_ISSUER_URL=https://$HOST"
echo "               위 3개를 lambda-env.json 에 반영한 뒤(나머지 6개 키도 함께!)"
echo "               aws lambda update-function-configuration --environment file://\$ENV_JSON"
