# Slackbot 운영 런북

이 문서는 `.claude/workspace/slackbot-integration-20260914/PLAN.md`의 `TASK-032`(Slack
Event Subscription URL 등록 확인) / `TASK-035`(Claude API 비용 가드) / `TASK-036`(E2E
검증)이 공유하는 산출물이다. 실제 배포된 `slack-handler`/`slack-worker` Lambda를 대상으로
한 운영 조회 방법과, 각 태스크의 실측 검증 기록을 남긴다.

## AWS 리소스 참조

- AWS CLI profile: `devoks`(계정 `703630528452`), region: `ap-northeast-2`
  (⚠️ `default` 프로파일은 이 프로젝트와 무관한 다른 계정을 가리키므로 반드시
  `export AWS_PROFILE=devoks`를 먼저 설정할 것)
- Lambda: `devoks-slack-handler`(handler, timeout 10s) / `devoks-slack-worker`(worker,
  timeout 300s / 1024MB)
- DynamoDB 멱등성 테이블: `devoks-slack-idempotency`
- 로그 조회:
  ```bash
  export AWS_PROFILE=devoks
  aws logs tail /aws/lambda/devoks-slack-handler --region ap-northeast-2 --follow
  aws logs tail /aws/lambda/devoks-slack-worker --region ap-northeast-2 --follow
  ```
  `aws logs tail --follow`는 파일로 리다이렉트했을 때 출력이 지연/버퍼링될 수 있으므로,
  즉시 판정이 필요하면 `aws logs filter-log-events --start-time <epoch_ms>`로 직접
  조회하는 편이 더 신뢰할 수 있다.
- worker의 판정 결과는 `observability.emit_query_observation`이 남기는 JSON 한 줄
  (`event: "slack_query"`)로 확인한다 — `outcome`(`ok`/`denied`/`error`)과
  `reason_code`가 핵심 판정 필드다.

## E2E 검증 (`TASK-036`) — 2026-09-16 실행 기록

### 검증 방법

실 Slack 워크스페이스에서 시나리오별로 봇을 멘션하면서 handler/worker CloudWatch 로그를
동시에 관찰해, `emit_query_observation`이 남긴 `outcome`/`reason_code`로 판정했다.

### 결과 요약

| 시나리오 | traces | 판정 | 근거 |
|---|---|---|---|
| 멘션 → 스레드 답변 게시 | `AC-SB-006-1` | ✅ PASS | 질의 여러 건이 `outcome:"ok"`로 완료(9~38초 소요), 실제 Claude 토큰 사용량(`usage.input_tokens`/`output_tokens`)까지 기록됨. worker 코드상 `outcome:"ok"`는 Slack 게시(`post_message`)까지 성공해야만 나오는 값(게시 실패 시 `"error"`로 격하되도록 `_post_and_finish`가 구현돼 있음) |
| 미등록 사용자 거부 | `AC-SB-004-2` | ✅ PASS | 최초 질의가 `outcome:"denied", reason_code:"user_unregistered", client_id:null`로 처리됨 — Claude 호출 없이 등록 안내만 게시 |
| 재시도 중복 억제 | `AC-SB-003-1`, `EDGE-SB-004` | ✅ PASS | handler 로그에 `slack retry received (event_id=..., x-slack-retry-num=1)` 발생 확인. 해당 재시도는 19ms 만에 200 반환(최초 dispatch는 1000ms 이상) — 같은 시각 worker 쪽엔 중복 호출 없이 1건만 기록되어 재전달이 정상 차단됨을 확인 |
| 연타 코얼레싱(같은 스레드) | `EDGE-SB-015` | ✅ PASS | 진행 중인 질의에 약 2.8초 뒤 같은 스레드로 재멘션 → `outcome:"denied", reason_code:"coalesced_in_progress"`(379ms, Claude 미호출). 원래 질의는 그대로 진행돼 9.6초 뒤 `outcome:"ok"`로 정상 완료 — 락이 다른 질의를 막지 않음도 함께 확인 |
| per-person 감사 `client_id` | (전체 traces 공통) | ✅ PASS | 성공한 질의의 `client_id`가 실제 Slack 사용자 ID로 기록됨 — `"slackbot"` 같은 서비스 계정 문자열이 아님 |
| 봇 미초대 채널(`not_in_channel`) | `EDGE-SB-020` | ⏳ **PENDING** | 아직 재현하지 않음 |

**종합: traces 6개 중 5개 확인, `EDGE-SB-020` 1건 미검증 — `TASK-036`은 아직 완료 처리하지
않음(PLAN.md 참고).**

### 알아둘 함정 — 연타 코얼레싱은 반드시 "같은 스레드" 안에서 재현해야 한다

`extract_reply_target_ts`(`servers/slackbot/src/devoks_slackbot/slack/events.py:161-174`)는
기존 스레드 안의 멘션이면 `event.thread_ts`를, 아니면(=새 메시지) `event.ts`(그 메시지
자신의 고유값)를 코얼레싱 키로 쓴다. 채널에 새 멘션을 연달아 두 번 보내면 각 메시지가
서로 다른 `thread_ts`를 갖게 되어 `(user_id, thread_ts)` 락 키가 달라지므로 코얼레싱이
**걸리지 않는다** — 실제로 첫 시도에서 이 함정에 걸려 두 메시지 모두 `outcome:"ok"`로
독립 처리됐고, 기존 스레드에 답장(reply)으로 재멘션하도록 바꾼 뒤에야 재현에
성공했다. 재검증 시에도 동일하게: ① 멘션으로 스레드를 하나 연다 → ② 그 스레드
안에서 답장으로, 첫 질의 응답이 오기 전(수 초~15초 이내)에 다시 멘션한다.

### 미해결 항목

- `EDGE-SB-020`(봇이 초대되지 않은 채널에서 멘션 시 `chat.postMessage`의
  `not_in_channel` 실패 분류) — 검증 절차 제안: ① 테스트 채널에서 봇을 일시적으로
  제거 → ② 그 채널에서 멘션 → ③ worker 로그에서 게시 실패가
  `reason_code:"not_in_channel"`로 분류되어 남는지 확인 → ④ 검증 후 봇을 다시 초대.
  실제 채널 멤버십을 건드리는 작업이라 별도로 시간을 잡아 진행할 것.

## URL 검증(`TASK-032`) / 비용 가드(`TASK-035`)

아직 이 문서에 기록되지 않음 — 각 태스크 완료 시 이어서 작성한다.
