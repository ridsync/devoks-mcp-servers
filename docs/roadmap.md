# Roadmap

이 문서는 아직 FRD/PLAN으로 확정되지 않은, **확장 예정 스펙 수준의 아이디어**를 모아둔다.
실행 착수 시점에 `.claude/workspace/<name>/FRD.md`로 정식 요구사항화한다.

## GitHub 쓰기 권한 — Slack에서 이슈 생성·코드 수정 요청

### 목표

지금은 Slack에서 Management MCP의 GitHub 툴로 **조회만** 가능하다. 확장되면 Slack에서
자연어로 "이 버그 이슈 만들어줘", "이 함수 고쳐서 PR 올려줘" 같은 요청을 하고, 그 결과를
같은 스레드에서 확인할 수 있게 된다.

### 현재 상태 (읽기 전용)

- `servers/management`의 GitHub 클라이언트(`adapters/knowledge/github/client.py`)는
  GitHub REST API에 `GET` 요청만 보낸다 — `POST`/`PATCH`/`PUT`/`DELETE` 코드가 없다.
- 노출된 툴 4개(`list_repos`, `get_repo_tree`, `read_file`, `search_code`) 전부 조회용.
- `CTR-007`(FRD)에 "Stage 1은 전 툴이 읽기 전용이라 단일 역할(`reader`)로 시작"이라고
  명시돼 있다 — 우연이 아니라 의도된 설계.

### 이미 갖춰져 있어 재사용 가능한 것

- **어댑터 레지스트리 패턴** — 새 write 툴을 추가하는 비용이 "디렉토리 1개 +
  `tools/registry.py` 한 줄"로 설계돼 있다(`DSN-005`). 새 GitHub write 어댑터를 붙이는
  구조 자체는 이미 있다.
- **사람별 인가·감사** — Slackbot이 사람마다 별도 MCP 토큰을 발급하므로(`infra/11-slackbot-user-tokens.sh`),
  Slack에서 쓰기 작업을 요청해도 감사 로그의 `client_id`가 "slackbot"이 아니라 **실제
  요청한 사람**으로 남는다. role×tool 인가(`auth/policy.py`, `MCP_ROLE_TOOLS`)도 이미
  있어 "누구는 이슈 생성 가능, 누구는 조회만"류 역할 분리가 구조적으로 바로 된다.
- **Slack → Claude API MCP 커넥터 → Management MCP** 경로 — Slackbot worker가 이미
  이 경로로 동작 중이라, 새 MCP 툴만 추가되면 자연어 요청 → 툴 호출 흐름은 추가 배선
  없이 그대로 이어진다.

### 실제로 필요한 작업

1. **GitHub App 권한 확장** — Contents/Issues 등에 Write 권한 추가. **조직 관리자만
   가능**한 작업(FRD: "App 등록은 사용자만 가능")이라 가장 먼저 확인해야 할 블로커.
2. **write 어댑터/툴 구현**
   - 이슈 생성(`create_issue`)은 REST API 1회 호출로 비교적 단순.
   - "코드 수정"은 브랜치 생성 → 파일 커밋 → PR 오픈까지 필요해 별도 설계가 필요하다.
     실제 코드 변경 내용 자체는 Claude가 생성하고, 새 MCP 툴은 그 결과를 커밋/PR로
     반영하는 수단만 제공하면 된다.
3. **역할 확장** — 현재 `MCP_ROLE_TOOLS`에는 읽기 전용 `reader` 역할만 있다. write
   가능 역할(예: `contributor`)을 추가하고, 어떤 사람에게 부여할지 정책을 정한다.
4. **안전장치** — 쓰기는 읽기보다 리스크가 크다. 최소한:
   - merge처럼 되돌리기 어려운 동작은 자동 실행하지 않고 사람 승인을 거치게 한다
     (PR 생성까지는 자동, merge는 별도 승인).
   - 현재 읽기 전용 툴에 적용된 오남용 방어(4중 적용, `infra/05-abuse-protection.sh`)를
     쓰기 작업에도 적용할지 재검토한다.

### 관련 근거 문서

- `.claude/workspace/management-mcp-bootstrap-20260903/FRD.md` — `CTR-007`(현재 읽기 전용
  단일 역할), `DSN-005`(어댑터 레지스트리 패턴)
- `.claude/workspace/slackbot-integration-20260914/FRD.md` — 사람별 MCP 토큰·감사 설계
- `infra/11-slackbot-user-tokens.sh` — per-person 토큰 발급 스크립트(현재 읽기 전용
  토큰에 이미 적용된 패턴, write 역할에도 그대로 재사용 가능)
