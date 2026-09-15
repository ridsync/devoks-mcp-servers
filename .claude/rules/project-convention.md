# Project Convention — devoks-mcp-servers (Custom)

이 문서는 **Python FastAPI preset을 출발점**으로 삼되, 이 저장소의 실제 스택(FastAPI/SQLAlchemy
미사용, `mcp` SDK + Starlette + AWS Lambda)에 맞춰 다시 쓴 **Custom convention**입니다.
FastAPI/Pydantic/SQLAlchemy 관련 항목은 실제 코드가 없으므로 전부 제거했고, 대신 코드베이스에서
실측한 패턴(fail-fast Settings, composition-root factory, adapter registry, FRD 추적 ID)으로
채웠습니다.

---

## Stack

- **Language:** Python 3.14 이상 — 선호가 아니라 하한. GitHub 어댑터가 PEP 758(괄호 없는 다중
  예외 `except A, B, C:`) 문법을 쓰므로 3.13 이하에서는 import 시점에 `SyntaxError`.
- **Package/Dependency Manager:** uv workspace 모노레포 (`[tool.uv.workspace] members = ["servers/*"]`,
  락파일 `uv.lock`은 루트에 1개). `pytest`/`ruff`/`pyright` 설정도 전부 루트 `pyproject.toml`이
  SSOT — 멤버 패키지에 중복 선언하지 않는다.
- **Runtime/Protocol:**
  - `servers/management` — `mcp` SDK(`mcp==2.1.1`) 기반 MCP 서버. Starlette로 `/healthz` +
    `Mount("/", mcp.streamable_http_app())` 구성.
  - `servers/slackbot` — Starlette ASGI + uvicorn, AWS Lambda Web Adapter 뒤에서 handler/worker
    두 Lambda로 분리 실행(Slack Events API → Claude API MCP 커넥터 브리지).
- **ORM/DB:** 없음. `servers/slackbot`만 idempotency/in-flight 락 용도로 DynamoDB(조건부 쓰기)를
  boto3로 직접 사용 — SQLAlchemy 등 ORM 도입 안 함.
- **Settings:** `dataclasses`(frozen, slots) + 직접 작성한 fail-fast 파서. pydantic-settings 미사용.
- **ASGI Server:** uvicorn. 배포는 컨테이너 이미지 + AWS Lambda(Function URL/Lambda Web Adapter).
- **Lint/Format:** Ruff(`E,F,I,UP,B,SIM,ASYNC`, line-length 100, double quote).
- **Type Check:** pyright, `typeCheckingMode = "strict"`.
- **Test:** pytest + pytest-asyncio(`asyncio_mode = "auto"`), `servers/slackbot`은 추가로
  `moto[dynamodb]`(DynamoDB 목킹) + `boto3-stubs`(타입 스텁, dev-only).
- **AWS 배포:** ECR + GitHub OIDC, Lambda, 커스텀 도메인, 오남용 방지 — 전부 `infra/*.sh` 스크립트로
  관리(Terraform/CDK 아님).

---

## Coding Style

### Naming

| 대상 | 규칙 | 예시 |
|------|------|------|
| 서버 패키지 디렉토리 | `servers/<server_name>/src/<distribution_name>/` | `servers/management/src/devoks_mcp_management/` |
| 계층/책임별 디렉토리 | snake_case, 단일 책임 | `adapters/`, `auth/`, `audit/`, `tools/`, `slack/` |
| 어댑터 트리 | `adapters/<layer>/<source>/` (Knowledge/Runtime/Business 3계층) | `adapters/knowledge/github/` |
| Settings 데이터클래스 | PascalCase, 역할 접미사(여러 엔트리포인트가 있으면 역할별로 분리) | `Settings`, `HandlerSettings`, `WorkerSettings` |
| 팩토리 함수 | `create_` 접두 — 모듈 스코프에 인스턴스를 두지 않는다 | `create_app`, `create_server`, `create_app_from_env` |
| 환경변수 파서 진입점 | `load_` 접두, 유일한 entry point | `load_settings`, `load_handler_settings` |
| 상수(허용 범위/기본값) | UPPER_SNAKE_CASE, `_DEFAULT_*`/`_MIN_*`/`_MAX_*` 접두로 범위 명시 | `_DEFAULT_PORT`, `_MIN_CLIENT_TOKEN_LENGTH` |
| 테스트 파일 | `test_<module>.py`, src 모듈과 1:1 대응(도메인 하위 디렉토리 없이 flat) | `test_config.py`, `test_registry.py` |

