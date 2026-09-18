# Build vs Buy — 메신저 완전형 에이전트 솔루션 비교 참고자료

> **목적:** Claude Tag(Claude in Slack)처럼 메신저에 바로 붙는 **완전형(turnkey) 에이전트 솔루션**이
> 나올 때마다, 우리가 자체 구축한 `servers/management` + `servers/slackbot` 조합을 계속
> 직접 구현으로 유지할지 판단하기 위한 비교 기록이다. 특정 제품 하나를 평가하는 문서가 아니라
> **"솔루션 사용 vs 직접 구현" 판단축**을 남겨, 유사 솔루션(다른 메신저의 완전형 에이전트 앱 등)이
> 나왔을 때도 같은 틀로 재사용하기 위한 것이다.
>
> - 최초 작성: 2026-09-18
> - 비교 대상: Claude Tag(공개 베타, Anthropic 공식 문서 기준)
> - 우리 쪽 근거: [`../.claude/workspace/slackbot-integration-20260914/FRD.md`](../.claude/workspace/slackbot-integration-20260914/FRD.md)

---

## 1. 비교 대상 개요

### 1.1 Claude Tag (Claude in Slack)

Anthropic이 제공하는 공식 엔터프라이즈 제품(현재 공개 베타). Slack에서 `@Claude` 멘션만으로
호출되며, 서버·샌드박스·MCP 커넥터 인프라를 Anthropic이 전부 관리한다. Team/Enterprise
플랜 전용.

### 1.2 우리 구현 (Management MCP + Slackbot)

`AgentClient → Management MCP → Knowledge/Runtime/Business` 3계층 게이트웨이를 우리가
직접 소유·운영한다. Slackbot은 그 게이트웨이에 붙는 **클라이언트 중 하나**일 뿐이고,
Claude Code CLI·Codex 등 다른 클라이언트도 같은 게이트웨이를 공유한다(자세한 아키텍처는
[README.md](../README.md), [slackbot-guide.md](slackbot-guide.md) 참고).

---

## 2. 유사 솔루션 지형 (2026-09-18 기준)

Claude Tag는 유일한 사례가 아니다. Anthropic/OpenAI 외에 Slack(Salesforce) 자체와
제3자 벤더도 유사 카테고리 제품을 이미 GA로 운영 중이다.

| 제공자 | 제품 | 핵심 특징 | MCP 지원 | 상태 |
|---|---|---|---|---|
| OpenAI | ChatGPT app for Slack / ChatGPT Agents App | 메시지 검색·요약·인라인 답변, 관리자 승인 시 채널 참여·리마인더 생성 | Agents App 세부는 소스 접근 제한(403)으로 미확인 | GA |
| Slack(Salesforce) 자체 | Slackbot 에이전틱 개편(2026-03-31) | 회의 전사, 데스크톱 활동 모니터링, 경량 CRM 등 30개 기능 | 지원(제3자 도구 실행) | Business+/Enterprise+ GA, Free/Pro는 2026-04부터 제한적 롤아웃 |
| Dust.tt | Slack 에이전트 | Slack·Notion·GitHub 등 100+ 커넥터, 작업 완료 후 Slack 자동 업데이트 | MCP를 확장 레이어로 채택 | GA |
| Glean | Slack 에이전트 | 사내 전체(문서·티켓·Slack) 통합 검색, 워크플로 자동화 | 자체 MCP 서버 제공 | GA |
| Perplexity | Computer for Enterprise(`@computer`) | Claude·Gemini·GPT-5·Grok 라우팅("Model Council"), 400+ 커넥터 | 명시 안 됨 | GA, $325/seat/월 |
| (참고) Microsoft Copilot/Copilot Studio | — | Teams 대상, Slack 직접 경쟁재 아님 | Teams는 지원, M365 Copilot Chat은 2026-06 기준 미지원 | — |

**특이사항:**

- Slack(Salesforce) 자체 에이전틱 Slackbot이 **Anthropic Claude 기반으로 동작**한다고
  공식 발표됨(TechCrunch, Salesforce TDX 2026) — "Slack 네이티브 vs Claude Tag" 구도가
  실제로는 "Anthropic 공식 채널 vs Claude 기반이지만 Salesforce가 운영하는 채널"에
  가까울 수 있다.
