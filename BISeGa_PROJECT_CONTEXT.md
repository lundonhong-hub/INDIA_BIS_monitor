# BISeGa Monitor — 프로젝트 컨텍스트 (AI 학습/인수인계용)

> 이 문서 하나로 AI(또는 새 개발자)가 프로젝트 전체를 파악하고 바로 이어서 작업할 수 있도록 정리했다.
> "무엇을/왜/어떻게" + 시행착오 기록까지 담는다. 코드 최신본(monitor.py 473줄) 기준.

---

## 1. 한 줄 요약

인도 정부 관보(eGazette)를 30분마다 자동 확인 → 지정 키워드(기본 `copper`)가 들어간 신규 공보가 뜨면
**PDF를 첨부해 Gmail + 텔레그램으로 동시 알림**. GitHub Actions(Public 저장소)에서 무료 24시간 자동 실행.

## 2. 왜 만들었나 (도메인 배경)

- 사용자는 **동관(copper tube) 제조사(LS메탈) 동관품질팀장**. **인도 비즈니스가 중요**.
- 인도의 **BIS 품질규제·반덤핑·관세 공보**가 통관/수출입에 직접 영향.
- 특히 `Copper Products (Quality Control) Order` 류 개정을 **놓치면 안 됨**.
- 매일 수동 검색하던 일을 자동화하는 것이 목표.

## 3. 기술 스택

| 항목 | 내용 |
|------|------|
| 언어 | Python 3.12 |
| 브라우저 자동화 | **Playwright (Chromium)** — requests로는 사이트 접근 불가(6번 참고) |
| 알림 | Gmail SMTP(앱 비번) + **Telegram Bot API** |
| 실행/스케줄 | GitHub Actions cron, **Public 저장소 → 무제한 무료** |
| 상태 저장 | `state.json` (저장소에 자동 커밋) |

## 4. 파일 구성

```
INDIA_BIS_monitor/
├── monitor.py                     # 메인 로직 (473줄)
├── requirements.txt               # playwright, requests
├── state.json                     # 이미 알림 보낸 Gazette ID 목록 (자동 관리)
├── README.md                      # 사용자용 설치/운영 가이드
├── BISeGa_PROJECT_CONTEXT.md      # 이 문서 (AI/개발자용)
└── .github/workflows/monitor.yml  # 30분 주기 자동 실행 정의
```

## 5. 동작 흐름 (monitor.py 함수별)

1. **`main()`** — 오케스트레이션
   Playwright 실행 → `VIEW_ALL_TARGETS`(Extra Ordinary, Weekly) 각각 수집 → 중복 제거 →
   `state.json`과 대조해 신규 매칭만 추림 → PDF 확보 → **메일 + 텔레그램 발송** → state 저장

2. **`launch_browser(p)`** — Chromium 실행
   **핵심 플래그**: `--disable-http2`, `--disable-quic`
   (구형 ASP.NET 서버가 HTTP/2 처리 못 해 `ERR_HTTP2_PROTOCOL_ERROR` → HTTP/1.1 강제)

3. **`goto_retry(page, url, tries=3)`** — 페이지 이동 재시도 래퍼

4. **`collect_from_view_all(page, target)`** — 목록 수집 (가장 중요·까다로움)
   - 홈(`egazette.gov.in`) 접속 → 세션 자동 확보
   - "View All" 링크(`__doPostBack('lnk_Extra_All'/'lnk_Week_All')`) 클릭 → 전체 최신 목록
   - **최대 3회 재시도**, 매 시도 홈부터 새로 시작
   - **`page.wait_for_function`으로 Gazette ID 패턴이 화면에 나타날 때까지 대기**
     → 빈 페이지로 넘어가 0건 되는 것 방지 (**안정성의 핵심**)
   - 이후 페이지네이션(2,3,4,5…) 순회 파싱

5. **`parse_gazette_rows(page)`** — 화면에서 공보 행 추출
   - 방법 A: `gvGazetteList_lbl_UGID_N` 패턴(검색결과형)
   - 방법 B: 일반 `<tr>`에서 정규식 `CG-[A-Z]{2}-[A-Z]-\d{8}-\d+`로 Gazette ID 추출
     (RecentUploads 목록형 — **현재 실제 경로**)

6. **`match_keywords(row)`** — subject/metadata/department/ministry/전체행텍스트를
   소문자로 합쳐 `KEYWORDS` 포함 검사

7. **`build_pdf_url_candidates` + `try_download_pdf`** — PDF 확보
   - Gazette ID(`CG-DL-E-30062026-273943`)에서 연도(2026)+끝번호(273943) 추출
   - `https://egazette.gov.in/WriteReadData/{연도}/{번호}.pdf` 직접 다운로드
     (**PDF는 세션 없이 이 URL로 바로 열림** — 목록 페이지와 다름)
   - `%PDF` 시그니처로 유효성 검증

8. **`send_email(matches)`** — Gmail 발송
   PDF 첨부(누적 20MB 이내), 초과분은 링크 대체(Gmail 25MB 한도)

9. **`send_telegram(matches)`** — 텔레그램 발송
   - `sendMessage`(HTML parse_mode, **모든 동적 텍스트 `html.escape` 처리**) + `sendDocument`(PDF 첨부)
   - `TELEGRAM_TOKEN`/`TELEGRAM_CHAT_ID` 없으면 조용히 건너뜀
   - 메일과 **독립적** — 하나 실패해도 다른 하나는 정상 발송(try/except)

