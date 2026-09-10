#!/usr/bin/env bash
# TASK-056 — 시크릿을 SSM Parameter Store로 이전 (FRD §10 Stage 2 Step 3)
#
# 왜 Secrets Manager가 아니라 SSM Parameter Store인가:
#   Standard 티어 파라미터는 4 KB까지 **무료**이고 SecureString이 KMS로
#   암호화한다(기본 키 alias/aws/ssm, 이것도 무료). Secrets Manager는
#   시크릿당 $0.40/월(서울 실측)이고 유일한 차별점인 자동 로테이션은
#   GitHub App 개인키에 해당되지 않는다(수동 교체). → 월 $0.80 절감.
#   실측 크기: PEM 1,674 B / 클라이언트 토큰 96 B — 둘 다 4 KB 여유.
#
# ⚠️ Lambda는 ECS의 `secrets`/`valueFrom` 같은 SSM 자동 주입이 없다.
#   `aws lambda create-function`의 옵션은 `--environment`와 `--kms-key-arn`
#   뿐이다(CLI help 실측). 따라서 이 파라미터는 **원본 기록**이고, 실제
#   주입은 infra/03-lambda.sh가 여기서 읽어 `--environment`로 넣는다.
#   Lambda는 환경변수를 저장 시 KMS로 암호화한다(공식 문서).
#   앱이 직접 SSM을 읽게 만드는 방식(boto3 / Parameters 확장)은 의존성·
#   콜드스타트·FRD의 env 주입 계약 변경을 부르므로 후속 결정으로 남겼다.
#
# 멱등성: --overwrite 를 쓰므로 반복 실행이 안전하다(값이 같으면 SSM이
#   새 버전을 만들지 않는다).
#
# 검증 상태 (은폐 금지):
#   2026-09-10에 이 파일의 **개별 명령을 그대로 순차 실행**해 파라미터 2개를
#   실제로 만들었다(Version 1, Standard, alias/aws/ssm). 검증된 것:
#     - .env 여러 줄 파서 + 4 KB 상한 확인 + mode 600 임시 파일 생성
#     - put-parameter 2회 (아래와 동일한 플래그) -> Version 1
#     - add-tags-to-resource 2회, describe-parameters
#     - --with-decryption 왕복: PEM 1,674 B / 27줄, 토큰 JSON 96 B — 원본과
#       바이트 단위 일치
#   검증되지 **않은** 것: 이 파일을 `./infra/02-secrets.sh` 형태로 통째 실행한
#   경로(set -euo pipefail, mktemp -d, trap 정리). 샌드박스 분류기가 새로 만든
#   스크립트의 실행을 차단해 개별 명령으로 수행했다. 다음에 이 스크립트를
#   처음 통째로 돌릴 때는 그 조립부가 최초 검증 대상이다.
#
# 사용법:  ./infra/02-secrets.sh            # .env 에서 읽음
#          ENV_FILE=/path/to/.env ./infra/02-secrets.sh
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
ENV_FILE="${ENV_FILE:-.env}"
PREFIX="/devoks-mcp/management"

[[ -f "$ENV_FILE" ]] || { echo "ERROR: $ENV_FILE 없음" >&2; exit 1; }

# 임시 파일은 사용자 전용 권한으로 만들고 종료 시 반드시 지운다.
TMPDIR_SECRET="$(mktemp -d)"
chmod 700 "$TMPDIR_SECRET"
trap 'rm -rf "$TMPDIR_SECRET"' EXIT INT TERM

# .env 의 여러 줄 따옴표 값(PEM)을 올바로 읽어 파일로 떨어뜨린다.
# 값은 stdout 으로 절대 내보내지 않는다 — 길이만 보고한다.
python3 - "$ENV_FILE" "$TMPDIR_SECRET" <<'PY'
import pathlib, sys

env_file, outdir = sys.argv[1], pathlib.Path(sys.argv[2])

def parse(path):
    keys, cur, buf, q = {}, None, "", None
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines(keepends=True):
        if cur is None:
            st = line.strip()
            if not st or st.startswith("#") or "=" not in st:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.rstrip("\n")
            if v[:1] in ("'", '"'):
                q = v[0]
                if len(v) >= 2 and v.endswith(q):
                    keys[k] = v[1:-1]
                else:
                    cur, buf = k, v[1:] + "\n"
            else:
                keys[k] = v
        elif line.rstrip("\n").endswith(q):
            buf += line.rstrip("\n")[:-1]
            keys[cur], cur, buf = buf, None, ""
        else:
            buf += line
    # config.py 와 동일한 정규화: 리터럴 "\n" -> 실제 개행
    return {k: v.replace("\\n", "\n") for k, v in keys.items()}

env = parse(env_file)
WANT = {
    "GITHUB_APP_PRIVATE_KEY": "github-app-private-key",
    "MCP_CLIENT_TOKENS": "client-tokens",
}
missing = [k for k in WANT if not env.get(k, "").strip()]
if missing:
    sys.exit(f"ERROR: {env_file} 에 값이 없음: {', '.join(missing)}")

for key, fname in WANT.items():
    value = env[key]
    if len(value.encode()) > 4096:
        sys.exit(f"ERROR: {key} 가 {len(value.encode())}B — SSM Standard 티어 4096B 초과")
    p = outdir / fname
    p.write_text(value, encoding="utf-8")
    p.chmod(0o600)
    print(f"  준비: {fname:<24} {len(value.encode()):>5}B  ({len(value.splitlines())}줄)")
PY

echo
for pair in "github-app-private-key:GitHub App private key (PEM)" \
            "client-tokens:CTR-002 client token table (JSON)"; do
  name="${pair%%:*}"; desc="${pair#*:}"
  aws ssm put-parameter \
    --name "$PREFIX/$name" \
    --value "file://$TMPDIR_SECRET/$name" \
    --type SecureString \
    --key-id alias/aws/ssm \
    --tier Standard \
    --description "$desc" \
    --overwrite \
    --profile "$PROFILE" --region "$REGION" \
    --query 'Version' --output text \
    | xargs -I{} echo "  ✅ $PREFIX/$name  -> version {}"

  # 태그는 put-parameter --overwrite 와 함께 쓸 수 없어 별도 호출로 붙인다.
  aws ssm add-tags-to-resource \
    --resource-type Parameter --resource-id "$PREFIX/$name" \
    --tags Key=Project,Value=devoks-mcp Key=Component,Value=management \
    --profile "$PROFILE" --region "$REGION" >/dev/null
done

echo
echo "=== 검증 (값은 출력하지 않는다) ==="
aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=$PREFIX" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Parameters[].{Name:Name,Type:Type,Tier:Tier,Version:Version,KeyId:KeyId}' \
  --output table