- Dust.tt·Glean은 이미 MCP를 정식 채택하고 있어, 우리 Management MCP를 이들 제품의
  커스텀 커넥션으로 붙이는 것도 §5 하이브리드 옵션과 같은 성격의 대안 경로가 될 수 있다.

## 3. 기능/특성 비교

| 항목 | Claude Tag | 우리 구현 |
|---|---|---|
| 호출 방식 | Slack `@Claude` 멘션, 설치 불필요 | 자체 Lambda(handler/worker)로 Slack Events API 수신 |
| DM | 지원(개인 계정 실행, 조직 비용 미청구) | 미구현(채널 멘션만) |
| 스레드 컨텍스트 유지 | 지원 | 미구현 — 매 질문 독립 처리(`EDGE-SB-014`, 의도적 보류) |
| 파일/이미지 첨부 | 지원 | 미구현 |
| 코드 실행·PR 생성 | 지원(에피메랄 샌드박스에서 clone→branch→push→PR) | 미구현 — GitHub 툴 4개 전부 읽기 전용([roadmap.md](roadmap.md)의 미착수 아이디어와 겹침) |
| 정기 작업(Routine) | 지원(스케줄·채널 감시·리포지토리 이벤트 트리거) | 미구현 |
| MCP 커스텀 커넥션 | 지원 명시(인증 방식은 공식 문서 미상세) | Claude API MCP 커넥터로 이미 구현·운영 중(`DSN-SB-002`) |
| RBAC/인가 | Enterprise 한정 role-capability, Anthropic 관리 | `MCP_ROLE_TOOLS` + Guard로 role×tool×repo 자체 구현, 사람별 MCP 토큰으로 `client_id` 감사 추적 |
| 감사 로그 | 관리자용 조회 화면 존재, 필드/보관기간 비공개 | JSON Lines 1줄/호출 전량 기록(`CTR-003`/`CTR-SB-008`), 필드 스펙을 우리가 직접 통제 |
| 데이터 보존 | 트랜스크립트 전부 Anthropic 보관, **ZDR/CMEK 정책 조직은 사용 불가** | 자체 인프라라 데이터 소재·보존 정책을 직접 통제 |
| 지식 소스 연동 | Connections(자격증명) + 공개 채널 검색 + 웹 접근 | GitHub Knowledge 어댑터 1개(읽기 전용), 어댑터 레지스트리로 확장 가능(Notion 등 미착수) |
| 운영 비용 | 플랜 비용에 포함, 별도 인프라 운영 불필요 | 질의당 약 $0.05~0.15 + AWS 인프라 비용, 직접 계측 필요(`EDGE-SB-017`) |
| 배포 독립성 | Anthropic 종속(Bedrock/Vertex 등 불가) | 완전 자체 인프라, 배포 파이프라인 직접 소유 |
| 재사용성 | Slack/claude.ai 생태계 전용 | Management MCP를 Claude Code·Codex 등 여러 클라이언트가 공유 |

---

## 4. 의사결정에 쓸 판단축

유사 완전형 솔루션이 나왔을 때, 아래 축으로 먼저 점검한다. **하나라도 "자체 구현" 쪽에
해당하면 완전 대체는 어렵고, 하이브리드(§5)를 검토한다.**

| 판단축 | 완전형 솔루션 채택 쪽 | 자체 구현 유지 쪽 |
|---|---|---|
| 데이터 소재/보존 요건 | ZDR·CMEK 등 규제 요건 없음 | ZDR/CMEK 필요, 또는 데이터 소재를 계약상 통제해야 함 |
| 사용 클라이언트 범위 | 해당 메신저 하나로 충분 | Claude Code·Codex 등 여러 클라이언트가 같은 지식 소스를 공유해야 함 |
| 인가 세밀도 | 조직 단위 role-capability로 충분 | role×tool×repo처럼 세밀한 정책이 필요 |
| 비용 구조 | 플랜 비용 안에서 예측 가능 | 사용량 폭주 리스크를 직접 통제해야 함 |
| 유지보수 리소스 | 인프라 운영 인력을 아끼고 싶음 | 이미 인프라·CI/CD 파이프라인을 보유·운영 중 |
| 필요 기능 성숙도 | 파일 처리·코드 실행 샌드박스·정기 작업 등이 바로 필요 | 읽기 전용 조회만으로 충분하거나, 직접 만들 가치가 있는 도메인 특화 기능 |

---

