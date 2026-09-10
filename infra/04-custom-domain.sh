#!/usr/bin/env bash
# TASK-060 — 커스텀 도메인 mcp.devoks.kr (FRD §10 Stage 2 Step 7)
#
# ============================================================================
# 왜 CloudFront 가 아니라 API Gateway HTTP API 인가 (실측 근거)
#
# CloudFront + Lambda Function URL 경로는 MCP 와 두 겹으로 어긋난다:
#
#   1) OAC(Origin Access Control)로 Function URL 을 보호하려면 AuthType=AWS_IAM
#      이 필요한데, AWS 문서는 "If you use PUT or POST methods with your Lambda
#      function URL, your users must compute the SHA256 of the body and include
#      the payload hash value in the x-amz-content-sha256 header... Lambda
#      doesn't support unsigned payloads" 라고 명시한다. MCP Streamable HTTP 는
#      전부 POST 다 — 모든 MCP 클라이언트가 SigV4 본문 서명을 해야 하고, 그런
#      클라이언트는 없다.
#   2) OAC 를 포기해도(AuthType=NONE) AllViewerExceptHostHeader 원본 요청
#      정책이 필수가 되고, 그러면 앱이 보는 Host 가 CloudFront 도메인이 아니라
#      lambda-url 도메인이 된다 → MCP_ALLOWED_HOSTS 와 MCP_PUBLIC_URL 이 강제로
#      갈라진다(EDGE-019, 전 요청 421 의 원천).
#   또한 인증된 POST-only JSON-RPC 라 CDN 캐싱 가치가 0 이다.
#
# API Gateway HTTP API 로 실측 확인한 것 (2026-09-10):
#   - GET /healthz 200
#   - 무인증 POST /mcp → 401 (421 이 아니다 = Host 검증을 통과했다는 증거.
#     Host 가 API Gateway 도메인 그대로 도착한다)
#   - Authorization 헤더가 정책 설정 없이 전달된다 → initialize → tools/list →
#     4툴 실호출 전부 성공
#   - 통합 타임아웃이 30,000 ms 로 고정돼 있다(get-integrations 실측) →
#     EDGE-018 의 근거. GitHub 타임아웃을 20초로 낮춘 이유가 이것이다.
#   - ACM 인증서가 **같은 리전**(ap-northeast-2)이면 된다. CloudFront 는
#     us-east-1 을 요구한다.
#   - 요금 $1.23/백만 요청(Price List API 실측) → 내부 사용량에서는 월 $0.
#
# ⚠️ create-api --target 은 통합만 만들고 **Lambda 호출 권한을 추가하지
#    않는다**(실측). add-permission 을 직접 해야 하며, 없으면 API Gateway 가
#    500 을 반환한다.
# ============================================================================
set -euo pipefail

PROFILE="${AWS_PROFILE_NAME:-devoks}"
REGION="${AWS_REGION_NAME:-ap-northeast-2}"
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)"
FN=devoks-mcp-management
DOMAIN="${DOMAIN:-mcp.devoks.kr}"

# --- 1. ACM 인증서 요청 (DNS 검증) --------------------------------------
CERT_ARN="$(aws acm request-certificate \
  --domain-name "$DOMAIN" --validation-method DNS --key-algorithm RSA_2048 \
  --tags Key=Project,Value=devoks-mcp Key=Component,Value=management \
  --profile "$PROFILE" --region "$REGION" --query CertificateArn --output text)"
echo "cert: $CERT_ARN"

sleep 5
echo "── 가비아 DNS 에 추가할 검증 레코드 ──"
aws acm describe-certificate --certificate-arn "$CERT_ARN" \
  --profile "$PROFILE" --region "$REGION" \
  --query 'Certificate.DomainValidationOptions[0].ResourceRecord' --output table

# --- 2. HTTP API + Lambda 프록시 통합 -----------------------------------
API="$(aws apigatewayv2 create-api \
  --name "$FN" --protocol-type HTTP \
  --target "arn:aws:lambda:$REGION:$ACCOUNT:function:$FN" \
  --tags Project=devoks-mcp,Component=management \
  --profile "$PROFILE" --region "$REGION" --query ApiId --output text)"
echo "api: $API  endpoint: https://$API.execute-api.$REGION.amazonaws.com"

# ⚠️ --target 이 이걸 해주지 않는다 (위 주석 참고)
aws lambda add-permission --function-name "$FN" \
  --statement-id ApiGatewayInvoke --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:$REGION:$ACCOUNT:$API/*/*" \
  --profile "$PROFILE" --region "$REGION" >/dev/null

# --- 3. MCP_ALLOWED_HOSTS 에 새 진입점 추가 ------------------------------
# 세 진입점(Function URL / API Gateway 기본 엔드포인트 / 커스텀 도메인)을 모두
# 허용해야 전환 기간에 어느 쪽이든 동작한다. MCP_PUBLIC_URL 은 도메인이 실제로
# 응답하기 전에 바꾸면 안 된다 — well-known 의 resource 가 도달 불가한 URL 을
# 광고하게 되고, 그건 AC-002-4 위반이다.
echo "→ MCP_ALLOWED_HOSTS 에 $API.execute-api.$REGION.amazonaws.com 과 $DOMAIN 추가 (환경변수 전체 교체 주의)"

# ---------------------------------------------------------------------------
# 4~6 단계는 **도메인 소유자의 DNS 작업에 블록된다**
#
#   4. (사람) 가비아에서 DNS 관리를 활성화하고 위 검증 CNAME 을 추가한다.
#      2026-09-10 확인 시점에 devoks.kr 은 NS·SOA 가 없었다 — 도메인만 등록돼
#      있고 DNS 존이 서비스되지 않는 상태다. 그 상태에서는 어떤 CNAME 도
#      해석되지 않으므로 ACM 검증이 영구히 PENDING 이다.
#
#   5. 검증 완료 후(aws acm wait certificate-validated) 커스텀 도메인 생성:
#        aws apigatewayv2 create-domain-name --domain-name "$DOMAIN" \
#          --domain-name-configurations CertificateArn=$CERT_ARN,EndpointType=REGIONAL,SecurityPolicy=TLS_1_2
#        aws apigatewayv2 create-api-mapping --domain-name "$DOMAIN" \
#          --api-id "$API" --stage '$default'
#      그러면 RegionalDomainName 이 나온다 — 그게 두 번째 CNAME 의 타깃이다.
#
#   6. (사람) 가비아에 mcp → <RegionalDomainName> CNAME 추가. 해석되면
#      MCP_PUBLIC_URL / MCP_ISSUER_URL 을 https://$DOMAIN/mcp 로 전환한다
#      (경로가 /mcp 로 끝나고 후행 슬래시가 없어야 한다 — CTR-001).
# ---------------------------------------------------------------------------