- 이름은 **도메인 + 맥락 + 의도**를 담아 3단어 이상 권장. 제네릭 이름 회피.

### Code Size

- **Functions:** ≤50 lines (target), **100 lines max** (hard limit).
- **Files:** ≤500 lines (target), **1000 lines max** (hard limit) — 단, `config.py`류의 fail-fast
  파서처럼 검증 항목이 실제로 많아 자연히 길어지는 파일은 예외로 인정하되 함수 단위 분리는 유지한다.

### Package Structure (책임/계층 우선, 도메인 아님)

```
servers/
  management/
    src/devoks_mcp_management/
      config.py          # Settings — 환경변수 SSOT, fail-fast 파서
      app.py              # Starlette 앱 composition root (factory)
      server.py            # MCPServer composition root (factory)
      types.py               # 공유 상수/타입
      auth/
        verifier.py           # TokenVerifier 구현체 (SDK Protocol 준수)
        policy.py               # role -> 허용 tool 인가 판정
      audit/
        logger.py                # 감사 로그 직렬화/emit (순수 함수, SDK 비의존)
      tools/
        registry.py                # 어댑터 registrar 수집점 (단일 진입점)
        guard.py                     # tool 함수 wrap: 인가 + 감사 + 에러 정규화
      adapters/knowledge/github/
        client.py                      # 외부 API 클라이언트
        credentials.py                   # 토큰 발급/캐싱
        tools.py                           # register(mcp, guard) -> None
    tests/
      test_config.py, test_app.py, test_registry.py, ...  # src와 1:1, flat
  slackbot/
    src/devoks_slackbot/
      config.py            # load_handler_settings / load_worker_settings (역할별 분리)
      handler.py             # Lambda handler composition root (factory)
      worker.py                # Lambda worker composition root (factory)
      idempotency.py             # DynamoDB 조건부 쓰기 기반 중복 방지
      identity.py                  # Slack user <-> 내부 identity 매핑
      observability.py               # 구조화 로깅
      ask.py                          # Claude API MCP 커넥터 호출
      slack/
        signature.py                    # HMAC 서명 검증 (body 파싱 전에 실행)
        events.py                         # 이벤트 타입 판별(봇 자기 메시지 등)
        client.py, format.py
    tests/
      test_config.py, test_idempotency.py, ...
```

- **계층(책임) 우선, 도메인 우선 아님** — FastAPI preset과 반대 방향. 이 저장소는 도메인이 아니라
  "인증/감사/툴 등록/어댑터"라는 책임 경계로 최상위를 나눈다. 새 지식 소스(Notion, Sentry 등)를
  추가할 때도 `adapters/<layer>/<source>/`에 디렉토리 하나 + `tools/registry.py`에 한 줄만
  추가하면 되도록 설계돼 있다 — 이 계약을 깨지 않는다.
- `servers/management`와 `servers/slackbot`은 **의도적으로 별개의 패키지**이며 공용 config/유틸
  패키지로 묶지 않는다(FRD §4.4 — 배포 독립성을 결합보다 우선). 두 서버 사이에 비슷한 코드가
  보여도 곧바로 공유 패키지로 추출하지 말 것.

### Import Order

- 표준 라이브러리 → 서드파티 → 로컬(`devoks_mcp_management.*` / `devoks_slackbot.*`) 순.
- 정렬은 Ruff(`I` — isort 규칙)가 자동 수행 — 수동 정렬 불필요.

---

## Core Rules

- **팩토리, 모듈 레벨 싱글턴 금지.** `create_app`/`create_server`류 팩토리만 앱·서버 인스턴스를
  만든다. 모듈을 import하는 것만으로 환경변수를 읽거나 예외를 던지는 일이 있어서는 안 된다 —
  실제로 환경을 읽는 건 `create_app_from_env()`처럼 명시적으로 호출되는 함수뿐이다.
