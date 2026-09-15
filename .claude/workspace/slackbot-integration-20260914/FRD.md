---
doc_type: frd
id: FRD-slackbot-integration
title: Slackbot 연동 (Stage 3)
status: approved
owner: ridsync
updated: 2026-09-14
plan: ./PLAN.md
issue: https://github.com/ridsync/devoks-mcp-servers/issues/4
---

# [Slackbot 연동] Feature Requirement Document

> **ID 접두 `-SB-`** — Stage 1 FRD(`../management-mcp-bootstrap-20260903/FRD.md`)가
> `REQ-001..008`/`CTR-001..011`/`EDGE-001..022`/`DSN-001..008`을 쓰고 있다.
> 이 문서의 ID는 전부 `REQ-SB-xxx` 형태이고, **접두 없는 ID는 항상 Stage 1을 가리킨다.**

## 1. Goal

Slack에서 **자연어로** 사내 코드·지식을 물으면 답하는 봇을 만든다. 이미 가동 중인
Management MCP 서버(`https://mcp.devoks.kr/mcp`)를 **Claude API의 MCP 커넥터**로 연결해,
Slackbot 자신은 MCP 클라이언트를 구현하지 않는다.

## 2. Context

- **현재 상황(as-is):** MCP 서버는 완성돼 라이브지만 **접근 경로가 MCP 클라이언트(Claude Code)뿐**이다.
  Stage 1 FRD §2가 상정한 "Slack·Notion·Discord 등 하나의 MCP로 통합" 중 Slack 경로가 비어 있다.
- **관련 기능:** Management MCP 서버(4툴 · OAuth 2.1 리소스 서버 · 감사 로그),
  Stage 2 배포 인프라(API Gateway → Lambda arm64 · LWA 패키징 · CI 자동 배포).
- **이 단계가 여는 것:** 사람 단위 감사. Stage 1의 `CTR-002` 토큰 테이블에 사람별 행을 넣으면
  감사 레코드의 `client_id`가 사람이 되어, "누가 어떤 저장소를 읽었는지"가 남는다.

## 3. Requirements (EARS)

### REQ-SB-001: Slack 이벤트 수신과 서명 검증

- **동작:** Slack Events API가 보내는 POST를 받아 **요청이 진짜 Slack에서 왔는지** 검증한다.
- **사용자 흐름:** Slack 채널에서 `@devoks <질문>` → Slack이 우리 엔드포인트로 POST.
- **AC:**
  - `AC-SB-001-1` WHEN 요청의 `X-Slack-Signature`가 `CTR-SB-001`의 절차로 계산한 값과 일치하면 THE SYSTEM SHALL 이벤트 처리를 계속한다.
  - `AC-SB-001-2` IF 서명이 불일치하면 THEN THE SYSTEM SHALL **HTTP 401**로 거부하고 이벤트를 처리하지 않는다.
  - `AC-SB-001-3` IF `X-Slack-Request-Timestamp`가 현재 시각과 **5분을 초과**해 차이 나면 THEN THE SYSTEM SHALL HTTP 401로 거부한다(리플레이 방지).
  - `AC-SB-001-4` THE SYSTEM SHALL 서명 비교에 **상수 시간 비교**를 쓴다(`AC-002-1`의 토큰 비교와 같은 이유).
  - `AC-SB-001-5` THE SYSTEM SHALL 헤더 이름을 **대소문자 무관**하게 읽는다 — Slack 공식 문서가 대소문자를 가정하지 말라고 명시한다.
  - `AC-SB-001-6` WHEN 요청 본문의 `type`이 `url_verification`이면 THE SYSTEM SHALL **서명 검증을 통과한 뒤** `challenge` 값을 그대로 반환한다.

### REQ-SB-002: 3초 이내 ACK와 비동기 처리 분리

- **동작:** Slack이 요구하는 3초 안에 2xx를 돌려주고, 실제 작업은 별도 실행으로 넘긴다.
- **AC:**
  - `AC-SB-002-1` WHEN 서명 검증을 통과한 이벤트를 받으면 THE SYSTEM SHALL **작업을 비동기로 넘긴 뒤 즉시 HTTP 200**을 반환한다.
  - `AC-SB-002-2` THE SYSTEM SHALL ACK 경로에서 **Claude API도 MCP 서버도 호출하지 않는다** — 둘 다 3초 예산을 넘길 수 있다(`CTR-SB-002`).
  - `AC-SB-002-3` IF 비동기 전달 자체가 실패하면 THEN THE SYSTEM SHALL 그 사실을 로그에 남기고 **여전히 200**을 반환한다 — 500을 반환하면 Slack이 재시도해 중복이 늘어날 뿐 상황이 나아지지 않는다.

### REQ-SB-003: 중복 이벤트 억제 (멱등성)

