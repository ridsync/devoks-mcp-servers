---
doc_type: frd
id: FRD-management-mcp-bootstrap
title: Management MCP 서버 부트스트랩 (Stage 1)
status: approved
owner: 원빈
updated: 2026-09-03
plan: ./PLAN.md
issue: https://github.com/ridsync/devoks-mcp-servers/issues/1
---

# [Management MCP 서버 부트스트랩] Feature Requirement Document

> 사내 프로젝트/서비스의 지식·상태를 여러 에이전트 클라이언트에서 단일 진입점으로 조회하는
> Management MCP 서버의 **Stage 1(서버 골격 + GitHub 조회 + 컨테이너화/CI)** 요구서.
> Stage 2(AWS Lambda 실배포)·Stage 3(Slackbot 연동)은 §10 로드맵에 후속 작업으로 기록한다.
> **배포 타깃은 2026-09-10에 ECS/Fargate → Lambda + Function URL로 변경 확정**됐다(비용 실측 근거 §10).

## 1. Goal

Claude Code·Codex 등 각 AgentClient와 Slack·Notion·Discord 환경이 **하나의 MCP 엔드포인트**로 사내 지식·서비스 상태를 조회할 수 있는 Management MCP 서버를 세운다. Stage 1은 GitHub 소스코드 조회 4개 툴만 노출하되, 이후 Knowledge/Runtime/Business 3계층을 계속 붙일 수 있는 **Auth·RBAC·Audit·Tool Routing 골격**을 함께 고정한다.

## 2. Context

- **현재 상황(as-is):**
  - 저장소가 사실상 빈 상태다 — `git init` + `Initial commit`(95e6e6b) 하나뿐이고 추적 파일이 0개다.
  - 사내 지식 조회가 클라이언트마다 개별 자격증명·개별 경로로 흩어져 있어, **누가 무엇을 조회했는지 감사 추적이 없다.**
  - 각 에이전트가 GitHub 토큰을 직접 들고 있으면 권한 범위 통제와 회수가 불가능하다.
- **관련 기능:**
  - 아키텍처 상단의 **Slackbot**(MCP 클라이언트) — Stage 3
  - 하위 3계층 — **Knowledge**(GitHub/Notion/PRD·TRD), **Runtime**(Sentry/Grafana/CloudWatch/GitHub CI), **Business**(Data API/Read DB/Analytics). Stage 1은 Knowledge의 GitHub만.

## 3. Requirements (EARS)

### REQ-001: MCP 서버 부트스트랩 & Streamable HTTP 전송

- **동작:** MCP 2026-07-28 스펙의 Streamable HTTP 전송으로 기동해 `/mcp`에서 프로토콜 요청에 응답하고, 오케스트레이터용 헬스 엔드포인트를 제공한다.
- **사용자 흐름:** AgentClient가 `/mcp`에 접속 → 툴 목록 수신 → 툴 호출
- **AC:**
  - `AC-001-1` WHEN 서버가 Streamable HTTP 전송으로 기동되면 THE SYSTEM SHALL `CTR-001`의 경로에서 MCP `initialize`·`tools/list` 요청에 성공 응답한다.
  - `AC-001-2` WHEN 클라이언트가 `tools/list`를 호출하면 THE SYSTEM SHALL 등록된 `CTR-007` 툴 이름 전체를 스키마와 함께 반환한다.
  - `AC-001-3` THE SYSTEM SHALL `CTR-001`의 헬스 경로에서 HTTP 200과 서버 이름·버전을 담은 JSON을 반환한다.
  - `AC-001-4` IF **인증된** 요청의 `Host` 헤더가 `CTR-006`의 허용 호스트 목록에 없으면 THEN THE SYSTEM SHALL 421을 반환하고 거부된 호스트명을 서버 로그에 경고로 남긴다.
    > "인증된"이 붙은 이유: SDK 미들웨어 순서가 **인증 → 전송보안**이다(스택 실측: `bearer_auth` → `streamable_http_manager` → `transport_security`). 따라서 토큰 없이 미허용 Host로 보내면 421이 아니라 **401**이 먼저 나온다. 미인증 호출자에게 Host 유효성을 알려주지 않는 편이 오히려 안전하므로 이 순서를 그대로 수용한다 — 대신 421을 관측하려면 **유효 토큰을 함께 보내야** 한다(`TASK-010` 테스트 전제).
  - `AC-001-5` IF 허용 호스트 환경변수가 비어 있으면 THEN THE SYSTEM SHALL 기동에 실패하고 누락 키 이름을 표준 오류에 출력한다.

### REQ-002: 클라이언트 인증 (Auth)

- **동작:** OAuth 2.1 resource server로서 매 요청의 Bearer 토큰을 검증하고, 미인증 요청을 툴 실행 전에 차단한다. Stage 1의 검증 구현체는 정적 토큰 테이블이다.
- **사용자 흐름:** 클라이언트가 `Authorization: Bearer <token>` 전송 → 검증 통과 시 툴 실행
- **AC:**
  - `AC-002-1` WHEN 요청이 `CTR-002`에 등록된 유효 토큰을 담고 있으면 THE SYSTEM SHALL 툴 호출을 처리하고, 핸들러 안에서 호출자의 `client_id`·`scopes`를 조회할 수 있게 한다.
  - `AC-002-2` IF `Authorization` 헤더가 없거나 토큰이 검증에 실패하면 THEN THE SYSTEM SHALL 401을 반환하고 어떤 툴도 실행하지 않는다.
  - `AC-002-3` WHEN 401을 반환할 때 THE SYSTEM SHALL `WWW-Authenticate` 헤더에 `resource_metadata` 포인터를 포함한다.
  - `AC-002-4` THE SYSTEM SHALL `CTR-001`의 Protected Resource Metadata 경로에서 RFC 9728 문서를 반환하고, 그 안의 `resource`가 `CTR-006`의 공개 URL과 일치하게 한다.
  - `AC-002-5` IF 토큰이 `CTR-002`의 필수 스코프를 모두 갖지 않으면 THEN THE SYSTEM SHALL HTTP **403**과 `error="insufficient_scope"`로 거부하고 툴을 실행하지 않는다.
  - `AC-002-6` THE SYSTEM SHALL 토큰 테이블을 `CTR-006`의 환경변수에서만 읽고, 소스코드에 토큰 리터럴을 두지 않는다.

### REQ-003: 툴·리소스 단위 인가 (RBAC)

- **동작:** 인증된 호출자의 역할을 기준으로 툴 호출 가능 여부와 접근 가능한 GitHub 저장소를 판정한다. 판정은 GitHub API 호출 **이전**에 끝난다.
- **사용자 흐름:** 툴 호출 → 역할 조회 → 툴 허용 판정 → repo allowlist 판정 → 실행
- **AC:**
  - `AC-003-1` WHEN 호출자의 역할이 `CTR-007`에서 해당 툴을 허용하면 THE SYSTEM SHALL 툴을 실행한다.
  - `AC-003-2` IF 호출자의 역할이 해당 툴을 허용하지 않으면 THEN THE SYSTEM SHALL 툴 본문을 실행하지 않고 권한 거부 사유를 담은 툴 오류를 반환한다.
  - `AC-003-3` IF 툴 인자로 받은 저장소가 `CTR-008`의 allowlist에 없으면 THEN THE SYSTEM SHALL GitHub API를 호출하지 않고 거부한다.
  - `AC-003-4` WHILE allowlist가 비어 있는 동안 THE SYSTEM SHALL 모든 저장소 접근을 거부한다.
  - `AC-003-5` THE SYSTEM SHALL 인가 거부 응답에 allowlist 전체 목록이나 미허용 저장소의 존재 여부를 노출하지 않는다.

### REQ-004: 감사 로그 (Audit)

- **동작:** 모든 툴 호출 시도를 성공·실패·거부 구분과 함께 구조화 JSON 한 줄로 남긴다. ECS 로그 드라이버가 그대로 수집할 수 있도록 stdout에 쓴다.
- **사용자 흐름:** 툴 호출 시도 → (실행 또는 거부) → 감사 레코드 1건 emit
- **AC:**
  - `AC-004-1` WHEN 툴 호출이 종료되면(성공·실패 무관) THE SYSTEM SHALL `CTR-003`의 필드를 모두 담은 JSON 객체를 stdout에 개행 구분 1줄로 기록한다.
  - `AC-004-2` IF 인가 거부로 툴 본문이 실행되지 않으면 THEN THE SYSTEM SHALL `outcome`이 거부임을 나타내는 감사 레코드를 기록한다.
  - `AC-004-3` THE SYSTEM SHALL 감사 레코드에 Bearer 토큰 원문, GitHub App private key, 조회한 파일 본문을 포함하지 않는다.
  - `AC-004-4` WHEN 툴이 예외로 실패하면 THE SYSTEM SHALL 감사 레코드에 오류 종류를 남기고, 예외의 스택트레이스는 서버 로그에만 남긴다.

