# CLAUDE.md — devoks-mcp-servers

이 문서는 **프로젝트 사실 SSOT**다(Tech Stack / Commands / Architecture / Sensitive Files).
"어떻게 짜야 하는가"(네이밍·패턴·comment rule 등 규범)는 여기 두지 않는다 — 그건
`.claude/rules/project-convention.md`가 SSOT다. 이 파일이 새 사실과 충돌하면 이 파일을
먼저 고친다.

---

## Tech Stack

- **Language:** Python 3.14 이상 — 하한(PEP 758 문법 의존, 3.13 이하는 import 시점 SyntaxError).
- **Package manager:** uv workspace 모노레포 (`[tool.uv.workspace] members = ["servers/*"]`,
  락파일 `uv.lock` 루트에 1개). Ruff/pyright/pytest 설정도 전부 루트 `pyproject.toml`.
- **`servers/management`** — `mcp` SDK(`mcp==2.1.1`) 기반 MCP 서버 + Starlette + uvicorn.
  GitHub Knowledge 어댑터(4개 툴: `list_repos`/`get_repo_tree`/`read_file`/`search_code`).
  Stage 1 완료, AWS Lambda 실배포 완료 — `mcp.devoks.kr`에 라이브.
- **`servers/slackbot`** — Starlette + uvicorn, AWS Lambda Web Adapter 위에서 handler/worker
  Lambda 2개로 분리 실행. boto3(DynamoDB, idempotency), `anthropic`(Claude API MCP 커넥터,
  worker 전용). Slack Events API ↔ Claude API 브리지. Stage 3, **진행 중**
  (issue [#4](https://github.com/ridsync/devoks-mcp-servers/issues/4), 현재 브랜치
  `feat/4-slackbot-integration`).
- **Lint/Format:** Ruff (`E,F,I,UP,B,SIM,ASYNC`, line-length 100, double quote).
- **Type check:** pyright, `typeCheckingMode = "strict"`.
- **Test:** pytest + pytest-asyncio(`asyncio_mode = "auto"`). `servers/slackbot`은 추가로
  `moto[dynamodb]` + `boto3-stubs`(dev-only).
- **Infra/배포:** AWS Lambda + ECR + GitHub OIDC + SSM Parameter Store. IaC 도구(Terraform/CDK)
  없이 `infra/*.sh` 스크립트로 순번 관리(`01`~`05`).
- **저장소 공개 범위:** GitHub public repo로 유지하기로 확정(오남용 방지 대책 4중 적용 후).

---

## Commands

전부 **저장소 루트**에서 실행(설정이 루트 `pyproject.toml`에 있는 SSOT라 서버별로 옮겨 다닐
필요 없음).

```bash
uv sync                    # 의존성 설치 (workspace 전체)
uv run pytest -q           # 테스트
uv run ruff check .        # lint
uv run ruff format .       # format
uv run pyright             # type check
```

**management 로컬 기동:**

```bash
cp .env.example .env       # 최초 1회 — GITHUB_APP_PRIVATE_KEY 등 채워야 함
uv run --env-file .env uvicorn devoks_mcp_management.app:create_app_from_env --factory \
  --host 127.0.0.1 --port 8000 --app-dir servers/management/src

curl http://127.0.0.1:8000/healthz   # {"name":"devoks-management-mcp","version":"0.1.0"}
```

**컨테이너 빌드(management):**

```bash
docker buildx build --platform linux/arm64 -f servers/management/Dockerfile -t <tag> .
```

- 빌드 컨텍스트는 반드시 저장소 루트(`.`) — `uv.lock`이 루트에 1개뿐이라 서버 디렉토리를
  컨텍스트로 주면 `uv sync`부터 실패.
- 로컬 Docker 없어도 개발 지장 없음 — 이미지 빌드·기동 검증은 `.github/workflows/ci.yml`의
  `docker` 잡(네이티브 arm64 러너)이 전담.

**배포 인프라(순서대로, `infra/`):** `01-ecr-and-github-oidc.sh` → `02-secrets.sh`(SSM 등록)
→ `03-lambda.sh`(Lambda 생성 + SSM→env 주입) → `04-custom-domain.sh` → `05-abuse-protection.sh`.

---

## Architecture

- **management:** `AgentClient → Management MCP → Knowledge/Runtime/Business` 3계층 어댑터.
  Stage 1은 Knowledge 계층의 GitHub 어댑터 하나만 구현. Auth(Bearer 토큰 검증)/RBAC(role×tool×
  repo 인가)/Audit(감사 로그) 골격은 계층이 늘어나도 바뀌지 않도록 고정돼 있음.
  - 프로토콜 레그 2개 공존: `MCP-Protocol-Version: 2026-07-28`(세션리스) vs `2025-11-25` 이하
    (세션 발급 필수, sticky session 또는 `stateless_http=True` 필요). 현재는 태스크 1개 유지로
    미룬 상태(FRD §7·§10).
- **slackbot:** Slack Events API → **handler Lambda**(서명 검증 → self-message 체크 →
  idempotency claim → worker 비동기 invoke) → **worker Lambda**(Claude API MCP 커넥터 호출 →
  Slack 응답 → 완료 기록). 이미지 1개, ASGI 팩토리 2개(`handler:create_app` /
  `worker:create_app`)로 Lambda 2개를 분기 — 이미지를 둘로 나누지 않음(FRD §4.4).
  `anthropic` SDK는 worker 전용 import(handler 콜드스타트 예산 보호, `DSN-SB-008`).
- **진행 상태:** Stage 1(management) 완료 · Stage 2(AWS Lambda 배포) 완료(`mcp.devoks.kr` 라이브,
  오남용 방지 4중 적용 완료) · Stage 3(slackbot) 진행 중.
- **SSOT 문서:**
  - `README.md` — 빠른 시작·환경변수·실행 가이드(Stage 1 기준)
  - `docs/WORKFLOW.md` — 전체 작업 흐름(왜 이 순서, 무엇이 실측으로 뒤집혔는지)
  - `.claude/workspace/management-mcp-bootstrap-20260903/{FRD,PLAN}.md` — Stage 1/2 요구사항·
    설계(DSN)·계약(CTR)·엣지케이스(EDGE)·태스크(TASK), ID 접두 없음
  - `.claude/workspace/slackbot-integration-20260914/{FRD,PLAN}.md` — Stage 3, ID 접두 `-SB-`
    (`CTR-SB-004` 등). 접두 없는 `CTR-002`/`EDGE-016` 같은 참조는 Stage 1 FRD를 가리킴.
  - `.claude/rules/project-convention.md` — 코딩 규범(패턴·네이밍·comment rule) SSOT

---

## Sensitive Files

- `.env`, `.env.*`(백업 포함) — `.gitignore`로 전부 무시, `.env.example`만 추적 대상.
  - 근접 사고 이력(`TASK-046`): `.env.bak.*` 형태 백업 파일이 예전 gitignore 패턴에서
    빠져 있어 GitHub App private key가 커밋될 뻔함 → `.env.*` 전체 무시 + `!.env.example`
    예외 패턴으로 수정 완료.
- **필수 시크릿 값(코드/설정에 하드코딩 금지):** `GITHUB_APP_PRIVATE_KEY`, `MCP_CLIENT_TOKENS`
  (Bearer 토큰 테이블), `ANTHROPIC_API_KEY`, Slack signing secret.
- **배포 시 주입 경로:** AWS SSM Parameter Store(SecureString, KMS `alias/aws/ssm`)에 원본
  기록 → `infra/03-lambda.sh`가 읽어 Lambda `--environment`로 주입(Lambda가 저장 시 KMS
  암호화). Secrets Manager는 쓰지 않음(`infra/02-secrets.sh` 근거 참고).
- **저장소가 public이므로** 커밋 메시지·PR 본문·이슈에도 실제 엔드포인트/토큰/계정 식별자를
  남기지 않도록 주의(과거 Function URL이 커밋 메시지에 노출된 근접 사고 있었음).
