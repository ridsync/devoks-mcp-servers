#!/usr/bin/env bash
# ============================================================================
# Stage 3 TASK-034 — 사람별 MCP 토큰 발급 + Slack 사용자 매핑
#   (CTR-SB-006 §5.3 · CTR-002 · EDGE-SB-006 · EDGE-SB-012)
#
# 이 스크립트가 Stage 3 의 존재 이유를 완성한다: **per-person 감사**.
# 사람마다 MCP 토큰을 하나씩 주면 감사 레코드(`CTR-003`)의 `client_id` 가
# `slackbot` 이 아니라 사람이 된다. MCP 서버 코드는 한 줄도 바뀌지 않는다 —
# `CTR-002` 가 이미 `{token: {client_id, role, scopes}}` 구조이기 때문이다.
#
# ⚠️ 이 스크립트는 **프로덕션 MCP 서버의 토큰 테이블을 수정한다.**
# 기존 토큰을 하나라도 떨어뜨리면 그 클라이언트(= Claude Code 등록 포함)가
# 즉시 401 을 받는다. 그래서:
#   - 병합은 **추가만** 한다(기존 키를 덮어쓰지 않는다)
#   - 쓰기 **전후로 항목 수를 세어** 줄어들면 중단한다
#   - 값은 어디에도 출력하지 않는다(개수와 client_id 만 보여준다)
#   - SSM 은 버전이 남으므로 사고 시 이전 버전으로 되돌릴 수 있다
#
# 건드리는 곳 네 군데 — 넷이 모두 맞아야 동작한다:
#   ① SSM /devoks-mcp/management/client-tokens   (MCP 서버가 인증에 쓴다)
#   ② SSM /devoks-mcp/slackbot/slack-user-token-map (worker 가 조회에 쓴다)
#   ③ Lambda devoks-mcp-management  환경변수 MCP_CLIENT_TOKENS
#   ④ Lambda devoks-slack-worker    환경변수 SLACK_USER_TOKEN_MAP
# SSM 만 바꾸고 Lambda 를 갱신하지 않으면 아무 일도 일어나지 않는다 —
# 런타임은 SSM 을 읽지 않고 환경변수만 본다(프로젝트 컨벤션).
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
MGMT_SSM="${MGMT_SSM:-/devoks-mcp/management/client-tokens}"
SLACKBOT_SSM="${SLACKBOT_SSM:-/devoks-mcp/slackbot/slack-user-token-map}"
MGMT_FN="${MGMT_FN:-devoks-mcp-management}"
WORKER_FN="${SLACKBOT_WORKER_FN:-devoks-slack-worker}"
ROLE="${MCP_ROLE:-reader}"
SCOPES="${MCP_SCOPES:-devoks:read}"

WORK_DIR="$(mktemp -d)"; chmod 700 "$WORK_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM
say() { printf '\n== %s\n' "$1"; }

# --- 입력 --------------------------------------------------------------------
SLACK_USER_ID="${1:-}"
CLIENT_ID="${2:-}"
if [[ -z "$SLACK_USER_ID" || -z "$CLIENT_ID" ]]; then
  cat <<USAGE
사용법: $0 <SLACK_USER_ID> <CLIENT_ID>

  SLACK_USER_ID  Slack 사용자 ID. Slack 앱에서 프로필 → Copy member ID.
                 U 로 시작(Enterprise Grid 는 W). 봇의 ID 가 아니라 **사람** 것.
  CLIENT_ID      감사 로그에 남을 사람 식별자. 예: okwon

예:  $0 U01ABCDEF okwon
USAGE
  exit 2
fi
[[ "$SLACK_USER_ID" =~ ^[UW][A-Z0-9]+$ ]] || { echo "❌ SLACK_USER_ID 형식이 아니다: $SLACK_USER_ID" >&2; exit 1; }
[[ "$CLIENT_ID" =~ ^[a-z0-9_.-]+$ ]] || { echo "❌ CLIENT_ID 는 소문자·숫자·._- 만: $CLIENT_ID" >&2; exit 1; }

say "대상: Slack $SLACK_USER_ID → client_id '$CLIENT_ID' (role=$ROLE, scopes=$SCOPES)"

# --- 현재 값 읽기 (값은 파일로만 존재, 출력하지 않는다) ------------------------
say "1) 현재 값 로드"
aws ssm get-parameter --name "$MGMT_SSM" --with-decryption \
  --profile "$PROFILE" --region "$REGION" --query 'Parameter.Value' --output text \
  > "$WORK_DIR/mgmt.json"; chmod 600 "$WORK_DIR/mgmt.json"
# --output text 는 끝에 개행을 붙인다. JSON 파서는 무시하므로 그대로 둔다.
if aws ssm get-parameter --name "$SLACKBOT_SSM" --with-decryption \
     --profile "$PROFILE" --region "$REGION" --query 'Parameter.Value' --output text \
     > "$WORK_DIR/map.json" 2>/dev/null; then
  chmod 600 "$WORK_DIR/map.json"
else
  printf '{}' > "$WORK_DIR/map.json"; chmod 600 "$WORK_DIR/map.json"
  echo "  (slack-user-token-map 없음 — 빈 매핑에서 시작)"
fi

# --- 병합 --------------------------------------------------------------------
say "2) 병합 (추가만 — 기존 항목은 건드리지 않는다)"
python3 - "$WORK_DIR" "$SLACK_USER_ID" "$CLIENT_ID" "$ROLE" "$SCOPES" <<'PY'
import json, pathlib, secrets, sys

work, slack_user, client_id, role, scopes = sys.argv[1:6]
w = pathlib.Path(work)

tokens = json.loads((w / "mgmt.json").read_text())
mapping = json.loads((w / "map.json").read_text())
before_tokens, before_map = len(tokens), len(mapping)