### REQ-005: GitHub 소스코드 조회 툴 (코어 4개)

- **동작:** GitHub REST API를 직접 호출해 저장소 목록·디렉토리 구조·파일 내용·코드 검색을 제공한다.
- **사용자 흐름:** 에이전트가 "이 서버 코드 구조 알려줘" → `list_repos` → `get_repo_tree` → `read_file` / `search_code`
- **AC:**
  - `AC-005-1` WHEN `list_repos`가 호출되면 THE SYSTEM SHALL `CTR-008` allowlist에 있는 저장소만 이름·설명·기본 브랜치와 함께 반환한다.
  - `AC-005-2` WHEN `get_repo_tree`가 저장소·경로·ref로 호출되면 THE SYSTEM SHALL 해당 경로의 엔트리를 이름·타입(file/dir)·크기와 함께 반환한다.
  - `AC-005-3` WHEN `read_file`이 저장소·경로·ref로 호출되면 THE SYSTEM SHALL 파일 내용을 텍스트로 반환한다.
  - `AC-005-4` IF 파일 크기가 `CTR-004`의 상한을 넘으면 THEN THE SYSTEM SHALL 상한까지만 반환하고 절단되었음과 전체 크기를 함께 알린다.
  - `AC-005-5` IF 파일이 UTF-8로 디코딩되지 않으면 THEN THE SYSTEM SHALL 내용 대신 바이너리임을 알리는 메시지와 파일 크기를 반환한다.
  - `AC-005-6` WHEN `search_code`가 질의와 저장소 범위로 호출되면 THE SYSTEM SHALL 매칭 경로와 발췌를 `CTR-005`의 개수 이하로 반환한다.
  - `AC-005-7` IF GitHub API가 4xx 또는 5xx로 응답하면 THEN THE SYSTEM SHALL 상태코드와 사유를 담은 툴 오류를 반환하고 예외 스택트레이스를 클라이언트에 노출하지 않는다.
  - `AC-005-8` IF 요청한 저장소·ref·경로가 GitHub에 없으면 THEN THE SYSTEM SHALL 무엇을 찾지 못했는지 명시한 툴 오류를 반환한다.
  - `AC-005-9` IF GitHub가 rate limit 초과로 응답하면 THEN THE SYSTEM SHALL 재시도 가능 시각을 포함한 툴 오류를 반환한다.

### REQ-006: GitHub App 자격증명 관리

- **동작:** GitHub App installation 액세스 토큰을 발급·캐시·만료 전 갱신해 API 호출에 사용한다.
- **사용자 흐름:** 서버 기동 → App 자격증명 검증 → 첫 호출 시 installation 토큰 발급 → 만료 임박 시 갱신
- **AC:**
  - `AC-006-1` WHEN GitHub API 호출이 필요하면 THE SYSTEM SHALL 유효한 installation 액세스 토큰을 사용한다.
  - `AC-006-2` WHILE 캐시된 토큰의 잔여 수명이 `CTR-009`의 여유 시간보다 길게 남아 있는 동안 THE SYSTEM SHALL 토큰을 재발급하지 않는다.
  - `AC-006-3` IF 캐시된 토큰의 잔여 수명이 `CTR-009`의 여유 시간 이하이면 THEN THE SYSTEM SHALL 호출 전에 토큰을 재발급한다.
  - `AC-006-4` IF `CTR-006`의 GitHub App 환경변수 중 하나라도 누락되면 THEN THE SYSTEM SHALL 기동에 실패하고 누락된 키 이름을 출력한다.
  - `AC-006-5` IF 여러 요청이 동시에 갱신 조건을 만족하면 THEN THE SYSTEM SHALL 토큰 발급을 1회로 합치고 나머지 요청은 그 결과를 공유한다.

### REQ-007: 컨테이너화 & CI 파이프라인

- **동작:** 프로덕션 배포용 컨테이너 이미지와, 품질 게이트를 자동 실행하는 CI를 갖춘다.
- **사용자 흐름:** 코드 push → CI가 lint·type·test·이미지 빌드 실행 → 실패 시 차단
- **AC:**
  - `AC-007-1` WHEN Docker 이미지를 빌드하면 THE SYSTEM SHALL 컨테이너 프로세스를 루트가 아닌 사용자로 실행하는 이미지를 산출한다.
  - `AC-007-2` WHEN 컨테이너가 `CTR-006`의 필수 환경변수와 함께 기동되면 THE SYSTEM SHALL 헬스 경로에서 200을 응답한다.
  - `AC-007-3` WHEN CI가 실행되면 THE SYSTEM SHALL lint·타입 검사·테스트·이미지 빌드를 모두 수행하고, 하나라도 실패하면 파이프라인을 실패로 종료한다.
  - `AC-007-4` THE SYSTEM SHALL 이미지에 GitHub App private key·Bearer 토큰 등 시크릿을 포함하지 않는다.
  - `AC-007-5` THE SYSTEM SHALL 이미지를 `CTR-010`의 타깃 아키텍처로 빌드한다.

### REQ-008: 후속 단계 인수인계 기록

- **동작:** Stage 2·3의 남은 작업을 문서로 남겨 다음 세션이 근거를 재수집하지 않게 한다.
- **AC:**
  - `AC-008-1` THE SYSTEM SHALL Stage 2(실배포)와 Stage 3(Slackbot 연동)의 남은 작업·미결 결정 사항을 §10 로드맵에 기록한다.
  - `AC-008-2` THE SYSTEM SHALL 저장소 루트에 서버 실행 방법·필수 환경변수·MCP 클라이언트 등록 방법을 담은 README를 제공한다.

## 4. Design Spec

> 복잡도 **임계 초과** — 신규 저장소 레이아웃, 신규 계층(auth/audit/adapters), 아키텍처 계약(MCP 툴 표면), 상태 소유 위치(GitHub 토큰 캐시)가 모두 이번에 처음 정해진다. 4.2~4.5를 채운다.

### 4.1 데이터 흐름 (UI 없음)

```
AgentClient ──HTTPS──▶ [API Gateway HTTP API / 로컬] ──▶ Starlette app
                                        ├─ GET  /healthz              → 200 {name, version}
                                        ├─ GET  /.well-known/oauth-protected-resource/mcp  (SDK 자동)
                                        └─ Mount /mcp → MCPServer
                                             │
                                    ① TransportSecurity (Host/Origin 검사)
                                             │
                                    ② TokenVerifier.verify_token  → AccessToken | None
                                             │  (None이면 401, 툴 미실행)
                                             │
                                    ③ 툴 래퍼 (@guarded)
                                        ├─ policy.authorize(role, tool, repo)  ← 순수 함수, I/O 없음
                                        │     거부 시 여기서 종료 (GitHub 미호출)
                                        ├─ 툴 본문 실행 → GitHubClient
                                        │                    └─ InstallationTokenProvider (캐시·갱신)
                                        └─ finally: audit.emit(레코드 1줄 → stdout)
```

- **조건 분기:** ① 실패 → 421/403 · ② 실패 → 401 · ③ 인가 실패 → 툴 오류(감사 레코드 `denied`) · GitHub 오류 → 툴 오류(감사 레코드 `error`)
- **시안:** 해당 없음(서버 전용). 입력 아키텍처 다이어그램은 `FRD.draft.md`에 원문 보존.

### 4.2 모듈 구조 · 책임 · 상태 소유

```
devoks-mcp-servers/                      # uv workspace 루트 (모노레포)
├─ pyproject.toml                        // [tool.uv.workspace] members = ["servers/*"]
└─ servers/management/
   ├─ pyproject.toml                     // 이 서버의 의존성·엔트리포인트
   └─ src/devoks_mcp_management/
      ├─ config.py          // 책임: 환경변수 → 불변 Settings. 기동 시 전량 검증(Fail-Fast).
      │                     //        상태 소유: 없음(읽기 전용 값 객체)
      ├─ server.py          // 책임: MCPServer 생성, token_verifier·auth 결선, registry 호출
      ├─ app.py             // 책임: Starlette 조립(/healthz + Mount /mcp), lifespan 배선
      ├─ auth/
      │  ├─ verifier.py     // 책임: TokenVerifier 구현(정적 테이블). 교체 지점은 이 파일 하나.
      │  └─ policy.py       // 책임: 인가 판정. 순수 함수만, I/O·전역상태 금지.
      ├─ audit/logger.py    // 책임: 감사 레코드 직렬화·마스킹·emit
      ├─ tools/
      │  ├─ registry.py     // 책임: 계층별 어댑터의 툴을 MCPServer에 등록
      │  └─ guard.py        // 책임: 인가 판정 + 감사 emit로 툴 본문을 감싸는 데코레이터
      └─ adapters/knowledge/github/
         ├─ credentials.py  // 책임: installation 토큰 발급·캐시·갱신
         │                  //        ★ 상태 소유: 캐시된 토큰과 만료시각. lifespan이 1회 생성해 공유.
         ├─ client.py       // 책임: GitHub REST 호출·오류 정규화·절단·바이너리 판정
         └─ tools.py        // 책임: 4개 MCP 툴의 인자 스키마와 응답 변환
```