10. **`load_state()`/`save_state()`** — 중복 방지
    알린 Gazette ID를 `state.json`에 기록, 다음 실행 때 건너뜀. 매 실행 끝에 저장소로 자동 커밋.

## 6. 중요한 설계 결정과 이유 (★시행착오 기록 — 필독★)

같은 삽질을 반복하지 않도록 기록.

### (1) requests → Playwright 전환
- **실패**: `requests`로 `SearchMinistry.aspx` POST 시 항상 0건 또는 `error.aspx` 리다이렉트
- **원인**: `ASPFIXATION` 세션 보안 쿠키 + 엄격한 페이지 이동 순서 강제. 세션 없이 직접 접근 차단.
- **해결**: 실제 브라우저(Playwright)로 사람처럼 클릭. requests 불가.

### (2) 부처 지정 검색 포기 → 최신목록 필터링 방식 (★핵심 방향전환★)
- **실패**: 홈→Search→SearchMenu→"Ministry" 클릭 체인에서 진입점 못 찾음(이미지 버튼 추정).
  `SearchMinistry.aspx?id=907733` 직접 접근도 세션-id 불일치로 error.aspx.
- **전환**: 부처 검색 미로 **포기**. 홈의 **"View All"(RecentUploads.aspx)** 전체 최신목록을
  긁어 키워드 필터링.
- **이점**: 부처 무관하게 어디서 나온 copper든 다 잡음. 부처명은 결과에 딸려오는 정보로만 사용.
- **주의**: 현재 코드에 "부처 지정" 기능은 **의도적으로 없음**.

### (3) HTTP/2 프로토콜 에러
- **증상**: `net::ERR_HTTP2_PROTOCOL_ERROR`
- **해결**: `--disable-http2`, `--disable-quic` 플래그

### (4) "Execution context was destroyed" / 빈 페이지 0건
- **증상**: 클릭 직후 `inner_text`/`query_selector`로 읽다 페이지 갱신과 충돌. 또는 덜 로딩된 채 0건.
- **해결**: `page.wait_for_function(...)`으로 브라우저 내부에서 "Gazette ID 나타날 때까지" 안전 대기.
  (Python 쪽 반복 `inner_text` 읽기는 충돌 유발 → 금지)

### (5) state.json push 충돌 (rejected)
- **증상**: `Commit updated state`에서 `! [rejected] main -> main (fetch first)`
- **원인**: 수동/자동 실행 겹칠 때 두 실행이 동시에 push
- **해결**: 워크플로우에서 `git stash → git pull --rebase → stash pop → commit → push` 순서로 변경

### (6) 왜 Public 저장소인가
- Playwright+브라우저 실행이 1~3분. Private는 월 2000분 무료라 빠듯.
- Public은 GitHub Actions **완전 무제한 무료**. 민감정보는 코드가 아니라 **Secrets**에 저장 → 안전.

## 7. 안정성에 대한 솔직한 한계

- 개별 실행이 **100% 성공을 보장하지 않음**. 인도 정부 서버의 일시 지연/점검/네트워크 순단 시 그 회차 실패 가능.
- 그러나 **30분 반복 + state.json 기억** 구조로, 한 회차 실패해도 공보가 목록에 남아있는 한 다음 회차에 잡음.
  → "개별 실행은 가끔 실패, **최종적으로 공보 놓칠 확률은 거의 0**"이 정확한 표현.

## 8. 설정 변경 지점 (monitor.py 상단)

```python
KEYWORDS = ["copper"]                 # 감시 키워드. 줄 추가로 확장 (대소문자 무시)
ATTACH_PDF = True                     # PDF 첨부 on/off
ATTACH_SIZE_LIMIT = 20 * 1024 * 1024  # 첨부 누적 상한(약 20MB)
VIEW_ALL_TARGETS = ["lnk_Extra_All", "lnk_Week_All"]  # 수집 목록(Extra Ordinary/Weekly)
```
- **실행 주기**: `.github/workflows/monitor.yml`의 `cron: "*/30 * * * *"`
- **특정 부처만 필터**(선택): `match_keywords`에 부처 조건 추가(현재 미구현)

## 9. 환경변수 / Secrets (GitHub Settings → Secrets → Actions)

| 이름 | 용도 |
|------|------|
| `GMAIL_USER` | 보내는 Gmail 주소 |
| `GMAIL_APP_PASSWORD` | Gmail 16자리 앱 비밀번호 |
| `ALERT_TO` | 알림 수신 주소(콤마로 다중 가능) |
| `TELEGRAM_TOKEN` | @BotFather로 만든 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 수신 chat_id (`getUpdates`로 확인) |

## 10. 운영/배포 요약

- GitHub **Public** 저장소에 파일 업로드
- 위 Secrets 등록 → Actions 탭에서 수동 실행 또는 30분 자동
- `state.json`을 `[]`로 비우면 기억 초기화(현재 목록의 매칭 공보 재수신)

## 11. 알려진 개선 여지 (TODO 후보)

- `parse_gazette_rows` 방법 B는 행 전체 텍스트를 subject에 넣어 컬럼 분리 약함
  → RecentUploads 실제 DOM 구조에 맞춘 컬럼별 정밀 파싱
- copper가 Extra Ordinary에만 나온다고 확인되면 Weekly 수집 생략 → 실행시간 단축
- N회 연속 실패 시 관리자 통지, 매일 아침 실행 요약 알림 등
- 특정 부처 한정 필터 옵션
