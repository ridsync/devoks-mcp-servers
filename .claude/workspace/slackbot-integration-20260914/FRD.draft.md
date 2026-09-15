---
doc_type: frd-draft
id: FRD-slackbot-integration-draft
title: Slackbot 연동 (Stage 3) — 입력 초안 원문
updated: 2026-09-14
---

# 출처

`devoks-sdlc:feature-frd-author` 호출 시 전달된 초안. 아래는 **원문 verbatim**이며,
정련 과정에서 사용자 확인으로 확정된 결정은 맨 아래 "확정 결정 누적"에 적는다.
FRD 초안 자체는 Stage 1 FRD(`.claude/workspace/management-mcp-bootstrap-20260903/FRD.md`)
§10 "Stage 3 — Slackbot 연동" 절에서 출발했다.

---

# 초안 원문 (verbatim)

## 목표
Slack에서 자연어로 사내 코드/지식을 물으면 답하는 Slackbot. 기존 Management MCP 서버(https://mcp.devoks.kr/mcp, 가동 중)를 Claude API의 MCP 커넥터로 연결한다.

## 이미 확정된 결정 (사용자 AskUserQuestion 확답 — 재질문 금지)
1. **봇 역할**: LLM 기반 자연어 질의. Claude API(claude-opus-5)가 MCP 4툴을 스스로 조합해 답한다. 명령형 슬래시 커맨드 아님.
2. **연결 방식**: Slack Events API + Lambda. (Socket Mode 아님 — 상시 프로세스가 필요해 Lambda 배포 결정과 상충)
3. **저장소 위치**: 모노레포 `servers/slackbot/`. 루트 pyproject.toml의 `members = ["servers/*"]`가 이미 잡아준다.
4. **사용자 매핑**: 사람별 토큰. Slack user ID ↔ MCP 토큰 1:1. CTR-002가 `{token: {client_id, role, scopes}}` 구조라 서버 코드 변경 0줄로 per-person 감사가 된다.

## 핵심 기술 근거 (claude-api 스킬에서 실측 확인 — 이 전제로 설계할 것)
- **Claude API MCP 커넥터를 쓴다. Slackbot은 MCP 클라이언트를 구현하지 않는다.** 툴 루프도 스키마 관리도 불필요.
- 필수 파라미터 2개가 **반드시 짝**: `mcp_servers=[{type:"url", url, name, authorization_token}]` + `tools=[{type:"mcp_toolset", mcp_server_name:<같은 name>}]`. 하나만 넣으면 검증 오류(문서화된 함정).
- 베타 플래그 `mcp-client-2025-11-20`, `client.beta.messages.create(...)` 경로.
- `authorization_token`이 요청마다 지정되므로 사람별 토큰이 그대로 실린다 → per-person 감사 자동 충족.
- 모델 `claude-opus-5` ($5/$25 per 1M). thinking 기본 on(adaptive), `budget_tokens`는 400 오류. 깊이는 `output_config.effort`. assistant prefill 400 오류.
- MCP 커넥터 가용성: Claude API에서 beta (Bedrock/Vertex 미지원 — 우리는 Claude API 직접 호출이라 무관).

## 아키텍처 (확정)
Slack ──POST──▶ API Gateway ──▶ Lambda(slack-handler): 서명검증 → 3초 내 200 ACK → async invoke
                                 └──▶ Lambda(worker): Claude API(MCP 커넥터) → chat.postMessage

## 반드시 다룰 제약·엣지케이스
- **Slack 3초 ACK 한계** vs 우리 지연: MCP 서버 콜드스타트 ~1.9초 + 툴 호출 0.3~1초 + Claude API 추론 수 초 → 동기 응답 불가능. 즉시 ACK + 비동기 필수.
- **Slack 재시도**: 3초 내 200을 못 받으면 Slack이 같은 이벤트를 재전송(X-Slack-Retry-Num) → 중복 응답 방지 필요.
- **서명 검증**: HMAC-SHA256, `X-Slack-Signature`/`X-Slack-Request-Timestamp`, 5분 타임스탬프 윈도우(리플레이 방지). 검증 실패 시 401.
- **URL 검증 핸드셰이크**: Slack이 최초 등록 시 `type: url_verification` + `challenge`를 보낸다.
- **등록되지 않은 Slack 사용자**: 토큰 매핑이 없으면 거부 + 안내 (fail-safe).
- **API Gateway 통합 타임아웃 30초 하드 한계**(기존 EDGE-018) — handler는 즉시 반환하므로 영향 없지만 worker 경로에는 해당 없음을 명시.
- **Lambda 환경변수 4KB 총량**(기존 EDGE-021) — 사람별 토큰이 늘면 여기 걸린다. 현재 여유 1,855B, 토큰당 약 123B → 15개. 이것이 FRD §10에 기록된 OAuth 전환 트리거 ①과 같은 제약.
- 비용: 질문당 대략 $0.05~$0.15. 프롬프트 캐싱 고려.
- 스레드 컨텍스트 유지 정책, 응답 길이·코드블록 렌더링 정책 (Slack 메시지 길이 제한).

## 산출물
새 워크스페이스에 FRD.md. 기존 Stage 1 FRD와 ID 공간이 겹치지 않게 할 것.

## 금지
- 커밋·푸시·PR 생성 금지.
- AWS 리소스 생성 금지.
- 이미 확정된 4개 결정을 다시 묻지 말 것.

---

# 확정 결정 누적 (정련 과정)

## Slack 공식 문서 실측 (2026-09-14, FRD 작성 전 확인)

| 항목 | 확인된 사실 | 출처 |
|---|---|---|
| 서명 헤더 | `X-Slack-Signature`. **헤더명은 대소문자 무관** — "header names are meant to be case-insensitive, so the letter case should not be assumed" | verifying-requests-from-slack |
| 서명 형식 | `v0=` + HMAC-SHA256 hex digest. "The full signature is formed by prefixing the hex digest with `v0=`" | 〃 |
| 서명 대상 | 요청 **본문**을 SHA-256으로 해싱하고 HMAC signing secret과 결합 | 〃 |
| 타임스탬프 윈도우 | **5분**. "we verify that the timestamp does not differ from local time by more than five minutes" | 〃 |
| ACK 기한 | **3초 이내 HTTP 2xx**. "Your app should respond to the event request with an HTTP 2xx within three seconds" | events-api |
| 재시도 | **3회**, 지수 백오프. `x-slack-retry-num` 헤더에 시도 번호 `1`/`2`/`3` | 〃 |
| URL 검증 | 설정 과정에서 `url_verification` 수신 | 〃 |
| 메시지 길이 | `text`는 **40,000자**에서 절단·분할. Slack 권고는 **4,000자 이하** | changelog/2018-truncating-really-long-messages |

## ID 체계 결정

기존 Stage 1 FRD가 `REQ-001..008` / `CTR-001..011` / `EDGE-001..022` / `DSN-001..008`을 쓰고 있어
**`-SB-` 접두로 분리**한다: `REQ-SB-001`, `AC-SB-001-1`, `CTR-SB-001`, `EDGE-SB-001`, `DSN-SB-001`.
접두 없는 ID(`EDGE-021` 등)는 항상 Stage 1을 가리키므로 교차 참조가 모호하지 않다.