- **상태 소유 결정:** 프로세스가 들고 있는 가변 상태는 **`credentials.py`의 토큰 캐시 단 하나**다. `lifespan`이 이를 1회 생성해 `GitHubClient`에 주입하고, 툴은 클라이언트를 주입받아 쓴다. 툴·정책·감사 모듈은 상태를 갖지 않는다.
- **왜 이 경계인가:** 2026-07-28 전송은 세션리스라 요청 간 서버 상태가 필요 없다(§7 참조). 유일한 예외가 토큰 캐시이므로 그것만 한 곳에 격리하면 다중 워커·다중 태스크로 그대로 수평 확장된다.

### 4.3 적용 설계 결정 (DSN)

| ID | 설계 결정 | 근거 |
|----|-----------|------|
| DSN-001 | 인증은 SDK `TokenVerifier` 프로토콜 구현체 **1개 파일**로 국소화하고, 서버 코드는 `AccessToken`만 소비한다 | Stage 2 이후 사내 IdP OAuth로 교체할 때 변경 범위를 `verifier.py`로 묶는다. SDK가 요구하는 인터페이스가 동일하므로 교체 비용이 이 파일에 국한된다 |
| DSN-002 | 인가 판정(`policy.py`)은 **I/O 없는 순수 함수**로 분리한다 | 역할×툴×저장소 조합을 GitHub 없이 단위 테스트로 전수 검증할 수 있다. 인가 버그는 보안 사고이므로 가장 싸게 테스트되는 형태로 둔다 |
| DSN-003 | 감사 로그는 SDK `server.middleware`가 아니라 **툴 래퍼 데코레이터**(`tools/guard.py`)에서 emit한다 | SDK 문서가 middleware를 *provisional*(2.x 마이너에서 시그니처·의미가 바뀔 수 있음, 토대로 삼지 말 것)로 명시했다. 감사는 규정 요구이므로 불안정 API에 얹지 않는다. 더불어 툴 래퍼는 인가 판정과 같은 지점이라 "거부도 감사된다"가 구조적으로 보장된다 |
| DSN-004 | GitHub 자격증명·HTTP 클라이언트는 `lifespan`에서 1회 구성해 주입한다 | 요청마다 App JWT 서명·토큰 교환을 반복하지 않는다. SDK의 lifespan이 정확히 이 용도다 |
| DSN-005 | 계층(Knowledge/Runtime/Business)은 `adapters/<layer>/<source>/` **디렉토리 경계**로 표현하고, 툴 등록은 `registry.py`가 수집한다 | 아키텍처 3계층을 코드 구조에 그대로 반영해, 이후 Sentry·Notion·Read DB 추가가 "디렉토리 하나 + registry 한 줄"이 되게 한다 |
| DSN-006 | 설정은 기동 시 **전량 검증**하고 누락 시 즉시 실패한다(Fail-Fast) | 미인증·미인가 상태로 서버가 떠 있는 시간을 0으로 만든다. 특히 `allowed_hosts` 누락은 조용히 전 요청 421이 되므로 반드시 기동에서 잡는다 |
| DSN-007 | `transport_security`의 허용 호스트는 **환경변수 주입**으로만 결정한다 | 로컬·CI·스테이징·프로덕션의 호스트명이 다르다. 코드에 박으면 Stage 2 배포에서 전 요청 421로 막힌다 |
| DSN-008 | 저장소는 **uv workspace 모노레포**로 두고 이번 서버를 `servers/management/`에 배치한다 | 레포명이 복수형(`devoks-mcp-servers`)이고 3계층이 각각 별 서버로 갈라질 여지가 있다. 공용 코드(`packages/`)는 두 번째 서버가 생길 때 추출한다 — 지금 만들면 사용자가 1명인 추상화가 된다 |

### 4.4 저장소 배치

- **워크스페이스:** 루트 `pyproject.toml`에 `[tool.uv.workspace] members = ["servers/*"]`. 락파일(`uv.lock`)은 루트 1개.
- **이번 서버:** `servers/management/` — 소스는 `src/devoks_mcp_management/`, 테스트는 `servers/management/tests/`.
- **CI:** `.github/workflows/ci.yml` (루트).
- **컨테이너:** `servers/management/Dockerfile`, 빌드 컨텍스트는 루트(워크스페이스 락파일이 필요).
- **`packages/` 미생성:** 두 번째 서버가 등장할 때 공통 코드를 추출한다.

### 4.5 재사용 대상

- **SDK 제공분을 직접 만들지 않는다:** JSON Schema 생성(타입 힌트가 스키마), Protected Resource Metadata 발행, 401 + `WWW-Authenticate` 응답, DNS rebinding 보호, in-memory 테스트 클라이언트는 모두 SDK가 제공한다.
- **테스트 인프라:** `Client(mcp)` in-memory 전송으로 툴 로직을, ASGI transport로 HTTP 계층(인증·Host 검사)을 검증한다 — in-memory는 HTTP를 건너뛰므로 인증이 검증되지 않는다(§7 기술 제약).
- **기존 프로젝트 코드 재사용 없음** — 빈 저장소다.

## 5. Contract

### 5.1 핵심 파라미터 / 데이터

| ID | 항목 | 타입/범위 | 기본값 | 의미 |
|----|------|-----------|--------|------|
| CTR-001 | 엔드포인트 경로 | 고정 문자열 | `/mcp` (MCP), `/healthz` (헬스), `/.well-known/oauth-protected-resource/mcp` (RFC 9728, SDK 자동) | 서버가 노출하는 HTTP 경로 계약 |
| CTR-002 | 클라이언트 토큰 테이블 | `{token: {client_id, role, scopes[]}}` JSON | 없음(필수) | 정적 Bearer 인증 원본. `required_scopes`는 `["devoks:read"]` |
| CTR-003 | 감사 레코드 필드 | JSON 객체 | — | `ts`(ISO8601 UTC), `event`(`tool_call`), `client_id`, `role`, `tool`, `args_summary`(저장소·경로·질의만, 파일 본문 제외), `outcome`(`ok`\|`denied`\|`error`), `reason_code`(nullable — `denied`일 때 어느 인가 규칙이 거부했는지), `error_kind`(nullable — `error`일 때 예외 클래스명), `duration_ms`, `request_id` |

> `reason_code`는 FRD 초판에 없었다 — `TASK-004`가 "거부 사유를 감사 레코드 어디에 실을지 미정"을 신고해 추가했다. `error_kind`에 겹쳐 쓰지 않고 분리한 이유: 로그 질의가 **"인가가 막았다"와 "본문이 예외를 냈다"를 값 파싱 없이 구별**할 수 있어야 하고, `AC-003-5`가 요구하는 2층 분리(클라이언트에는 구별 불가한 고정 메시지, 감사에는 식별 가능한 사유 코드)의 착지점이 명시적으로 필요하기 때문이다.
| CTR-004 | `read_file` 응답 상한 | bytes, 1..1048576 | 262144 (256 KiB) | 초과 시 절단 + 전체 크기 통지 |
| CTR-005 | `search_code` 최대 결과 수 | int, 1..100 | 30 | 모델 컨텍스트 보호 |
| CTR-009 | installation 토큰 갱신 여유 | seconds, 60..1800 | 300 | 잔여 수명이 이 값 이하면 재발급 |
| CTR-010 | 이미지 타깃 아키텍처 | `linux/arm64` \| `linux/amd64` | `linux/arm64` | Lambda `arm64`(Graviton). 로컬 개발기(Apple Silicon)와 일치해 크로스빌드가 불필요하다. Lambda arm64 요율이 x86 대비 20% 저렴하다(실측: `$0.0000133334` vs `$0.0000166667` /GB-초, 서울) |
| CTR-011 | Lambda 패키징 계약 | 고정값 | — | 단일 이미지가 Lambda·컨테이너 런타임 양쪽에서 동작해야 한다. ① LWA 확장을 `/opt/extensions/lambda-adapter`에 배치 ② `AWS_LWA_PORT` = `MCP_PORT` ③ `AWS_LWA_READINESS_CHECK_PATH` = `/healthz`(`CTR-001`) ④ `AWS_LWA_INVOKE_MODE` = `BUFFERED`(`MCP_JSON_RESPONSE=true`이므로 스트리밍 불필요). LWA 확장은 **Lambda 런타임에서만 기동**되므로 로컬 `docker run`·Fargate에는 무영향 |

