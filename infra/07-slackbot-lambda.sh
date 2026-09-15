#!/usr/bin/env bash
# TASK-023 — Lambda 2개 프로비저닝 (slack-handler / slack-worker)
# (FRD-SB §4.4 진입점 분기 · §5.2 환경 키 · DSN-SB-001 · CTR-SB-009 ·
#  EDGE-SB-012/013/016/018 · EDGE-021 Lambda 환경변수 4 KB 총량)
#
# ============================================================================
# 🔴 미실행. infra/06-idempotency-table.sh와 같은 사유(`aws` 호출 금지 지시)로
#    문법 검사(`bash -n`)만 했다. 메인 루프가 사용자 승인을 받은 뒤 처음
#    실행하는 시점이 이 조립부의 최초 검증이다.
#
# 전제: infra/01-ecr-and-github-oidc.sh(ECR `devoks-slackbot`),
#       infra/06-idempotency-table.sh(DynamoDB `devoks-slack-idempotency` +
#       IAM 정책 문서 `infra/06-idempotency-table-access-policy.json`).
#
# ----------------------------------------------------------------------------
# 진입점 분기: 이미지 1개, `ImageConfig.Command`로 팩토리만 교체
# ----------------------------------------------------------------------------
# `servers/slackbot/Dockerfile`의 기본 CMD(2026-09-15 grep으로 확인, 추측 아님)
# = handler 팩토리. worker는 같은 배열에서 모듈 경로 한 조각만 바꾼
# `ImageConfig.Command`로 분기한다(Dockerfile에 `ENTRYPOINT`가 없으므로 `CMD`
# 전체가 곧 실행 프로세스다 — Lambda의 `--image-config`도 같은 규칙을 따라
# `Command`만 넘기면 EntryPoint는 비워진 채(이미지 기본값과 동일하게) 유지된다.
# 두 배열 다 이 이유로 인자 하나만 다르고 나머지 6개 토큰은 완전히 같다).
# handler에도 (기본값과 값이 같더라도) 명시적으로 지정한다 — Dockerfile의
# 기본 CMD가 나중에 바뀌어도 handler가 항상 `handler:create_app`을 가리키도록
# 암묵적 의존을 없애기 위해서다.
#
# ----------------------------------------------------------------------------
# 🔴 생성 순서: worker 먼저, handler 나중 (닭과 달걀)
# ----------------------------------------------------------------------------
# handler는 `WORKER_FUNCTION_NAME`을 필수 환경변수로 요구한다(없으면 기동 시
# `ConfigError`, config.py `_HANDLER_REQUIRED_KEYS`). Lambda ARN은
# 계정/리전/이름으로 결정되므로 그 **문자열 자체**는 worker를 실제로 만들기
# 전에도 알 수 있다 — 그럼에도 함수 생성은 worker를 먼저 끝낸다: handler가
# 배포돼 트래픽을 받을 수 있게 되는 순간에 handler의 invoke 대상(worker)이
# 실제로 존재하지 않으면 `ResourceNotFoundException`으로 매 이벤트가 실패한다.
# handler의 IAM(6번)도 worker ARN을 참조하므로 같은 순서를 따른다.
#
# ----------------------------------------------------------------------------
# IAM: 관리형 정책 대신 최소권한 인라인 정책 (infra/03-lambda.sh 패턴 재사용)
# ----------------------------------------------------------------------------
# `AWSLambdaBasicExecutionRole`(관리형, `logs:*` on `Resource:"*"`)을 쓰지
# 않는다. 자기 로그그룹 쓰기 + 자기 ECR 리포(`devoks-slackbot`) pull로 한정한
# 인라인 정책을 쓴다 — Sid 구조까지 `infra/03-lambda.sh`를 그대로 따른다.
#
# 🔴 PLAN.md TASK-023 원문은 "worker는 DynamoDB+SSM"이라고 적었지만, 이는
# `infra/03-lambda.sh`가 이미 확립한 패턴과 어긋난다: 시크릿은 이 스크립트가
# **배포 시점에** SSM에서 읽어 Lambda 환경변수로 주입하므로(아래 3번),
# **런타임(worker 컨테이너 자신)은 SSM을 전혀 호출하지 않는다** —
# `servers/slackbot/src/devoks_slackbot/worker.py`가 실제로 import하는 boto3
# 클라이언트도 `dynamodb`/`lambda`뿐이고 `ssm`은 어디에도 없다(코드로 확인).
# 실행 역할에 SSM 권한을 주는 것은 실제로 쓰이지 않는 권한을 부여하는
# 최소권한 원칙 위반이다. 그래서 handler·worker 둘 다 SSM 권한을 받지
# **않는다** — worker가 추가로 받는 것은 DynamoDB 4액션뿐이다.
# (참고: SSM `get-parameter`를 실제로 호출하는 것은 이 스크립트를 실행하는
# 오퍼레이터 자신의 AWS 자격증명이지, Lambda 실행 역할이 아니다.)
#
# ----------------------------------------------------------------------------
# 실행 한도 — 둘 다 초기값, TASK-033이 실측 후 조정(FRD §10 미결 1)
# ----------------------------------------------------------------------------
# worker: 300s / 1024MB (`CTR-SB-009`). Claude API 추론 + MCP 툴 다회 호출 +
#   MCP 서버 콜드스타트(`EDGE-SB-016`: 정상 ~1,900 ms, 새 이미지 직후
#   ~8,511 ms, Stage 1 실측)의 합을 덮어야 한다는 근거는 PLAN §1/FRD가 이미
#   확정한 값을 그대로 옮긴 것이다.
# handler: 10s / 512MB. `CTR-SB-002`(3초)보다 크게 잡는 이유 — Lambda
#   타임아웃이 SLA와 같으면 콜드스타트 중에 함수가 **강제 종료**되어 아예
#   응답을 못 보낸다(3초를 살짝 넘겨서라도 응답하는 것보다 나쁘다). handler는
#   `anthropic`/`mcp`를 import하지 않으므로(`DSN-SB-008`) 콜드스타트가
#   management(~1,900 ms, 그 안에 `mcp` 522 ms 포함)보다 가벼울 것으로
#   추정되지만(boto3 385+82 ms로 `mcp` 522 ms를 대체하는 셈이라 비슷한 값이
#   나올 수도 있다), **같은 이미지 계열(LWA 확장 · python:3.14-slim-trixie ·
#   arm64)의 첫 콜드스타트가 관리형 함수에서 8,511 ms까지 뛴 전례**가 있어
#   그 정도 스파이크를 살아남을 여유를 둔다. API Gateway 통합 타임아웃 하드
#   30초(`EDGE-018`)보다 한참 작게 둬 진짜 행(hang)이면 Lambda 자신의
#   타임아웃이 먼저 잡혀 CloudWatch에 원인이 남는다(API GW의 불투명한 504가
#   먼저 뜨지 않는다) — Stage 1이 GitHub HTTP 타임아웃을 20초로 낮춘 것과
#   같은 이유(`EDGE-018` 코멘트). 메모리는 management의 실측(512MB에서
#   Init Duration 1,923 ms, 메모리를 올려도 개선 없음, Max Memory Used
#   116 MB)을 그대로 전이한다 — handler의 import 그래프는 management보다
#   가벼우므로(같은 아키텍처에서 무거운 쪽이 512로 충분했다면) 512로
#   충분할 것이라는 추정이며, 직접 측정한 값은 아니다.
# 둘 다 arm64(`CTR-010`).
#
# ----------------------------------------------------------------------------
# 환경 키 — FRD §5.2가 스펙, 역할별로 다르다
# ----------------------------------------------------------------------------
# 공통: SLACK_SIGNING_SECRET · SLACK_BOT_TOKEN · SLACK_BOT_USER_ID ·
#   IDEMPOTENCY_TABLE. handler 전용: WORKER_FUNCTION_NAME(ARN이 아니라 함수
#   **이름**만 넣는다 — boto3 Lambda `invoke`의 `FunctionName`은 이름/ARN/
#   partial ARN을 모두 받아들이고(공식 문서), 이름 쪽이 훨씬 짧아 4 KB
#   예산에 유리하다). worker 전용: ANTHROPIC_API_KEY · MCP_SERVER_URL ·
#   SLACK_USER_TOKEN_MAP. 선택 키(SLACKBOT_MAX_RESPONSE_CHARS 등 3개)는
#   **기본값과 같으면 넣지 않는다** — `infra/03-lambda.sh`가 이미 문서화한
#   이유(EDGE-021 예산 절약)를 그대로 따른다.
#
# 시크릿(4종: SLACK_SIGNING_SECRET·SLACK_BOT_TOKEN·SLACK_BOT_USER_ID·
# ANTHROPIC_API_KEY, TASK-031이 이렇게 호명함)은 SSM `SecureString`
# `/devoks-mcp/slackbot/*`(제안 — infra/02-secrets.sh의 `/devoks-mcp/management`
# 계열을 그대로 잇는다. TASK-031/034가 다른 이름을 쓰면 이 스크립트의
# 파라미터 이름을 맞춰 갱신할 것)에서 읽어 env로 주입한다. **TASK-031이
# 아직 실제 값을 등록하지 않았으므로**(PLAN §3 의존 그래프상 TASK-023이
# TASK-031보다 먼저다 — Slack 앱·Anthropic 키는 사용자만 만들 수 있는
# 착수 전 블로커라 PR3에서야 해소된다), SSM에 파라미터가 없으면 의도적으로
# 무효인 placeholder를 대신 넣는다(레포 관례 — `.claude/rules/
# project-convention.md` "placeholder 값은 의도적으로 유효성 검증에 실패하도록
# 만든다"). `SLACK_USER_TOKEN_MAP`은 TASK-031의 "4종"에 포함되지 않지만
# (등록은 TASK-034가 `infra/02-secrets.sh` 쪽에서 사람별로 늘려간다) 같은
# 종류의 자격증명(WorkerSettings가 `repr=False`로 은닉)이라 같은 SSM 경로
# 관례로 함께 다룬다 — placeholder는 `{}`(빈 매핑, 사용자 0명이라는 실제
# 상태와 정확히 같아 "의도적으로 무효"보다 "의도적으로 안전한 초기값"에
# 가깝다).
# 회전(`EDGE-SB-018`)은 SSM 값 갱신 후 `aws lambda
# update-function-configuration --environment file://<새로 조립한 env.json>`
# 재실행이며 코드 변경이 없다 — 이 스크립트를 다시 돌리면(SSM에 실값이
# 이미 들어있는 상태로) 같은 조립 로직이 실값을 담은 env.json을 만든다.
#
# ----------------------------------------------------------------------------
# 시크릿 비노출
# ----------------------------------------------------------------------------
# SSM에서 읽은 값·placeholder 값 전부 **파일로만** 오간다(bash 변수에 담지
# 않는다 — `ps`/`set -x` 노출 경로 차단, `infra/02-secrets.sh`와 같은 이유).
# 조립된 `*-env.json`은 시크릿 평문을 담으므로 `chmod 600` + 작업 디렉터리
# 전체를 `trap ... EXIT INT TERM`으로 삭제한다. 4 KB 예산 점검은 **바이트
# 수만** stdout에 낸다 — 값은 어디에도 출력하지 않는다.
#
# ----------------------------------------------------------------------------
# 만들지 않는 것 / 경계
# ----------------------------------------------------------------------------
# 🔴 Function URL을 만들지 않는다 — Stage 2에서 이미 삭제 + `Principal:"*"`
#   회수(`EDGE-022`). API Gateway(`TASK-024`)가 유일한 입구이고, handler
#   호출 권한(리소스 기반 정책)도 TASK-024가 API Gateway 쪽에서 붙인다.
# 🔴 예약 동시성(`--reserved-concurrent-executions`)을 설정하지 않는다 —
#   `TASK-024`가 API Gateway 스로틀(`rate 10/s · burst 20` 상속)과 계정 전체
#   동시성 예산을 함께 보고 결정한다. 여기서 먼저 값을 박으면 그 판단을
#   선점한다(`infra/06-idempotency-table.sh`가 IAM 역할 통합 여부 결정을
#   이 태스크로 넘긴 것과 같은 이유).
#
# 사용법:  IMAGE_TAG=<커밋 SHA> ./infra/07-slackbot-lambda.sh
#          IDEMPOTENCY_TABLE_NAME=... MCP_SERVER_URL=... (기본값 있음, 아래 참고)
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"