- **동작:** 같은 Slack 이벤트가 두 번 이상 도착해도 답변은 한 번만 게시한다.
- **AC:**
  - `AC-SB-003-1` WHEN 이미 처리한 `event_id`가 다시 도착하면 THE SYSTEM SHALL 작업을 시작하지 않고 200만 반환한다.
  - `AC-SB-003-2` THE SYSTEM SHALL 멱등 판정을 **처리 시작 전에 원자적으로**(조건부 쓰기) 수행한다 — 조회 후 쓰기로 나누면 동시 도착한 재시도가 둘 다 통과한다.
  - `AC-SB-003-3` THE SYSTEM SHALL 멱등 기록을 `CTR-SB-007`의 TTL 이후 자동 만료시킨다.

### REQ-SB-004: Slack 사용자 → MCP 자격 매핑

- **동작:** Slack 사용자마다 **자기 MCP 토큰**으로 질의해, 감사가 사람 단위로 남게 한다.
- **AC:**
  - `AC-SB-004-1` WHEN 등록된 Slack user ID의 이벤트를 처리하면 THE SYSTEM SHALL 그 사용자에게 매핑된 MCP 토큰을 Claude API 호출의 `authorization_token`으로 싣는다.
  - `AC-SB-004-2` IF Slack user ID가 매핑에 없으면 THEN THE SYSTEM SHALL 질의를 실행하지 않고 **등록이 필요하다는 안내만** 게시한다(fail-safe).
  - `AC-SB-004-3` THE SYSTEM SHALL 미등록 거부 메시지에 **어떤 사용자가 등록돼 있는지 드러내지 않는다**(`AC-003-5`의 두 계층 분리와 같은 방침).
  - `AC-SB-004-4` THE SYSTEM SHALL 토큰 값을 로그·Slack 메시지·오류 응답 어디에도 출력하지 않는다.

### REQ-SB-005: Claude API MCP 커넥터를 통한 질의 처리

- **동작:** 자연어 질문을 Claude API에 넘기고, Claude가 MCP 4툴을 스스로 조합해 답하게 한다.
- **AC:**
  - `AC-SB-005-1` WHEN 질의를 처리하면 THE SYSTEM SHALL `mcp_servers`와 `tools`의 `mcp_toolset` 항목을 **항상 함께** 보낸다(`CTR-SB-004`) — 한쪽만 보내면 API가 검증 오류로 거부한다.
  - `AC-SB-005-2` THE SYSTEM SHALL MCP 클라이언트·툴 루프·툴 스키마를 **구현하지 않는다** — Anthropic이 서버 사이드로 MCP 서버에 접속한다(`DSN-SB-002`).
  - `AC-SB-005-3` IF Claude API가 오류를 반환하면 THEN THE SYSTEM SHALL 사용자에게 **재시도 가능 여부를 담은 안내**를 게시하고, 스택트레이스·API 키·MCP 토큰은 노출하지 않는다.
  - `AC-SB-005-4` IF 응답의 `stop_reason`이 `refusal`이면 THEN THE SYSTEM SHALL 거부되었음을 알리고 `content`를 답변으로 쓰지 않는다.
  - `AC-SB-005-5` THE SYSTEM SHALL `max_tokens`·모델·`effort`를 `CTR-SB-004`의 값으로 고정하고, 사용자 입력이 이를 바꾸지 못하게 한다.

### REQ-SB-006: Slack 응답 게시

- **동작:** 답변을 질문이 온 스레드에 게시한다.
- **AC:**
  - `AC-SB-006-1` WHEN 답변이 준비되면 THE SYSTEM SHALL 원본 메시지의 스레드(`thread_ts`)에 게시한다 — 채널을 어지럽히지 않기 위해서다.
  - `AC-SB-006-2` IF 답변이 `CTR-SB-005`의 길이를 넘으면 THEN THE SYSTEM SHALL 잘린 사실을 명시하며 절단한다 — 말없이 40,000자에서 Slack이 자르게 두지 않는다.
  - `AC-SB-006-3` THE SYSTEM SHALL 봇 자신이 게시한 메시지로 다시 트리거되지 않는다.
  - `AC-SB-006-4` WHEN 처리에 시간이 걸리면 THE SYSTEM SHALL 접수 사실을 먼저 알린다(`CTR-SB-002`의 예산상 즉시 답변이 불가능하므로).

### REQ-SB-007: 관측성과 비용 가시성

- **동작:** 누가 무엇을 물었고 얼마가 들었는지 운영자가 알 수 있게 한다.
- **AC:**
  - `AC-SB-007-1` THE SYSTEM SHALL 질의마다 **한 줄의 구조화 레코드**를 stdout에 남긴다(`CTR-SB-008`).
  - `AC-SB-007-2` THE SYSTEM SHALL 그 레코드에 Claude API의 `usage`(입력·출력·캐시 토큰)를 포함해 비용을 사후 계산 가능하게 한다.
  - `AC-SB-007-3` THE SYSTEM SHALL 레코드에 질문 원문을 통째로 싣지 않는다 — 길이와 해시만 남긴다(`CTR-003`의 `args_summary` 방침과 동일).

### REQ-SB-008: 배포 (기존 인프라 재사용)