### 5.2 환경 키

| ID | 키 | 타입 | 기본값 | 용도 |
|----|----|------|--------|------|
| CTR-006 | `MCP_ALLOWED_HOSTS` | 콤마구분 문자열 | 없음(**필수**) | `transport_security` 허용 Host. 미설정 시 전 요청 421이므로 기동 실패시킨다 |
| | `MCP_PUBLIC_URL` | URL | 없음(**필수**) | `AuthSettings.resource_server_url` — RFC 9728 문서의 `resource` 값. **경로 컴포넌트가 `/mcp`로 끝나야 하고 후행 슬래시가 없어야 한다** (아래 주의) |
| | `MCP_ISSUER_URL` | URL | 없음(**필수**) | `AuthSettings.issuer_url` |
| | `MCP_CLIENT_TOKENS` | JSON 문자열 | 없음(**필수**) | `CTR-002` 토큰 테이블 |
| | `MCP_REPO_ALLOWLIST` | 콤마구분 `owner/repo` | `""` (**빈 값 = 전부 거부**) | `CTR-008` |
| | `MCP_ROLE_TOOLS` | JSON 문자열 | 없음(**필수**) | `CTR-007` 역할↔툴 매핑 |
| | `GITHUB_APP_ID` | 문자열 | 없음(**필수**) | App JWT의 `iss` 클레임. GitHub은 **client ID 또는 App ID 둘 다** 받고 **client ID를 권장**한다(공식 클레임 표) — 서버는 이 값을 그대로 통과시키므로 어느 쪽이든 동작한다 |
| | `GITHUB_APP_PRIVATE_KEY` | PEM 문자열 | 없음(**필수**) | App JWT 서명 키. 파일이 아닌 시크릿 주입 |
| | `GITHUB_APP_INSTALLATION_ID` | 문자열 | 없음(**필수**) | 조직 설치 식별자 |
| | `MCP_PORT` | int | 8000 | 리스닝 포트 |
| | `MCP_LOG_LEVEL` | 문자열 | `INFO` | 서버 로그 레벨(감사 로그와 별개) |
| | `MCP_READ_FILE_MAX_BYTES` | int | `CTR-004` 기본값 | `CTR-004` 오버라이드. 범위 밖이면 기동 실패 |
| | `MCP_SEARCH_CODE_MAX_RESULTS` | int | `CTR-005` 기본값 | `CTR-005` 오버라이드. 범위 밖이면 기동 실패 |
| | `MCP_TOKEN_REFRESH_LEEWAY_SECONDS` | int | `CTR-009` 기본값 | `CTR-009` 오버라이드. 범위 밖이면 기동 실패 |
| | `MCP_STATELESS_HTTP` | bool | `true` | `streamable_http_app(stateless_http=)`. Lambda 배포의 **필수 조건**(§7 배포 타깃 제약). `false`로 두면 legacy 레그가 `Mcp-Session-Id`를 발급하는데 Lambda는 인스턴스가 임의로 교체되므로 세션이 유실된다 |
| | `MCP_JSON_RESPONSE` | bool | `true` | `streamable_http_app(json_response=)`. `true`면 SSE 대신 단일 JSON 응답 → Lambda 버퍼드 호출과 정합. `false`면 `AWS_LWA_INVOKE_MODE=RESPONSE_STREAM`이 함께 필요해진다(`CTR-011`) |

> **⚠️ `MCP_PUBLIC_URL`의 경로가 well-known 경로를 결정한다** (`TASK-009` 실측 발견). SDK가 `.well-known` 라우트를 이 URL의 **path 컴포넌트에서 파생**한다:
>
> | `MCP_PUBLIC_URL` | 실제 well-known 경로 | `CTR-001` 일치 |
> |---|---|---|
> | `https://h/mcp` | `/.well-known/oauth-protected-resource/mcp` | ✅ |
> | `https://h/mcp/` (후행 슬래시) | `/.well-known/oauth-protected-resource/mcp/` | ❌ |
> | `https://h` (경로 없음) | `/.well-known/oauth-protected-resource` | ❌ |
>
> 잘못 설정해도 **서버는 정상 기동하고** 401 포인터도 자기 일관적이라, 문서화된 `CTR-001` 경로를 쓰는 클라이언트만 조용히 깨진다. `MCP_ALLOWED_HOSTS`와 같은 종류의 침묵 실패이므로 `DSN-006`(Fail-Fast)에 따라 기동 시 검증한다(`TASK-011`).
>
> **`Origin` 허용 목록은 env 키가 없다** — `allowed_origins=[]`(빈 목록)로 고정이며, 이는 "브라우저 경유 전면 차단"을 뜻한다. 비브라우저 MCP 클라이언트는 `Origin`을 보내지 않으므로 Stage 1·2에 영향이 없다. 브라우저 기반 MCP 클라이언트가 필요해지면 `MCP_ALLOWED_ORIGINS` 키 추가가 필요하다(§10 Stage 3 고려사항).

> 위 3개 키는 FRD 초판에서 누락됐다 — §5.1이 `CTR-004/005/009`의 범위·기본값을 정의했는데 §5.2에 대응 env 키를 적지 않아, `TASK-003` 구현이 공백을 신고하며 `MCP_` 접두 관례에 맞춰 채웠고 그 이름을 여기 확정 기록했다. 세 키 모두 **선택**이며 미설정 시 `types.py`의 기본값을 쓴다(필수 키들과 달리 누락이 기동 실패 사유가 아니다).

### 5.3 RBAC 매핑 · 저장소 범위

| ID | 항목 | 형식 | Stage 1 초기값 |
|----|------|------|----------------|
| CTR-007 | 역할↔툴 매핑 | `{role: [tool_name]}` | `reader` → `list_repos`, `get_repo_tree`, `read_file`, `search_code` (Stage 1은 전 툴이 읽기 전용이라 단일 역할로 시작) |
| CTR-008 | 저장소 allowlist | `owner/repo` 완전일치 목록. 와일드카드 없음 | 빈 값이 기본 — 배포 시 명시 주입. 로컬 검증 초기값 `ridsync/devoks-mcp-servers` |

### 5.4 상태 전이 (installation 토큰)

| 현재 상태 | 조건 | 다음 상태 |
|-----------|------|-----------|
| 캐시 없음 | GitHub 호출 필요 | 발급 → 캐시됨 |
| 캐시됨 | 잔여 수명 > `CTR-009` | 캐시됨 (재사용) |
| 캐시됨 | 잔여 수명 ≤ `CTR-009` | 재발급 → 캐시됨 |
| 발급 진행 중 | 다른 요청이 동시 도착 | 진행 중인 발급 결과 대기 (중복 발급 없음, `AC-006-5`) |
| 발급 실패 | GitHub 오류 | 캐시 없음 + 툴 오류 반환 |

## 6. Resources & References (착수 전 체크)

### 6.1 참고 코드 / 재사용

- [x] **기존 프로젝트 코드 없음** — 빈 저장소(추적 파일 0개, `Initial commit` 하나). 재사용 대상이 없으므로 SDK 문서의 정식 패턴을 근거로 삼는다.
- [ ] `examples/servers/simple-auth/` (SDK 저장소) — `IntrospectionTokenVerifier`가 "프로덕션 검증기의 전형적 형태"로 문서에 명시됨. Stage 2 OAuth 전환의 참고 원본.

### 6.2 외부 문서

- [x] `https://py.sdk.modelcontextprotocol.io/run/authorization/` — `TokenVerifier`·`AuthSettings`·`get_access_token()`
- [x] `https://py.sdk.modelcontextprotocol.io/run/deploy/` — `transport_security` Host allowlist, 세션리스 확장
- [x] `https://py.sdk.modelcontextprotocol.io/run/asgi/` — `streamable_http_app()` Starlette 조립
- [x] `https://py.sdk.modelcontextprotocol.io/advanced/middleware/` — middleware가 provisional임(→ `DSN-003`)
- [x] `https://py.sdk.modelcontextprotocol.io/get-started/testing/` — in-memory `Client`, 인증 미검증 한계
- [x] `https://py.sdk.modelcontextprotocol.io/migration/` — v1→v2 파괴적 변경 (웹의 v1 예제 복붙 방지)
- [ ] `https://docs.github.com/en/rest` — 조회 엔드포인트 계약(§6.4)
- [ ] GitHub App installation 토큰 발급 절차 (App JWT → `POST /app/installations/{id}/access_tokens`)

### 6.3 Assets

