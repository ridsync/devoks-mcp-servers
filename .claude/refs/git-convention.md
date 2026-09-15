---
description: 브랜치·커밋 메시지 Conventional Commits 관행, 타입별 권장 매핑, 브랜치 권장 패턴(비강제)
---

# Git · Branch · Commit

이 파일이 브랜치명·커밋 메시지 규칙의 **SSOT**이다. [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)을 따른다.

## 커밋 형식

- **형식**: `<type>[optional scope]: <description>`
- **설명**: 한국어 문장.
- **Breaking change**: `feat!:` / `fix!:` 또는 본문·푸터에 `BREAKING CHANGE: …`

## Scope

- **정의(Conventional Commits)**: 스코프는 커밋에 **추가 맥락 정보(additional contextual information)**를 제공한다. 타입 뒤 괄호 안에 코드베이스의 한 구역을 나타내는 **명사**로 쓴다 (예: `fix(user): …`, `fix(payment): …`, `feat(react): …`).
- 커밋에 주요한 변경사항에 대한 대표 명사로서 추가적인 맥락을 제공한다.
- 만약 로직은 변경사항이 없고, 전역 UI변경사항이면 `ui`와 같은 단순 명사를 쓴다.

## 타입별 권장 매핑

각 타입은 **언제** · **예** · **헷갈릴 때** 순으로 본다.

### `feat`

- **언제**: 사용자·운영자에게 **새 동작·새 기능**이 보이거나, API/계약상 **기능 추가**.
- **UI**: 새 화면, 새 버튼 동작, 새 설정 항목, 라우트 추가.
- **예**: `feat(auth): 소셜 로그인 버튼 추가`
- **헷갈림**: 기능인가 스타일인가 → **동작이나 데이터 흐름이 바뀌면** `feat`. 스타일만이면 `style`.

### `fix`

- **언제**: **버그 수정** — 의도와 다른 동작, 크래시, 잘못된 계산/상태.
- **UI**: 잘못된 표시·레이아웃이 **로직/조건 버그**일 때.
- **예**: `fix(payment): 재시도 후 결제 상태 누락 수정`
- **헷갈림**: 리팩터이면서 버그도 고침 → 사용자 관점이 **고장 수리**면 `fix`, 동작 동일·구조만 정리면 `refactor`.

### `docs`

- **언제**: README, 규칙 문서, 주석만으로 **의도·사용법**을 바꿀 때 (런타임 코드 변경 없음).
- **예**: `docs: git_convention 브랜치 권장 패턴 보강`

### `style`

- **언제**: **의미·동작 불변** — 포맷/린트/세미콜론/Prettier, 읽기 좋은 변수명 (동작 동일).
- **UI**: 색·간격·타이포만 조정하고 **이벤트·조건·데이터는 그대로**.
- **예**: `style(ui): 메뉴 카드 간격 토큰 정렬`
- **헷갈림**: 스타일 변경인데 접근성 때문에 **포커스 동작이 새로 생기면** → 해당 변경은 `feat`로 쪼개거나 본문에 명시.

### `refactor`

- **언제**: **외부 관측 동작 동일** — 구조·이름·모듈 분리·중복 제거.
- **예**: `refactor(auth): 토큰 갱신 로직 분리`
- **헷갈림**: 목적이 성능만이면 `perf`가 더 정확.

### `perf`

- **언제**: **성능** — 렌더 최적화, 불필요 리렌더 제거, 쿼리/루프 개선 (동작은 동일).
- **예**: `perf(chart): 대용량 포인트 시 가상화 적용`

### `test`

- **언제**: 테스트 코드 추가/수정, 스냅샷·모킹·커버리지.
- **예**: `test(payment): 결제 금액 유효성 검증 케이스 추가`

### `build`

- **언제**: Vite, Gradle, 번들 설정, `package.json` 스크립트 중 **빌드 파이프라인**.
- **예**: `build: Android 릴리스 서명 설정 분기 추가`

### `ci`

- **언제**: GitHub Actions, 워크플로 YAML, CI 전용 스크립트.
- **예**: `ci: PR 시 eslint job 분리`

### `chore`

- **언제**: 위 타입에 **딱 맞지 않는 잡무** — 의존성 범프, 생성물·메타데이터 등.
- **원칙**: `build`/`ci`와 겹치면 **빌드는 `build`, CI는 `ci`** 우선, 나머지를 `chore`.

### `revert`

- **언제**: 이전 커밋 되돌리기. 필요 시 푸터에 `Refs: <sha>` (공식 FAQ 예시).
- **예**: `revert: 다크모드 전환 실험 롤백`

## 브랜치명 (권장 · 비강제)

- **원칙**: 이름만으로 **의도(기능/수정/긴급/릴리스)**를 추론할 수 있게 한다. 아래 패턴을 **우선**하되, CI/훅으로 **강제하지 않는다** (문서·리뷰 가이드 수준).
- **권장 접두사**: `feature/` 또는 `feat/` (신규 기능), `fix/` (버그), `hotfix/` (운영 긴급, 팀이 구분할 때만), `chore/` (선택), `release/` (예: `release/1.2.0`).
- **형식**: 소문자, 단어는 하이픈(`kebab-case`). 이슈 티켓이 있으면 접두에 붙여도 된다 (예: `fix/PROJ-42-auth-token-refresh`).
- **참고**: [Conventional Branch](https://conventional-branch.github.io/) (커뮤니티 관행, 필수 아님).

## 자동화 (선택)

- 커밋 메시지 검증은 기본 **미설정**. 프로젝트에 Lefthook/Husky 등 커밋 훅 설정이 있다면 `commit-msg` + commitlint 등을 팀 합의로 추가할 수 있다.

## 참고 링크

1. [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/)
2. [Semantic Versioning](https://semver.org/)
3. [@commitlint/config-conventional](https://github.com/conventional-changelog/commitlint/tree/master/%40commitlint/config-conventional)
4. [Angular Commit Message Guidelines](https://github.com/angular/angular/blob/main/CONTRIBUTING.md#-commit-message-guidelines)
5. [git interpret-trailers](https://git-scm.com/docs/git-interpret-trailers)
6. [Conventional Branch](https://conventional-branch.github.io/) (선택)