## 5. 하이브리드 옵션 (제안)

완전 대체/완전 자체 구현 외에, **"Slack UX는 완전형 솔루션에, 지식 게이트웨이는 자체
Management MCP에"** 맡기는 조합도 가능하다.

- Claude Tag의 **커스텀 MCP 커넥션**에 `mcp.devoks.kr`을 등록하면, 우리 `servers/slackbot`
  (handler/worker Lambda, DynamoDB 멱등성, 서명 검증 등)을 걷어내고 Slack 표면 운영 부담을
  Anthropic에 넘길 수 있다. Management MCP는 그대로 지식 게이트웨이로 유지되므로 Claude
  Code·Codex 쪽 연동은 영향받지 않는다.
- **트레이드오프:** 데이터가 Anthropic에 보관되므로 §4의 "데이터 소재" 요건이 걸리면 이
  옵션은 배제된다. 또한 Claude Tag의 커스텀 커넥션 인증 방식(Bearer 토큰 지원 여부 등)이
  공식 문서에 미상세라, 우리의 사람별 MCP 토큰(`CTR-SB-006`) 매핑을 그대로 재사용할 수
  있는지는 **검증이 필요**하다.
- 채택 전 확인할 것: Claude Tag 커스텀 커넥션의 인증 방식 공식 문서화 여부, per-person
  토큰 전달 가능 여부, 우리 쪽 `MCP_ROLE_TOOLS` 인가 정책을 그대로 존중하는지.

---

## 6. 재검토 트리거

아래 중 하나가 발생하면 이 문서를 다시 본다.

- Claude Tag가 공개 베타 → GA로 전환되며 가격/기능이 확정될 때.
- Claude Tag 커스텀 MCP 커넥션의 인증 방식이 공식 문서화될 때(§5 하이브리드 옵션 검증 가능해짐).
- [roadmap.md](roadmap.md)의 "GitHub 쓰기 권한" 작업에 실제로 착수하는 시점(완전형 솔루션이
  이미 제공하는 기능과 중복 투자인지 재확인).
- Slack 워크스페이스의 실사용량이 현재 대비 크게 늘어 자체 인프라 운영 비용/부담이
  유의미해질 때.

---

## 7. 확인되지 않은 사항 (공식 문서 기준)

아래는 조사 시점(2026-09-18) 공식 문서에서 상세가 확인되지 않은 항목이다 — 실제 도입
검토 시 Anthropic 문서·세일즈에 재확인이 필요하다.

- 커스텀 MCP 커넥션의 구체적 인증 메커니즘(Bearer/OAuth 등).
- 감사 로그의 필드 상세·보관 기간.
- 세션 수명의 정확한 기준(공식 문서 표현상 "약 1시간 또는 1일" 수준으로만 언급).
- ChatGPT Agents App in Slack의 세부 기능·MCP 지원 여부(소스 페이지 접근 제한, 403).
- Perplexity Computer for Enterprise의 MCP(또는 유사 프로토콜) 지원 여부(문서 미기재).

### 출처 — Claude Tag

- https://claude.com/docs/claude-tag/overview.md
- https://claude.com/docs/claude-tag/concepts/how-it-works.md
- https://claude.com/docs/claude-tag/concepts/security-and-data.md
- https://claude.com/docs/claude-tag/admins/customize.md

### 출처 — 유사 솔루션 지형(§2)

- https://help.openai.com/en/articles/12462158-chatgpt-app-in-slack
- https://help.openai.com/en/articles/20001199-chatgpt-agents-app-in-slack
- TechCrunch(2026-03-31), Salesforce TDX 2026 발표 — Slack 에이전틱 Slackbot 개편
- https://docs.dust.tt/docs/slack-mcp
- https://glean.com/agents/slack , https://docs.glean.com
- VentureBeat, Slack Marketplace(A07NV1D07QT) — Perplexity Computer for Enterprise

---

## 더 읽을 것

- [slackbot-guide.md](slackbot-guide.md) — 우리 slackbot의 처리 흐름·멱등성·배포 인프라
- [roadmap.md](roadmap.md) — GitHub 쓰기 권한 등 아직 FRD로 확정되지 않은 확장 아이디어
- [`../.claude/workspace/slackbot-integration-20260914/FRD.md`](../.claude/workspace/slackbot-integration-20260914/FRD.md) — 우리 slackbot의 요구사항·설계·계약