ECR_REPO_NAME="devoks-slackbot"
REPO_URI="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$ECR_REPO_NAME"
IMAGE_TAG="${IMAGE_TAG:?IMAGE_TAG=<커밋 SHA> 를 지정하라}"

HANDLER_FN="devoks-slack-handler"
WORKER_FN="devoks-slack-worker"
HANDLER_ROLE="$HANDLER_FN-lambda"
WORKER_ROLE="$WORKER_FN-lambda"
HANDLER_LOG_GROUP="/aws/lambda/$HANDLER_FN"
WORKER_LOG_GROUP="/aws/lambda/$WORKER_FN"
# ARN은 계정/리전/이름만으로 결정된다 — worker를 실제로 만들기 전에도 handler
# 쪽 IAM 정책·env 값 조립에 안전하게 쓸 수 있다(위 "생성 순서" 절 참고).
WORKER_ARN="arn:aws:lambda:$REGION:$ACCOUNT:function:$WORKER_FN"

IDEMPOTENCY_TABLE_NAME="${IDEMPOTENCY_TABLE_NAME:-devoks-slack-idempotency}"
IDEMPOTENCY_TABLE_ARN="arn:aws:dynamodb:$REGION:$ACCOUNT:table/$IDEMPOTENCY_TABLE_NAME"
DYNAMODB_POLICY_FILE="infra/06-idempotency-table-access-policy.json"

