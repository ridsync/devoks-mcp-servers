# 작업 흐름 — Management MCP 서버 (Stage 1 → Stage 2)

> 이 문서는 **어떤 순서로, 왜 그렇게 결정했고, 무엇이 실측으로 뒤집혔는지**를
> 단계별로 되짚는다. 상세 계약·근거는 아래 두 문서가 SSOT다.
>
> | 문서 | 역할 |
> |---|---|
> | [`.claude/workspace/management-mcp-bootstrap-20260903/FRD.md`](../.claude/workspace/management-mcp-bootstrap-20260903/FRD.md) | 요구사항(REQ/AC) · 설계(DSN) · 계약(CTR) · 엣지케이스(EDGE) · 로드맵 |
> | [`.claude/workspace/management-mcp-bootstrap-20260903/PLAN.md`](../.claude/workspace/management-mcp-bootstrap-20260903/PLAN.md) | Task 분해(TASK-001~061) · 각 Task의 검증 증거 |
>
> ID 현황: **REQ 8 · AC 8군 · CTR 11 · EDGE 21 · DSN 8 · TASK 43(미완 1)**

---

## 한눈에 보기

```
Stage 1 — 서버 구축 (로컬·CI에서 완결)
  1. FRD 확정 ......... 요구사항·계약을 ID로 고정, 미결정은 질문으로 회수
  2. PLAN 분해 ........ Task마다 파일 경로 + traces(AC/CTR/EDGE) 명시
  3. 바닥부터 구현 ..... 타입·설정 → 인증·인가·감사 → GitHub 어댑터 → 4툴
  4. 컨테이너·CI ...... arm64 이미지 + 품질 게이트
  5. 보안 리뷰 ........ 🔴 경로 트래버설 재현·차단, qualifier 주입 차단

Stage 2 — 실배포 (AWS)
  6. 배포 타깃 재결정 .. 비용 실측 → Fargate+ALB($38) → Lambda($0.01)
  7. 런타임 전환 ...... stateless-JSON + Lambda Web Adapter
  8. 이미지 공급 경로 .. ECR + GitHub OIDC (불변 subject claim 함정)
  9. 시크릿 ........... SSM Parameter Store (Lambda는 자동 주입이 없다)
 10. 함수·Function URL  최소권한 역할 + 권한 statement 2개 함정
 11. 라이브 검증 ...... 4툴 실호출 + 보안 경계 3종 + 감사 로그
 12. 🔴 토큰 노출 발견 . 공개 저장소의 플레이스홀더가 운영 토큰이었다 → 교체
 13. 후속 과제 8건 .... fail-open·비ASCII 500·감사 분류·의존성 게이트
 14. 커스텀 도메인 .... ACM + API Gateway (CloudFront는 MCP와 비호환)
```

---

## Stage 1 — 서버 구축

### 1. FRD 확정 — "물어봐서 좁히기"

배포 타깃·GitHub 연동 방식·인증 방식·초기 툴셋·저장소 구조를 **먼저 질문으로
확정**한 뒤 FRD를 썼다. 확정 사항은 EARS 형식 AC로, 수치·형식은 CTR로,
실패 모드는 EDGE로 ID를 붙였다. 이 ID들이 이후 모든 Task의 `traces`가 된다.

핵심 결정:
- MCP Python SDK **v2(`mcp` 2.1.1)** — v1의 `FastMCP`는 import 경로 자체가
  사라졌으므로 웹의 v1 예제는 동작하지 않는다 (FRD §7)
- **Python 3.14는 선호가 아니라 하한** — `client.py`가 PEP 758 문법을 쓰므로
  3.13에서는 import 자체가 SyntaxError다
- 정적 Bearer로 시작하되 교체 지점을 `verifier.py` 한 파일에 국소화(`DSN-001`)

### 2~3. 바닥부터 위로

`types.py`·`config.py` → `auth/`(인증·인가) → `audit/` → `tools/guard.py` →
GitHub 어댑터 → 4툴. **인증·인가·감사를 GitHub 어댑터보다 먼저** 완성해
각 계층이 독립적으로 검증되게 했다.

이 단계에서 문서를 실측으로 고친 사례:
- `AuthSettings`의 URL을 `AnyHttpUrl`로 감싸면 **후행 슬래시가 붙어**
  `AC-002-4`(RFC 9728 `resource` 일치)가 깨진다 → plain `str` 전달