- **Fail-Fast, 단 방향은 필드마다 다르다.** 대부분의 필수 키는 없으면 기동 자체가 실패해야
  한다(`MCP_ALLOWED_HOSTS` 등). 반대로 `MCP_REPO_ALLOWLIST`처럼 "없으면 전체 거부"가 안전한
  기본값인 필드는 **없어도 기동은 성공**하고 대신 전부 막는 fail-safe 방향을 택한다 — 새 설정
  필드를 추가할 때 이 두 방향 중 무엇이 맞는지 먼저 판단할 것.
- **모든 검증 오류는 한 번에 모아서 던진다.** 첫 오류에서 바로 raise하지 않고 `errors: list[str]`에
  누적한 뒤 `ConfigError("; ".join(errors))`로 한 번에 던진다 — 배포자가 수정-재기동을 N번이
  아니라 1번만 거치게 하기 위함.
- **비밀은 절대 `repr`/로그에 노출하지 않는다.** 토큰 테이블·PEM 등은 `field(repr=False)`로
  선언하고, 파싱 실패 메시지에도 원문 대신 길이/타입만 남긴다.
- **서명/인증 검증은 body를 파싱하기 전에, raw bytes로 수행한다.** JSON으로 파싱·재직렬화하면
  key 순서/공백이 바뀌어 서명 검증이 깨진다(Slack HMAC 등). "검증 → 파싱" 순서를 바꾸지 않는다.
- **여러 엔트리포인트(Lambda 역할 등)가 서로 다른 필수 키를 가지면 Settings 타입 자체를
  분리한다** (`HandlerSettings`/`WorkerSettings`). 하나의 `Settings`에 옵셔널 필드로 합쳐서
  런타임에 `None` 체크로 구분하지 않는다 — 잘못된 역할의 필드 접근이 타입 에러로 즉시 드러나야
  한다.
- **중복 처리(idempotency)는 "확인 후 쓰기"가 아니라 원자적 조건부 연산으로.** DynamoDB
  `ConditionExpression = attribute_not_exists(pk)` 같은 단일 원자적 쓰기로 경쟁 조건을 막는다 —
  check-then-write 2단계 방식은 동시 재시도에서 둘 다 "없음"을 관측할 수 있어 금지.
- **처리 순서가 계약(FRD)에 명시된 곳은 주석에 "reorder 금지"를 남기고 실제로 재배치하지
  않는다** (예: 서명 검증 → self-message 체크 → idempotency 체크 순서).

---

## Design Pattern

### Settings (Fail-Fast dataclass)

```python
@dataclass(frozen=True, slots=True)
class Settings:
    allowed_hosts: tuple[str, ...]
    ...
    client_tokens: Mapping[str, ClientToken] = field(repr=False)
    github_app_private_key: str = field(repr=False)


def load_settings(env: Mapping[str, str]) -> Settings:
    errors: list[str] = []
    # ... 각 필드 파싱, 실패 시 errors.append(...)
    if errors:
        raise ConfigError("; ".join(errors))
    return Settings(...)
```

- `load_settings(env)`처럼 **환경변수 매핑을 명시적 인자로 받는다** — 함수 내부에서
  `os.environ`을 직접 읽지 않는다. 테스트가 실제 프로세스 환경을 건드리지 않고 가짜 env를
  주입할 수 있어야 한다.

### Composition Root (Factory, ASGI app)

```python
def create_app(settings: Settings) -> Starlette:
    ...
    return Starlette(routes=[...], lifespan=lifespan)


def create_app_from_env(env: Mapping[str, str] | None = None) -> Starlette:
    return create_app(load_settings(env if env is not None else os.environ))
```

- uvicorn/Dockerfile `CMD`가 직접 부르는 건 `create_app_from_env`(또는 인자 없는
  `create_app`) 하나뿐이다. 모듈 최상단에 `app = create_app(...)` 같은 즉시 평가 인스턴스를
  두지 않는다.

### Adapter Registry (단일 수집점)

```python
_ADAPTER_REGISTRARS: tuple[Registrar, ...] = (register_github_tools,)


def register_tools(mcp: MCPServer, guard: Guard) -> None:
    for register in _ADAPTER_REGISTRARS:
        register(mcp, guard)
```