| ID | 화면 | 참조 | 비고 |
|----|------|------|------|
| — | 없음 | — | 서버 전용 기능. 입력 아키텍처 다이어그램 원문은 `FRD.draft.md`에 보존 |

### 6.4 API / Data

| ID | 리소스 | 용도 | 상태 |
|----|--------|------|------|
| RES-API-001 | `POST /app/installations/{id}/access_tokens` | installation 토큰 발급 (`REQ-006`) | GitHub App 등록 필요 |
| RES-API-002 | `GET /installation/repositories` | `list_repos` 원본 | 준비 |
| RES-API-003 | `GET /repos/{o}/{r}/contents/{path}?ref=` | `get_repo_tree`·`read_file` 원본 | 준비 |
| RES-API-004 | `GET /search/code` | `search_code` 원본. **분당 10회**(인증 상태) — 다른 검색 엔드포인트 30회보다 훨씬 빡빡한 별도 버킷이다(공식 문서 확인). 발췌는 `Accept: application/vnd.github.text-match+json` → `text_matches[].fragment`. 에이전트가 반복 호출로 금방 소진하므로 **툴 설명에 이 제약을 명시**해 모델이 검색을 남발하지 않게 한다 | 준비 |
| RES-API-005 | GitHub App (org 설치) | 서버 자격증명 | **미생성 — 사용자 작업 필요** |

## 7. Constraints

- **위험/의존 제약:**
  - DB 스키마·마이그레이션 **없음**(Stage 1은 상태 저장소 미도입, 감사는 stdout).
  - 외부 서비스 의존: GitHub REST API + GitHub App 등록. **App 등록은 사용자만 가능**(조직 관리자 권한) → 이것이 없으면 GitHub 툴의 실호출 검증이 막힌다.
  - 시크릿 취급 코드(App private key, Bearer 토큰)를 새로 도입한다.
- **환경 제약 (실측):**
  - **Docker 미설치** (`docker: command not found`). 따라서 `AC-007-1`·`AC-007-2`의 이미지 빌드·기동 검증은 로컬에서 불가하며 **CI에서 수행**한다. 로컬 실동작 검증은 `uv run`으로 한다.
  - **CI 실제 실행은 push 후에만 확인 가능** — 이번 워크플로는 커밋·푸시를 하지 않으므로 `AC-007-3`은 워크플로 파일의 정적 검증까지만 이번 범위다.
  - git remote가 **개인 계정** `https://github.com/ridsync/devoks-mcp-servers.git`인데 사내 조직은 `org-devoks`다 → 저장소 이관 여부는 Stage 2 미결 사항(§10).
  - 로컬 Python **3.14.2**, uv 0.11.4. `mcp` 2.1.1은 `>=3.10`이며 의존성에 3.14 분기가 있어 3.14를 지원한다.
  - **⚠️ Python 3.14는 선호가 아니라 하한이다** — `adapters/knowledge/github/client.py`가 **PEP 758**(괄호 없는 다중 예외, `except ValueError, OSError, OverflowError:`)을 사용한다. 3.13에서는 **SyntaxError로 import 자체가 실패**한다(실측). `pyproject.toml`의 `requires-python = ">=3.14"`·`.python-version`과 일관되지만, **컨테이너 베이스 이미지도 3.14+여야 한다**(`TASK-030`). 3.13으로 내려갈 필요가 생기면 그 한 줄을 괄호 형태로 바꾸면 된다.
- **기술 제약:**
  - **SDK는 v2(`mcp` 2.1.1)를 쓴다.** v1의 `FastMCP`·`mcp.server.fastmcp.*`는 import 경로 자체가 사라졌으므로, 웹에 널려 있는 v1 예제를 그대로 옮기면 동작하지 않는다. `from mcp.server import MCPServer`가 옳다.
  - `token_verifier=`와 `auth=`는 **항상 동반**해야 한다 — 하나만 주면 `MCPServer(...)`가 요청을 받기 전에 `ValueError`를 낸다.
  - `server.middleware`는 소스에서 provisional이므로 감사의 토대로 쓰지 않는다(`DSN-003`).
  - in-memory `Client(mcp)`와 stdio 전송은 HTTP 계층을 건너뛰므로 **인증이 전혀 검증되지 않는다** — 인증 테스트는 ASGI/HTTP 레벨로 작성해야 한다.
  - **⚠️ 프로토콜 레그가 두 개이고 확장 요구가 다르다** (`TASK-010` 실측):

    | 요청의 `MCP-Protocol-Version` | `Mcp-Session-Id` | 다중 태스크 요구 |
    |---|---|---|
    | `2026-07-28` | **미발급** (자기완결 POST, `params._meta`에 버전 탑재) | 없음 — 어떤 태스크든 응답 가능 |
    | `2025-11-25` 이하 | **발급 + 필수** (없으면 `Missing session ID`) | **sticky session** 또는 `stateless_http=True` |

    SDK가 `LATEST_PROTOCOL_VERSION = 2026-07-28`을 지원하지만, **SDK 자체 클라이언트의 기본 핸드셰이크는 `2025-11-25`로 협상**된다(실측). 즉 현실의 AgentClient가 legacy 레그로 붙을 가능성이 높다. `stateless_http`는 legacy 레그 전용 플래그이고 2026-07-28 경로에서는 코드가 그 줄에 도달하지 않지만, **"켜도 의미 없다"가 아니다** — legacy 레그에서는 그것이 sticky session의 대안이다. 대가는 서버→클라이언트 역채널(sampling, push elicitation, `roots/list`)과 재개 가능성 상실이며, Stage 1 툴은 모두 단발 조회라 그 대가가 없다. **Stage 2에서 결정해야 한다**(§10).
  - **배포 타깃 제약 (Lambda, 2026-09-10 확정 — §10 근거):**
    - 진입점은 **API Gateway HTTP API + 커스텀 도메인 `mcp.devoks.kr`** 하나다. 부트스트랩에 쓰던 Function URL은 **2026-09-14에 삭제**했다 — API Gateway를 우회해 스로틀링이 무력화되는 뒷문이었기 때문이다(`EDGE-022`). `Principal:"*"` 권한도 함께 회수해 누가 Function URL을 재생성해도 자동 공개되지 않는다.
    - `session_manager.run()`은 **인스턴스당 1회만** 호출 가능하다(`RuntimeError` — SDK 소스 실측). 그 안에서 `create_server`에 준 MCP 프로토콜 lifespan(`_make_github_lifespan`)이 **컨테이너 수명당 1회** 진입하고 `_handle_stateless_request`가 쓰는 anyio task group이 생긴다. LWA는 uvicorn을 정상 부팅시키므로 컨테이너 1개 = `run()` 1회로 자연 충족되지만, **Lambda 핸들러에서 앱을 재생성하는 방식으로 바꾸면 즉시 깨진다.**
    - `stateless=True`에서도 `run()`은 **여전히 필요하다** — 세션 딕셔너리만 안 쓰고 lifespan·task group은 그대로 쓴다(SDK 소스 실측). "stateless니까 lifespan 불필요"는 오독이다.
    - Lambda 실행 환경은 호출 사이에 **동결**된다. 요청 처리가 응답 반환 전에 완결돼야 하므로 `json_response=True`가 필수다(SSE 장기 스트림은 동결과 충돌).
    - **CloudFront + Function URL은 MCP와 비호환** — OAC로 Function URL을 보호하려면 `AuthType=AWS_IAM`이 필요하고, AWS 문서는 "`PUT`·`POST`를 쓰면 클라이언트가 본문 SHA256을 `x-amz-content-sha256` 헤더로 보내야 하며 **Lambda는 unsigned payload를 지원하지 않는다**"고 명시한다. MCP Streamable HTTP는 전부 POST이므로 모든 MCP 클라이언트가 SigV4 본문 서명을 해야 하는데 그런 클라이언트는 없다. 커스텀 도메인은 **API Gateway HTTP API**로 간다(§10 Step 7).
  - **`AuthSettings`의 URL은 plain `str`로 넘긴다** — `pydantic.AnyHttpUrl`로 먼저 감싸면 경로 없는 URL에 후행 슬래시가 붙는다(`https://mcp.example.com` → `https://mcp.example.com/`). `AuthSettings`의 `url_preserve_empty_path=True`는 pydantic이 **문자열을 검증할 때만** 적용되고, 이미 만들어진 `AnyHttpUrl` 인스턴스는 재검증 없이 통과하기 때문이다(실측). MCP 스펙도 후행 슬래시 **없는** 형태를 권장하므로, `AC-002-4`(RFC 9728 `resource` = 공개 URL 일치)를 지키려면 문자열 전달이 맞다. 대가로 pyright `reportArgumentType` ignore 2곳이 필요하다.

