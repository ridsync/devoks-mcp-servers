#!/usr/bin/env bash
# TASK-024 — 기존 HTTP API에 Slack 이벤트 라우트 추가
# (FRD-SB §4.1 흐름도 · CTR-SB-002 · Stage 1 FRD EDGE-018/EDGE-022)
#
# ============================================================================
# 🔴 미실행. infra/06-idempotency-table.sh · infra/07-slackbot-lambda.sh와 같은
#    사유(`aws` 호출 금지 지시)로 `bash -n` 문법 검사만 했다. 메인 루프가 사용자
#    승인을 받은 뒤 처음 실행하는 시점이 이 조립부의 최초 검증이다.
#
# 전제: infra/04-custom-domain.sh(HTTP API + 커스텀 도메인 `mcp.devoks.kr`,
#       스테이지 `$default`) · infra/05-abuse-protection.sh(스테이지 스로틀
#       rate 10/s·burst 20) · infra/07-slackbot-lambda.sh(`devoks-slack-handler`/
#       `devoks-slack-worker` 함수, handler 자체 타임아웃 10s).
#
# 🔴 이 스크립트는 오직 `devoks-slack-handler`에만 라우트를 추가한다.
#   `devoks-slack-worker`는 이 API(또는 다른 어떤 API Gateway·Function URL)에도
#   절대 연결하지 않는다 — worker의 `POST /events`는 Slack 서명을 검증하지
#   않고, handler가 검증을 끝낸 페이로드를 `lambda:InvokeFunction`으로만
#   받는다는 전제로 안전하다(근거: `servers/slackbot/src/devoks_slackbot/
#   worker.py` 모듈 docstring, `infra/07-slackbot-lambda.sh`의 "만들지 않는
#   것 / 경계" 절). 보안 검증 결과, 2026-09-16.
#
# ----------------------------------------------------------------------------
# 이 API를 다시 만들지 않는다 — 이름으로 조회한다
# ----------------------------------------------------------------------------
# 계정 ID·API ID를 하드코딩하지 않는다(이 저장소 전 스크립트의 관례 —
# `ACCOUNT`는 항상 `sts get-caller-identity`로 조회한다). API ID도 같은 이유로
# `infra/04-custom-domain.sh`가 API를 만들 때 준 이름(`--name "$FN"`, FN이 곧
# management 함수 이름)으로 `get-apis`를 조회해 얻는다 — 이 값이 바뀔 일은
# 없지만, 리전 전체를 뒤지지 않고 이름 하나로 결정적으로 찾을 수 있다.
#
# ----------------------------------------------------------------------------
# `$default`보다 구체 라우트가 우선한다
# ----------------------------------------------------------------------------
# API Gateway HTTP API의 라우팅 규칙: 리터럴 경로가 있는 라우트가 먼저
# 매칭되고, `$default`는 **다른 어떤 라우트도 매칭되지 않았을 때만** 걸리는
# catch-all이다. 지금은 라우트가 `$default` 하나뿐이라 모든 경로(포함
# `/slack/events`)가 management 통합으로 간다 — 이 스크립트가 `POST
# /slack/events` 라우트를 추가해야만 그 경로만 떨어져 나와 handler로 간다.
# `$default` 라우트·통합 자체는 건드리지 않는다(update/delete 호출 없음) —
# 프로덕션 MCP 경로이기 때문이다.
#
# ----------------------------------------------------------------------------
# 통합 타임아웃 — 기존(30,000 ms, 하드 한계)보다 짧게 둔다
# ----------------------------------------------------------------------------
# Stage 1 FRD `EDGE-018`: API Gateway HTTP API 통합 타임아웃의 하드 한계는
# 30,000 ms다(더 늘릴 수 없다). management 통합이 그 한계 그대로(30,000 ms)인
# 이유는 GitHub HTTP 호출이 최대 20초까지 걸릴 수 있어 여유가 필요했기
# 때문이다(management 쪽 `EDGE-018` 코멘트 참고).
#
# handler는 다르다 — `CTR-SB-002`(3초 ACK 예산) 안에서 서명 검증·멱등 판정·
# 비동기 전달만 하고, `infra/07-slackbot-lambda.sh`가 handler 자체 Lambda
# 타임아웃을 10초로 잡아뒀다(그 스크립트의 "실행 한도" 절 — 3초를 살짝 넘겨도
# 콜드스타트 중 강제 종료보다 낫다는 근거). 통합 타임아웃은 **그 10초보다
# 커야** handler 자신의 타임아웃이 먼저 걸려 CloudWatch에 원인이 남는다(API
# Gateway의 불투명한 504가 먼저 뜨지 않는다 — management 쪽 `EDGE-018`
# 코멘트가 GitHub 타임아웃을 20초로 낮춘 것과 같은 이유). 그렇다고 기존과
# 같은 30,000 ms를 그대로 쓸 이유는 없다 — GitHub처럼 최대 20초짜리 외부
# 호출이 handler 안에는 전혀 없다. 그래서 handler의 10초 타임아웃 위에
# 호출 오버헤드 여유(5초)만 얹은 **15,000 ms**를 쓴다 — 10초보다 크므로
# "Lambda 자신의 타임아웃이 먼저 걸린다"는 불변식은 유지하면서, 진짜로
# 어딘가 멈췄을 때 30초가 아니라 15초 만에 실패가 드러난다.
#
# ----------------------------------------------------------------------------
# Lambda 호출 권한 — Function URL과 규칙이 다르다(혼동 금지)
# ----------------------------------------------------------------------------
# Stage 1 `infra/03-lambda.sh`(`EDGE-020` 교훈): **Function URL**은
# `lambda:InvokeFunctionUrl` + `lambda:InvokeFunction` 두 statement가 필요했다
# (공식 문서: 하나만 붙이면 전부 403). 여기는 Function URL이 아니라 API
# Gateway가 REST/HTTP API 경로로 직접 Lambda를 호출하는 경우다 — 공식 문서상
# 필요한 액션은 `lambda:InvokeFunction` **하나**뿐이다(`InvokeFunctionUrl`은
# Function URL 전용 액션이라 이 경로에는 아무 의미가 없다). Function URL
# 관련 권한을 여기 추가하지 않는다 — Stage 2에서 Function URL 자체를
# 이미 삭제했다(`EDGE-022`, `infra/05-abuse-protection.sh`).
#
# `--source-arn`은 **이 API의 이 라우트**(`POST /slack/events`)로 한정한다 —
# `infra/04-custom-domain.sh`가 management에 준 것처럼 `$API/*/*`(전 라우트
# 와일드카드)를 쓰지 않는다. 스테이지 세그먼트만 `*`로 둔다(현재 `$default`
# 하나뿐이라 실질적으로는 동일하지만, 리터럴 `$default`를 셸에서 이스케이프
# 없이 안전하게 쓰기 위해 이 저장소의 다른 스크립트도 스테이지는 와일드카드로
# 남겨두는 쪽을 택해왔다).
#
# 재실행 안전: 같은 `--statement-id`로 `add-permission`을 두 번 부르면
# `ResourceConflictException`이 난다. `infra/03-lambda.sh`(`create-role` 재실행
# 시 `2>/dev/null || true`)와 `infra/07-slackbot-lambda.sh`의 같은 패턴을
# 그대로 따른다 — "이미 있으면 성공으로 친다".
#
# ----------------------------------------------------------------------------
# 스로틀 — 상속을 확인만 한다, 새로 만들지 않는다
# ----------------------------------------------------------------------------
# `infra/05-abuse-protection.sh`가 스테이지(`$default`) 기본 `RouteSettings`에
# `ThrottlingRateLimit=10, ThrottlingBurstLimit=20`을 이미 걸어뒀다. 라우트별
# `RouteSettings`를 새로 지정하지 않으면 스테이지 기본값을 그대로 물려받는다
# (FRD-SB §4.1 "Stage 2 설정 공유"). 이 스크립트는 `update-stage`/
# `update-route`(RouteSettings)를 **호출하지 않는다** — 상속되는 값을
# `get-stage`로 조회해 보여주기만 한다.
#
# ⚠️ 공유의 의미: Slack 트래픽과 MCP 트래픽이 **같은 10 req/s를 나눠 쓴다.**
# 팀 규모(내부 사용자 1~3명, `infra/05-abuse-protection.sh` 근거)에서는
# 충분하지만, 한쪽이 몰리면(예: Slack 멘션 폭주) 다른 쪽(MCP 직접 호출)이
# 429를 받는다. 라우트별로 분리 스로틀을 둘지는 실사용 후 판단할 문제이고,
# 지금은 "공유"가 FRD의 명시적 결정이다 — 여기서 선점하지 않는다.
#
# ----------------------------------------------------------------------------
# 예약 동시성 — handler 10 / worker 5 (`EDGE-022` 남용 방어의 연장)
# ----------------------------------------------------------------------------
# `infra/05-abuse-protection.sh`가 management에 예약 동시성 10을 건 이유는
# rate limit이 아니라 **동시성 소진**이었다 — 5로 잡았다가 60요청 동시 발사
# 테스트에서 503이 48건 나왔고, 원인은 콜드스타트(~1.9초)가 겹치면서 5개
# 슬롯이 순식간에 바닥난 것이었다(그 스크립트의 실측 코멘트).
#
# handler: 같은 이미지 계열(LWA·python:3.14-slim-trixie·arm64)이라 콜드스타트
# 리스크가 같은 부류다. API Gateway 스로틀(10/s)이 유입을 이미 눌러주고
# handler 자신도 짧지만(수백 ms), 순간적으로 여러 Slack 이벤트가 겹치며
# 콜드스타트와 마주칠 수 있다는 점은 management와 동일하므로 같은 값 10을
# 그대로 쓴다 — 5로도 충분할 수 있지만, 이미 한 번 5→10 정정이 실측으로
# 증명된 마당에 같은 실수를 반복할 이유가 없다.
#
# worker: **최대 300초**를 잡고 있고(`CTR-SB-009`), 동시성 1당 Claude API
# 질의 1건(질의당 $0.05~$0.15)이 물린다. 팀 규모(1~3명)에서 동시에 겹치는
# 질의는 많아야 팀원 수 안팎이다. 5로 두면 최악의 경우에도 동시 비용 노출이
# 5 × $0.15 = $0.75/버스트로 제한되고, 팀 전원이 동시에 멘션해도 처리
# 가능한 여유(2배)가 남는다. worker 동시성을 handler보다 낮게 두는 것은
# 의도적이다 — worker 1건이 최대 300초를 붙잡고 있는 동안 그 슬롯은 다른
# 어떤 질의도 처리하지 못하므로, 높은 동시성은 "여러 질의를 더 빨리 처리"가
# 아니라 "동시에 나가는 Claude 비용의 상한을 올리는 것"에 가깝다.
#
# 예약 동시성은 계정 전체 동시성 풀에서 **떼어 가는 것**이다(AWS 공식 문서:
# 예약 설정이 계정의 미예약 풀을 100 밑으로 떨어뜨리면 그 설정 자체가
# 거부된다). management(10) + handler(10) + worker(5) = 25 — 통상적인 계정
# 기본 동시성 한도(리전당 1000, 계정별로 다를 수 있음) 대비 미예약 풀에
# 여유가 충분히 남는다.
#
# 사용법:  ./infra/08-slackbot-route.sh
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"

