#!/usr/bin/env bash
# Stage 2 · Step 1 — ECR repository + GitHub Actions OIDC role
#
# Creates the image registry and the identity GitHub Actions uses to push to it.
# Run once per AWS account. Idempotent-ish: re-running reports "already exists"
# for each resource rather than duplicating (AWS returns EntityAlreadyExists).
#
# Why OIDC instead of an access key in repo secrets: a long-lived key that can
# push images is a credential to rotate and to leak. OIDC hands the workflow a
# token that expires in an hour and is bound to this repository.
#
# Prereqs: aws CLI authenticated as a principal that can create IAM roles.
#   AWS_PROFILE=devoks ./infra/01-ecr-and-github-oidc.sh
set -euo pipefail

PROFILE="${AWS_PROFILE:-devoks}"
REGION="${AWS_REGION:-ap-northeast-2}"
ACCOUNT_ID="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
GITHUB_REPO="${GITHUB_REPO:-ridsync/devoks-mcp-servers}"

# GitHub's OIDC `sub` claim carries immutable owner and repository IDs for any
# repository created after 2026-07-15 (and for any repo renamed or transferred
# after that date):
#
#   repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:refs/heads/BRANCH
#
# NOT the `repo:OWNER/REPO:ref:...` form that AWS's docs and essentially every
# blog post still show. A trust policy written against the old shape is
# refused with "Not authorized to perform sts:AssumeRoleWithWebIdentity" and
# nothing in that message hints at why. Note the delimiter is `@`; GitHub's own
# docs briefly described it as `-`.
#
# Matching on the IDs rather than the names is the point of the feature: the
# policy keeps working through a rename or transfer, because IDs do not move.
# See https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-github-actions-oidc-tokens/
REPO_ID="$(gh api "repos/${GITHUB_REPO}" --jq .id)"
OWNER_ID="$(gh api "repos/${GITHUB_REPO}" --jq .owner.id)"
OWNER_NAME="${GITHUB_REPO%%/*}"
REPO_NAME="${GITHUB_REPO##*/}"
OIDC_SUBJECT="repo:${OWNER_NAME}@${OWNER_ID}/${REPO_NAME}@${REPO_ID}:*"
ECR_REPO="devoks-mcp-management"
# Same string as ECR_REPO by this project's naming convention (component name
# reused for the ECR repo and the Lambda function alike) — kept as its own
# variable so the IAM policy below reads as "this Lambda function", not as an
# accidental reuse of the ECR repo name.
FUNCTION_NAME="devoks-mcp-management"
ROLE_NAME="devoks-mcp-github-actions"
TAGS="Key=Project,Value=devoks-mcp Key=Stage,Value=stage2 Key=ManagedBy,Value=infra-script"

aws() { command aws --profile "$PROFILE" "$@"; }
say() { printf '\n== %s\n' "$1"; }

say "ECR repository: $ECR_REPO"
aws ecr create-repository --region "$REGION" \
  --repository-name "$ECR_REPO" \
  --image-scanning-configuration scanOnPush=true \
  --image-tag-mutability MUTABLE \
  --encryption-configuration encryptionType=AES256 \
  --tags $TAGS \
  --query 'repository.repositoryUri' --output text || echo "  (already exists)"

say "Lifecycle policy"
# Untagged layers are build leftovers nobody can reference; 20 tagged images is
# well past any rollback we would actually perform.
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

say "GitHub OIDC provider"
# The thumbprint must match the top intermediate CA of
# token.actions.githubusercontent.com. GitHub moved to Let's Encrypt, so the
# DigiCert values still circulating in blog posts (6938fd4d…, 1c58a3a8…) are
# stale. Compute it from the live chain instead of pasting a constant:
THUMBPRINT="$(
  openssl s_client -servername token.actions.githubusercontent.com \
    -showcerts -connect token.actions.githubusercontent.com:443 </dev/null 2>/dev/null \
  | awk '/-----BEGIN CERTIFICATE-----/{n++} n==2' \
  | openssl x509 -noout -fingerprint -sha1 \
  | sed 's/.*=//' | tr -d ':' | tr 'A-Z' 'a-z'
)"
ROOT_THUMBPRINT="$(
  openssl s_client -servername token.actions.githubusercontent.com \
    -showcerts -connect token.actions.githubusercontent.com:443 </dev/null 2>/dev/null \
  | awk '/-----BEGIN CERTIFICATE-----/{n++} n==3' \
  | openssl x509 -noout -fingerprint -sha1 \
  | sed 's/.*=//' | tr -d ':' | tr 'A-Z' 'a-z'
)"
echo "  intermediate CA sha1: $THUMBPRINT"
echo "  root CA sha1        : $ROOT_THUMBPRINT"
# Both registered: AWS no longer validates thumbprints for IdPs backed by a
# trusted root, but the API still requires the field, and Let's Encrypt serves
# different chains to different clients - so pinning only one is a coin flip.
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list "$THUMBPRINT" "$ROOT_THUMBPRINT" \
  --tags Key=Project,Value=devoks-mcp \
  --query OpenIDConnectProviderArn --output text || echo "  (already exists)"