if slack_user in mapping:
    sys.exit(
        f"❌ {slack_user} 는 이미 매핑에 있다. 재발급하려면 먼저 기존 항목을 "
        f"의도적으로 제거하라 — 실수로 덮어쓰면 그 사람의 기존 토큰이 고아가 된다."
    )
if any(v.get("client_id") == client_id for v in tokens.values()):
    sys.exit(f"❌ client_id '{client_id}' 가 이미 토큰 테이블에 있다. 다른 이름을 쓰라.")

# MCP 서버가 32자 이상을 요구한다(TASK-046, 공개 placeholder 사고의 재발 방지).
token = secrets.token_urlsafe(32)
assert len(token) >= 32, "token_urlsafe(32) 가 32자 미만일 수 없다"

tokens[token] = {"client_id": client_id, "role": role, "scopes": scopes.split(",")}
mapping[slack_user] = token

# 안전장치: 항목 수는 정확히 하나씩만 늘어야 한다.
if len(tokens) != before_tokens + 1 or len(mapping) != before_map + 1:
    sys.exit("❌ 병합 결과가 예상과 다르다 — 쓰지 않고 중단한다.")

(w / "mgmt.new.json").write_text(json.dumps(tokens, separators=(",", ":")))
(w / "map.new.json").write_text(json.dumps(mapping, separators=(",", ":")))
(w / "mgmt.new.json").chmod(0o600)
(w / "map.new.json").chmod(0o600)

# EDGE-SB-012: 매핑은 Lambda 환경변수 4 KB 예산 안에 들어가야 한다.
size = len((w / "map.new.json").read_text().encode())
print(f"  토큰 테이블 {before_tokens} → {len(tokens)}개")
print(f"  사용자 매핑 {before_map} → {len(mapping)}개  ({size} B)")
print(f"  등록된 client_id: {sorted(v.get('client_id', '?') for v in tokens.values())}")
if size > 2048:
    print(f"  ⚠️  매핑이 {size} B — 4 KB 예산(EDGE-021)의 절반을 넘었다. "
          f"Stage 1 §10 OAuth 전환 트리거 ①을 재검토할 시점이다.")
PY

# --- 쓰기 --------------------------------------------------------------------
say "3) SSM 갱신 (버전이 남으므로 되돌릴 수 있다)"
for pair in "mgmt.new.json:$MGMT_SSM:CTR-002 client token table (JSON)" \
            "map.new.json:$SLACKBOT_SSM:CTR-SB-006 Slack user -> MCP token map"; do
  f="${pair%%:*}"; rest="${pair#*:}"; name="${rest%%:*}"; desc="${rest#*:}"
  v="$(aws ssm put-parameter --name "$name" --value "file://$WORK_DIR/$f" \
        --type SecureString --key-id alias/aws/ssm --tier Standard \
        --description "$desc" --overwrite \
        --profile "$PROFILE" --region "$REGION" --query 'Version' --output text)"
  echo "  ✅ $name -> version $v"
done

# --- Lambda 환경변수 재주입 ---------------------------------------------------
# update-function-configuration --environment 는 **전체를 교체**한다.
# 현재 값을 읽어 해당 키 하나만 갈아끼운 뒤 되써야 다른 변수가 날아가지 않는다.
reinject() {
  local fn="$1" key="$2" valuefile="$3"
  aws lambda get-function-configuration --function-name "$fn" \
    --profile "$PROFILE" --region "$REGION" \
    --query 'Environment.Variables' --output json > "$WORK_DIR/$fn.env.json"
  chmod 600 "$WORK_DIR/$fn.env.json"
  python3 - "$WORK_DIR/$fn.env.json" "$key" "$valuefile" <<'PY'
import json, pathlib, sys
envfile, key, valuefile = sys.argv[1:4]
p = pathlib.Path(envfile)
env = json.loads(p.read_text())
before = len(env)
env[key] = pathlib.Path(valuefile).read_text()
if len(env) < before:
    sys.exit("❌ 환경변수가 줄었다 — 중단")
total = sum(len(k.encode()) + len(v.encode()) for k, v in env.items())
p.write_text(json.dumps({"Variables": env}, separators=(",", ":")))
print(f"  {len(env)}개 키 / {total} B ({total / 4096 * 100:.1f}% of 4 KB)")
if total > 4096:
    sys.exit("❌ 4 KB 초과 — 쓰지 않는다(EDGE-021)")
PY
  aws lambda update-function-configuration --function-name "$fn" \
    --environment "file://$WORK_DIR/$fn.env.json" \
    --profile "$PROFILE" --region "$REGION" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn" --profile "$PROFILE" --region "$REGION"
  echo "  ✅ $fn 재주입 완료"
}

say "4) Lambda 환경변수 재주입 (SSM 만 바꾸면 런타임은 모른다)"
reinject "$MGMT_FN"   MCP_CLIENT_TOKENS     "$WORK_DIR/mgmt.new.json"
reinject "$WORKER_FN" SLACK_USER_TOKEN_MAP  "$WORK_DIR/map.new.json"

say "5) 검증 (값은 출력하지 않는다)"
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=/devoks-mcp" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Parameters[].{Name:Name,Version:Version}' --output table

cat <<NEXT

  이제 Slack 에서 $CLIENT_ID 님이 봇을 멘션하면, MCP 감사 레코드의
  client_id 가 'slackbot' 이 아니라 '$CLIENT_ID' 로 남는다 — Stage 3 의 목적이다.

  되돌리려면: SSM 이전 버전으로 put-parameter 후 이 스크립트의 reinject 와
  같은 방식으로 Lambda 환경변수를 되돌린다.

NEXT