MANAGEMENT_API_NAME="devoks-mcp-management" # infra/04-custom-domain.sh가 --name으로 붙인 값
STAGE_NAME='$default'
DOMAIN="${DOMAIN:-mcp.devoks.kr}"

HANDLER_FN="devoks-slack-handler" # infra/07-slackbot-lambda.sh와 동일 명명
WORKER_FN="devoks-slack-worker"
HANDLER_ARN="arn:aws:lambda:$REGION:$ACCOUNT:function:$HANDLER_FN"

# servers/slackbot/src/devoks_slackbot/handler.py의 공개 상수 그대로
# (2026-09-15 `grep -n 'SLACK_EVENTS_PATH' servers/slackbot/src/devoks_slackbot/handler.py`
# 실행 결과: `SLACK_EVENTS_PATH: Final[str] = "/slack/events"`). Slack은 이
# 경로로 POST만 보낸다(GET/기타 메서드 없음).
SLACK_EVENTS_PATH="/slack/events"
ROUTE_KEY="POST $SLACK_EVENTS_PATH"

# EDGE-018 하드 한계 30,000ms보다 짧다 — 위 헤더 "통합 타임아웃" 절 근거.
INTEGRATION_TIMEOUT_MILLIS=15000

HANDLER_RESERVED_CONCURRENCY=10
WORKER_RESERVED_CONCURRENCY=5