say "IAM role: $ROLE_NAME"
# Scoped to this repository. The audience check is what stops another GitHub
# account's workflow from assuming this role.
aws iam create-role \
  --role-name "$ROLE_NAME" \
  --description "GitHub Actions OIDC - push container images to ECR + swap Lambda code (never configuration)" \
  --max-session-duration 3600 \
  --tags $TAGS \
  --assume-role-policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Principal\": { \"Federated\": \"arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com\" },
      \"Action\": \"sts:AssumeRoleWithWebIdentity\",
      \"Condition\": {
        \"StringEquals\": { \"token.actions.githubusercontent.com:aud\": \"sts.amazonaws.com\" },
        \"StringLike\":   { \"token.actions.githubusercontent.com:sub\": \"${OIDC_SUBJECT}\" }
      }
    }]
  }" --query Role.Arn --output text || echo "  (already exists)"

say "Role policy: ECR push + Lambda code swap, this repository/function only"
# 보안 검증(2026-09-16) 발견 사항 정정: `.github/workflows/ci.yml`의 "Deploy to
# Lambda" 스텝이 이 역할로 `devoks-mcp-management`에 `update-function-code`를
# 실제로 실행하는데(+ `wait function-updated-v2`, `get-function-configuration`),
# 그 권한이 이전에는 이 스크립트에도 어느 추적 스크립트에도 선언돼 있지 않았다
# — 라이브 IAM 정책이 IaC(이 저장소의 infra/*.sh)보다 넓게 드리프트된 상태였다.
# `SwapImageOnlyNeverConfiguration`은 `infra/10-slackbot-ecr-and-oidc.sh`가
# slackbot 두 함수에 부여한 것과 정확히 같은 Sid·같은 액션 집합·같은 원칙이다:
# 코드(이미지) 교체만 허용하고 `lambda:UpdateFunctionConfiguration`은 **절대**
# 주지 않는다 — 환경변수(시크릿 포함)를 CI가 바꿀 수 있으면 안 되고, 설정
# 변경은 `infra/03-lambda.sh`를 사람이 직접 실행하는 경로로만 한다.
aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "ecr-push-${ECR_REPO}" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      { \"Sid\": \"EcrAuthTokenIsAccountScopedByDesign\",
        \"Effect\": \"Allow\", \"Action\": \"ecr:GetAuthorizationToken\", \"Resource\": \"*\" },
      { \"Sid\": \"PushAndPullOnlyThisRepository\",
        \"Effect\": \"Allow\",
        \"Action\": [ \"ecr:BatchCheckLayerAvailability\", \"ecr:InitiateLayerUpload\",
                      \"ecr:UploadLayerPart\", \"ecr:CompleteLayerUpload\", \"ecr:PutImage\",
                      \"ecr:BatchGetImage\", \"ecr:GetDownloadUrlForLayer\" ],
        \"Resource\": \"arn:aws:ecr:${REGION}:${ACCOUNT_ID}:repository/${ECR_REPO}\" },
      { \"Sid\": \"SwapImageOnlyNeverConfiguration\",
        \"Effect\": \"Allow\",
        \"Action\": [ \"lambda:UpdateFunctionCode\", \"lambda:GetFunction\",
                      \"lambda:GetFunctionConfiguration\", \"lambda:PublishVersion\" ],
        \"Resource\": \"arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}\" }
    ]
  }"
echo "  attached"

say "Done"
cat <<EOF
  ECR URI  : ${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}
  Role ARN : arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}
  OIDC sub : ${OIDC_SUBJECT}

  Put these in .github/workflows/ci.yml (they are identifiers, not secrets).
EOF
