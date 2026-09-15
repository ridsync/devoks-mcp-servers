#!/usr/bin/env bash
# ============================================================================
# Stage 3 — slackbot 용 ECR 리포지토리 + GitHub OIDC 권한 확장
#   (AC-SB-008-2 · AC-SB-008-3)
#
# PLAN 에 이 태스크가 없었다. TASK-020~024 를 쓰고 나서야 드러났다:
#   - `infra/07-slackbot-lambda.sh` 는 **이미지 URI 로** Lambda 를 만든다
#     → ECR 리포지토리와 이미지가 먼저 있어야 한다
#   - `infra/01-ecr-and-github-oidc.sh` 는 `devoks-mcp-management` **하나만**
#     만들었고, OIDC 역할 정책도 그 리포·그 함수 ARN 으로 정확히 한정돼 있다
#     (Stage 2 의 최소권한 방침 — 좋은 설계지만 새 서버를 추가할 때마다
#     이 스크립트 같은 확장이 필요하다는 뜻이다)
#
# 기존 `ecr-push-devoks-mcp-management` 인라인 정책은 **건드리지 않는다**.
# 프로덕션 MCP 배포 경로이므로, 별도 인라인 정책을 새로 붙여 관리한다.
#
# 이 스크립트 이후의 순서:
#   1) `gh variable set SLACKBOT_DEPLOY_ENABLED --body true`
#   2) 브랜치 push → CI 가 이미지를 ECR `:SHA` 로 푸시
#      (`Deploy to Lambda` 는 기본 브랜치 조건이라 feature 브랜치에서는 건너뛴다)
#   3) `infra/06-idempotency-table.sh`
#   4) `IMAGE_TAG=<sha> infra/07-slackbot-lambda.sh`
#   5) `infra/08-slackbot-route.sh`
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
ECR_REPO="${SLACKBOT_ECR_REPO:-devoks-slackbot}"
ROLE_NAME="${GITHUB_OIDC_ROLE:-devoks-mcp-github-actions}"
POLICY_NAME="ecr-push-$ECR_REPO"
HANDLER_FN="${SLACKBOT_HANDLER_FN:-devoks-slack-handler}"
WORKER_FN="${SLACKBOT_WORKER_FN:-devoks-slack-worker}"
TAGS="Key=Project,Value=devoks-mcp Key=Stage,Value=stage3 Key=Component,Value=slackbot Key=ManagedBy,Value=infra-script"

aws() { command aws --profile "$PROFILE" "$@"; }
say() { printf '\n== %s\n' "$1"; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"

say "ECR repository: $ECR_REPO"
# 01 과 동일한 설정 — 스캔 on push, AES256, MUTABLE(:latest 를 굴려야 하므로).
aws ecr create-repository --region "$REGION" \
  --repository-name "$ECR_REPO" \
  --image-scanning-configuration scanOnPush=true \
  --image-tag-mutability MUTABLE \
  --encryption-configuration encryptionType=AES256 \
  --tags $TAGS \
  --query 'repository.repositoryUri' --output text || echo "  (already exists)"

say "Lifecycle policy"
# 01 과 같은 규칙: 태그 없는 빌드 잔재는 7일, 태그된 이미지는 최근 20개.
aws ecr put-lifecycle-policy --region "$REGION" \
  --repository-name "$ECR_REPO" \
  --lifecycle-policy-text '{
    "rules": [
      { "rulePriority": 1,
        "description": "Expire untagged images after 7 days",
        "selection": { "tagStatus": "untagged", "countType": "sinceImagePushed",
                       "countUnit": "days", "countNumber": 7 },
        "action": { "type": "expire" } },
      { "rulePriority": 2,
        "description": "Keep only the 20 most recent images",
        "selection": { "tagStatus": "any", "countType": "imageCountMoreThan",
                       "countNumber": 20 },
        "action": { "type": "expire" } }
    ]
  }' --query repositoryName --output text

say "GitHub OIDC 역할 권한 확장: $ROLE_NAME / $POLICY_NAME"
# 기존 management 정책과 같은 형태·같은 최소권한 원칙:
#   - ecr:GetAuthorizationToken 은 계정 스코프가 API 설계상 강제다(01 참고)
#     → 이미 management 정책이 갖고 있으므로 여기서 중복 부여하지 않는다
#   - 푸시/풀은 이 리포지토리 하나로 한정
#   - Lambda 는 **코드 교체만** — UpdateFunctionConfiguration 은 주지 않는다.
#     환경변수(시크릿 포함)를 CI 가 바꿀 수 있으면 안 된다. 설정 변경은
#     infra/07 이 사람 손으로 한다.
#   - 함수가 아직 없어도 된다 — IAM 정책의 Resource 는 단순 문자열이다.
PERMS="$(mktemp)"; trap 'rm -f "$PERMS"' EXIT INT TERM
cat > "$PERMS" <<JSON
{"Version":"2012-10-17","Statement":[
 {"Sid":"PushAndPullOnlyThisRepository","Effect":"Allow",
  "Action":["ecr:BatchCheckLayerAvailability","ecr:InitiateLayerUpload",
            "ecr:UploadLayerPart","ecr:CompleteLayerUpload","ecr:PutImage",
            "ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"],
  "Resource":"arn:aws:ecr:$REGION:$ACCOUNT:repository/$ECR_REPO"},
 {"Sid":"SwapImageOnlyNeverConfiguration","Effect":"Allow",
  "Action":["lambda:UpdateFunctionCode","lambda:GetFunction",
            "lambda:GetFunctionConfiguration","lambda:PublishVersion"],
  "Resource":["arn:aws:lambda:$REGION:$ACCOUNT:function:$HANDLER_FN",
              "arn:aws:lambda:$REGION:$ACCOUNT:function:$WORKER_FN"]}]}
JSON
aws iam put-role-policy --role-name "$ROLE_NAME" \
  --policy-name "$POLICY_NAME" --policy-document "file://$PERMS"
echo "  ✅ $POLICY_NAME 부착"

say "검증"
echo "리포지토리:"
aws ecr describe-repositories --region "$REGION" --repository-names "$ECR_REPO" \
  --query 'repositories[].{name:repositoryName,uri:repositoryUri,scan:imageScanningConfiguration.scanOnPush,mutability:imageTagMutability}' \
  --output table
echo "역할에 붙은 인라인 정책 (기존 management 정책이 그대로 남아 있어야 한다):"
aws iam list-role-policies --role-name "$ROLE_NAME" --query 'PolicyNames' --output table
echo "새 정책이 허용하는 Lambda (설정 변경 권한이 없어야 한다):"
aws iam get-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY_NAME" \
  --query 'PolicyDocument.Statement[?Sid==`SwapImageOnlyNeverConfiguration`].[Action,Resource]' \
  --output json

cat <<NEXT

  다음 단계
    1) gh variable set SLACKBOT_DEPLOY_ENABLED --body true
    2) 브랜치 push → CI 가 $ECR_REPO:<sha> 로 이미지 푸시
       (Deploy to Lambda 는 기본 브랜치에서만 — feature 브랜치는 건너뛴다)
    3) infra/06-idempotency-table.sh
    4) IMAGE_TAG=<sha> infra/07-slackbot-lambda.sh
    5) infra/08-slackbot-route.sh

NEXT