say() { printf '\n== %s\n' "$1"; }

# ============================================================================
# 1) 기존 HTTP API 조회 (이름으로, ID 하드코딩 금지)
# ============================================================================
say "1) API 조회: $MANAGEMENT_API_NAME"
API_ID="$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='$MANAGEMENT_API_NAME'].ApiId | [0]" \
  --output text --profile "$PROFILE" --region "$REGION")"
if [[ -z "$API_ID" || "$API_ID" == "None" ]]; then
  echo "ERROR: API '$MANAGEMENT_API_NAME'를 찾지 못했다 — infra/04-custom-domain.sh가 먼저 실행됐는지 확인하라" >&2
  exit 1
fi
echo "  API: $API_ID"

# ============================================================================
# 2) slack-handler용 AWS_PROXY 통합 (조회 후 없을 때만 생성 — 재실행 안전)
# ============================================================================
say "2) 통합: $HANDLER_ARN"
INTEGRATION_ID="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
  --query "Items[?IntegrationUri=='$HANDLER_ARN'].IntegrationId | [0]" \
  --output text --profile "$PROFILE" --region "$REGION")"
if [[ -n "$INTEGRATION_ID" && "$INTEGRATION_ID" != "None" ]]; then
  echo "  (이미 존재: IntegrationId=$INTEGRATION_ID — create-integration 생략)"
