# FRD 초안 원문 (보존)

- **출처**: `/devoks-sdlc:feature-workflow-runner` 커맨드 인자로 사용자가 본문 제공
- **수신 일시**: 2026-09-03
- **정련 산출물**: `./FRD.md`

---

## 초안 원문 (verbatim)

```
- 목표 : devoks-mcp-servers 프로젝트 및 git 초기화, mcp-server 환경 설정 및 구축
- 주 용도 : 사내 프로젝트/서비스의 에이전트 정보제공,제어등의 통합 Management MCP 서버 제공. 각AgentClient(CaludeCode,Codex등),Slack,Notion,Discord등의 환경에서 MCP Client로 사내 프로젝트 및 서비스의 지식,정보조회 및 서비스상태조회등의 MCP기능 (초기에는 시범적으로 Github 소스코드 정보조회만 우선 구축하고 점진적으로 확장 구축)
- 아키텍쳐 참고
                       ┌────────────────────┐
                       │       Slack        │
                       │                    │
User ─────────────────▶│      Slackbot      │
                       └─────────┬──────────┘
                                 │
                              MCP/HTTPS
                                 │
                       ┌─────────▼──────────┐
                       │ Management MCP     │
                       │                   │
                       │ Auth / RBAC       │
                       │ Audit             │
                       │ Tool Routing      │
                       └────┬────┬────┬────┘
                            │    │    │
              ┌─────────────┘    │    └──────────────┐
              ▼                  ▼                   ▼
       ┌────────────┐     ┌─────────────┐     ┌────────────┐
       │ Knowledge  │     │  Runtime    │     │ Business   │
       ├────────────┤     ├─────────────┤     ├────────────┤
       │ GitHub MCP │     │ Sentry      │     │ Data API   │
       │ Notion     │     │ Grafana     │     │ Read DB    │
       │ PRD / TRD  │     │ CloudWatch  │     │ Analytics  │
       └────────────┘     │ GitHub CI   │     └────────────┘
                          └─────────────┘
- 기술 스택 : python 서버기반으로 레퍼런스에서 권장하는 수준으로 구성하고, 배포는 Docker + ECS/Fargate, Fly.io, Render, Railway, Cloud Run 중 하나를 선택한다 (일반적이고, 구축 및 운용이 용이한것으로 선택)
- 레퍼런스 : https://modelcontextprotocol.io/docs/2026-07-28/develop/build-server 등 추가로 검색 참조할것
구축 계획에 좀더 구체화가 필요한사항은 물어봐라. 같이 검토 및 논의하고 확정하자.
```

---

## 정련 중 조사로 확인된 사실 (2026-09-03)

> 추측 아님 — 공식 문서·PyPI 실측.

| 항목 | 확인 값 | 출처 |
|------|---------|------|
| 공식 Python SDK 패키지·버전 | `mcp` **2.1.1** (v2가 stable 라인, `pip install mcp` 기본값). `requires_python >=3.10` | PyPI `/pypi/mcp/json` |
| v2 서버 API | `from mcp.server import MCPServer` — v1의 `FastMCP`/`mcp.server.fastmcp.*`는 제거(rename 아님, import 경로 소멸). 모든 필드 snake_case, 전송 설정은 `run()`/app builder로 이동 | py.sdk.modelcontextprotocol.io README·whats-new |
| 별도 프레임워크 | PrefectHQ `fastmcp` **4.0.2** (독립 프로젝트, 공식 SDK와 별개) | PyPI `/pypi/fastmcp/json` |
| 현행 스펙 | **2026-07-28** | modelcontextprotocol.io |
| 배포 전송 | Streamable HTTP. 2026-07-28 클라이언트는 **세션리스**(`Mcp-Session-Id` 미설정) → 어떤 워커/레플리카든 응답 가능. `stateless_http`는 legacy(2025-11-25 이하) 레그 전용 플래그 | SDK v2 `run/deploy` |
| 인증 모델 | 서버는 OAuth 2.1 **resource server**. `TokenVerifier.verify_token()` 1개 메서드 + `AuthSettings(issuer_url, resource_server_url, required_scopes)` — 둘은 항상 동반(하나만 주면 `ValueError`) | SDK v2 `run/authorization` |
| 인증 자동 산출물 | `/.well-known/oauth-protected-resource/<path>` (RFC 9728) 자동 발행, 미인증 요청은 401 + `WWW-Authenticate`에 `resource_metadata` 포인터 | SDK v2 `run/authorization` |
| 핸들러 내 신원 | `get_access_token()` → `AccessToken(client_id, scopes, subject, expires_at, claims)`. stdio·in-memory 클라이언트에서는 항상 `None` | SDK v2 `run/authorization` |
| 배포 함정 | `streamable_http_app()`은 기본적으로 localhost만 허용(DNS rebinding 보호). 실제 호스트명 뒤에서 `transport_security=TransportSecuritySettings(allowed_hosts=[...])` 없으면 **전 요청 421 Misdirected Request** | SDK v2 `run/deploy` |
| 미들웨어 | `server.middleware` 리스트(`async (ctx, call_next)`)는 소스에서 **provisional** 표기 — 관측(로깅/트레이싱)·거부용으로만 쓰고 토대로 삼지 말 것 | SDK v2 `advanced/middleware` |
| 테스트 | `Client(mcp)` in-memory 전송 제공(서브프로세스·포트 불필요). 단 HTTP 계층을 건너뛰므로 **인증은 검증되지 않음** | SDK v2 `get-started/testing` |
| ASGI 통합 | `mcp.streamable_http_app()` → Starlette 앱. `Mount()`로 여러 서버 합성·헬스체크 라우트 추가 가능 | SDK v2 `run/asgi` |