- 스코프 부족은 "인증 실패"가 아니라 **403 `insufficient_scope`** (401
  `invalid_token`과 다른 상태코드) → `EDGE-010` 수정
- `MCP_PUBLIC_URL`의 **경로가 well-known 경로를 결정**한다 → 기동 시 검증 추가

### 4. 컨테이너 · CI

로컬에 Docker가 없어 이미지 검증은 **CI가 1차 검증자**다(FRD §7). arm64
멀티스테이지, 비root(uid 10001), 시크릿은 전부 런타임 주입.

### 5. 보안 리뷰 — 🔴 Critical 재현

`repo`만 인가 판정을 받고 `path`는 검증되지 않아
`read_file(repo=<허용>, path="../../../victim/secret/contents/.env")`가
**allowlist를 완전히 우회**했고, 감사에는 `outcome=ok`로 남았다.
`path="../../../../installation/repositories"`로 **4툴 표면 밖 임의 GitHub
GET**에도 도달했다. → `EDGE-013`(세그먼트 검증 + 정규화 후 URL prefix assert),
`EDGE-014`(qualifier 주입), `EDGE-015`(취소 전파) 3건 수정.

---

## Stage 2 — 실배포

### 6. 배포 타깃 재결정 — 비용 실측이 설계를 바꿨다

AWS **Price List Query API**로 서울 리전 요율을 실측했다(블로그 아님).

| 구성 | 월 비용 | 내역 |
|---|---|---|
| Fargate + ALB (원안) | **$38** | ALB $16.43 + ALB 공용IPv4 2개 $7.30 = **$23.73(62%)**, 실제 연산은 $8.29 |
| **Lambda + Function URL** | **$0.01** | 프리티어 월 100만 요청 + 400,000 GB-초 **상시 무료** |

비용의 62%가 "트래픽 0에도 24시간 대기하는 고정 진입점"이었고, 이 서버는
읽기 전용 4툴·요청 간 상태 없음·내부 팀 사용이라 **상시 대기가 필요 없다**.

탈락한 대안: **App Runner**(TLS·도메인 내장이라 매력적이었으나 Price List
API로 확인한 제공 리전에 **ap-northeast-2 없음**), **Fargate + Cloudflare
Tunnel**($11.94, 사이드카·외부 의존).

이 결정이 FRD §7의 미결 사항("legacy 레그 확장 방식")을 **`stateless_http=True`**
로 확정했다. 대가는 서버→클라이언트 역채널 상실이고, Stage 1 툴에는 무해하며,
**Stage 3(Slackbot elicitation)이 재검토 트리거**다.

### 7. 런타임 전환

SDK 소스를 직접 읽어 확인한 두 사실:
- `streamable_http_app(stateless_http=, json_response=)`가 **완전히 독립**이다
  — `stateless_http`는 `Mcp-Session-Id` 발급 여부, `json_response`는 응답
  미디어타입. 4조합 전부 실측해 테스트로 고정했다.
- **`stateless=True`에서도 `session_manager.run()`은 필수**다. 그 안에서 GitHub
  lifespan이 진입하고 task group이 생긴다. "stateless니까 lifespan 불필요"는 오독.

**Lambda Web Adapter**가 Runtime Interface Client를 자체 포함하므로
`python:3.14-slim-trixie`를 그대로 쓴다 — 이 성질이 없었다면
`public.ecr.aws/lambda/python`으로 옮겨야 했고 거기엔 3.14 태그가 없어
PEP 758 하한과 충돌했다.

### 8. 이미지 공급 경로 — 문서에 없던 함정

`AssumeRoleWithWebIdentity` 거부. 신뢰 정책도 공급자도 정확해 보였다.
추측 대신 **GitHub이 실제로 보내는 클레임을 측정**했다:

```
실제 sub : repo:ridsync@8566036/devoks-mcp-servers@1355671954:ref:...
쓰던 패턴: repo:ridsync/devoks-mcp-servers:*
```

GitHub **immutable subject claims** — 2026-07-15 이후 생성 저장소는 소유자·
저장소 불변 ID가 `@`로 붙는다. **AWS 문서와 사실상 모든 블로그의 구 형식은
신규 저장소에서 깨지고, 오류 메시지는 이유를 전혀 알려주지 않는다.**
thumbprint도 GitHub이 Let's Encrypt로 이전해 블로그의 DigiCert 값은 폐기됐다
→ 인증서 체인에서 실시간 계산.

