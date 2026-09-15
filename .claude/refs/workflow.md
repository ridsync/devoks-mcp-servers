# Workflow

워크플로우 규칙은 작업 진행 절차와 기준을 정의하며, 테스트 작성, 코드 리뷰, Git 컨벤션, 배포 방식 등 개발 과정 전반에 필요한 핵심 지침을 요약합니다. 모든 팀원은 일관된 품질과 효율을 위해 본 문서의 내용을 준수해야 합니다.

---

## Testing

> 명령어: `CLAUDE.md > Commands` 참조

- 새 기능: 관련 테스트 작성 권장. 미작성 시 이유를 완료 메시지에 명시.
- 기존 테스트 깨짐: 수정 필수
- 환경: 프로젝트가 채택한 테스트 러너·설정 (`project-convention.md` 참고)

---

## Code Review

> 상세 규칙: `.claude/refs/code-review.md` (SSOT)

---

## Git Convention

> 상세 규칙: `.claude/refs/git-convention.md` (SSOT)

### Pre-commit

**Lefthook** (`lefthook.yml`):
- `lint`: staged 파일 ESLint 검사
- `format`: staged 파일 Prettier 포맷
- 자동 stage fixed files

---

## Deploy (CI/CD)

배포 파이프라인(빌드 명령, 배포 대상, 환경 변수 등)은 프로젝트·스택마다 다르므로 이 문서에서 고정하지 않는다.
프로젝트의 `.claude/CLAUDE.md` 또는 `.claude/rules/project-convention.md`에 실제 배포 명령·환경을 명시한다.

- **모드/타겟을 명시적으로 지정** — fallback/auto-inference 금지 (예: 디버그/릴리스 모드를 인자로 강제)