## 8. Edge Cases & Error Handling

| ID | 상황 | 기대 동작 |
|----|------|-----------|
| EDGE-001 | `MCP_REPO_ALLOWLIST`가 빈 값 | 모든 저장소 접근 거부(fail-safe). 기동은 성공하되 거부 사유를 명확히 알린다 (`AC-003-4`) |
| EDGE-002 | `MCP_ALLOWED_HOSTS` 미설정 | 기동 실패 + 누락 키 출력. 조용히 전 요청 421로 뜨는 상태를 금지 (`AC-001-5`) |
| EDGE-003 | GitHub rate limit 초과 (403/429 + `x-ratelimit-reset`) | 재시도 가능 시각을 담은 툴 오류. 무한 재시도 금지 (`AC-005-9`) |
| EDGE-004 | 파일이 `CTR-004` 상한 초과 | 상한까지 절단 + 절단 사실·전체 크기 통지 (`AC-005-4`) |
| EDGE-013 | **`path`·`ref` 인자에 경로 트래버설**(`../`, `.`, 백슬래시) — 보안 검증에서 **실제 재현된 Critical** | GitHub 호출 전에 거부한다. `repo`만 인가 판정을 받고 `path`는 검증되지 않아 `read_file(repo=<허용>, path="../../../victim/secret/contents/.env")`가 **allowlist를 완전히 우회**하고 `path="../../../../installation/repositories"`로 **4툴 표면 밖 임의 GitHub GET**에 도달했다. 감사에는 `outcome=ok`로 남아 사후 탐지도 어려웠다. **이중 방어**: ① 세그먼트 검증(`..`/`.`/백슬래시 거부) ② 정규화 후 URL이 `/repos/{owner}/{repo}/contents`로 시작하는지 assert (`AC-003-3`, `CTR-008`) |
| EDGE-014 | **`search_code`의 `query`에 GitHub 검색 qualifier 주입**(`repo:`/`org:`/`user:`, boolean `OR`) — **High** | 쿼리를 거부하고, 방어적으로 **응답 항목도 allowlist로 재필터**한다. `query="password OR repo:victim/secret"`이 `q=... repo:victim/secret repo:<허용>`로 나가 GitHub 문서상 지원되는 다중 `repo:` OR 결합으로 경계를 우회한다. 응답의 `repository`를 파싱하고도 필터에 쓰지 않던 것이 2차 결함 (`AC-003-3`, `CTR-008`) |
| EDGE-015 | **토큰 갱신에 병합된 호출자 중 하나가 취소됨** — **High** | 취소가 공유 태스크로 전파되지 않아야 한다. `await inflight`는 asyncio 표준 동작상 대기자의 취소를 공유 `Task`로 전파하므로, 한 요청의 정상적 취소(클라이언트 재시도·프론팅 계층 타임아웃 — `EDGE-018`)가 **무관한 동시 호출 전부를 실패**시킨다. `AC-006-5`의 "나머지 요청은 그 결과를 공유한다"가 깨진다 |
| EDGE-012 | 파일이 **1 MB 초과** (GitHub contents API가 기본 JSON에 내용을 싣지 않는 구간) | raw 미디어타입으로 받아 `CTR-004` 상한까지 절단해 반환 — `EDGE-004`와 같은 결과. 공식 문서: ≤1 MB는 전 기능 지원, **1–100 MB는 raw·object 미디어타입만**, >100 MB는 미지원. **>100 MB는 절단조차 불가**하므로 크기와 함께 조회 불가를 알린다 (`AC-005-4`, `TASK-025`) |
| EDGE-005 | 파일이 바이너리(UTF-8 디코딩 실패) | 내용 대신 바이너리 표시 + 크기 (`AC-005-5`) |
| EDGE-006 | 없는 저장소·ref·경로 | 무엇을 못 찾았는지 명시한 툴 오류. allowlist 밖 저장소는 존재 여부조차 알리지 않음 (`AC-005-8`, `AC-003-5`) |
| EDGE-007 | installation 토큰 만료 직전 동시 다중 호출 | 발급 1회로 합치고 결과 공유 (`AC-006-5`) |
| EDGE-008 | GitHub App private key가 잘못된 PEM | 기동 시 실패 + 어떤 키가 문제인지 표시. 키 내용은 로그에 출력하지 않음 (`AC-006-4`, `AC-004-3`) |
| EDGE-009 | 툴 본문이 예상 못한 예외로 실패 | 클라이언트에는 정규화된 툴 오류, 스택트레이스는 서버 로그만. 감사 레코드는 `error`로 남음 (`AC-004-4`, `AC-005-7`) |
| EDGE-010 | 유효 토큰이지만 필수 스코프 부족 | **403** + `error="insufficient_scope"` — 미인증의 **401** `invalid_token`과 **다른 상태코드**다(SDK `bearer_auth.py` 실측). 툴 미실행 (`AC-002-5`) |
| EDGE-011 | `Host`는 허용이나 `Origin`이 미허용(브라우저 경유) | 403. 서버 로그에 사유 기록 (`AC-001-4` 계열) |
| EDGE-016 | **콜드스타트** — 유휴 후 첫 요청이 컨테이너 초기화를 유발 | **실측 2종을 구분해야 한다**(`TASK-059`, CloudWatch `REPORT` 라인의 `Init Duration`): ① **새 이미지 배포 직후 첫 1회 = 8,511 ms** — 63 MB 이미지를 Lambda 내부 형식으로 최적화·캐싱하는 일회성 비용이다. CI 배포(`TASK-061`) 직후 첫 요청이 항상 이 값을 낸다 ② **이후 정상 상태 = ~1,900 ms**. **메모리를 올려도 개선되지 않는다** — 512 MB 1,923 ms / 1024 MB 2,007 ms / 1769 MB 1,877 ms(노이즈 범위), `Max Memory Used` 116 MB. 따라서 메모리 512 MB는 측정에 근거한 선택이며 "콜드스타트를 위해 메모리를 올린다"는 흔한 처방은 이 워크로드에 **효과가 없다**. 정상 동작으로 허용하고, 제거가 필요하면 프로비저닝 동시성(월 $5.39 실측)이 있으나 기본값은 아니다. `session_manager.run()`이 인스턴스당 1회라는 제약(§7)과 맞물리므로 **재시도로 콜드스타트를 회피하려는 로직을 추가하면 안 된다** |
| EDGE-020 | **Function URL이 모든 요청에 403** — 리소스 정책·URL 설정이 전부 정상으로 보이는데도 | 권한 statement가 **두 개** 필요하다. 유통되는 거의 모든 예시가 `lambda:InvokeFunctionUrl` 하나만 보여주지만, 공식 문서는 "resource-based policy doesn't grant `lambda:invokeFunctionUrl` **and `lambda:InvokeFunction`** → 403 Forbidden"이라고 명시한다. 하나만 붙이면 `get-policy`·`get-function-url-config` 출력이 모두 정상이고 오류 메시지도 어느 액션이 빠졌는지 알려주지 않는다(`TASK-058`에서 실제로 겪음 — 함수 직접 호출로 LWA는 정상임을 먼저 분리 확인한 뒤 원인을 좁혔다). 두 번째 statement는 반드시 `lambda:InvokedViaFunctionUrl` 조건(`--invoked-via-function-url`)으로 **Function URL 경로에만** 한정한다 — 없으면 일반 Invoke API로도 누구나 호출 가능해진다 |
| EDGE-022 | **공개 엔드포인트에 대한 무인증 폭주** — 인증은 막지만 비용은 막지 못한다 | 무단 요청도 **401을 내기 전에 Lambda가 호출되므로 과금**된다. 적용 전에는 스로틀링·예약 동시성·예산 알림이 모두 없어 상한이 없었다(계정 동시성 1000 × 요청당 3 ms = 이론상 초당 33만 요청). 실측 단가 기준 노출: 1억 요청 = **$175**, 10억 요청 = **$1,754**. 4중 방어로 닫는다: ① **Function URL 삭제** — 이것이 API Gateway를 **우회**하므로 먼저 없애지 않으면 나머지가 전부 무의미하다(`Principal:"*"` 권한도 함께 회수해 재생성 시 자동 공개를 막는다) ② API Gateway 스로틀링 rate 10/s·burst 20 — 429는 Lambda를 호출하지 않고 끊기므로 거절 비용이 가장 싸다 ③ 예약 동시성 10(백스톱) ④ 예산 $10 + FORECASTED 알림. **저장소 공개 여부와 무관한 문제**다 — 엔드포인트는 어느 쪽이든 인터넷에 있다 (`infra/05-abuse-protection.sh`) |
| EDGE-021 | **Lambda 환경변수 총량 4 KB(aggregate) 초과** | 함수 생성·설정 변경이 실패한다. 실측 2,252 B / 4,096 B(55%)이고 그 중 PEM이 1,674 B다. 그래서 **기본값과 같은 선택 키는 주입하지 않는다**(`MCP_PORT`·`MCP_LOG_LEVEL`·`MCP_STATELESS_HTTP`·`MCP_JSON_RESPONSE` 등 — 6개 생략). RSA 4096비트 키(약 3,250 B)로 교체하면 총량이 3,800 B대가 되어 여유가 사라지므로, 그 시점에는 앱이 SSM을 직접 읽는 방식으로 전환해야 한다(`TASK-056`의 잔여 노출 결정과 같은 트리거) |
| EDGE-017 | Lambda 동기 호출 **응답 페이로드 6 MB** 한계 초과 | 도달 불가 — `CTR-004` 상한이 1 MiB이고 SDK `max_request_body_size` 기본값이 4 MiB다. 단 `MCP_READ_FILE_MAX_BYTES`를 상한 밖으로 올릴 수 없게 이미 기동 검증이 막고 있다(`CTR-004` 범위 `1..1048576`). 이 경계는 **CTR-004 상한을 올리려는 향후 변경의 하드 제약**으로 기록한다 |
| EDGE-018 | **API Gateway HTTP API 통합 타임아웃 30초(하드)** vs GitHub HTTP 타임아웃 30초 | 현재 `_GITHUB_HTTP_TIMEOUT_SECONDS = 30.0`이라 여유가 0이고, 느린 GitHub 응답이 API Gateway 504로 나가 툴 오류가 `EDGE-003`/`EDGE-009`의 정규화 경로를 타지 못한다. **GitHub 타임아웃을 20초로 낮춰** 서버가 먼저 타임아웃을 잡고 정규화된 툴 오류를 반환하게 한다. Function URL 직결(Step 5~6)에서는 15분 한계라 해당 없으나, Step 7 이후 상시 적용된다 |
| EDGE-019 | **프론팅 계층이 `Host`를 치환** | `MCP_ALLOWED_HOSTS`(앱이 검증하는 값)와 `MCP_PUBLIC_URL`(클라이언트가 보는 값)이 갈라지면 전 요청 421이다. Function URL 직결은 둘 다 lambda-url 호스트로 일치한다. **CloudFront는 `AllViewerExceptHostHeader`가 필수여서 앱이 lambda-url 호스트를 보게 되어 강제로 갈라진다** — API Gateway HTTP API는 `Host`가 `mcp.devoks.kr`로 도착해 일치를 유지한다(§7 · §10 Step 7의 기술 선택 근거) |