- **동작:** Stage 2가 만든 배포 경로를 그대로 타서 새 인프라 패턴을 만들지 않는다.
- **AC:**
  - `AC-SB-008-1` THE SYSTEM SHALL `servers/slackbot/`에 uv workspace 멤버로 배치된다 — 루트 `pyproject.toml`의 `members = ["servers/*"]`가 이미 이를 포함한다.
  - `AC-SB-008-2` THE SYSTEM SHALL Stage 2의 컨테이너 패키징 규약(`CTR-010`·`CTR-011`: arm64 · LWA 확장 · `AWS_LWA_*`)을 따른다.
  - `AC-SB-008-3` THE SYSTEM SHALL 기존 CI의 품질 게이트(ruff·pyright·pytest·OSV 감사)를 통과한다.

## 4. Design Spec

### 4.1 흐름

```
Slack 채널: @devoks 인증 로직 어떻게 돼 있어?
      │
      ▼ POST (X-Slack-Signature, X-Slack-Request-Timestamp)
API Gateway HTTP API  ── rate 10/s · burst 20 (Stage 2 설정 공유)
      │
      ▼
Lambda: slack-handler                    ◀── 3초 예산 안에서만 동작
      ├─ ① 서명 검증 (HMAC-SHA256, 5분 윈도우)      실패 → 401
      ├─ ② url_verification 이면 challenge 반환
      ├─ ③ 멱등 판정 (event_id 조건부 쓰기)          중복 → 200 (작업 없음)
      ├─ ④ 비동기 전달 (Lambda Event invoke)
      └─ ⑤ HTTP 200 즉시 반환
             │
             ▼ (비동기)
Lambda: slack-worker                     ◀── 실행 한도 CTR-SB-009
      ├─ ① Slack user ID → MCP 토큰 조회        미등록 → 안내 게시 후 종료
      ├─ ② "생각 중" 메시지 게시 (thread_ts)
      ├─ ③ Claude API 호출
      │      mcp_servers=[{url: mcp.devoks.kr/mcp, authorization_token: <그 사람 토큰>}]
      │      tools=[{type: mcp_toolset, mcp_server_name: ...}]   ← 짝이 필수
      │            │
      │            ▼ (Anthropic 서버 사이드)
      │      mcp.devoks.kr ── 4툴 ── GitHub
      ├─ ④ 응답 길이 정책 적용 (CTR-SB-005)
      ├─ ⑤ chat.postMessage (thread_ts)
      └─ ⑥ 관측 레코드 1줄 (usage 포함)
```

**설계상 핵심:** `slack-handler`는 **네트워크 I/O를 거의 하지 않는다**. 3초 예산을 지키는
유일한 방법이 그것이고, 그래서 서명 검증(순수 CPU)과 멱등 판정(단일 조건부 쓰기)만 남긴다.

### 4.2 모듈 구조 · 책임

```
servers/slackbot/src/devoks_slackbot/
├─ config.py           # 환경 키 파싱 · Fail-Fast (DSN-SB-005)
├─ slack/
│   ├─ signature.py    # 서명 검증 — 순수 함수 (DSN-SB-007)
│   ├─ events.py       # 이벤트 파싱 · 봇 자기 메시지 판별
│   └─ client.py       # chat.postMessage 래퍼
├─ identity.py         # Slack user ID → MCP 토큰 (DSN-SB-003)
├─ idempotency.py      # event_id 조건부 쓰기 (DSN-SB-004)
├─ ask.py              # Claude API 호출 — MCP 커넥터 계약 (DSN-SB-002)
├─ observability.py    # 관측 레코드 1줄
├─ handler.py          # ① ACK 경로 진입점
└─ worker.py           # ② 비동기 경로 진입점
```

**상태 소유:** 모듈은 전부 무상태다. 유일한 공유 상태는 멱등성 저장소이며,
그 접근은 `idempotency.py` 한 곳으로 제한한다.

### 4.3 적용 설계 결정 (DSN)