### 9. 시크릿 — Lambda에는 자동 주입이 없다

`aws lambda create-function`의 옵션은 `--environment`와 `--kms-key-arn`뿐이다
(CLI help 실측). **ECS의 `secrets`/`valueFrom` 같은 SSM 자동 주입이 없다.**
그래서 SSM은 **원본 기록**이고 주입은 스크립트가 한다. Secrets Manager 대신
SSM Standard를 쓴 이유는 4KB까지 무료(시크릿당 $0.40/월 절감).

### 10. 함수 · Function URL — 403의 진짜 원인

정책과 URL 설정이 교과서적으로 정확한데 **모든 요청이 403**이었다.
함수를 직접 호출해 **LWA는 정상임을 먼저 분리**한 뒤 문서 원문에서 찾았다:

> resource-based policy doesn't grant `lambda:invokeFunctionUrl` **and
> `lambda:InvokeFunction`** → 403 Forbidden

유통되는 거의 모든 예시가 앞의 하나만 보여준다(`EDGE-020`). 두 번째는
`lambda:InvokedViaFunctionUrl` 조건으로 **URL 경로에만** 한정해야 한다 —
없으면 일반 Invoke API로도 누구나 호출 가능해진다.

### 11. 라이브 검증

| 항목 | 결과 |
|---|---|
| LWA 기동 | `EXTENSION Name: lambda-adapter State: Ready` + `"server": "uvicorn"` |
| 콜드스타트 | 새 이미지 첫 1회 **8,511 ms**, 이후 **~1,900 ms**, 웜 2~4 ms |
| 메모리 | **증설 무효**(512/1024/1769 MB → 1,923/2,007/1,877 ms), 사용량 116 MB |
| 4툴 실호출 | 전부 성공 (422~1,028 ms) |
| 보안 경계 | 트래버설·allowlist 밖·qualifier 주입 3종 전부 차단 |
| 감사 로그 | 13건, CTR-003 11필드 전량, **`duration_ms=0` = GitHub 호출 없이 차단** |

"콜드스타트엔 메모리를 올려라"는 흔한 처방이 이 워크로드엔 **무효**임을
측정으로 확인했다 → 512 MB 유지는 근거 있는 선택이다.

### 12. 🔴 토큰 노출 — 가장 중요한 발견

운영 토큰이 `dev-local-token-change-me`, 즉 **공개 저장소의 `.env.example`에
게시된 문자열**이었다(sha256 비교 확인). Function URL도 커밋 메시지에 적어
푸시했으니 **공개 URL + 공개 토큰**이었다.

- 즉시 교체(`token_urlsafe(32)`, 43자/256bit), 구 토큰 401 / 신규 200 확인
- 노출 기간 접근 로그 전수 확인 → **외부 IP 1개(우리 검증 트래픽)뿐**
- `.env.bak.*`가 untracked-but-not-ignored여서 `git add .` 한 번에 유효한 PEM이
  공개될 수 있었다 → `.gitignore`에 `.env.*` + `!.env.example`

**근본 원인은 구조적이었다.** `GITHUB_APP_PRIVATE_KEY` 플레이스홀더는 원래부터
파싱에 실패해 배포가 불가능했는데, **토큰 플레이스홀더만 유효했다.** 그래서
복사하면 "동작하는 서버 + 공개된 자격증명 + 아무 신호 없음"이 됐다.
→ `TASK-046`이 32자 하한을 넣고 `.env.example` 플레이스홀더를 **일부러 기동
실패하게** 바꿨다. CI 스모크 토큰도 고정 리터럴에서 매 실행 생성으로 바꿨다.

### 13. 후속 과제