## 9. Testing Strategy

- **대상 우선순위:** ① 인가 판정(`policy.py`) ② 감사 레코드 필드·마스킹 ③ GitHub 응답 정규화(절단·바이너리·오류) ④ 토큰 캐시 갱신·동시성 ⑤ HTTP 계층 인증
- **필수 범위:**
  - `policy.py` — 역할×툴×저장소 조합 전수. **순수 함수이므로 GitHub 없이 검증**(`DSN-002`).
  - `audit/logger.py` — `CTR-003` 필드 존재, 토큰·PEM·파일 본문 **미포함** 단정.
  - `adapters/.../client.py` — httpx mock으로 200/404/403(rate limit)/5xx, 절단 경계, 바이너리.
  - `credentials.py` — 만료 여유 경계(`CTR-009`) 전후, 동시 갱신 1회 합치기.
  - **인증은 ASGI/HTTP 레벨로** — in-memory `Client(mcp)`는 인증을 건너뛴다(§7). 401·`WWW-Authenticate`·RFC 9728 문서·421 Host 거부는 HTTP 테스트로 작성한다.
- **추적:** 테스트 설명에 `AC-xxx-y` ID를 박아 PLAN `traces`와 양방향 추적한다.

## 10. Roadmap — 후속 단계 (Stage 2 · Stage 3)

> `AC-008-1` 충족. 이번 워크플로 실행 범위는 **Stage 1**이며, 아래는 다음 세션이 이어받을 작업이다.

### Stage 2 — AWS Lambda 실배포 (배포 타깃 변경 확정: 2026-09-10)

> **배포 타깃이 ECS/Fargate → Lambda + Function URL로 바뀌었다.** 근거는 AWS Price List Query API로 실측한 서울 리전 요율이다. Fargate(0.25 vCPU / 0.5 GB, ARM) + ALB 구성은 **월 $38**인데, 그 중 **ALB 시간당 $16.43 + ALB가 2개 AZ에 강제로 갖는 공용 IPv4 2개 $7.30 = $23.73(62%)**가 "트래픽이 0이어도 24시간 대기하는 고정 진입점" 값이다. 실제 연산은 $8.29(22%)뿐이다.
>
> 이 서버는 **읽기 전용 4툴 · 요청 간 상태 없음 · 내부 팀 사용**이라 상시 대기가 필요 없다. Lambda 프리티어(월 100만 요청 + 400,000 GB-초, **상시 무료**) 안에서 동일 기능이 **월 $0.01**(ECR 저장분)로 제공된다 — 월 5,000회 호출 × 400 ms × 512 MB = 1,000 GB-초로 무료 한도의 0.25%다. 월 20만 회까지 무료 구간이다.
>
> **이 결정이 아래 "legacy 레그 확장 방식" 미결 사항을 (b)로 확정한다** — `stateless_http=True`. §7이 이미 기록한 대로 대가는 서버→클라이언트 역채널(sampling, push elicitation, `roots/list`)과 재개 가능성 상실이고, Stage 1의 단발 조회 툴에는 무해하다. **Stage 3(Slackbot이 elicitation을 쓰기 시작하는 시점)이 재검토 트리거**다.
>
> **Step 1 산출물은 전부 재사용된다** — Lambda는 ECR 컨테이너 이미지로 배포되므로 ECR 리포지토리·라이프사이클·GitHub OIDC 공급자·IAM 역할·CI 빌드·arm64 Dockerfile이 그대로 쓰인다. 태스크 정의·ALB·대상그룹은 **아직 만들지 않았으므로 폐기 비용이 0**이다.
>
> 검토했으나 탈락한 대안: **App Runner**(TLS·커스텀 도메인 내장으로 ALB가 불필요했으나 **ap-northeast-2 미제공** — Price List API로 제공 리전 확인: `ap-northeast-1, ap-south-1, ap-southeast-1/2, eu-*, us-*`), **Fargate + Cloudflare Tunnel**(월 $11.94, 콜드스타트 없음, 사이드카·외부 의존 추가).

