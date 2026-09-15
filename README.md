# devoks-mcp-servers

사내 프로젝트/서비스의 지식·상태를 여러 에이전트 클라이언트(Claude Code, Codex, Slackbot 등)에서
**단일 MCP 엔드포인트**로 조회하기 위한 MCP 서버 모음(uv workspace 모노레포)이다.

> **프로젝트 사실(SSOT)**: [`.claude/CLAUDE.md`](.claude/CLAUDE.md) · **작업 흐름 서술**:
> [`docs/WORKFLOW.md`](docs/WORKFLOW.md) · **배포 도메인**: `mcp.devoks.kr`(Stage 2 완료)

## 서버 개요

| 서버 | 경로 | 역할 | 상태 |
|---|---|---|---|
| Management MCP | `servers/management/` | GitHub Knowledge 어댑터 기반 MCP 서버 | Stage 1 완료 · Stage 2 배포 완료(`mcp.devoks.kr`) |
| Slackbot | `servers/slackbot/` | Slack Events API ↔ Claude API MCP 커넥터 브리지 | Stage 3 진행 중 (issue [#4](https://github.com/ridsync/devoks-mcp-servers/issues/4)) |

이 문서는 **Management MCP 서버(Stage 1)** 기준으로 작성됐다. 요구사항·설계 결정·실측으로
확인된 함정의 전체 근거는 워크스페이스 문서에 있다 — 이 README는 그 요약이 아니라
**"막히는 지점을 미리 치워주는" 실행 가이드**다. Stage 3(Slackbot 연동)의 잔여 작업·미결
결정은 이 문서에 복제하지 않고 아래에서 가리키기만 한다:

- `.claude/workspace/management-mcp-bootstrap-20260903/FRD.md` — 요구사항·계약(Contract)·
  제약·§10 로드맵
- `.claude/workspace/management-mcp-bootstrap-20260903/PLAN.md` — 작업 분해·진행 상태
- `.claude/workspace/slackbot-integration-20260914/FRD.md` / `PLAN.md` — Stage 3 요구사항·
  진행 상태(자세한 내용은 아래 "Slackbot (Stage 3)" 절 참고)

## Stage 1의 범위 — 무엇이고 무엇이 아닌지

아키텍처는 AgentClient → Management MCP → **Knowledge / Runtime / Business** 3계층 어댑터로
구성된다. **Stage 1은 이 중 Knowledge 계층의 GitHub 어댑터 하나만 구현한다.**

```mermaid
flowchart LR
    AC["AgentClient<br/>(Claude Code / Codex / Slackbot)"] -->|Bearer token| MM["Management MCP<br/>servers/management"]
    MM --> KG["Knowledge — GitHub ✅"]
    MM -.-> KN["Knowledge — Notion 등 ❌"]
    MM -.-> RT["Runtime — Sentry/Grafana/CI ❌"]
    MM -.-> BZ["Business — Data API/Read DB ❌"]

    SlackEvt["Slack Events API"] --> H["handler Lambda<br/>서명검증→idempotency claim"]
    H -->|async invoke| W["worker Lambda<br/>Claude API MCP 커넥터"]
    W --> MM
    W --> SlackEvt
```

| 계층 | 상태 |
|---|---|
| Knowledge — GitHub | ✅ 구현됨 (`list_repos`, `get_repo_tree`, `read_file`, `search_code`) |
| Knowledge — Notion, PRD/TRD 등 | ❌ 미구현 (FRD §10) |
| Runtime — Sentry/Grafana/CloudWatch/GitHub CI | ❌ 미구현 (FRD §10) |
| Business — Data API/Read DB/Analytics | ❌ 미구현 (FRD §10) |
| AWS Lambda 실배포 | ✅ 완료 — Stage 2. `mcp.devoks.kr` 라이브, 오남용 방지 4중 적용 완료 |
| Slackbot (MCP 클라이언트) | 🟡 진행 중 — Stage 3, `servers/slackbot/` 구현 중(issue #4) |

Auth(Bearer 토큰 검증)·RBAC(역할×툴×저장소 인가)·Audit(감사 로그) 골격은 계층이 늘어나도
바뀌지 않도록 Stage 1에서 이미 고정해 뒀다.

## 요구사항

- **Python 3.14 이상 — 선호가 아니라 하한이다.** GitHub 어댑터
  (`servers/management/src/devoks_mcp_management/adapters/knowledge/github/client.py`)가
  **PEP 758**(괄호 없는 다중 예외 `except A, B, C:`) 문법을 쓴다. Python 3.13에서는 이 파일을
  import하는 순간 **SyntaxError**가 난다 — 런타임 동작 차이가 아니라 애초에 뜨지 않는다.
  `.python-version`·`servers/management/pyproject.toml`의 `requires-python`·
  `servers/management/Dockerfile`의 베이스 이미지가 전부 3.14+로 맞춰져 있다.
- [uv](https://docs.astral.sh/uv/) — 이 저장소는 uv workspace 모노레포다
  (`[tool.uv.workspace] members = ["servers/*"]`, 락파일 `uv.lock`은 루트에 1개).
- 컨테이너 이미지를 직접 빌드/실행하려면 Docker(buildx)가 필요하다 — **로컬 필수는 아니다**,
  아래 "컨테이너 빌드" 절 참고.

## 빠른 시작

```bash
# 1) 의존성 설치 (workspace 전체)
uv sync

# 2) 환경변수 준비 — 아래 "환경변수" 절을 먼저 읽어라. JSON 값은 반드시
#    작은따옴표로 전체를 감싸야 한다(이유는 .env.example 안의 주석 참고).
cp .env.example .env
# .env를 열어 최소한 GITHUB_APP_PRIVATE_KEY를 (더미여도 괜찮으니) 유효한 PEM으로 바꿔라.

# 3) 로컬 서버 기동 — uv가 .env를 로드해 환경에 주입한다(bash sourcing보다 안전 —
#    아래 "환경변수" 절 참고).
uv run --env-file .env uvicorn devoks_mcp_management.app:create_app_from_env --factory \
  --host 127.0.0.1 --port 8000 --app-dir servers/management/src

# 4) 확인 (다른 터미널에서)
curl http://127.0.0.1:8000/healthz
# => {"name":"devoks-management-mcp","version":"0.1.0"}
```

위 4단계 전부 이 문서 작성 시점에 로컬에서 실측 검증됐다(`uv sync` exit 0 / 서버 기동 /
`curl /healthz` → `200`).

`--app-dir servers/management/src`가 필요한 이유: 개발 환경에서는 패키지 소스가
`src/` 레이아웃 아래에 있다. `uv sync`가 이 workspace 멤버를 editable로 설치해 두므로
저장소 루트에서 `--app-dir` 없이 실행해도 실제로는 동작하지만(둘 다 실측 확인),
컨테이너 안에서는 `--no-editable`로 설치되므로(아래 "컨테이너 빌드" 절) 소스 경로
지정 없이 `/app/.venv/bin/uvicorn devoks_mcp_management.app:create_app_from_env
--factory`만으로 동작한다. `--app-dir`를 붙인 형태를 기본으로 안내하는 이유는 실행
위치(cwd)나 venv 동기화 상태에 덜 의존하는 더 안정적인 형태이기 때문이다.

**`--factory`는 필수다.** `app.py`는 의도적으로 모듈 레벨 `app` 속성을 두지 않는다 — 이
모듈을 import하는 것만으로 환경변수를 읽거나 `ConfigError`를 던지는 일이 없어야 하기
때문이다(`create_app_from_env()`를 **호출**할 때만 환경을 읽는다).

## 환경변수

전체 목록·설명·실제로 동작하는 예시 값은 **`.env.example`**을 봐라 — 필수 8개와 선택
키를 구분해 각 줄에 주석을 달아 뒀고, 이 값들은 `load_settings()`(설정 검증 함수)를
실제로 통과함이 검증됐다.

⚠️ **JSON 값(`MCP_CLIENT_TOKENS`, `MCP_ROLE_TOOLS`)과 `GITHUB_APP_PRIVATE_KEY`는 반드시
값 전체를 작은따옴표(`'`)로 감싸라.** 감싸지 않으면 `uv run --env-file .env`도
`bash -c 'set -a; source .env; set +a'`도 내부의 큰따옴표를 셸 인용부호로 오인해
잘라먹어 JSON이 깨진다 — 둘 다 실측으로 확인된 함정이다. `.env.example`은 이미 올바른
형태로 작성돼 있으니 그대로 값만 채우면 된다.

주의해야 할 함정 몇 가지(전부 실측 확인, 자세한 근거는 FRD §5.2/§7):

- **`MCP_ALLOWED_HOSTS`가 없으면 기동 자체가 실패한다.** 의도된 설계다 — 없이 뜨면 실제
  호스트명 뒤에서 전 요청이 421이 되고 클라이언트에는 일반 전송 오류로만 보여 원인
  추적이 어렵기 때문이다. 로컬 개발은 `127.0.0.1,localhost`로 충분하다 — 서버가 각 호스트
  를 포트 없는 형태와 `host:*`(모든 포트) 형태 양쪽으로 자동 확장하므로, 포트별 항목을
  따로 나열할 필요가 없다.
- **`MCP_PUBLIC_URL`의 경로가 `/mcp`로 끝나야 하고 후행 슬래시가 없어야 한다.** 틀려도
  서버는 **정상 기동한다** — 대신 RFC 9728 well-known 경로가 조용히 다른 곳으로
  어긋난다. 이 프로젝트는 이 흔한 실수를 기동 시점 검증으로 잡아 오류 메시지에 무엇이
  잘못됐는지 정확히 출력하도록 만들어 뒀다.
- **`MCP_REPO_ALLOWLIST`는 다른 필수 키들과 반대 방향이다.** 이 키는 없어도 기동은
  성공하지만, **빈 값(기본값) = 모든 저장소 접근 거부**(fail-safe)다. GitHub 조회 툴에서
  실제로 결과를 받으려면 반드시 채워야 한다. 형식은 `owner/repo` 완전일치, 대소문자
  구분, 와일드카드 없음.

## 테스트 · 린트 · 타입체크

```bash
uv sync
uv run pytest -q          # 235 passed (이 문서 작성 시점 실측)
uv run ruff check .
uv run ruff format .
uv run pyright
```

네 명령 모두 저장소 루트에서 실행한다 — `pytest`/`ruff`/`pyright` 설정이 전부 루트
`pyproject.toml`에 있다(워크스페이스 SSOT). 별도 옵션이나 작업 디렉터리 변경이 필요
없다.

## MCP 클라이언트 등록

### Claude Code (CLI)

아래 형태는 `claude mcp add --help`의 공식 예시와 일치함을 확인했다(확인 시점 `claude` 2.1.259).
형태가 달라 보이면 `claude mcp add --help`를 다시 확인하라.

```bash
# 로컬 개발 서버에 등록
claude mcp add --transport http devoks-management-local \
  http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer dev-local-token-change-me"

# 배포된 서버에 등록 (MCP_CLIENT_TOKENS에 등록된 실제 토큰으로 교체)
claude mcp add --transport http devoks-management \
  https://<host>/mcp \
  --header "Authorization: Bearer <token>"
```

- 엔드포인트는 항상 `<base-url>/mcp` 형태다(`CTR-001`).
- **등록 범위(`-s, --scope`)의 기본값은 `local`** — 현재 프로젝트에서 본인만 쓰는 등록이다.
  팀이 공유하려면 `--scope project`(저장소에 커밋되는 설정), 여러 프로젝트에서 쓰려면
  `--scope user`를 준다. **`--scope project`로 등록하면 위 명령의 토큰이 저장소에
  커밋된다** — 공유 설정에는 개인 토큰을 넣지 말고, 팀 공용 토큰을 `MCP_CLIENT_TOKENS`에
  별도 `client_id`로 발급해 쓰는 편이 감사 추적상으로도 맞다.
- `Authorization: Bearer <token>` 헤더의 토큰은 `MCP_CLIENT_TOKENS`(서버 환경변수)에
  등록된 값이어야 한다 — 등록되지 않은 토큰은 401을 받는다.

### 토큰 발급 (`MCP_CLIENT_TOKENS`)

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

43자 / 256비트가 나온다. `config.py`가 **32자 미만을 거부**하므로 손으로 지어낸 값은
기동 단계에서 막힌다. 클라이언트마다 **다른 토큰**을 발급하면 감사 로그(`CTR-003`)의
`client_id`로 누가 호출했는지 구별된다 — 같은 토큰을 공유하면 그 구별이 사라진다.

> ⚠️ **`.env.example`의 토큰 플레이스홀더는 일부러 기동에 실패한다.** 실제로, 예전
> 플레이스홀더는 검증을 통과하는 유효한 값이었고, 그 결과 인터넷에 공개된 Lambda가
> **공개 저장소에 게시된 문자열**을 베어러 토큰으로 쓰고 있었다(접근 로그 확인 결과
> 제3자 접근 없음, 즉시 교체). `GITHUB_APP_PRIVATE_KEY` 플레이스홀더는 원래부터 PEM
> 파싱에 실패해 이 사고가 불가능했는데 토큰만 그 성질이 없었다. 이제 둘 다 "교체하지
> 않으면 뜨지 않는다"로 맞췄다.
- 로컬 서버(`http://127.0.0.1:8000/mcp`)에 등록해 붙이려면, 서버 쪽 `MCP_ALLOWED_HOSTS`에
  `127.0.0.1`(또는 `localhost`, 클라이언트가 접속에 쓰는 호스트명에 맞춰)이 포함돼야
  한다 — 위 "환경변수" 절의 `.env.example` 기본값이 이미 이렇게 돼 있다. 포트(`:8000`)는
  서버가 `_expand_allowed_hosts`로 자동 확장하므로 `MCP_ALLOWED_HOSTS`에 포트를 직접
  적을 필요는 없다.
- 다른 MCP 클라이언트(Codex 등)는 각자의 원격 HTTP MCP 서버 등록 방법을 따르되, 위
  세 가지(엔드포인트 URL 형태, Bearer 헤더, 허용 Host)는 공통으로 필요하다.

## 인증·인가 — 응답 코드

| 상황 | 응답 |
|---|---|
| 토큰 없음 / 검증 실패 | `401` (`WWW-Authenticate`에 `resource_metadata` 포인터 포함, 에러코드 `invalid_token`) |
| 유효 토큰이지만 스코프 부족(`devoks:read` 없음) | `403` `insufficient_scope` |
| 허용되지 않은 `Host` 헤더 (인증 통과 후) | `421` |
| 허용되지 않은 `Origin` (브라우저 경유) | `403` |
| 역할이 해당 툴을 허용하지 않음 / 저장소가 allowlist 밖 | 툴 오류 (거부 사유는 감사 로그에만, 응답 자체는 고정 메시지) |

⚠️ **미들웨어 순서가 인증 → 전송보안이다.** 즉 토큰 없이 미허용 `Host`로 보내면 `421`이
아니라 **`401`이 먼저** 온다. `421`을 실제로 관측하려면 **유효한 토큰을 함께 보내야
한다.**

## GitHub 조회 툴 (4개)

`list_repos` → `get_repo_tree` → `read_file` / `search_code` 순으로 탐색하도록 설계됐다.

- **`list_repos`** — GitHub App installation이 실제로 볼 수 있는 저장소와
  `MCP_REPO_ALLOWLIST`의 교집합만 반환한다.
- **`get_repo_tree`** — 저장소 경로의 엔트리(이름·타입·크기)를 반환한다.
- **`read_file`** — `status` 필드로 4가지 상태를 구분한다:
  - `complete` — 전체 내용
  - `truncated` — `MCP_READ_FILE_MAX_BYTES` 상한까지만 반환(전체 크기는 `total_size`)
  - `binary` — UTF-8 디코딩 실패 (내용 없음, 크기만)
  - `unavailable` — GitHub Contents API의 **100 MB 초과** 파일만 해당(1–100 MB는 raw
    폴백으로 상한까지 절단 반환되므로 `truncated`가 된다)
- **`search_code`** — GitHub 코드 검색은 **인증 상태에서도 분당 10회**로 제한된다(다른
  GitHub 검색 엔드포인트의 분당 30회보다 훨씬 빡빡한 별도 버킷). 반복 호출로 금방
  소진되므로, 초기 검색 후에는 `get_repo_tree`/`read_file`로 좁혀 가는 편이 낫다(툴
  자체 설명에도 명시돼 있다).

## 감사 로그

모든 툴 호출은 성공·거부·오류와 무관하게 stdout에 **JSON Lines 1줄**로 남는다(필드는
FRD `CTR-003`). Lambda가 stdout을 CloudWatch Logs로 그대로 수집한다. **`MCP_LOG_LEVEL`(서버 로그
레벨)과는 완전히 별개**이며, 그 값으로 필터링되지 않는다 — 감사 로그는 항상 전량
남는다.

## 컨테이너 빌드

```bash
docker buildx build --platform linux/arm64 \
  -f servers/management/Dockerfile -t <tag> .
```

- **빌드 컨텍스트는 반드시 저장소 루트(`.`)여야 한다** — uv workspace 락파일(`uv.lock`)이
  루트에 1개뿐이기 때문이다. `servers/management/`를 컨텍스트로 주면 첫 `uv sync`부터
  실패한다.
- `.dockerignore`도 루트에 있다(`.env`, `.venv/`, `.git/` 등 제외).
- **Docker가 로컬에 설치돼 있지 않아도 개발·검증에 지장이 없다.** 이미지 빌드와 컨테이너
  기동(`/healthz` 스모크 테스트) 검증은 `.github/workflows/ci.yml`의 `docker` 잡(네이티브
  arm64 러너)이 담당한다. 이 README의 컨테이너 빌드 명령 자체는 **이번 작업에서
  실행 검증하지 못했다**(로컬 Docker 미설치) — Dockerfile 자체는 별도로 작성·정적
  검증됐다(`hadolint` 0 findings 등, 자세한 내용은 PLAN `TASK-030` 참고).

## Directory Structure

```
devoks-mcp-servers/
├── servers/
│   ├── management/        # MCP 서버 (Stage 1, 배포 완료)
│   │   ├── src/devoks_mcp_management/
│   │   └── tests/
│   └── slackbot/           # Slack↔Claude 브릿지 (Stage 3, 진행 중)
│       ├── src/devoks_slackbot/
│       └── tests/
├── infra/                  # AWS 배포 스크립트 (01~05 순번)
├── docs/WORKFLOW.md         # 작업 흐름 서술
├── .claude/
│   ├── CLAUDE.md            # 프로젝트 사실 SSOT
│   ├── rules/project-convention.md  # 코딩 규범 SSOT
│   └── workspace/           # 서버별 FRD/PLAN
└── README.md
```

> 이 트리는 무엇이 어디 있는지만 보여준다 — 진행 상태·태스크 개수는 각 PLAN.md가 SSOT,
> 여기 중복 기재하지 않는다(drift 방지).

## 알려진 제약

- **MCP SDK는 v2(`mcp==2.1.1`)다.** v1의 `FastMCP`·`mcp.server.fastmcp.*` import 경로는
  이 버전에 없다 — 웹에 있는 v1 예제를 그대로 옮기면 동작하지 않는다.
  `from mcp.server import MCPServer`가 맞는 경로다.
- **프로토콜 레그가 2개이고 요구사항이 다르다:**

  | `MCP-Protocol-Version` | `Mcp-Session-Id` | 다중 태스크(수평 확장) 요구 |
  |---|---|---|
  | `2026-07-28` | 미발급(세션리스) | 없음 |
  | `2025-11-25` 이하 | 발급 + 필수 | sticky session 또는 `stateless_http=True` 필요 |

  MCP SDK 자체 클라이언트의 기본 핸드셰이크가 **`2025-11-25`로 협상**된다(실측) — 즉
  실제 AgentClient가 legacy 레그로 붙을 가능성이 높다. 태스크(컨테이너 인스턴스)를
  1개로 유지하는 동안은 이 결정을 미룰 수 있지만, Stage 2에서 2개 이상으로 늘리는
  순간 반드시 결정해야 한다 — 자세한 내용은 FRD §7·§10.

## Slackbot (Stage 3) — 진행 중

`servers/slackbot/`에 구현 중이다. 이 README는 아직 management 기준이라 slackbot의
실행·환경변수·테스트 절은 다루지 않는다 — 현재 SSOT는:

- `.claude/workspace/slackbot-integration-20260914/FRD.md` — 요구사항(`REQ-SB-*`)·
  설계(`DSN-SB-*`). ID 접두 없는 `CTR-002`/`EDGE-016` 등은 Stage 1 FRD를 가리킨다.
- `.claude/workspace/slackbot-integration-20260914/PLAN.md` — 작업 분해·진행 상태
  (issue [#4](https://github.com/ridsync/devoks-mcp-servers/issues/4))

Stage 3가 완료되면 이 절을 management와 동급의 전체 섹션(빠른 시작/환경변수/테스트)으로
승격한다.

## 로드맵 · 미결 정책

Stage 1·2가 끝난 현재 상태에서 남은 미결 결정 사항(저장소 공개 범위 재검토, 정적 토큰 →
OAuth 전환, Slackbot 연동 완료 등)은 전부 FRD에 기록돼 있다 — 여기서 다시 나열하지 않는다:

→ `.claude/workspace/management-mcp-bootstrap-20260903/FRD.md` §10 Roadmap
→ `.claude/workspace/management-mcp-bootstrap-20260903/PLAN.md` (작업 단위 진행 상태)
→ `.claude/workspace/slackbot-integration-20260914/FRD.md` / `PLAN.md` (Stage 3)
