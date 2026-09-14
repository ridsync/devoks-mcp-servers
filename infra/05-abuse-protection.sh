#!/usr/bin/env bash
# 남용 방어 (2026-09-14) — 공개 엔드포인트의 비용 폭주·스캐닝 대응
#
# ============================================================================
# 왜 필요했나
#
# 인증은 튼튼했다: 256비트 베어러 토큰, `secrets.compare_digest`, 무단 요청은
# 401(실측). 브루트포스는 비현실적이다. 문제는 **인증이 아니라 남용**이었다 —
# 401 을 내기 *전에* Lambda 가 이미 호출되므로 무단 요청도 과금된다.
#
# 적용 전 상태: 스로틀링 없음 / 예약 동시성 없음 / 예산 알림 없음.
# 계정 동시성 1000 × 요청당 3ms = 이론상 초당 33만 요청, 상한 없음.
# 실측 단가 기준 비용 노출:
#
#     1억 요청  →  Lambda $22 + API GW $123 + CloudWatch Logs $30 = $175
#     10억 요청 →  $1,754
#
# ⚠️ 순서가 중요하다 — Function URL 삭제가 먼저다.
#    Function URL 은 API Gateway 를 **우회**한다. 즉 API GW 에 스로틀링을 걸어도
#    공격자가 Function URL 로 오면 그대로 뚫린다. 우회로를 먼저 없애지 않으면
#    나머지 세 조치가 전부 무의미하다.
#
# 저장소 공개 여부와는 무관한 문제다. 공개면 발견 확률이 높아질 뿐이고,
# 비공개여도 엔드포인트는 어차피 인터넷에 있다.
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
FN=devoks-mcp-management
API="${API_ID:?API_ID=<HTTP API id> 를 지정하라}"
ALERT_EMAIL="${ALERT_EMAIL:?ALERT_EMAIL=<알림 주소> 를 지정하라}"

# --- 1. Function URL 제거 (스로틀링 우회로 차단) -------------------------
# 부트스트랩 때 도메인이 없어서 만든 경로다. 커스텀 도메인이 생긴 뒤로는
# 역할이 끝났고, 남겨두면 방어를 우회하는 뒷문이 된다.
aws lambda delete-function-url-config --function-name "$FN" \
  --profile "$PROFILE" --region "$REGION" 2>/dev/null || true

# 리소스 정책에서 Principal:"*" 를 완전히 걷어낸다. Function URL 설정만 지우고
# 권한을 남기면, 누가 Function URL 을 다시 만드는 순간 즉시 공개로 돌아간다.
for SID in FunctionUrlPublicInvoke FunctionUrlPublicInvokeFunction; do
  aws lambda remove-permission --function-name "$FN" --statement-id "$SID" \
    --profile "$PROFILE" --region "$REGION" 2>/dev/null || true
done
# 검증: Principal:"*" 가 0개여야 한다.
aws lambda get-policy --function-name "$FN" --profile "$PROFILE" --region "$REGION" \
  --query Policy --output text | python3 -c '
import json,sys
n=sum(1 for s in json.load(sys.stdin)["Statement"] if s.get("Principal")=="*")
print(f"Principal:* statements = {n}"); raise SystemExit(0 if n==0 else 1)'

# --- 2. API Gateway 스로틀링 ---------------------------------------------
# rate 10/s 산정 근거: 내부 사용자 1~3명이고 MCP 툴 호출은 순차다. search_code
# 는 GitHub 자체가 분당 10회로 막으므로 초당 10 이면 정상 사용의 수십 배다.
# 429 는 Lambda 를 호출하지 않고 API Gateway 에서 끊기므로 거절 비용이 가장 싸다.
aws apigatewayv2 update-stage --api-id "$API" --stage-name '$default' \
  --default-route-settings 'ThrottlingRateLimit=10,ThrottlingBurstLimit=20,DetailedMetricsEnabled=true' \
  --profile "$PROFILE" --region "$REGION" >/dev/null

# --- 3. Lambda 예약 동시성 (백스톱) --------------------------------------
# 비용 상한은 위 rate limit 이 이미 잡는다. 이건 그게 뚫렸을 때의 블래스트 반경
# 고정이고, 계정 동시성 1000 을 이 함수 혼자 먹는 것도 막는다.
#
# 처음 5 로 잡았다가 10 으로 올렸다. 실측 이유: 60요청 동시 발사 테스트에서
# 429(rate limit)가 아니라 **503 이 48건** 나왔는데, 이는 동시성 5 가 먼저
# 소진된 것이었다. 콜드스타트가 ~1.9초라 5개가 겹치면 정상 사용도 막힌다.
aws lambda put-function-concurrency --function-name "$FN" \
  --reserved-concurrent-executions 10 --profile "$PROFILE" --region "$REGION" >/dev/null

# --- 4. 예산 알림 ---------------------------------------------------------
# 예산 2개까지 무료. FORECASTED 알림이 핵심이다 — 폭주는 월말 청구서가 아니라
# 진행 중에 알아야 한다.
BUDGET=$(mktemp); NOTIF=$(mktemp); trap 'rm -f "$BUDGET" "$NOTIF"' EXIT
cat > "$BUDGET" <<JSON
{"BudgetName":"devoks-mcp-monthly","BudgetLimit":{"Amount":"10","Unit":"USD"},
 "TimeUnit":"MONTHLY","BudgetType":"COST"}
JSON
python3 - "$NOTIF" "$ALERT_EMAIL" <<'PY'
import json, sys
out, email = sys.argv[1], sys.argv[2]
def n(kind, th):
    return {"Notification": {"NotificationType": kind, "ComparisonOperator": "GREATER_THAN",
                             "Threshold": th, "ThresholdType": "PERCENTAGE"},
            "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}]}
json.dump([n("ACTUAL", 50), n("ACTUAL", 100), n("FORECASTED", 100)], open(out, "w"))
PY
aws budgets create-budget --account-id "$ACCOUNT" \
  --budget "file://$BUDGET" --notifications-with-subscribers "file://$NOTIF" \
  --profile "$PROFILE" 2>/dev/null || echo "budget already exists - skipped"

echo "완료. 검증:"
echo "  구 Function URL 은 403 이어야 한다"
echo "  mcp.devoks.kr/healthz 는 200 이어야 한다"
echo "  60요청 동시 발사 시 429/503 이 섞여 나오고, 1초 간격 순차 요청은 전부 200"