- [x] **Step 1 — 이미지 공급 경로** (계정 `703630528452`, `infra/01-ecr-and-github-oidc.sh`)
  - ECR 리포지토리 `devoks-mcp-management` + scanOnPush + 라이프사이클 정책
  - GitHub OIDC 공급자 — thumbprint를 인증서 체인에서 **실시간 계산**(GitHub이 Let's Encrypt로 이전해 유통되는 DigiCert 값 `6938fd4d…`는 폐기됨)
  - IAM 역할 `devoks-mcp-github-actions` — **immutable subject claim** 신뢰 정책 `repo:ridsync@8566036/devoks-mcp-servers@1355671954:*` (2026-07-15 이후 생성 저장소는 소유자·저장소 불변 ID가 `@`로 붙는다. AWS 문서·블로그의 구 형식은 신규 저장소에서 깨지고 오류 메시지가 이유를 알려주지 않는다)
  - CI push 배선 — 이미지 검증 완료(58.8 MB, `linux/arm64`, `ubuntu-24.04-arm` 러너)
- [ ] **Step 2 — 코드 전환** (`CTR-011`, `MCP_STATELESS_HTTP`, `MCP_JSON_RESPONSE`)
  - `app.py`가 `streamable_http_app(stateless_http=…, json_response=…)`을 설정에서 받아 넘긴다
  - Dockerfile에 LWA 확장 1줄 + `AWS_LWA_*` 3개. **LWA가 Runtime Interface Client를 자체 포함**하므로 `python:3.14-slim-trixie` 베이스를 그대로 쓴다(AWS 공식 확인) — 베이스 이미지 교체 불필요
  - `_GITHUB_HTTP_TIMEOUT_SECONDS` 30.0 → 20.0 (`EDGE-018`)
- [ ] **Step 3 — 시크릿 이전: SSM Parameter Store Standard(`SecureString`)**
  - Secrets Manager가 아니라 SSM을 쓴다 — Standard 파라미터는 **4 KB까지 무료**이고 KMS 암호화가 동일하다. GitHub App PEM이 약 1.7 KB로 들어간다. Secrets Manager는 시크릿당 **$0.40/월**(실측)이고 자동 로테이션이 유일한 차별점인데 GitHub App 키는 수동 교체다 → 월 $0.80 절감
  - `/devoks-mcp/management/github-app-private-key`, `/devoks-mcp/management/client-tokens`
- [ ] **Step 4 — Lambda 실행 역할 + 로그 그룹 + 함수 생성**
  - 실행 역할: CloudWatch Logs 쓰기 + 위 2개 SSM 파라미터 `GetParameter` + KMS `Decrypt`만 (최소권한)
  - 로그 그룹 보존기간 설정 — CloudWatch Logs 수집은 서울에서 **$0.76/GB**(실측)로 비싼 편이라 감사 레코드 양이 늘면 체감된다. 프리티어 5 GB/월
  - 함수: ECR 이미지, `arm64`, 512 MB, 타임아웃 60초
- [x] **Step 5 — Function URL (`AuthType=NONE`)** — *부트스트랩 전용이었고 2026-09-14에 삭제됨(`EDGE-022`). 아래는 당시 근거 기록.*
  - `NONE`이 맞다 — `AWS_IAM`은 SigV4를 `Authorization` 헤더에 쓰므로 우리 Bearer 토큰과 정면 충돌한다. 인증 경계는 Stage 1에서 만든 OAuth 2.1 리소스 서버(`AC-002-*`)다
  - **2단계 주입이 필요하다** — Function URL의 `<url-id>`는 생성 시점에 결정되므로, 함수 생성 → URL 확보 → `MCP_PUBLIC_URL=https://<url-id>.lambda-url.ap-northeast-2.on.aws/mcp`·`MCP_ALLOWED_HOSTS=<url-id>.lambda-url.ap-northeast-2.on.aws` 주입 순서다(`EDGE-019`)
- [ ] **Step 6 — 실제 MCP 클라이언트(Claude Code) E2E 검증**
  - 원격 등록 → `initialize` → 4툴 호출. allowlist 밖 저장소 거부·경로 트래버설 차단(`EDGE-013`)이 배포 환경에서도 재현되는지 확인
  - `MCP_REPO_ALLOWLIST` 프로덕션 값 확정
- [ ] **Step 7 — 커스텀 도메인 `mcp.devoks.kr` (API Gateway HTTP API)**
  - **CloudFront가 아니다.** §7에 기록한 대로 CloudFront + Function URL은 OAC를 쓰는 순간 MCP와 비호환이고(POST 본문 SigV4 서명 요구), OAC를 포기해도 `AllViewerExceptHostHeader`가 필수여서 `Host`가 lambda-url 도메인으로 도착해 `MCP_PUBLIC_URL`과 강제로 갈라진다(`EDGE-019`). 인증된 POST-only JSON-RPC라 CDN 캐싱 가치도 0이다
  - API Gateway HTTP API + Lambda 프록시 통합은 그 문제가 전부 없다: ACM 인증서가 **같은 리전**(`ap-northeast-2`), `Host`가 `mcp.devoks.kr`로 도착해 두 설정값이 **일치**, `Authorization` 기본 전달, 요금 **$1.23/백만 요청**(실측) → 월 $0
  - 가비아 DNS에 CNAME 2건: ACM 검증용 underscore CNAME, `mcp` → API Gateway 리전 엔드포인트. (Route 53 위임은 월 $0.50이며 지금은 불필요 — ALIAS가 필요한 ALB가 없어졌다)
  - 완료 후 `MCP_PUBLIC_URL`·`MCP_ALLOWED_HOSTS`를 도메인으로 전환
- [ ] **Step 8 — CI 배포 스텝**
  - GitHub Actions IAM 역할에 `lambda:UpdateFunctionCode` 추가(현재는 ECR push 전용). 기본 브랜치 push에서만 동작하도록 게이트
- **미결 결정:**
  - 저장소를 개인 계정 `ridsync/`에서 조직 `org-devoks/`로 이관할지 (§7). 이관 시 **immutable subject claim의 소유자 ID가 바뀌므로 Step 1의 IAM 신뢰 정책을 함께 갱신**해야 한다
  - ~~**저장소 공개 범위**~~ — **해소(2026-09-14): PUBLIC 유지로 확정.** 노출 항목을 하나씩 공격 경로 기준으로 점검한 결과 **어느 것도 보안 통제로 기능하지 않는다**:
    - **계정 ID·역할 ARN** — 무해하다. 신뢰 정책이 `aud=sts.amazonaws.com`(StringEquals)와 **불변 subject** `repo:ridsync@8566036/devoks-mcp-servers@1355671954:*`로 고정돼 있어, 역할을 맡으려면 GitHub이 **그 저장소 ID로** OIDC 토큰을 발급해야 한다. 포크는 다른 ID를 받고, 같은 이름으로 재생성해도 ID가 다르다. **ARN을 알아도 얻는 것이 0이다.**
    - **엔드포인트** — 공개가 정상이다. 인증 경계는 256비트 베어러 토큰이고 무단 요청은 401(실측). 브루트포스는 비현실적이다.
    - **`org-devoks` 저장소명** — 정찰 정보일 뿐이며, GitHub App 설치 범위 + `CTR-008` 정확 일치가 실제 통제다. 이름은 통제가 아니다.
    - **`EDGE` 21건의 재현 절차** — Kerckhoffs 원칙상 설계가 알려져도 안전해야 보안이다. 우리 방어(입력 검증·정확 일치 allowlist·토큰 인증)는 알려진다고 약해지지 않고, 회귀는 테스트 338개가 막는다.
    - 덤으로 public이면 **Secret Scanning·Push Protection·Dependabot이 무료**다.
    - **비용 근거도 폐기됐다** — "private 전환 시 arm64 러너 과금"이라 적었으나 GitHub이 arm64 standard 러너를 private 저장소에서 **프리티어 대상**으로 바꿨다. 실사용 측정: 33 run/11일 → 월 약 151 job-분, 프리티어 2,000분의 **7.5%** → 전환하더라도 **$0**이었다. 즉 이 항목은 애초에 비용 문제가 아니었다.
    - **대신 진짜 문제를 찾았다** — 남용 방어 부재(`EDGE-022`). 저장소 공개 여부와 무관하며 별도로 닫았다.
  - 정적 Bearer → 사내 IdP OAuth 2.1 전환 시점과 IdP 선택 (`DSN-001`이 교체 지점을 `verifier.py`로 국소화해 둠)
  - ~~**AWS 계정 플랜(Free vs Paid)**~~ — **해소(2026-09-10)**: 계정 `703630528452`은 **Paid 플랜**이다(사용자 확인). 계정 자체는 오래 전에 생성됐고 IAM 사용자 `devoks`만 2026-09-04에 새로 만든 것 — 사용자 생성일을 계정 나이의 대리 지표로 삼은 것은 잘못된 추론이었다. 따라서 "크레딧 소진 시 계정 닫힘" 위험은 없다.
    **비용 추정에는 영향이 없다** — 월 $0.01 추정의 근거인 Lambda 프리티어(월 100만 요청 + 400,000 GB-초)는 **12개월 한정이 아니라 상시 무료(always free)**라 계정 나이와 무관하다. CloudWatch Logs 5 GB, CloudFront 1 TB도 상시 무료다. 12개월 한정인 ECR 500 MB만 만료됐을 것이므로 이미지 61.7 MB × $0.10/GB-월 = **월 $0.006**이 실제로 청구되며, 이는 §10 추정에 이미 포함돼 있다
  - 콜드스타트(`EDGE-016`)를 프로비저닝 동시성(월 $5.39)으로 제거할지 — Step 6의 실사용 체감으로 판단한다

### Stage 3 — Slackbot 연동

- [ ] Slack 앱 등록, 이벤트 구독(mention/slash command), 서명 검증
- [ ] Slackbot을 **MCP 클라이언트**로 구현해 Management MCP에 Bearer 토큰으로 접속
- [ ] Slack 사용자 → MCP 역할 매핑 (`CTR-007` 확장). 사람 단위 감사 추적이 필요해지는 시점 → OAuth 전환 트리거
- [ ] 스레드 컨텍스트 유지, 응답 길이·코드블록 렌더링 정책
- **미결 결정:** Slackbot을 이 모노레포의 `servers/slackbot/`으로 둘지, 별 저장소로 둘지 (`DSN-008` 확장 여지)

### Stage 1 이후 계층 확장 (순차)

- Knowledge: Notion, PRD/TRD 문서 조회 → `adapters/knowledge/<source>/` 추가 (`DSN-005`)
- Runtime: Sentry, Grafana, CloudWatch, GitHub CI → `adapters/runtime/`
- Business: Data API, Read DB, Analytics → `adapters/business/`. **Read DB 접근은 VPC 내부 경로가 필요**하므로 Stage 2의 네트워크 구성이 선행 조건이다.
