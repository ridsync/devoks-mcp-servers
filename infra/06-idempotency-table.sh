#!/usr/bin/env bash
# TASK-022 — 멱등성 DynamoDB 테이블 프로비저닝
# (FRD-SB §4.3 DSN-SB-004 · §5.1 CTR-SB-007 · §2 RES-SB-API-004)
#
# ============================================================================
# 🔴 미실행. 이 스크립트는 아직 통째로도, 개별 명령으로도 실행된 적이 없다
#    (`aws` 호출 금지 지시에 따라 TASK-022 구현 중에는 문법 검사만 했다).
#    메인 루프가 사용자 승인을 받은 뒤 처음 실행하는 시점이 이 조립부의
#    최초 검증이다 — infra/02-secrets.sh · infra/03-lambda.sh가 남긴 것과
#    같은 종류의 "검증 상태" 고지다.
#
# 파티션 키 속성명(`pk`)과 TTL 속성명(`ttl`)은 추측이 아니라
# `servers/slackbot/src/devoks_slackbot/idempotency.py`의 공개 상수
# `PARTITION_KEY_ATTR`/`TTL_ATTRIBUTE`를 읽어 그대로 하드코딩한 값이다
# (2026-09-15 `uv run python -c "from devoks_slackbot import idempotency as i;
# print(i.PARTITION_KEY_ATTR, i.TTL_ATTRIBUTE)"` 실행 결과: `pk ttl`). 코드가
# 이 값을 바꾸면 이 스크립트도 같이 바꿔야 한다 — 어긋나도 배포 전에는 아무
# 징후가 없고, 배포 후에는 `_claim`의 조건부 `PutItem`이 매번 실패해서야
# 드러난다(`ConditionExpression = attribute_not_exists(pk)`가 실제 파티션
# 키와 다른 이름을 참조하게 되므로).
#
# 빌링 모드가 PAY_PER_REQUEST(on-demand)인 이유: `DSN-SB-004`의 근거 자체가
# "쓰기가 이벤트 도착 시에만 일어나고 그 사이엔 0"이다. PROVISIONED로 만들면
# 그 근거가 성립하지 않는다(유휴 시간에도 처리량 요금이 붙는다).
#
# ⚠️ TTL 활성화는 테이블이 ACTIVE가 된 뒤에만 성공한다(CREATING 상태에서
#    `update-time-to-live`를 부르면 실패한다) — `aws dynamodb wait
#    table-exists`로 반드시 먼저 기다린다.
# ⚠️ `update-time-to-live`는 같은 테이블에 대해 1시간 이내 재호출하면
#    `ValidationException`을 던진다(공식 CLI 레퍼런스 기재, 실측 아님 —
#    실측하려면 실제 테이블에 두 번 호출해야 하는데 이 태스크는 AWS 호출이
#    금지돼 있다). 그래서 무조건 호출하지 않고 `describe-time-to-live`로
#    현재 상태를 먼저 확인해 이미 `ENABLED`+속성명 일치면 건너뛴다 — 반복
#    실행 안전성이 이 분기에 달려 있다.
#
# 파티션 키 하나뿐이다(정렬 키·GSI·LSI 없음) — 저장되는 두 종류의 행
# (`event#<event_id>` / `inflight#<user_id>#<thread_ts>`) 모두 파티션 키 값
# 자체가 이미 유일하고, `idempotency.py`는 `Query`/`Scan`을 쓰지 않는다(코드
# 확인: `PutItem`/`UpdateItem`/`GetItem`/`DeleteItem`만 호출) — 안 쓰는
# 인덱스를 만들 이유가 없다.
#
# IAM 정책: 이 스크립트는 정책 **문서**만 만든다(아래 POLICY_FILE). 실행
# 역할에 부착하는 것은 `TASK-023`의 일이다 — handler/worker를 역할 1개로
# 합칠지 2개로 나눌지가 아직 그 태스크의 결정 사항이라(PLAN.md TASK-023:
# "역할별 환경변수·권한 분리"), 여기서 역할을 만들어버리면 그 결정을
# 선점하게 된다. `Resource`는 이 테이블 ARN 하나로 한정했다 — 역할이
# 1개든 2개든 같은 문서를 그대로 붙이면 된다.
#
# 이름: ECR `devoks-slackbot` / Lambda `devoks-slack-handler`·
# `devoks-slack-worker`(TASK-021이 `.github/workflows/ci.yml`에 이미 적어둔
# 제안, TASK-023이 확정)와 같은 계열로 `devoks-slack-idempotency`를 제안한다.
# ci.yml의 `IDEMPOTENCY_TABLE=ci-smoke-idempotency-table`는 스모크 테스트용
# 더미 값이라 이 이름과 무관하다(실배포 테이블명을 참조하지 않는다).
#
# 사용법:  ./infra/06-idempotency-table.sh
#          IDEMPOTENCY_TABLE_NAME=my-table ./infra/06-idempotency-table.sh
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
TABLE_NAME="${IDEMPOTENCY_TABLE_NAME:-devoks-slack-idempotency}"
POLICY_FILE="infra/06-idempotency-table-access-policy.json"