else
  # --integration-method POST: Lambda 프록시 통합에서 이 값은 클라이언트가
  # 보내는 HTTP 메서드가 아니라 API Gateway가 Lambda를 호출할 때 쓰는
  # 내부 메서드다 — AWS_PROXY 통합은 항상 POST로 고정해야 한다(공식 문서).
  INTEGRATION_ID="$(aws apigatewayv2 create-integration --api-id "$API_ID" \
    --integration-type AWS_PROXY --integration-uri "$HANDLER_ARN" \
    --integration-method POST --payload-format-version 2.0 \
    --timeout-in-millis "$INTEGRATION_TIMEOUT_MILLIS" \
    --query IntegrationId --output text \
    --profile "$PROFILE" --region "$REGION")"
  echo "  생성: IntegrationId=$INTEGRATION_ID"
fi

# ============================================================================
# 3) 라우트: POST /slack/events (조회 후 없을 때만 생성)
# ============================================================================
say "3) 라우트: $ROUTE_KEY"
ROUTE_ID="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
  --query "Items[?RouteKey=='$ROUTE_KEY'].RouteId | [0]" \
  --output text --profile "$PROFILE" --region "$REGION")"
if [[ -n "$ROUTE_ID" && "$ROUTE_ID" != "None" ]]; then
  echo "  (이미 존재: RouteId=$ROUTE_ID — create-route 생략)"