| ID | 설계 결정 | 근거 |
|----|-----------|------|
| DSN-SB-001 | **Lambda 2개로 분리**(handler / worker) | 3초 ACK 예산과 실제 작업 시간(수 초~수십 초)이 한 자리에 공존할 수 없다. 하나의 Lambda로 하면 반드시 3초를 넘긴다 |
| DSN-SB-002 | **MCP 클라이언트를 구현하지 않고 Claude API MCP 커넥터에 위임** | 커넥터가 `authorization_token`을 지원해 사람별 토큰을 그대로 실을 수 있다. 툴 루프·스키마 관리·재시도가 전부 사라진다. 대가는 beta 의존(`mcp-client-2025-11-20`)과 Bedrock/Vertex 미지원(우리는 Claude API 직접 호출이라 무관) |
| DSN-SB-003 | **자격 조회를 `identity.py` 한 파일로 국소화** | `DSN-001`이 `verifier.py`로 인증 교체 지점을 국소화한 것과 같은 의도. OAuth 전환 트리거(Stage 1 §10)가 걸리면 이 파일만 바뀐다 |
| DSN-SB-004 | **멱등성 저장소는 DynamoDB(on-demand + TTL)** | Lambda 인스턴스 간 공유 상태가 필요하고, 조건부 쓰기(`attribute_not_exists`)가 `AC-SB-003-2`의 원자성을 그대로 준다. TTL이 만료를 대신해 정리 코드가 없다. 대안: ElastiCache(상시 비용), S3(조건부 쓰기 의미가 약함) |
| DSN-SB-005 | **설정은 Stage 1 `config.py`의 Fail-Fast 패턴을 복제** | 전 오류 수집 후 기동 실패. 미설정을 조용히 넘기면 런타임에야 드러난다(`DSN-006`과 동일 근거) |
| DSN-SB-006 | **컨테이너 패키징은 `servers/management/Dockerfile`을 본떠 재사용** | arm64 · LWA 확장 · `AWS_LWA_*`가 이미 검증됐다(`CTR-011`). 새 패턴을 만들 이유가 없다 |
| DSN-SB-007 | **서명 검증을 순수 함수로 분리** | 입력(본문·헤더·시각·시크릿) → 불리언. HTTP·Lambda 컨텍스트 없이 테스트 가능해야 리플레이·변조 케이스를 전부 고정할 수 있다 |
| DSN-SB-008 | **`slack-handler`는 Claude API SDK를 import 하지 않는다** | 콜드스타트가 3초 예산을 먹는 가장 큰 위험이다(`EDGE-SB-007`). 의존성을 물리적으로 분리해 import 비용 자체를 없앤다 |

### 4.4 모듈/레이어 배치

- **배치:** `servers/slackbot/` — uv workspace 멤버. 루트 `uv.lock` 하나를 공유한다.
- **배포 단위:** 이미지 1개 · Lambda 2개. 두 진입점(`handler.py` / `worker.py`)이 한 이미지에 들어가고
  Lambda마다 다른 핸들러를 가리킨다 — 이미지를 둘로 나누면 빌드·스캔·배포가 두 배가 되는데 얻는 게 없다.
- **`servers/management`와의 관계:** **코드 의존 없음.** 두 서버는 HTTP(MCP)로만 만난다.
  공유하고 싶은 유혹이 있는 것은 `config.py`의 Fail-Fast 패턴인데, **복제**를 택한다 —
  공용 패키지를 만들면 두 서버의 배포가 결합된다.

### 4.5 재사용 대상

- **재사용:** `servers/management/Dockerfile`(패키징), `.github/workflows/ci.yml`(품질 게이트·배포 스텝),
  `infra/03-lambda.sh`(실행 역할·로그 그룹 패턴), `servers/management/tests/conftest.py`(픽스처 규약).
- **변경 최소화:** **MCP 서버 코드는 건드리지 않는다.** 사람별 토큰은 `MCP_CLIENT_TOKENS`에 행을 추가하는
  설정 변경이며 `CTR-002` 스키마 그대로다.

## 5. Contract

### 5.1 핵심 파라미터

| ID | 항목 | 타입/범위 | 기본값 | 의미 |
|----|------|-----------|--------|------|
| CTR-SB-001 | Slack 서명 검증 | 고정 절차 | — | `sig_basestring = "v0:" + timestamp + ":" + raw_body` → HMAC-SHA256(signing_secret) → hex → `"v0=" + digest`. `X-Slack-Signature`와 **상수 시간 비교**. 헤더명 대소문자 무관. **`raw_body`는 파싱 전 바이트여야 한다** — JSON 재직렬화는 키 순서·공백이 달라져 서명이 깨진다 |
| CTR-SB-002 | ACK 예산 | ms | **3,000** | Slack이 요구하는 2xx 기한(공식 문서 실측). 초과 시 재시도 3회(지수 백오프). handler는 이 안에서 서명 검증 + 멱등 판정 + 비동기 전달만 한다 |
| CTR-SB-003 | 타임스탬프 허용 오차 | seconds | **300** (5분) | 공식 문서 실측값. 초과 시 401(리플레이 방지) |
| CTR-SB-004 | Claude API 호출 계약 | 고정값 | — | `model="claude-opus-5"` · `betas=["mcp-client-2025-11-20"]` · `client.beta.messages.create` · `mcp_servers=[{type:"url", url, name, authorization_token}]` **+** `tools=[{type:"mcp_toolset", mcp_server_name:<같은 name>}]` (**짝 필수**) · `output_config={"effort": "medium"}` · `max_tokens=8000`. `thinking`은 지정하지 않는다(Opus 5는 기본 adaptive). `budget_tokens`·assistant prefill은 **400 오류**라 사용 금지 |
| CTR-SB-005 | 응답 길이 상한 | chars, 1..40000 | **3,500** | Slack `text`는 40,000자에서 절단·분할되고 Slack 권고는 4,000자 이하다(실측). 3,500을 상한으로 두고 초과분은 절단 사실을 명시하며 자른다 |
| CTR-SB-007 | 멱등 기록 TTL | seconds, 300..86400 | **3,600** (1시간) | Slack 재시도는 3회·지수 백오프로 수 분 내에 끝나고, Lambda 비동기 재시도도 마찬가지다. 1시간이면 두 창을 모두 덮으면서 저장 비용이 사실상 0이다 |
| CTR-SB-009 | worker 실행 한도 | timeout 60..900 s · memory 512..2048 MB | **300 s / 1024 MB** | Claude API 추론 + MCP 툴 다회 호출의 합을 덮어야 한다. MCP 서버 콜드스타트(`EDGE-016`: 정상 ~1,900 ms, 새 이미지 직후 ~8,511 ms)가 여기 포함된다. **handler(`CTR-SB-002` 3초)와 전혀 다른 예산**이며 둘을 혼동하면 설계가 무너진다. 초기값이며 실측 후 조정한다(§10) |
| CTR-SB-008 | 관측 레코드 필드 | JSON 1줄 | — | `ts`(ISO8601 UTC) · `event`(`slack_query`) · `slack_user_id` · `client_id` · `channel` · `thread_ts` · `question_len` · `question_sha256`(앞 16자) · `outcome`(`ok`\|`denied`\|`error`) · `reason_code`(nullable) · `error_kind`(nullable) · `duration_ms` · `usage`(`input_tokens`·`output_tokens`·`cache_read_input_tokens`) · `request_id`. **질문 원문은 넣지 않는다** |