---

## 사용자 확인으로 확정된 결정

> Phase 1 정련 중 확인받은 항목을 아래에 누적한다.

### 1차 확인 (2026-09-03, AskUserQuestion)

| # | 슬롯 | 확정 값 | 비고 |
|---|------|---------|------|
| 1 | 배포 타깃 | **AWS ECS/Fargate** | 기존 AWS 인프라(CloudWatch·Read DB VPC) 정합 우선. Cloud Run 권장안을 물리고 선택 |
| 2 | GitHub 연동 방식 | **GitHub API 직접 호출** | 공식 GitHub MCP 서버 프록시 대신, 필요한 툴만 좁게 노출해 RBAC/Audit을 툴 경계에 붙인다 |
| 3 | 클라이언트 인증 | **정적 Bearer 토큰으로 시작** | TokenVerifier 인터페이스는 유지 → 이후 사내 IdP OAuth로 교체 시 구현체 1개만 변경 |
| 4 | 구축 범위 | **단계 분할** — 1단계(서버+Docker+CI) 이번 실행 → 2단계 클라우드 배포 → 최종 3단계 Slackbot 연동 | 사용자 원문: "1번 구현하고나서, 2번 실제 클라우드 배포까지 단계적으로 나누어서 진행하고싶다. 그리고, 최후에는 슬랙봇연동까지 최종완료. 추후에 작업할 수 있도록 FRD등 추후 남은 작업으로서 기록에 남겨두면 좋을것 같다" → FRD §10 Roadmap으로 기록 (`REQ-008`) |

### 2차 확인 (2026-09-03, AskUserQuestion)

| # | 슬롯 | 확정 값 | 비고 |
|---|------|---------|------|
| 5 | 초기 툴셋 | **코어 4개** — `list_repos` · `get_repo_tree` · `read_file` · `search_code` | PR/이슈·커밋이력·CI 상태는 후속 확장 |
| 6 | GitHub 자격증명 | **GitHub App** | installation 토큰 자동 갱신, 설치 단위 권한 제한, rate limit 5000/hr, 감사에 앱으로 기록 |
| 7 | 저장소 구조 | **모노레포** (uv workspace, `servers/management/`) | 레포명 복수형과 일치. `packages/` 공용 추출은 두 번째 서버 등장 시 |
| 8 | 저장소 접근 범위 | **명시적 repo allowlist** | 빈 allowlist = 전부 거부(fail-safe)로 설계 확정 |

### 환경 실측 (2026-09-03)

| 항목 | 값 | 영향 |
|------|-----|------|
| git remote | `https://github.com/ridsync/devoks-mcp-servers.git` (개인 계정) | 사내 조직은 `org-devoks` → 이관 여부는 Stage 2 미결 사항 |
| gh 인증 조직 | `org-devoks` | GitHub App 설치 대상 |
| Python | 3.14.2 (homebrew), uv 0.11.4 | `mcp` 2.1.1은 `>=3.10` + 3.14 의존성 분기 존재 → 3.14 사용 |
| Docker | **미설치** (`command not found`) | 이미지 빌드·기동 검증을 로컬에서 못 함 → CI에서 수행 (FRD §7 제약) |
| 현재 브랜치 | `main` (clean, 추적 파일 0개) | 구현 착수 전 브랜치 사전체크 필요 |