| Task | 내용 |
|---|---|
| `TASK-043` | 비ASCII Bearer가 `compare_digest`에서 500 → bytes 비교로 401 |
| `TASK-044` | 빈 allowlist에서 무의미한 GitHub 왕복 제거 |
| `TASK-045` | 비-str `repo`가 allowlist 검사를 **건너뛰던** fail-open → sentinel |
| `TASK-046` | 토큰 32자 하한 (위 12번) |
| `TASK-047` | 픽스처 `conftest.py` 추출 (Settings 생성 7곳 → 1곳) |
| `TASK-048` | OSV 의존성 감사 CI 게이트 (40개 패키지, 취약점 0건) |
| `TASK-049` | 보안 위반을 `error` → **`denied` + 전용 reason_code** |
| `TASK-050` | 베이스 이미지 CVE — 아래 참조 |

`TASK-049` 중 **추가 발견**: `search_code`의 `repo`도 `f"repo:{repo} {query}"`로
직접 보간되는데 `_split_repo`가 느슨해 `"victim/x OR repo:secret"`이 두 개의
qualifier로 GitHub에 도달했다. 현재 MCP 표면으로는 도달 불가(가드가 정확 일치
allowlist로 인가)지만 2층 방어 원칙에 맞춰 입력 측 검증을 넣었다.

**`TASK-050`은 제 분석이 두 번 틀렸고 측정이 결론을 냈다:**

1. 최초: "`apt-get upgrade`로는 한 건도 줄지 않는다" (Debian 트래커 근거)
2. 정정: OSV가 sqlite3 `+deb13u1`(trixie 보안 업데이트) 수정판을 보여줌 →
   1건 줄어들 것으로 예측
3. **측정: 전혀 변하지 않았다.** 빌드 로그 —
   `trixie-security InRelease` 있음, `0 upgraded, 0 newly installed`.
   → **1번이 맞았고 2번이 틀렸다.** sqlite3 수정판은 이미 설치돼 있고 ECR이
   소스 패키지 버전으로 매칭한 **오탐**이었다.

`perl`(CRITICAL 5건)은 Essential인 `perl-base`로 들어오므로 **제거 불가**이고
수정판도 trixie에 없다. 잔여 위험은 "컨테이너 내부 코드 실행을 전제"하므로
실효 통제는 우리가 통제하는 코드에 대한 게이트(`TASK-048`)다.

> **교훈**: 취약점 스캐너의 버전 매칭은 소스/바이너리 리비전을 구분하지 않을
> 수 있다. "수정판이 있다"는 조회 결과만으로 판단하지 말고 **적용 후 스캔을
> 다시 돌려 비교**해야 한다.

### 14. 커스텀 도메인 — CloudFront는 MCP와 비호환

처음에 CloudFront를 제안했으나 문서 원문이 뒤집었다:

> `PUT`·`POST`를 쓰면 클라이언트가 본문 SHA256을 `x-amz-content-sha256` 헤더로
> 보내야 하며 **Lambda는 unsigned payload를 지원하지 않는다**

MCP Streamable HTTP는 전부 POST다. OAC를 포기해도
`AllViewerExceptHostHeader`가 필수여서 `Host`가 lambda-url 도메인으로 도착해
`MCP_PUBLIC_URL`과 **강제로 갈라진다**(`EDGE-019`).

→ **API Gateway HTTP API**로 전환. 같은 리전 ACM, `Host`가 그대로 도착,
`Authorization` 기본 전달, $1.23/백만 요청. 도메인 없이 기본 엔드포인트로
전수 검증했다(무인증 `POST /mcp`가 **401이고 421이 아닌 것**이 Host 검증
통과의 증거). 통합 타임아웃 실측 **30,000 ms**가 `EDGE-018`의 근거다.

---

## 재현 스크립트

| 파일 | 단계 |
|---|---|
| `infra/01-ecr-and-github-oidc.sh` | ECR + OIDC 공급자 + IAM 역할 |
| `infra/02-secrets.sh` | SSM Parameter Store |
| `infra/03-lambda.sh` | 로그 그룹 + 실행 역할 + 함수 + Function URL |
| `infra/04-custom-domain.sh` | ACM + API Gateway (+ DNS 인수인계) |
| `scripts/audit-dependencies.py` | OSV 의존성 감사 (CI 게이트) |

각 스크립트 머리에 **무엇이 발목을 잡았는지**와 **검증 범위**를 적어뒀다.

## 남은 작업

- `TASK-060` — 가비아 DNS 활성화 + CNAME 2건 (사람 작업)
- FRD §10 미결: 저장소 공개 범위 · 조직 이관 · 정적 Bearer → OAuth
- Stage 3 — Slackbot 연동