### 5.2 환경 키

| 키 | 타입 | 기본값 | 용도 |
|----|------|--------|------|
| `SLACK_SIGNING_SECRET` | 문자열 | 없음(**필수**) | `CTR-SB-001` 서명 검증 키 |
| `SLACK_BOT_TOKEN` | 문자열 | 없음(**필수**) | `chat.postMessage`용 `xoxb-` 토큰 |
| `SLACK_BOT_USER_ID` | 문자열 | 없음(**필수**) | 봇 자기 메시지 판별(`AC-SB-006-3`) |
| `ANTHROPIC_API_KEY` | 문자열 | 없음(**필수**, worker만) | Claude API 인증 |
| `MCP_SERVER_URL` | URL | 없음(**필수**, worker만) | `https://mcp.devoks.kr/mcp` |
| `SLACK_USER_TOKEN_MAP` | JSON 문자열 | 없음(**필수**, worker만) | `CTR-SB-006` 사용자 매핑 |
| `IDEMPOTENCY_TABLE` | 문자열 | 없음(**필수**, 공통) | DynamoDB 테이블명. **2026-09-14 `TASK-014` 구현 중 handler 전용 → 공통으로 정정** — worker도 같은 테이블을 쓴다(`EDGE-SB-005` 완료 기록 · `EDGE-SB-015` 코얼레싱 락). 원안대로 handler에만 주면 worker가 기동 실패한다 |
| `WORKER_FUNCTION_NAME` | 문자열 | 없음(**필수**, handler만) | 비동기로 깨울 worker Lambda 이름/ARN (`AC-SB-002-1`). **2026-09-14 `TASK-012` 구현 중 추가** — 원안이 빠뜨린 키다. 이것 없이는 handler가 무엇을 호출할지 알 수 없어 `AC-SB-002-1`이 구현 불가다 |
| `SLACKBOT_MAX_RESPONSE_CHARS` | int | `CTR-SB-005` | 범위 밖이면 기동 실패 |
| `SLACKBOT_IDEMPOTENCY_TTL_SECONDS` | int | `CTR-SB-007` | 범위 밖이면 기동 실패 |
| `SLACKBOT_LOG_LEVEL` | 문자열 | `INFO` | 서버 로그(관측 레코드와 별개) |

> **필수 키가 handler/worker로 갈린다.** 한 이미지를 두 Lambda가 공유하므로 설정 검증도
> **역할별로** 해야 한다 — handler에 `ANTHROPIC_API_KEY`를 요구하면 불필요한 키가 3초 경로의
> 4 KB 예산(`EDGE-021`)을 먹고, worker에 `IDEMPOTENCY_TABLE`을 요구하면 쓰지 않는 권한을 부른다.

### 5.3 Slack 사용자 ↔ MCP 자격 매핑 (CTR-SB-006)

| 항목 | 형식 | Stage 3 초기값 |
|---|---|---|
| `SLACK_USER_TOKEN_MAP` | `{"<slack_user_id>": "<mcp_token>"}` JSON | 운영자가 사람마다 1행 |
| Slack user ID | `U`로 시작하는 대문자·숫자 문자열 (예: `U01ABCDEF`) | 워크스페이스마다 고유 |
| MCP 토큰 | Stage 1 `CTR-002`의 키와 **정확히 일치**해야 한다 | `secrets.token_urlsafe(32)` 43자 |

**이 매핑이 per-person 감사를 만든다.** MCP 서버 쪽 `MCP_CLIENT_TOKENS`에
`{"<token>": {"client_id": "okwon", "role": "reader", "scopes": ["devoks:read"]}}` 행을 두면,
감사 레코드(`CTR-003`)의 `client_id`가 사람이 된다. **MCP 서버 코드 변경은 없다.**