- 새 지식/런타임/비즈니스 소스를 추가하는 비용은 "디렉토리 1개 + 이 튜플에 한 줄"로 고정한다.
  다른 모듈(`server.py` 등)은 어떤 어댑터가 있는지 몰라도 된다.

### Guard (인가 + 감사 wrapping)

- 각 tool 함수는 `guard(...)`로 감싸 호출 시점에 role 기반 인가 판정(`auth/policy.py`)과 감사
  로그 emit(`audit/logger.py`)을 강제한다. tool 함수 본문에 인가/감사 코드를 직접 넣지 않는다.

### Role-scoped Settings (다중 엔트리포인트)

```python
def load_handler_settings(env: Mapping[str, str]) -> HandlerSettings: ...
def load_worker_settings(env: Mapping[str, str]) -> WorkerSettings: ...
```

- 하나의 이미지가 여러 Lambda 역할로 실행될 때, 역할별 필수 키가 다르면 반드시 위처럼
  분리한다(Core Rules 참고).

---

## Data & Platform Integration

### DynamoDB (idempotency 전용, ORM 없음)

- boto3 직접 사용, 조건부 `PutItem`(`ConditionExpression = attribute_not_exists(pk)`)으로
  원자적 claim. `moto[dynamodb]`로 실제 조건부 쓰기 시맨틱(`ConditionalCheckFailedException`
  포함)을 테스트한다 — 손으로 만든 스텁으로 대체하지 않는다.

### Settings / 환경변수

- 환경변수 SSOT는 서버별 `config.py`의 `load_settings`/`load_*_settings` 하나다 — 코드
  곳곳에서 `os.environ`/`os.getenv`를 직접 호출하지 않는다.
- **비밀 저장:** AWS SSM Parameter Store(SecureString, KMS `alias/aws/ssm`)에 원본을 기록하고,
  `infra/03-lambda.sh`가 이를 읽어 Lambda `--environment`로 주입한다(Lambda는 환경변수를
  저장 시 KMS로 암호화). Secrets Manager는 쓰지 않는다(자동 로테이션이 이 프로젝트의 시크릿
  성격과 맞지 않고 비용만 더 든다 — `infra/02-secrets.sh` 참고). 앱 코드가 직접 SSM을 읽는
  방식(런타임 조회)은 콜드스타트 비용과 env 주입 계약 변경 문제로 채택하지 않았다.

### Background/Async 처리

- Celery/RQ 등 메시지 큐 미사용. Lambda **async invoke**(handler → worker)로 비동기 처리를
  구현하고, Lambda 자체의 재시도(최대 2회 추가)는 idempotency 스토어로 흡수한다.

---

## Performance

- **Lambda 콜드스타트 예산을 명시적으로 관리한다.** 무거운 의존성(`anthropic` 등)은 import
  비용을 실측(ms 단위)하고, 그 의존성이 필요 없는 엔트리포인트(handler)에서는 절대 import하지
  않는다 — worker 전용 임포트가 handler 콜드스타트 예산을 잠식하지 않도록 분리한다.
- **외부 HTTP 클라이언트 타임아웃은 fronting 레이어(API Gateway 등)의 타임아웃보다 반드시
  짧게 잡는다.** 그래야 이 서버가 먼저 타임아웃하고 자체 에러 정규화/감사 기록을 남길 수 있다 —
  같거나 더 길게 두면 상위 레이어가 먼저 잘라 에러 컨텍스트가 소실된다.
- 공유 가능한 리소스(HTTP 클라이언트 등)는 lifespan에서 **1회 생성**해 재사용한다 — 요청/툴
  호출마다 새로 만들지 않는다(`AsyncExitStack`으로 역순 정리).

---

## Testing

- **Framework:** pytest + pytest-asyncio(`asyncio_mode = "auto"`, 루트 `pyproject.toml`에만
  선언 — 멤버 패키지에 중복 금지).
- **위치:** `servers/<name>/tests/test_<module>.py` — src 모듈과 1:1, 도메인별 하위 디렉토리
  없이 flat.
