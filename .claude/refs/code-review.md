---
description: 코드리뷰 규칙
---

# Code Review Guide

AI(Claude Code, Cursor, Copilot 등)와 사람이 코드리뷰 시 일관되게 참고하는 SSOT 가이드.
일반 코드와 AI 생성 코드 모두에 적용한다.

참고: [Vibe Coding – Code Review Guidelines](https://docs.vibe-coding-framework.com/best-practices/code-review-guidelines), [CodeRabbit – Review instructions](https://docs.coderabbit.ai/guides/review-instructions),
[OWASP Secure Code Review Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Secure_Code_Review_Cheat_Sheet.html), [OWASP Secure Coding Practices Quick Reference](https://owasp.org/www-project-secure-coding-practices-quick-reference-guide/stable-en/02-checklist/)

---

## 1. Review Philosophy

- **제로 트러스트**: 코드를 신뢰하지 않고 모든 로직을 의심하며 검토한다.
- **외부 유입 코드 격리**: 서드파티 통합·외부 기여 코드가 포함되면 격리된 환경에서 우선 검증한다.
- **리뷰 품질 한계 관리**:
  - (사람) 한 세션에서 400줄 이상 또는 60분 이상이면 구간을 분할해 재검토한다.
  - (AI 에이전트) diff 파일이 5개 이상이거나 변경 줄이 300줄을 초과하면
    레이어(구조 → 로직 → 보안) 또는 파일 단위로 분할해 순차 검토한다.

---

## 2. Pre-Review Gate

수동 리뷰 전에 최소 품질 게이트를 통과해야 한다.

- [ ] 프로젝트의 lint/format 도구 기준 위반 없음
- [ ] 프로젝트의 타입 검사 도구가 있다면 오류 없음

---

## 3. C.L.E.A.R. Framework

| 단계 | 내용 |
|------|------|
| **C - Context** | 요구사항·시스템 내 위치·변경 의도를 먼저 파악한다. |
| **L - Layered Examination** | 구조 -> 로직 -> 보안 -> 성능 -> 유지보수 순으로 점검한다. |
| **E - Explicit Verification** | 핵심 로직을 샘플 데이터와 실패 시나리오로 명시 검증한다. |
| **A - Alternative Consideration** | 대안과 트레이드오프를 비교해 선택의 타당성을 확인한다. |
| **R - Refactoring Recommendations** | 우선순위(High/Medium/Low)와 파일 위치를 포함한 실행 가능한 개선안을 제시한다. |

---

## 4. Layered Examination (Level 1 -> 5)

한 레이어에서 Critical/High 문제가 나오면 다음 레이어로 넘어가기 전에 정리한다.

### Level 1 – Structure / Architecture

- 전체 디렉토리/모듈 구조가 일관적인가?
- 모듈 경계와 책임 분리가 명확한가?
- 이 변경이 **기존 호출자의 계약**(시그니처·반환 형태·에러 계약·이벤트/페이로드 스키마)을 깨는가?
  깬다면 호출부를 전수 확인한다 — 단순 시그니처 불일치는 §2 타입 검사가 잡으므로,
  리뷰는 **타입이 통과해도 깨지는 것**(반환값 의미 변화, throw→null 같은 에러 계약 전환,
  이벤트 페이로드 필드 증감, 옵셔널→필수 전환)에 집중한다.
- 에러 처리 전략(throw/return/log)이 일관적인가?
- 평가/실행 분리가 프로젝트 아키텍처 규칙과 충돌하지 않는가?

### Level 2 – Core Logic / Algorithm

- 비즈니스 로직이 요구사항과 일치하는가?
- 상태 변이와 데이터 변환이 정확한가?
- 상태 관리(전역/로컬, 동기/비동기) 방식이 적절한가?
- 의도치 않은 Side Effect(이벤트 발행, 외부 API 호출, 전역 상태 변경)가 없는가?
- 동기/비동기 흐름에서 순서 보장이 필요한 구간이 안전한가?
- 반복·연속 호출되는 핸들러가 이전 호출의 결과를 잃지 않는가? (C1)
- 비동기 완료 후 화면 상태를 되쓰는 경로가, 그 사이 들어온 더 새로운 입력을 덮어쓰지 않는가? (C2)

> 위 두 항목과 §5·§6의 반복 입력 관련 체크는 `.claude/refs/interaction-integrity.md`가 SSOT다.
> **§1 적용 대상 판정(두 조건 AND)을 먼저 통과한 경우에만** 지적한다 — 표시만 바꾸는 조작·I/O 없는 단발 조작은 대상이 아니다.
> 대상이면 §2에서 조작의 의미(누적 / 최종값 / 단발)를 분류해 **해당하는 결함만** 본다.

### Level 3 – Security / Edge Cases

- 외부/사용자 입력 검증 누락이 없는가? (타입, 범위, 포맷)
- SQL 인젝션 위험(문자열 결합 쿼리) 없이 파라미터화되어 있는가?
- XSS 위험(미검증 입력 렌더링, 미이스케이프 출력)이 없는가?
- 하드코딩 자격증명(비밀번호, 토큰, 키)이 없는가?
- 인증/인가 검사가 보호 연산 **이전**에 수행되는가?
- 경로 탐색, 명령어 인젝션, ReDoS 위험은 해당 코드에서 최소한으로 확인했는가?
- 경계값/빈 목록/타임아웃/예외 경로에서 민감 정보 노출이 없는가?

### Level 4 – Performance / Efficiency

- 불필요한 연산·렌더·네트워크 요청이 없는가?
- 사용자가 연속으로 트리거할 수 있는 동작이 매 호출마다 I/O(저장·네트워크)를 발생시키지 않는가? (C3)
- 쿼리/API 호출이 적절히 최적화되어 있는가?
- 메모리/연결/구독 등 리소스가 적절히 해제되는가?

### Level 5 – Style / Maintainability

- 네이밍이 도메인과 팀 규칙에 맞는가?
- 코딩 표준(포매팅, 파일/함수 길이)을 따르는가?
- 자명한 코드(이름만으로 의도가 드러나는 상수·함수·getter 등)에 불필요한 주석(JSDoc 포함)이 남아있지 않은가? 남아있는 주석은 코드만으로 파악 불가능한 "왜"·히스토리를 담고 있는가? (기준: 프로젝트 Comment Rules — `.claude/rules/project-convention.md`)
- 읽는 사람이 흐름을 따라가기 쉬운가?

---

## 5. Detailed Analysis

### Architecture & Design

- [ ] 프로젝트의 architecture patterns를 따르고 있는가?
- [ ] 컴포넌트가 feature structure에 적절히 구조화되어 있는가?
- [ ] 관심사 분리가 명확한가? (components, hooks, APIs, queries)

### Code Quality

- [ ] 읽는 사람이 흐름을 빠르게 파악할 수 있는가?
- [ ] Code smells나 anti-patterns가 있는가?
- [ ] DRY를 따르고 있는가?
- [ ] 변수와 함수 이름이 명확한가?
- [ ] 코드 복잡도(중첩 조건, 함수 길이, 분기 수)가 과도하지 않은가?
- [ ] 중복 로직이 분리되어 재사용 가능한 구조인가? 읽는 사람이 흐름을 빠르게 파악할 수 있는가?
- [ ] 자명한 코드에 불필요한 주석(주석 남발)이 없는가? 남은 주석은 "왜"를 설명하는가?

### Performance

- [ ] (컴포넌트 기반 UI 프레임워크 사용 시) 불필요한 re-render가 있는가? Memoization이 필요한가?
- [ ] Code splitting이나 lazy loading 기회가 있는가?
- [ ] API 호출이 최적화되어 있는가?
- [ ] 연타·연속 입력이 가능한 경로에서 I/O가 코얼레싱되는가? 입력마다 저장/요청이 나가지 않는가? (C3)

### Best Practices

| 영역 | 체크 항목 |
|------|----------|
| UI 프레임워크 | (해당 시) Hooks/lifecycle, dependency 배열 등을 프레임워크 규칙대로 사용하고 있는가? |
| State Management | 목적에 맞는 상태 관리 방법이 사용되었는가? |
| Error Handling | 에러가 적절히 catch·처리되는가? |
| Accessibility | 적절한 ARIA labels, 키보드 네비게이션이 지원되는가? |
| Testing | 유틸 함수,리엑트 훅,컴포넌트등에 테스트코드가 작성되었는가? Edge cases가 커버되는가? |

---

## 6. Component Type Checklists

### Authentication / Authorization

- [ ] 비밀번호·토큰이 평문으로 로그·응답에 포함되지 않는다.
- [ ] 인증 실패 시 안전한 에러 메시지만 노출.
- [ ] 권한 검사가 보호된 연산 **이전**에 수행된다.
- [ ] 토큰 만료·갱신·저장 방식이 보안 권장사항을 따른다.

### Data Access

- [ ] 쿼리는 파라미터화/prepared statement 사용.
- [ ] 필요한 컬럼만 조회, 인덱스 고려.
- [ ] N+1 쿼리 없거나 배치/조인으로 해소.
- [ ] 트랜잭션으로 묶여 있고 롤백 처리 있음.
- [ ] 대량 결과는 페이지네이션·스트리밍으로 제한.

### API Endpoint

- [ ] 입력의 타입·형식·범위 검증.
- [ ] 인증·인가 미들웨어/가드 적용.
- [ ] 응답 형식 일관, 에러 시 민감 정보 미포함.
- [ ] 적절한 HTTP 상태 코드와 에러 메시지.
- [ ] 중복 제출·클라이언트 재시도로 같은 요청이 두 번 도착해도 결과가 한 번만 확정되는가? (멱등성 — C4)

### UI Component

- [ ] 시맨틱 HTML·ARIA 접근성 요구사항.
- [ ] 사용자 입력 검증·이스케이프 (XSS 방지).
- [ ] 로딩·에러·빈 상태 정의 및 표시.
- [ ] 디자인 시스템·스타일 가이드 준수.
- [ ] 키보드·포커스 상호작용 동작 확인.
- [ ] 빠른 반복 입력에서 입력 횟수만큼 값이 정확히 반영되는가? 응답 대기 중에도 최신 입력을 보여주는가? (C1 · C2)

### Build / Lint Verification

- [ ] 프로젝트의 lint 도구 오류 없음.
- [ ] 프로젝트의 테스트 스위트 통과.
- [ ] 클린 빌드 성공.

---

## 7. AI-Generated Code Considerations

| 특성 | 대응 |
|------|------|
| **맥락 부족** | 원본 프롬프트·요구사항을 먼저 확인, 시스템 내 위치 파악 후 검토. |
| **낯선 패턴** | 팀 패턴(Provider/Feature 분리, 계약 검증)과 비교, 필요 시 대안 고려. |
| **대량 생성** | 레이어별·파일별로 나누어 검토, 고위험 부분 우선. |
| **겉보기 신뢰** | 로직·보안·엣지 케이스를 명시적으로 검증. |
| **기존 시스템 통합** | 연동 지점에서 계약·에러 전파·성능 영향 확인. |

---

## 8. Severity Classification

| 심각도 | 대상 | 예시 |
|--------|------|------|
| **Critical** | 보안 취약·데이터 손실·시스템 오류 | SQL 인젝션, 민감 정보 노출 |
| **High** | 보안·정확성·심각한 버그 | rate limiting 누락, 잘못된 비즈니스 로직 |
| **Medium** | 유지보수·일관성·성능 | 중복 코드, 과도한 re-render |
| **Low** | 스타일·문서·코스메틱 | 네이밍, 포맷팅, 자명한 코드에 붙은 불필요한 주석 |

형식: "현재 문제 → 권장 방향 → 해당 위치(`file_path:line_number`)"

---

## 9. Path-Based Review Focus

| 경로 패턴 | 검토 초점 |
|-----------|-----------|
| `**/context/**` | 네트워크·상태·계약 검증·에러 전파 |
| `**/*.jsx` | UI 컴포넌트 체크리스트 (접근성, 로딩/에러 상태, 디자인 시스템) |
| `**/model/**` | SSOT 준수, 도메인 규칙 |
| `**/common/**` | 범용성, 사이드이펙트 격리 |

---

## 10. Review Pitfalls

| 함정 | 예방 |
|------|------|
| 표면적 검토 | 문법·포맷만 보지 말고, 레이어별 검토와 체크리스트 적용. |
| "AI 코드라 맞을 것" | 로직·보안·엣지 케이스를 직접 설명·추적. |
| 자동화 과신 | 자동화 결과는 참고로만 사용하고, 최종 판단은 수동 리뷰로 확정. |
| 맥락 무시 | 요구사항·시스템 내 위치 먼저 파악. |
| 테스팅 | 컴포넌트,유틸,훅 등 주요 비즈니스 로직이 포함된 함수(클래스)의 테스트 코드 필요성 및 생성 여부 검토. |
| 보안 검토 생략 | 입력 검증·인증/인가·에러 메시지 노출 항상 점검. |
| 리뷰 피로 | 대량 변경은 구간별로 나누고, 고위험 우선 검토. |
| 개발 머신 기준 판정 | 반복 입력 결함(`interaction-integrity.md` §1 적용 대상)은 저사양 기기·느린 I/O에서만 드러난다. 로컬 재현 여부와 무관하게 코드 경로로 판정한다 — "로컬에서 잘 됨"은 근거가 아니다. |