# servers/slackbot/src/devoks_slackbot/idempotency.py의 공개 상수 그대로.
PARTITION_KEY_ATTR="pk" # idempotency.PARTITION_KEY_ATTR
TTL_ATTRIBUTE="ttl"     # idempotency.TTL_ATTRIBUTE

say() { printf '\n== %s\n' "$1"; }

say "DynamoDB table: $TABLE_NAME"
aws dynamodb create-table \
  --table-name "$TABLE_NAME" \
  --attribute-definitions "AttributeName=$PARTITION_KEY_ATTR,AttributeType=S" \
  --key-schema "AttributeName=$PARTITION_KEY_ATTR,KeyType=HASH" \
  --billing-mode PAY_PER_REQUEST \
  --tags Key=Project,Value=devoks-mcp Key=Component,Value=slackbot \
  --profile "$PROFILE" --region "$REGION" \
  --query 'TableDescription.TableStatus' --output text || echo "  (already exists)"

say "테이블이 ACTIVE가 될 때까지 대기 (TTL 설정은 그 전엔 실패한다)"
aws dynamodb wait table-exists --table-name "$TABLE_NAME" --profile "$PROFILE" --region "$REGION"

say "TTL: $TTL_ATTRIBUTE"
CURRENT_TTL_STATUS="$(aws dynamodb describe-time-to-live --table-name "$TABLE_NAME" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'TimeToLiveDescription.TimeToLiveStatus' --output text)"
CURRENT_TTL_ATTR="$(aws dynamodb describe-time-to-live --table-name "$TABLE_NAME" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'TimeToLiveDescription.AttributeName' --output text)"

if [[ "$CURRENT_TTL_STATUS" == "ENABLED" && "$CURRENT_TTL_ATTR" == "$TTL_ATTRIBUTE" ]]; then
  echo "  (이미 ENABLED, 속성명 $CURRENT_TTL_ATTR — update-time-to-live 재호출 생략)"
else
  aws dynamodb update-time-to-live \
    --table-name "$TABLE_NAME" \
    --time-to-live-specification "Enabled=true,AttributeName=$TTL_ATTRIBUTE" \
    --profile "$PROFILE" --region "$REGION" >/dev/null
fi

say "IAM 최소 권한 정책 문서 생성 (부착은 TASK-023 소관)"
TABLE_ARN="$(aws dynamodb describe-table --table-name "$TABLE_NAME" \
  --profile "$PROFILE" --region "$REGION" --query 'Table.TableArn' --output text)"
cat > "$POLICY_FILE" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SlackbotIdempotencyTableReadWrite",
      "Effect": "Allow",
      "Action": [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
        "dynamodb:DeleteItem"
      ],
      "Resource": "$TABLE_ARN"
    }
  ]
}
JSON
echo "  작성: $POLICY_FILE"
echo "  TASK-023 할 일: 이 문서를 handler·worker 각 Lambda 실행 역할에"
echo "  put-role-policy(또는 attach-role-policy, 관리형으로 만든다면)로 부착."
echo "  idempotency.py는 Scan/Query를 쓰지 않으므로 위 4개 Action 외에는"
echo "  추가하지 않는다. 역할을 1개(공유)로 할지 handler/worker 2개로"
echo "  나눌지는 TASK-023의 결정이며, 이 문서는 Resource를 테이블 ARN"
echo "  하나로 한정했으므로 어느 쪽에 붙여도 그대로 쓸 수 있다."

say "검증"
echo "테이블 상태:"
aws dynamodb describe-table --table-name "$TABLE_NAME" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Table.{Status:TableStatus,Billing:BillingModeSummary.BillingMode,Arn:TableArn}' \
  --output table

echo "TTL 상태:"
aws dynamodb describe-time-to-live --table-name "$TABLE_NAME" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'TimeToLiveDescription' --output table

echo
echo "다음 단계 (TASK-023): IDEMPOTENCY_TABLE=$TABLE_NAME"
echo "               테이블 ARN: $TABLE_ARN"
