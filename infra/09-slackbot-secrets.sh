#!/usr/bin/env bash
# ============================================================================
# Stage 3 TASK-031 — Slackbot 시크릿을 SSM SecureString 에 등록한다.
#   (FRD §5.2 환경 키 · EDGE-SB-018 시크릿 회전 · RES-SB-API-001/002)
#
# 이 스크립트의 존재 이유는 **값이 어디에도 남지 않게** 하는 것이다.
#
#   - 대화창에 붙여넣지 않는다 — 에이전트 트랜스크립트는 디스크에 남고,
#     나중에 컨텍스트로 다시 읽힐 수 있다.
#   - 셸 히스토리에 남기지 않는다 — `read -rs` 프롬프트로 받으므로 명령줄
#     인자가 아니고, 따라서 `~/.zsh_history` 에 기록되지 않는다.
#   - 프로세스 목록에 남기지 않는다 — `aws ssm put-parameter --value` 에
#     값을 직접 주면 `ps` 로 보인다. 그래서 `file://` 로 넘긴다
#     (`infra/02-secrets.sh` 와 같은 방식).
#   - 디스크에 오래 남기지 않는다 — 임시 파일은 `chmod 600` 이고
#     `trap ... EXIT INT TERM` 으로 반드시 지운다.
#   - 확인 출력에 값을 넣지 않는다 — **바이트 길이만** 보여준다. 길이는
#     "제대로 다 붙여넣었나"를 확인하기에 충분하고 값을 드러내지 않는다.
#     (Bot User ID 는 시크릿이 아니므로 그대로 보여준다.)
#
# 실행 후 `infra/07-slackbot-lambda.sh` 를 **다시 실행**해야 Lambda 환경변수에
# 실값이 주입된다 — 07 은 생성 시점에 SSM 을 읽어 env 로 굽기 때문이다.
# 이것이 EDGE-SB-018 이 말하는 회전 절차이기도 하다: SSM 갱신 → 07 재실행.
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
SSM_PREFIX="${SLACKBOT_SSM_PREFIX:-/devoks-mcp/slackbot}"   # 07 의 SSM_PREFIX 와 같아야 한다

WORK_DIR="$(mktemp -d)"
chmod 700 "$WORK_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT INT TERM

say() { printf '\n== %s\n' "$1"; }

# ---------------------------------------------------------------------------
# 값을 화면에 띄우지 않고 받아 파일로 떨군다.
#   $1 파일명  $2 표시 이름  $3 힌트  $4 선택적 검증 정규식  $5 "secret"|"plain"
# 검증 실패 메시지에 **값을 넣지 않는다** — 오타를 알려주려다 시크릿을 찍는
# 실수가 흔하다. 무엇이 틀렸는지만 말한다.
# ---------------------------------------------------------------------------
prompt_secret() {
  local fname="$1" label="$2" hint="$3" pattern="${4:-}" kind="${5:-secret}"
  local value=""
  while :; do
    printf '\n%s\n  %s\n  입력(화면에 보이지 않습니다, 그냥 붙여넣고 Enter): ' "$label" "$hint"
    if [[ "$kind" == "plain" ]]; then
      read -r value
    else
      read -rs value
      printf '\n'
    fi
    value="${value#"${value%%[![:space:]]*}"}"   # 앞 공백 제거
    value="${value%"${value##*[![:space:]]}"}"   # 뒤 공백 제거
    if [[ -z "$value" ]]; then
      echo "  ❌ 비어 있습니다. 다시 입력하세요." >&2
      continue
    fi
    if [[ -n "$pattern" && ! "$value" =~ $pattern ]]; then
      echo "  ❌ 형식이 예상과 다릅니다(값은 표시하지 않습니다). 힌트를 다시 확인하세요." >&2
      continue
    fi
    break
  done
  # 개행 없이 쓴다 — SSM 에 저장되는 값이 입력과 정확히 같아야 한다.
  # 서명 시크릿 끝에 개행이 하나만 붙어도 모든 서명 검증이 실패한다.
  printf '%s' "$value" > "$WORK_DIR/$fname"
  chmod 600 "$WORK_DIR/$fname"
  if [[ "$kind" == "plain" ]]; then
    echo "  ✅ 받았습니다: $value"
  else
    echo "  ✅ 받았습니다 ($(wc -c < "$WORK_DIR/$fname" | tr -d ' ')바이트)"
  fi
  unset value
}

put_param() {
  local fname="$1" ssm_name="$2" desc="$3"
  local version
  version="$(aws ssm put-parameter \
    --name "$SSM_PREFIX/$ssm_name" \
    --value "file://$WORK_DIR/$fname" \
    --type SecureString --key-id alias/aws/ssm --tier Standard \
    --description "$desc" --overwrite \
    --profile "$PROFILE" --region "$REGION" \
    --query 'Version' --output text)"
  # 태그는 --overwrite 와 같이 못 쓴다(02-secrets.sh 에서 확인된 제약).
  aws ssm add-tags-to-resource \
    --resource-type Parameter --resource-id "$SSM_PREFIX/$ssm_name" \
    --tags Key=Project,Value=devoks-mcp Key=Component,Value=slackbot \
    --profile "$PROFILE" --region "$REGION" >/dev/null
  echo "  ✅ $SSM_PREFIX/$ssm_name -> version $version"
}