MCP_SERVER_URL_VALUE="${MCP_SERVER_URL:-https://mcp.devoks.kr/mcp}"

SSM_PREFIX="/devoks-mcp/slackbot"

HANDLER_TIMEOUT_SECONDS=10
# 2026-09-15 실측으로 512 → 1024 상향. 최초 콜드스타트 Init Duration 이
# 3,087 ms 로 CTR-SB-002 의 3,000 ms 예산을 넘겼다(이후 1,232 ms).
# Lambda 는 메모리에 비례해 CPU 를 주므로 init 이 줄어든다. handler 실행
# 자체는 3 ms 라 GB-초 증가가 사실상 없다 — 예산을 사는 가장 싼 방법이다.
HANDLER_MEMORY_MB=1024
WORKER_TIMEOUT_SECONDS=300
WORKER_MEMORY_MB=1024
ENV_BUDGET_LIMIT_BYTES=4096
ENV_BUDGET_WARN_PCT=80

WORK_DIR="$(mktemp -d)"
chmod 700 "$WORK_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM

say() { printf '\n== %s\n' "$1"; }

# servers/slackbot/Dockerfile의 CMD 그대로(2026-09-15 확인) — 모듈 경로 한
# 토큰만 다르다.
cat > "$WORK_DIR/handler-image-config.json" <<'JSON'
{"Command": ["/app/.venv/bin/uvicorn", "devoks_slackbot.handler:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]}
JSON
cat > "$WORK_DIR/worker-image-config.json" <<'JSON'
{"Command": ["/app/.venv/bin/uvicorn", "devoks_slackbot.worker:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]}
JSON

# --- 로그 그룹: 보존기간을 미리 박는다 (infra/03-lambda.sh와 같은 이유) ----
ensure_log_group() {
  local log_group="$1" component="$2"
  aws logs create-log-group --log-group-name "$log_group" \
    --tags Project=devoks-mcp,Component="$component" \
    --profile "$PROFILE" --region "$REGION" 2>/dev/null || true
  aws logs put-retention-policy --log-group-name "$log_group" \
    --retention-in-days 30 --profile "$PROFILE" --region "$REGION"
}

# --- 실행 역할: 최소권한, 역할별로 다른 추가 권한 ------------------------
# kind: "handler" | "worker" — handler만 InvokeWorkerFunction 문을 더 받는다.
# 둘 다 SSM/KMS는 받지 않는다(위 헤더의 PLAN 문구 이탈 근거 참고).
ensure_execution_role() {
  local role="$1" log_group="$2" component="$3" kind="$4"
  local trust="$WORK_DIR/${role}-trust.json"
  local perms="$WORK_DIR/${role}-perms.json"
  local dynamodb_source=""
  [[ -f "$DYNAMODB_POLICY_FILE" ]] && dynamodb_source="$DYNAMODB_POLICY_FILE"

  cat > "$trust" <<JSON
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
JSON

  python3 - "$perms" "$REGION" "$ACCOUNT" "$log_group" "$ECR_REPO_NAME" \
    "$kind" "$dynamodb_source" "$IDEMPOTENCY_TABLE_ARN" "$WORKER_ARN" <<'PY'
import json, pathlib, sys

(outfile, region, account, log_group, ecr_repo,
 kind, dynamodb_source, table_arn, worker_arn) = sys.argv[1:]

# infra/03-lambda.sh와 동일한 Sid 구조: 자기 로그그룹 쓰기 + 자기 ECR 리포
# pull만 허용한다. AWSLambdaBasicExecutionRole(관리형, Resource "*")은 쓰지
# 않는다.
statements = [
    {
        "Sid": "WriteOwnLogsOnly",
        "Effect": "Allow",
        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
        "Resource": f"arn:aws:logs:{region}:{account}:log-group:{log_group}:*",
    },
    {
        "Sid": "RecreateOwnLogGroupIfDeleted",
        "Effect": "Allow",
        "Action": "logs:CreateLogGroup",
        "Resource": f"arn:aws:logs:{region}:{account}:log-group:{log_group}",
    },
    {
        "Sid": "PullOwnContainerImage",
        "Effect": "Allow",
        "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
        "Resource": f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo}",
    },
]

# infra/06-idempotency-table.sh가 실행 시 만들어 두는 정책 문서를 재사용한다
# (없으면 같은 내용을 여기서 재구성 — Action 4개 + 테이블 ARN 한정은 동일).
if dynamodb_source:
    source_doc = json.loads(pathlib.Path(dynamodb_source).read_text(encoding="utf-8"))
    statements.extend(source_doc["Statement"])
else:
    statements.append(
        {
            "Sid": "SlackbotIdempotencyTableReadWrite",
            "Effect": "Allow",
            "Action": [
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:GetItem",
                "dynamodb:DeleteItem",
            ],
            "Resource": table_arn,
        }
    )

if kind == "handler":
    statements.append(
        {
            "Sid": "InvokeWorkerFunction",
            "Effect": "Allow",
            "Action": "lambda:InvokeFunction",
            "Resource": worker_arn,
        }
    )

pathlib.Path(outfile).write_text(
    json.dumps({"Version": "2012-10-17", "Statement": statements}), encoding="utf-8"
)
PY

  aws iam create-role --role-name "$role" \
    --assume-role-policy-document "file://$trust" \
    --tags Key=Project,Value=devoks-mcp Key=Component,Value="$component" \
    --profile "$PROFILE" >/dev/null 2>&1 || true
  aws iam put-role-policy --role-name "$role" \
    --policy-name "$role-inline" --policy-document "file://$perms" --profile "$PROFILE"
}

# --- 시크릿: SSM에서 파일로만 읽는다 (bash 변수에 값 자체를 담지 않는다) --
# ⚠️ `--output text`는 값 끝에 개행을 하나 덧붙인다(AWS CLI의 text 포매터
#   자체 동작) — 조립 단계(build_env_json)에서 반드시 그 개행을 벗겨낸다.
#   벗기지 않으면 SLACK_SIGNING_SECRET 끝에 "\n"이 붙어 HMAC 서명 검증이
#   항상 실패한다(원본 시크릿과 바이트가 달라지므로).
fetch_secret_to_file() {
  local ssm_name="$1" outfile="$2" placeholder="$3"
  if aws ssm get-parameter --name "$ssm_name" --with-decryption \
       --profile "$PROFILE" --region "$REGION" \
       --query 'Parameter.Value' --output text > "$outfile" 2>/dev/null; then
    chmod 600 "$outfile"
    echo "  ✅ $ssm_name (SSM SecureString에서 로드)"
  else
    printf '%s' "$placeholder" > "$outfile"
    chmod 600 "$outfile"
    echo "  ⚠️  $ssm_name 없음 — placeholder 사용 (TASK-031/034가 실값 등록 후 이 스크립트를 다시 실행할 것)"
  fi
}

# --- env JSON 조립: role별로 다른 키 집합 (FRD §5.2) ----------------------
build_env_json() {
  local role="$1" outfile="$2"
  python3 - "$role" "$outfile" \
    "$WORK_DIR/slack_signing_secret" "$WORK_DIR/slack_bot_token" "$WORK_DIR/slack_bot_user_id" \
    "$WORK_DIR/anthropic_api_key" "$WORK_DIR/slack_user_token_map" \
    "$IDEMPOTENCY_TABLE_NAME" "$WORKER_FN" "$MCP_SERVER_URL_VALUE" <<'PY'
import json, pathlib, sys

(role, outfile, signing_secret_f, bot_token_f, bot_user_id_f,
 anthropic_key_f, user_token_map_f, idempotency_table, worker_fn, mcp_url) = sys.argv[1:]


def read(path: str) -> str:
    # SSM `--output text`가 덧붙이는 trailing 개행만 벗긴다 — 값 내부의
    # 개행(있을 수 없는 값들이지만)까지 건드리지 않도록 rstrip 대상은 "\n"만.
    return pathlib.Path(path).read_text(encoding="utf-8").rstrip("\n")


common = {
    "SLACK_SIGNING_SECRET": read(signing_secret_f),
    "SLACK_BOT_TOKEN": read(bot_token_f),
    "SLACK_BOT_USER_ID": read(bot_user_id_f),
    "IDEMPOTENCY_TABLE": idempotency_table,
}

if role == "handler":
    # ARN이 아니라 함수 이름만 — invoke()의 FunctionName은 이름/ARN 모두
    # 받고, 이름 쪽이 짧아 4 KB 예산(EDGE-021)에 유리하다.
    variables = {**common, "WORKER_FUNCTION_NAME": worker_fn}
elif role == "worker":
    variables = {
        **common,
        "ANTHROPIC_API_KEY": read(anthropic_key_f),
        "MCP_SERVER_URL": mcp_url,
        "SLACK_USER_TOKEN_MAP": read(user_token_map_f),
    }
else:
    sys.exit(f"unknown role: {role}")

pathlib.Path(outfile).write_text(json.dumps({"Variables": variables}), encoding="utf-8")
PY
  chmod 600 "$outfile"
}

# --- EDGE-021 / EDGE-SB-012: 4 KB 총량 확인, 함수 생성 전에 반드시 먼저 ---
check_env_budget() {
  local role="$1" envfile="$2"
  python3 - "$role" "$envfile" "$ENV_BUDGET_LIMIT_BYTES" "$ENV_BUDGET_WARN_PCT" <<'PY'
import json, sys

role, envfile, limit_s, warn_pct_s = sys.argv[1:]
limit = int(limit_s)
warn_pct = float(warn_pct_s)

with open(envfile, encoding="utf-8") as f:
    variables = json.load(f)["Variables"]

# AWS Lambda의 4 KB 한계는 키+값의 UTF-8 바이트 합이다(EDGE-021, Stage 1
# FRD 실측 근거) — 이 JSON 파일 자체의 바이트 수(따옴표·중괄호·쉼표 포함)가
# 아니다.
total = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in variables.items())
pct = 100 * total / limit
print(f"  {role}: {total} B / {limit} B ({pct:.1f}%)", file=sys.stderr)

if total > limit:
    sys.exit(
        f"ERROR: {role} 환경변수 총량이 {total} B로 {limit} B 한계를 초과했습니다 "
        f"(EDGE-021). SLACK_USER_TOKEN_MAP 등 불필요한 값을 줄이십시오."
    )
if pct >= warn_pct:
    print(
        f"  ⚠️  WARNING: {role} 환경변수가 4 KB 예산의 {pct:.1f}% 를 사용 중입니다 "
        f"(EDGE-SB-012: SLACK_USER_TOKEN_MAP은 사용자당 약 60 B) — "
        f"사용자 추가 시 주시하십시오.",
        file=sys.stderr,
    )
PY
}

# ============================================================================
# 1) 시크릿 로드 (SSM SecureString, 없으면 placeholder) — 값은 파일로만 존재
# ============================================================================
say "1) 시크릿 로드: $SSM_PREFIX/* (SecureString, --with-decryption)"
# 🔴 서명 시크릿의 placeholder 만 **랜덤**이다 — 나머지와 이유가 다르다.
#
# 이 저장소는 PUBLIC 이다. 고정 문자열을 placeholder 로 쓰면 그 값이 곧 공개된
# 서명 키가 되고, 누구나 `v0=HMAC(그 값, "v0:{ts}:{body}")` 를 계산해 **유효한
# Slack 서명을 위조**할 수 있다. handler 의 서명 검증(우회 16종 차단)을 통과한
# 요청은 멱등 claim 과 worker 비동기 호출까지 그대로 밀고 들어간다 —
# `https://mcp.devoks.kr/slack/events` 는 공개 엔드포인트이므로 Slack 앱 URL 을
# 등록하기 전에도 도달 가능하다(`EDGE-022` 가 막으려던 무단 호출 그 자체다).
#
# 지금은 `SLACK_USER_TOKEN_MAP` placeholder 가 `{}` 라 위조 요청도 전원 미등록으로
# 떨어지고 Anthropic 키도 가짜라 질의가 실패한다. 그러나 그것은 **다른 값들이
# 아직 placeholder 라서** 생기는 우연한 방어다. 토큰 맵과 API 키만 먼저 넣고
# 서명 시크릿을 빠뜨리면 그 순간 인증이 통째로 열린다.
#
# Stage 1 에 같은 사고가 있었다 — `.env.example` 의 공개 placeholder 가
# 프로덕션 토큰으로 그대로 올라갔다. 반복하지 않는다.
#
# 랜덤이면 아무도 값을 모르므로 **모든 서명이 실패**하고 엔드포인트가 무해해진다.
# 재실행마다 값이 바뀌지만 placeholder 이므로 무해하다. 다른 셋(봇 토큰·
# Anthropic 키·봇 user id)은 **외부로 나가는 자격증명**이라 알려져도 인증 경계가
# 아니고, 오히려 고정값이 "아직 설정 안 됨"을 읽는 사람에게 알려줘 유용하다.
fetch_secret_to_file "$SSM_PREFIX/slack-signing-secret" "$WORK_DIR/slack_signing_secret" \
  "UNSET-$(openssl rand -hex 32)"
fetch_secret_to_file "$SSM_PREFIX/slack-bot-token" "$WORK_DIR/slack_bot_token" \
  "xoxb-REPLACE-VIA-TASK-031"
fetch_secret_to_file "$SSM_PREFIX/slack-bot-user-id" "$WORK_DIR/slack_bot_user_id" \
  "UPLACEHOLDER0031"
fetch_secret_to_file "$SSM_PREFIX/anthropic-api-key" "$WORK_DIR/anthropic_api_key" \
  "sk-ant-REPLACE-VIA-TASK-031"
fetch_secret_to_file "$SSM_PREFIX/slack-user-token-map" "$WORK_DIR/slack_user_token_map" "{}"

# ============================================================================
# 2) env JSON 조립 + 4 KB 예산 확인 (함수 생성 전에 반드시 먼저 확인)
# ============================================================================
say "2) 환경변수 조립 + EDGE-021 4 KB 예산 확인"
build_env_json worker "$WORK_DIR/worker-env.json"
build_env_json handler "$WORK_DIR/handler-env.json"
check_env_budget worker "$WORK_DIR/worker-env.json"
check_env_budget handler "$WORK_DIR/handler-env.json"

# ============================================================================
# 3) worker 먼저 (로그 그룹 → 실행 역할 → 함수) — 위 헤더 "생성 순서" 참고
# ============================================================================
say "3-1) worker 로그 그룹: $WORKER_LOG_GROUP"
ensure_log_group "$WORKER_LOG_GROUP" slackbot-worker

say "3-2) worker 실행 역할: $WORKER_ROLE (DynamoDB 4액션만, SSM 없음)"
ensure_execution_role "$WORKER_ROLE" "$WORKER_LOG_GROUP" slackbot-worker worker

# ---------------------------------------------------------------------------
# create-function 은 두 가지로 실패한다. 둘 다 재실행 가능해야 한다.
#
# ① "The role defined for the function cannot be assumed by Lambda"
#    방금 만든 IAM 역할이 아직 전파되지 않았다(실제로 이 스크립트 최초 실행에서
#    발생했다). IAM 은 eventually consistent 라 기다리면 풀린다. `03-lambda.sh`
#    는 이 대비가 없었는데 Stage 2 때는 우연히 걸리지 않았을 뿐이다.
#
# ② ResourceConflictException — 이미 존재한다.
#    재실행이 곧 EDGE-SB-018 의 시크릿 회전 절차다(SSM 갱신 → 이 스크립트 재실행).
#    그때는 코드와 설정을 각각 갱신한다. 설정 갱신이 환경변수 재주입이다.
# ---------------------------------------------------------------------------
create_or_update_function() {
  local fn="$1" role="$2" mem="$3" timeout="$4" image_config="$5" env_json="$6" component="$7"
  local err="$WORK_DIR/create-err" attempt
  for attempt in $(seq 1 12); do
    if aws lambda create-function --function-name "$fn" \
         --package-type Image --code "ImageUri=$REPO_URI:$IMAGE_TAG" \
         --role "arn:aws:iam::$ACCOUNT:role/$role" \
         --architectures arm64 --memory-size "$mem" --timeout "$timeout" \
         --image-config "file://$image_config" \
         --environment "file://$env_json" \
         --tags "Project=devoks-mcp,Component=$component" \
         --profile "$PROFILE" --region "$REGION" >/dev/null 2>"$err"; then
      echo "  ✅ 생성"
      return 0
    fi
    if grep -q 'cannot be assumed by Lambda' "$err"; then
      echo "  ... IAM 역할 전파 대기 ($attempt/12)"
      sleep 5
      continue
    fi
    if grep -q 'ResourceConflictException' "$err"; then
      echo "  (이미 존재 — 코드·설정 갱신으로 전환)"
      aws lambda update-function-code --function-name "$fn" \
        --image-uri "$REPO_URI:$IMAGE_TAG" --publish \
        --profile "$PROFILE" --region "$REGION" >/dev/null
      aws lambda wait function-updated-v2 --function-name "$fn" --profile "$PROFILE" --region "$REGION"
      aws lambda update-function-configuration --function-name "$fn" \
        --memory-size "$mem" --timeout "$timeout" \
        --image-config "file://$image_config" \
        --environment "file://$env_json" \
        --profile "$PROFILE" --region "$REGION" >/dev/null
      aws lambda wait function-updated-v2 --function-name "$fn" --profile "$PROFILE" --region "$REGION"
      echo "  ✅ 갱신 (환경변수 재주입 = EDGE-SB-018 회전 절차)"
      return 0
    fi
    cat "$err" >&2
    return 1
  done
  echo "  ❌ IAM 역할 전파가 60초 안에 끝나지 않았다 — 잠시 후 다시 실행하라" >&2
  return 1
}

say "3-3) worker 함수 생성: $WORKER_FN (arm64, ${WORKER_TIMEOUT_SECONDS}s / ${WORKER_MEMORY_MB}MB, CTR-SB-009 초기값)"
create_or_update_function "$WORKER_FN" "$WORKER_ROLE" \
  "$WORKER_MEMORY_MB" "$WORKER_TIMEOUT_SECONDS" \
  "$WORK_DIR/worker-image-config.json" "$WORK_DIR/worker-env.json" slackbot-worker

aws lambda wait function-active-v2 --function-name "$WORKER_FN" --profile "$PROFILE" --region "$REGION"

# ============================================================================
# 4) handler (로그 그룹 → 실행 역할[worker invoke 포함] → 함수)
# ============================================================================
say "4-1) handler 로그 그룹: $HANDLER_LOG_GROUP"
ensure_log_group "$HANDLER_LOG_GROUP" slackbot-handler

say "4-2) handler 실행 역할: $HANDLER_ROLE (DynamoDB 4액션 + worker invoke)"
ensure_execution_role "$HANDLER_ROLE" "$HANDLER_LOG_GROUP" slackbot-handler handler

say "4-3) handler 함수 생성: $HANDLER_FN (arm64, ${HANDLER_TIMEOUT_SECONDS}s / ${HANDLER_MEMORY_MB}MB)"
create_or_update_function "$HANDLER_FN" "$HANDLER_ROLE" \
  "$HANDLER_MEMORY_MB" "$HANDLER_TIMEOUT_SECONDS" \
  "$WORK_DIR/handler-image-config.json" "$WORK_DIR/handler-env.json" slackbot-handler

aws lambda wait function-active-v2 --function-name "$HANDLER_FN" --profile "$PROFILE" --region "$REGION"

# ============================================================================
# 5) 요약 / 다음 단계
# ============================================================================
say "5) 완료"
echo "worker  : arn:aws:lambda:$REGION:$ACCOUNT:function:$WORKER_FN"
echo "handler : arn:aws:lambda:$REGION:$ACCOUNT:function:$HANDLER_FN"
echo
echo "다음 단계:"
echo "  - TASK-024: API Gateway 라우트 + 예약 동시성(이 스크립트는 둘 다 설정하지 않았다)"
echo "  - TASK-031: SSM $SSM_PREFIX/* 에 실제 시크릿 값을 등록한 뒤,"
echo "              이 스크립트를 다시 실행하거나"
echo "              'aws lambda update-function-configuration --environment file://<env.json>'"
echo "              으로 handler/worker 양쪽에 재주입할 것(EDGE-SB-018 회전 절차와 동일)."
echo "  - placeholder가 남아있는 동안 handler/worker는 실제 Slack/Anthropic 트래픽을"
echo "    올바르게 처리하지 못한다(서명 검증이 항상 실패하도록 설계된 안전한 실패 상태)."