else
  ROUTE_ID="$(aws apigatewayv2 create-route --api-id "$API_ID" \
    --route-key "$ROUTE_KEY" --target "integrations/$INTEGRATION_ID" \
    --query RouteId --output text \
    --profile "$PROFILE" --region "$REGION")"
  echo "  생성: RouteId=$ROUTE_ID"
fi

# ============================================================================
# 4) Lambda 호출 권한 — API Gateway → handler, 이 라우트로만 한정
# ============================================================================
say "4) Lambda 호출 권한: $HANDLER_FN"
# 스테이지는 *(현재 $default 하나뿐), 메서드+경로는 이 라우트로 고정 —
# 위 헤더 "Lambda 호출 권한" 절 참고. Function URL 관련 액션은 추가하지 않는다.
SOURCE_ARN="arn:aws:execute-api:$REGION:$ACCOUNT:$API_ID/*/POST$SLACK_EVENTS_PATH"
aws lambda add-permission --function-name "$HANDLER_FN" \
  --statement-id ApiGatewayInvokeSlackEvents --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com --source-arn "$SOURCE_ARN" \
  --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1 || true

# ============================================================================
# 5) 예약 동시성 — handler/worker 각각 (위 헤더 근거)
# ============================================================================
say "5) 예약 동시성: $HANDLER_FN=$HANDLER_RESERVED_CONCURRENCY / $WORKER_FN=$WORKER_RESERVED_CONCURRENCY"
aws lambda put-function-concurrency --function-name "$HANDLER_FN" \
  --reserved-concurrent-executions "$HANDLER_RESERVED_CONCURRENCY" \
  --profile "$PROFILE" --region "$REGION" >/dev/null
aws lambda put-function-concurrency --function-name "$WORKER_FN" \
  --reserved-concurrent-executions "$WORKER_RESERVED_CONCURRENCY" \
  --profile "$PROFILE" --region "$REGION" >/dev/null

# ============================================================================
# 6) 검증 출력 — 만든 것을 실제로 조회해 보여준다 (기존 스크립트 관례)
# ============================================================================
say "6) 검증"

echo "라우트 목록 (새 라우트와 \$default가 함께 보여야 한다):"
aws apigatewayv2 get-routes --api-id "$API_ID" \
  --query 'Items[].{RouteKey:RouteKey,Target:Target}' --output table \
  --profile "$PROFILE" --region "$REGION"

echo "새 통합 (Lambda ARN·타임아웃·payload 버전):"
aws apigatewayv2 get-integration --api-id "$API_ID" --integration-id "$INTEGRATION_ID" \
  --query '{Uri:IntegrationUri,TimeoutMs:TimeoutInMillis,PayloadFormatVersion:PayloadFormatVersion,IntegrationMethod:IntegrationMethod}' \
  --output table --profile "$PROFILE" --region "$REGION"

echo "스테이지 스로틀 (상속 확인 — 이 스크립트가 만든 게 아니다):"
aws apigatewayv2 get-stage --api-id "$API_ID" --stage-name "$STAGE_NAME" \
  --query '{Rate:DefaultRouteSettings.ThrottlingRateLimit,Burst:DefaultRouteSettings.ThrottlingBurstLimit,AutoDeploy:AutoDeploy}' \
  --output table --profile "$PROFILE" --region "$REGION"

echo "예약 동시성:"
for FN in "$HANDLER_FN" "$WORKER_FN"; do
  CONCURRENCY="$(aws lambda get-function-concurrency --function-name "$FN" \
    --query ReservedConcurrentExecutions --output text \
    --profile "$PROFILE" --region "$REGION")"
  echo "  $FN: $CONCURRENCY"
done

echo
echo "Slack Event Subscription에 등록할 URL (TASK-032):"
echo "  https://$DOMAIN$SLACK_EVENTS_PATH"