cat <<'INTRO'

  Slackbot 시크릿 등록 (SSM SecureString)

  입력한 값은 화면에 보이지 않고, 셸 히스토리에도 남지 않습니다.
  중간에 그만두려면 Ctrl+C — 임시 파일은 자동으로 지워집니다.

INTRO

say "1) Slack — https://api.slack.com/apps → 해당 앱"

prompt_secret slack_signing_secret \
  "Signing Secret" \
  "Basic Information → App Credentials → Signing Secret (32자 hex)" \
  '^[0-9a-f]{16,}$'

prompt_secret slack_bot_token \
  "Bot User OAuth Token" \
  "OAuth & Permissions → Bot User OAuth Token (xoxb- 로 시작)" \
  '^xoxb-'

prompt_secret slack_bot_user_id \
  "Bot User ID" \
  "curl -sS -H 'Authorization: Bearer <봇토큰>' https://slack.com/api/auth.test 의 user_id (bot_id 아님!)" \
  '^[UW][A-Z0-9]+$' plain

say "2) Anthropic — https://platform.claude.com → Settings → Keys"

prompt_secret anthropic_api_key \
  "Anthropic API Key" \
  "생성 직후 한 번만 보이는 값 (sk-ant- 로 시작)" \
  '^sk-ant-'

say "3) SSM 등록"
put_param slack_signing_secret slack-signing-secret "Slack request signing secret (CTR-SB-001)"
put_param slack_bot_token      slack-bot-token      "Slack bot user OAuth token (chat:write)"
put_param slack_bot_user_id    slack-bot-user-id    "Slack bot user ID (EDGE-SB-011 self-message guard)"
put_param anthropic_api_key    anthropic-api-key    "Anthropic API key (CTR-SB-004)"

# SLACK_USER_TOKEN_MAP 은 여기서 받지 않는다 — 사람별 MCP 토큰을 먼저 발급해야
# 하고(TASK-034), 그 값은 MCP 서버의 MCP_CLIENT_TOKENS 와 짝이 맞아야 한다.
# 07 은 이 파라미터가 없으면 `{}` 로 둔다(= 전원 미등록 = fail-safe).

say "4) 검증 (값은 출력하지 않는다)"
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=$SSM_PREFIX" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Parameters[].{Name:Name,Type:Type,Tier:Tier,Version:Version,KeyId:KeyId}' \
  --output table

cat <<INTRO

  다음 단계
    1) infra/07-slackbot-lambda.sh 재실행 — SSM 실값을 Lambda 환경변수로 굽는다
       (07 은 생성 시점에만 SSM 을 읽는다. 재실행하지 않으면 placeholder 그대로다)
    2) TASK-034 — 사람별 MCP 토큰 발급 후 $SSM_PREFIX/slack-user-token-map 등록
    3) TASK-032 — Slack Event Subscriptions 에 Request URL 등록

INTRO