### 5.4 상태 전이 (이벤트 1건)

| 현재 상태 | 조건 | 다음 상태 |
|---|---|---|
| 수신 | 서명 불일치 / 타임스탬프 초과 | **401 종료** |
| 서명 통과 | `type == url_verification` | `challenge` 반환 후 종료 |
| 서명 통과 | `event_id` 이미 존재 | **200 종료**(작업 없음) |
| 서명 통과 | `event_id` 신규 (조건부 쓰기 성공) | 비동기 전달 → **200 반환** |
| worker 시작 | Slack user 미등록 | 안내 게시 후 종료 |
| worker 시작 | 등록됨 | 접수 알림 → Claude API 호출 |
| Claude 응답 | `stop_reason == refusal` | 거부 안내 게시 |
| Claude 응답 | 정상 | 길이 정책 적용 → 스레드 게시 |
| Claude 호출 | 예외 | 오류 안내 게시(내부 정보 없이) |

## 6. Resources & References (착수 전 체크)

### 6.1 참고 코드 / 재사용

- [ ] `servers/management/src/devoks_mcp_management/config.py` — Fail-Fast 전 오류 수집 패턴, `field(repr=False)` 시크릿 은닉
- [ ] `servers/management/src/devoks_mcp_management/auth/verifier.py` — 상수 시간 비교, bytes 비교(`TASK-043`의 비ASCII 교훈)
- [ ] `servers/management/src/devoks_mcp_management/audit/logger.py` — JSON 1줄 관측 레코드
- [ ] `servers/management/Dockerfile` — arm64 · LWA(`CTR-011`) 패키징
- [ ] `servers/management/tests/conftest.py` — 픽스처 규약
- [ ] `infra/03-lambda.sh` · `infra/05-abuse-protection.sh` — 실행 역할·스로틀링 패턴

### 6.2 외부 문서