- 외부 API(GitHub, Slack, Claude)는 실제 네트워크 호출 없이 클라이언트 목/스텁으로 대체한다.
- AWS 의존(DynamoDB 등)은 `moto`로 실제에 가까운 조건부 연산 시맨틱을 검증한다 — 손으로 만든
  가짜 스토어로 대체하지 않는다.
- 실행은 항상 저장소 루트에서: `uv run pytest -q` / `uv run ruff check .` / `uv run ruff format .`
  / `uv run pyright`. 작업 디렉터리를 바꾸거나 서버별로 따로 실행할 필요가 없다(루트
  `pyproject.toml`이 testpaths 등 전체 설정의 SSOT).

---

## Security & Reliability

### Error Handling

| 상황 | 처리 |
|------|------|
| 환경변수 검증 실패 | `ConfigError` — 기동 자체를 막는다(Fail-Fast), 전체 오류를 모아서 |
| 인가 실패(role에 tool 미허용) | Guard가 감사 로그(`denied`)와 함께 차단 — tool 함수까지 도달 안 함 |
| 외부 API/네트워크 오류 | 클라이언트 계층에서 잡아 정규화된 에러로 변환 + 감사 로그(`error`) |
| Slack 서명 검증 실패 | 401, body 파싱 전에 즉시 반환 |
| DynamoDB 조건부 쓰기 충돌 | `ConditionalCheckFailedException`을 "이미 처리 중/완료"로 해석 — 예외를 삼키지 않고 명시적으로 분기 |

**금지:**
- 빈 except 블록(silent catch)
- 예외를 삼키고 기본값으로 대체(implicit fallback) — 특히 fail-safe가 아닌 필드에서
- 광범위한 `except Exception: pass`

### Auth

- `servers/management`: Bearer 토큰 → SDK `TokenVerifier` Protocol 구현체(`StaticTableTokenVerifier`)로
  검증. role은 `AccessToken.claims`의 확장 필드에 싣는다(서브클래싱 금지 — SDK가 원본 객체를
  그대로 왕복시키는지 실제로 확인된 경로만 신뢰).
- `servers/slackbot`: Slack HMAC 서명 검증(raw bytes, 파싱 전) — Core Rules 참고.
- 인가(role → 허용 tool)는 Guard 계층에서만 판정한다. tool 함수 본문 안에서 role을 직접
  비교하지 않는다.

### Security

- Secret은 `.env`(로컬) / SSM Parameter Store(배포)로만 주입한다 — 코드나 설정 파일에
  하드코딩하지 않는다. `.env`/`.env.*`는 `.gitignore`로 막혀 있고 `.env.example`만 추적 대상
  — 이 패턴을 깨지 않는다(과거 `.env.bak.*` 파일이 무시 대상에서 빠져 실제 GitHub App
  private key가 커밋될 뻔한 near-miss가 있었다).
- placeholder 값은 **일부러 유효성 검증에 실패하도록** 만든다(`GITHUB_APP_PRIVATE_KEY`
  placeholder는 파싱 불가능한 PEM, 토큰 placeholder는 최소 길이 미달) — "그대로 복사해도
  일단 동작하는" 상태를 만들지 않는다. 새 시크릿 필드를 추가할 때 이 원칙을 따른다.
- 토큰 등 민감값은 에러 메시지에도 원문을 남기지 않는다(길이/타입만).

---

## Comment Rules

- 기본은 **주석 없음** — 이름·타입으로 의도가 드러나면 주석을 추가하지 않는다.
- 이 저장소는 preset 기본값보다 **문서화 밀도가 높다**: FRD/PLAN에 근거를 둔 비자명한 설계
  결정은 반드시 추적 ID를 남긴다 — `REQ-*`(요구사항), `AC-*`(수용 기준), `CTR-*`(계약),
  `DSN-*`(설계 결정), `EDGE-*`(엣지 케이스), `TASK-*`(작업 ID). 서버별 접두는
  `servers/slackbot`이 `-SB-`를 붙인다(`DSN-SB-004` 등). 원본은
  `.claude/workspace/*/FRD.md` / `PLAN.md`.
