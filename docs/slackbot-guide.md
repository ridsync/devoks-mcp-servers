# Slackbot 가이드

`servers/slackbot/` — Slack Events API 이벤트를 받아 Claude API의 MCP 커넥터로 Management
MCP 서버에 질의하고 스레드에 답하는 브리지. handler/worker 두 AWS Lambda로 배포돼 있고,
실제 Slack 워크스페이스에서 동작이 확인됐다.

## 처리 흐름

1. **handler Lambda** — Slack 서명 검증(raw bytes, body 파싱 전) → `url_verification`
   challenge 응답 → 봇 자기 메시지 제외 → idempotency claim(DynamoDB 조건부 쓰기) →
   worker 비동기 invoke → 즉시 200 응답
2. **worker Lambda** — Slack user ID → MCP 토큰 자격 조회 → Claude API(MCP 커넥터로
   Management MCP 질의) → 응답 길이 정책 적용(3,500자 상한, 초과 시 절단 명시) → Slack
   스레드에 게시 → 완료 기록

이미지는 1개, ASGI 팩토리 2개(`handler:create_app` / `worker:create_app`)로 Lambda 2개를
분기한다(FRD §4.4) — 이미지를 둘로 나누지 않는다. `anthropic` SDK는 worker 전용 import다
— handler가 이를 import하면 콜드스타트 예산을 잠식하므로, 이 불변식을 테스트로 고정해
뒀다(`DSN-SB-008`).

## 멱등성(idempotency)

두 가지 중복 발생 경로를 모두 방어한다:

- Slack 자체의 웹훅 재시도(최대 3회) — handler의 `claim_event`가 `event_id` 조건부
  쓰기(`attribute_not_exists`)로 원자적으로 막는다.
- Lambda 비동기 invoke 재시도(최대 2회 추가) — worker에서도 별도로 완료 여부를 확인한다.

DynamoDB 조건부 쓰기는 "확인 후 쓰기" 2단계가 아니라 **단일 원자적 연산**이다 — 동시
도착 시 하나만 통과함을 테스트로 고정해 뒀다.

## 배포 인프라

`infra/` 아래 slackbot 전용 스크립트(management의 `01`~`05`에 이어지는 순번):

| 스크립트 | 역할 |
|---|---|
| `06-idempotency-table.sh` | DynamoDB 멱등성 테이블 프로비저닝(on-demand + TTL) |
| `07-slackbot-lambda.sh` | handler/worker Lambda 2개 생성(같은 이미지, `ImageConfig.Command`로 분기) |
| `08-slackbot-route.sh` | 기존 API Gateway에 Slack 이벤트 라우트 추가 |
| `09-slackbot-secrets.sh` | Slack Signing Secret·Bot Token·Anthropic API 키를 SSM `SecureString`에 등록 |
| `10-slackbot-ecr-and-oidc.sh` | slackbot용 ECR 리포지토리 + GitHub OIDC 권한 확장 |
| `11-slackbot-user-tokens.sh` | 사람별 MCP 토큰 발급 + Slack 사용자 매핑(per-person 감사) |

사람마다 MCP 토큰을 하나씩 발급하는 이유는 감사 레코드(`CTR-003`)의 `client_id`가
`slackbot`이 아니라 실제 호출한 사람이 되게 하기 위해서다 — Management MCP 서버 코드는
한 줄도 바뀌지 않는다(`MCP_CLIENT_TOKENS`가 이미 `{token: {client_id, role, scopes}}`
구조이기 때문).

## 더 읽을 것

- `.claude/workspace/slackbot-integration-20260914/FRD.md` — 요구사항(`REQ-SB-*`)·
  설계(`DSN-SB-*`). ID 접두 없는 `CTR-002`/`EDGE-016` 등은 management FRD를 가리킨다.
- `.claude/workspace/slackbot-integration-20260914/PLAN.md` — 작업 분해
- [Management MCP 가이드](management-guide.md) — worker가 질의하는 대상 서버