- [ ] Slack — [요청 검증](https://docs.slack.dev/authentication/verifying-requests-from-slack) (§5.1 CTR-SB-001 출처)
- [ ] Slack — [Events API](https://docs.slack.dev/apis/events-api/) (3초·재시도·`url_verification` 출처)
- [ ] Slack — [메시지 절단 changelog](https://docs.slack.dev/changelog/2018-truncating-really-long-messages/) (`CTR-SB-005` 출처)
- [ ] Claude API — MCP 커넥터 (`claude-api` 스킬 `shared/tool-use-concepts.md` § MCP Connector)
- [ ] Stage 1 FRD §7 배포 타깃 제약 · §8 `EDGE-016`/`EDGE-018`/`EDGE-021`

### 6.3 Assets

해당 없음 — UI 없음. Slack 메시지가 유일한 표면이다.

### 6.4 API / Data

| ID | 리소스 | 용도 | 상태 |
|----|--------|------|------|
| RES-SB-API-001 | Slack 앱 (Signing Secret · Bot Token · Bot User ID) | 서명 검증·게시·자기판별 | **필요 — 사용자만 생성 가능(워크스페이스 관리자)** |
| RES-SB-API-002 | Anthropic API 키 | Claude API 호출 | **필요 — 미보유 확인(`ant` CLI·환경변수 모두 없음)** |
| RES-SB-API-003 | `https://mcp.devoks.kr/mcp` | MCP 커넥터 대상 | ✅ 가동 중 |
| RES-SB-API-004 | DynamoDB 테이블(멱등성) | `event_id` 조건부 쓰기 | 필요 — 생성 예정 |
| RES-SB-API-005 | Slack 워크스페이스 사용자 ID 목록 | `CTR-SB-006` 매핑 | **필요 — `EDGE-SB-019`로 실형태 확인** |

## 7. Constraints

- **위험/의존 제약:**
  - 외부 서비스 **둘**에 새로 의존한다(Slack API · Anthropic API). 둘 다 사용자만 자격을 만들 수 있어 **착수 전 블로커**다.
  - 새 AWS 리소스가 생긴다(DynamoDB 테이블 · Lambda 2개 · Slack용 라우트). Stage 2와 달리 **상태 저장소가 처음 도입**된다.
  - MCP 서버 코드 변경은 없다. 설정(`MCP_CLIENT_TOKENS`)만 바뀐다.
- **기술 제약:**
  - **Claude API MCP 커넥터는 beta**다(`mcp-client-2025-11-20`). 계약이 바뀔 수 있고, Bedrock·Vertex에서는 쓸 수 없다(우리는 Claude API 직접 호출이라 현재는 무관).
  - **Opus 5의 API 표면 제약**: `budget_tokens` 400 · assistant prefill 400 · thinking 기본 on. 깊이는 `output_config.effort`로만 조절한다.
  - **Python 3.14 하한**은 Stage 1과 동일하게 유지한다 — 같은 워크스페이스·같은 `uv.lock`이다.
  - **3초는 협상 불가**다. handler 설계 전체가 이 숫자에서 나온다.
  - **비용이 사용량에 비례한다** — Stage 1·2는 프리티어 안에서 월 $0.51이었지만, Claude API는 질의당 과금이다(`EDGE-SB-017`).

## 8. Edge Cases & Error Handling

| ID | 상황 | 기대 동작 |
|----|------|-----------|
| EDGE-SB-001 | 서명 불일치 | **401**. 본문을 파싱하지 않고 거부한다 — 검증 전 파싱은 미검증 입력을 신뢰하는 것이다 |
| EDGE-SB-002 | 타임스탬프가 5분 밖(리플레이) | **401**. 서명이 유효해도 거부한다 — 캡처한 요청의 재전송을 막는 유일한 방어다 |
| EDGE-SB-003 | `url_verification` 핸드셰이크 | **서명 검증을 통과한 뒤** `challenge`를 그대로 반환. 검증 없이 응답하면 누구나 우리 엔드포인트를 등록 확인용으로 쓸 수 있다 |
| EDGE-SB-004 | Slack 재시도(`x-slack-retry-num` 1\|2\|3) | 멱등 판정에서 걸러 **답변을 한 번만** 게시. 재시도는 3초를 못 지켰다는 신호이므로 **경고 로그**도 남긴다 |
| EDGE-SB-005 | **Lambda 비동기 호출 자체의 재시도** | Lambda는 비동기 호출 실패 시 기본 **2회 재시도**한다 — Slack 재시도와 **별개의 중복 원인**이다. 멱등 판정이 handler가 아니라 **worker에서도** 성립해야 하는 이유다. worker는 처리 완료를 기록하고, 이미 완료된 `event_id`면 게시하지 않는다 |
| EDGE-SB-006 | 미등록 Slack 사용자 | 질의를 실행하지 않고 안내만 게시. **누가 등록돼 있는지 드러내지 않는다**(`AC-SB-004-3`) |
| EDGE-SB-007 | **handler 콜드스타트가 3초 예산을 잠식** | Stage 1 실측상 MCP 서버 컨테이너 콜드스타트는 ~1,900 ms였다. handler가 같은 규모가 되면 3초를 넘긴다. 그래서 `DSN-SB-008`이 **Claude SDK·MCP 관련 의존성을 handler에서 물리적으로 배제**한다. 배포 후 `Init Duration`을 실측해 예산 내인지 확인한다 |
| EDGE-SB-008 | Claude API 오류(429·5xx·타임아웃) | 재시도 가능 여부를 담은 안내를 게시. SDK 기본 재시도(2회)에 맡기고 자체 재시도 루프를 겹치지 않는다 |
| EDGE-SB-009 | `mcp_servers`만 보내고 `mcp_toolset` 누락 | API가 **검증 오류**로 거부한다(문서화된 함정). 두 값을 **한 함수에서 함께 구성**해 분리 불가능하게 만든다 |
| EDGE-SB-010 | 답변이 `CTR-SB-005` 초과 | 절단 사실을 명시하며 자른다. 말없이 Slack이 40,000자에서 자르게 두면 잘렸는지조차 모른다 |
| EDGE-SB-011 | 봇이 자기 메시지로 재트리거(무한 루프) | `SLACK_BOT_USER_ID`와 이벤트의 `user`/`bot_id`를 대조해 **작업 시작 전** 무시한다. 이 방어가 없으면 답변이 다시 질문이 되어 비용이 무한히 늘어난다 |
| EDGE-SB-012 | 사용자가 늘어 환경변수 4 KB 초과 | Stage 1 `EDGE-021`과 같은 제약이 **worker에도** 적용된다. `SLACK_USER_TOKEN_MAP`은 사용자당 약 60 B(ID 11 B + 토큰 43 B + 구분자)라 handler보다 여유가 있지만 무한하지 않다. Stage 1 §10 OAuth 트리거 ①(클라이언트 10개)과 **같은 임계**로 관리한다 |
| EDGE-SB-013 | worker가 타임아웃 | Claude API 추론 + MCP 툴 호출 여러 번이면 수십 초가 될 수 있다. worker 타임아웃을 넉넉히 두되(`CTR-SB-009`), 초과 시 **사용자에게 실패를 알린다** — 조용히 사라지면 사용자는 무한히 기다린다 |
| EDGE-SB-014 | 스레드 컨텍스트가 길어져 토큰 폭증 | Stage 3 초기에는 **스레드 히스토리를 싣지 않는다**(매 질문 독립). 컨텍스트 유지는 비용·복잡도를 함께 올리므로 실사용 후 결정한다(§10 미결) |
| EDGE-SB-015 | **같은 사용자가 연달아 여러 번 멘션(연타)** | 각 멘션은 **독립 `event_id`**이므로 멱등성이 걸러주지 않는다 — 전부 별개 질의로 처리되어 **비용이 횟수만큼 발생**한다. 단발형 조작이므로 각 질문에 각 답변을 게시하되, **동일 사용자·동일 스레드에 처리 중인 질의가 있으면 접수만 알리고 새 질의를 시작하지 않는다**(진행 중 1건으로 코얼레싱). 이 판정도 멱등성 저장소를 쓴다 |
| EDGE-SB-016 | MCP 서버가 콜드스타트 중 | Anthropic 쪽에서 첫 툴 호출이 ~1.9초(새 이미지 직후 ~8.5초, `EDGE-016`) 지연된다. worker 타임아웃 산정에 포함하고, 사용자에게는 접수 알림으로 대기를 인지시킨다 |
| EDGE-SB-017 | **비용 폭주** | 질의당 $0.05~$0.15 추정. Stage 2의 예산 알림($10)은 AWS 비용만 보므로 **Claude API 비용은 잡히지 않는다**. 관측 레코드의 `usage`(`CTR-SB-008`)로 사후 집계하고, Anthropic Console의 사용량 한도를 별도로 건다 |
| EDGE-SB-018 | Slack 봇 토큰·서명 시크릿 회전 | 둘 다 SSM `SecureString`에 두고 Lambda 환경변수로 주입한다(Stage 2 `TASK-056` 방식). 회전은 SSM 갱신 + 설정 재주입이며 코드 변경이 없다 |
| EDGE-SB-019 | **매핑 키의 실형태가 가정과 다름(데이터 리얼리즘)** | `CTR-SB-006`은 이벤트 payload의 사용자 식별자가 `U…` 형태의 Slack user ID라고 가정한다. 그러나 실제 `app_mention` payload에서 **봇 호출자가 `user`가 아닌 다른 필드로 오거나**(예: 워크플로·앱 경유), Enterprise Grid에서 **user ID가 워크스페이스마다 다를 수** 있다. 유닛테스트는 가짜 payload로 통과해도 실환경에서 매핑이 **전부 미등록으로 떨어질** 수 있다. → **실제 Slack 워크스페이스에서 `app_mention` payload를 1건 받아 사용자 식별자 필드와 형식을 확인한 뒤** 매핑 키를 확정한다 |
| EDGE-SB-020 | 채널에 봇이 초대되지 않음 | `chat.postMessage`가 `not_in_channel`로 실패한다. 오류를 분류해 운영자가 원인을 알 수 있게 로그에 남긴다 |

## 9. Testing Strategy

- **대상:** 순수 판정 로직 — 서명 검증 · 멱등 판정 · 자격 조회 · 응답 길이 정책 · 봇 자기판별 · Claude 요청 구성.
- **필수:**
  - `signature.py` — 정상 / 서명 변조 / 타임스탬프 5분 밖 / 헤더 대소문자 변형 / 본문 재직렬화로 인한 불일치(`CTR-SB-001`의 raw body 요구)
  - `idempotency.py` — 신규 통과 · 중복 차단 · **동시 도착 시 하나만 통과**(원자성, `AC-SB-003-2`)
  - `identity.py` — 등록/미등록 · 거부 메시지가 다른 사용자 존재를 드러내지 않음 · 토큰 미출력
  - `ask.py` — **`mcp_servers`와 `mcp_toolset`이 항상 함께 구성됨**(`EDGE-SB-009`) · `authorization_token`에 그 사람 토큰이 실림 · `budget_tokens`/prefill 미사용
  - `events.py` — 봇 자기 메시지 무시(`EDGE-SB-011`)
  - 응답 길이 — 경계값(`CTR-SB-005` ±1)과 절단 고지
- **HTTP 계층:** handler는 ASGI/Lambda 이벤트 레벨로 검증한다 — 401 경로가 실제로 401을 내는지는 순수 함수 테스트만으로 증명되지 않는다(Stage 1 §7의 "in-memory 클라이언트는 인증을 검증하지 않는다"와 같은 교훈).
- **실환경 검증(테스트로 대체 불가):** `EDGE-SB-019`(payload 실형태) · `EDGE-SB-007`(handler 콜드스타트 실측).
- **추적:** 각 테스트는 `REQ-SB-xxx`/`AC-SB-xxx`/`CTR-SB-xxx`/`EDGE-SB-xxx`에 매핑한다.

## 10. 미결 결정 (PLAN 착수 전/중 확정)

1. **worker 타임아웃과 메모리**(`CTR-SB-009` 실측 확정) — Claude API 추론 + MCP 툴 다회 호출의 실제 분포를 모른다. 초기값을 두되 실측 후 조정한다.
2. **스레드 컨텍스트 유지 여부**(`EDGE-SB-014`) — 초기에는 미유지. 실사용에서 "아까 그거" 류 질문이 얼마나 나오는지 보고 결정한다.
3. **접수 알림 방식** — 별도 메시지 vs 이모지 리액션. 리액션이 채널을 덜 어지럽히지만 사용자가 못 볼 수 있다.
4. **Anthropic 사용량 한도 설정값**(`EDGE-SB-017`) — AWS 예산과 별개로 Console에서 건다.
5. **Slack 앱 배포 범위** — 특정 채널 한정 vs 워크스페이스 전체.