- **"실측했다"는 주장은 실제로 실측한 것만 쓴다.** "installed 패키지 소스를 읽어 확인",
  "실제로 curl로 검증", "import 비용 384ms 측정"처럼 **검증 방법을 명시**하고, 추정/가정은
  별도로 "검증되지 않음"이라고 밝힌다 — 이 저장소 전반에서 일관되게 지켜지는 스타일이다.
- Workaround/Hack, Business Rule(코드로 유추 불가능한 도메인 규칙), Non-obvious Trade-off는
  일반 preset과 동일하게 "왜"만 남긴다.

---

## Pitfalls

| 실수 | 결과 | 방지 |
|------|------|------|
| `Mount("/", ...)` 뒤에 다른 라우트를 나열 | 루트 Mount가 모든 경로를 먼저 매칭 — 이후 라우트가 죽은 코드가 됨 | 항상 구체적 라우트(`/healthz`)를 `Mount("/")`보다 먼저 나열 |
| `MCP_PUBLIC_URL`에 trailing slash 또는 `/mcp`로 안 끝남 | 서버는 **정상 기동**하지만 well-known discovery 경로가 조용히 어긋남 | 기동 시점 형식 검증(현재 구현된 대로 유지) |
| 두 Lambda 역할(handler/worker) 설정을 하나의 `Settings`로 합침 | 잘못된 역할의 필드에 접근해도 런타임까지 안 걸림 | 역할별 `HandlerSettings`/`WorkerSettings` 분리 유지 |
| check-then-write로 idempotency 구현 | 동시 재시도 둘 다 "없음"을 관측 → 중복 처리 | DynamoDB 조건부 쓰기(단일 원자적 연산) |
| `.env.bak.*` 같은 백업 파일을 `.gitignore`가 못 잡음 | `git status`에 `??`로만 보이고 `git add .`에 그대로 딸려 들어가 시크릿 커밋 위험 | `.env.*` 패턴으로 광범위 무시 + `!.env.example`만 예외 |
| placeholder 값이 우연히 유효한 형식 | "그대로 복사"만 해도 동작하는 취약한 기본값이 배포됨 | placeholder는 의도적으로 검증에 실패하는 값으로 작성 |
| 서명 검증 전에 body를 JSON 파싱 | key 순서/공백 변경으로 서명 검증 우회 가능 | raw bytes로 먼저 검증, 파싱은 그 다음 |
| 두 서버(`management`/`slackbot`) 사이 비슷한 코드를 성급히 공유 패키지로 추출 | 배포 독립성 결합, 한쪽 변경이 다른 쪽 배포에 영향 | FRD §4.4 결정 유지 — 의도적 중복 허용 |

---

## Project Decisions

- **Architecture SSOT:** `README.md` + `.claude/workspace/*/FRD.md`/`PLAN.md` (Stage 1 =
  management GitHub 어댑터, Stage 2 = Lambda 배포, Stage 3 = Slackbot 연동).
- **DB SSOT:** 없음(관계형 DB/ORM 미사용). DynamoDB는 `servers/slackbot`의 idempotency
  전용 — boto3 직접 사용, 스키마 마이그레이션 개념 없음(단일 테이블, 조건부 쓰기).
- **Migration 정책:** 해당 없음. 인프라 변경은 `infra/*.sh` 스크립트(순번 접두 `01-`~`05-`)로
  관리하고 각 스크립트 상단에 실측 검증 상태를 기록한다.
- **Auth 전략:** `management`는 Bearer 토큰(SDK `TokenVerifier` Protocol, Stage 2에서 IdP
  introspection으로 교체 예정 — FRD §10), `slackbot`은 Slack HMAC 서명.
- **비동기 워커:** Celery/RQ 등 미사용 — Lambda async invoke(handler → worker) + DynamoDB
  idempotency로 대체.
- **Sensitive Files:** `.env`, `.env.*`(백업 포함), `GITHUB_APP_PRIVATE_KEY`,
  `ANTHROPIC_API_KEY`, Slack signing secret, `MCP_CLIENT_TOKENS` — 전부 SSM Parameter Store
  (SecureString)로 배포 주입, 로컬은 `.env`(gitignore 대상)만 사용. `.env.example`만 추적.
