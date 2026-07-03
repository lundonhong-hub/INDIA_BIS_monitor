"""
인도 eGazette 모니터링 (홈페이지 최신목록 방식)
────────────────────────────────────────────────────────
검색 폼(세션+id+HTTP2 미로)을 우회. 홈페이지는 세션 없이 열리고 최신 공보가
노출되며, 'View All'을 클릭하면 전체 최신 목록으로 이동한다.
그 목록에서 키워드(copper)를 필터링한다. 부처명은 결과에 딸려온다.

- 키워드가 제목/부처/메타에 등장하면 PDF 첨부해 Gmail 알림
- state.json으로 중복 알림 방지
- 첫 실행부터 현재 떠 있는 매칭 공보를 바로 알림
"""
import os
import re
import json
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from datetime import datetime
from html import escape
from playwright.sync_api import sync_playwright

# ══════════════════════════════════════════════════════════
#  설정 구역 — 여기만 고치면 됩니다
# ══════════════════════════════════════════════════════════

# 감시할 키워드 (대소문자 무시). 하나라도 걸리면 알림.
KEYWORDS = [
    "copper",
    # "brass",
    # "동관",
]

ATTACH_PDF = True
ATTACH_SIZE_LIMIT = 20 * 1024 * 1024

STATE_FILE = "state.json"
HOME_URL = "https://egazette.gov.in/"
PDF_BASE = "https://egazette.gov.in/WriteReadData"

# 홈에서 볼 카테고리별 'View All' 링크 (Extra Ordinary / Weekly 둘 다)
# __doPostBack 타깃 이름
VIEW_ALL_TARGETS = ["lnk_Extra_All", "lnk_Week_All"]

# ══════════════════════════════════════════════════════════


def launch_browser(p):
    return p.chromium.launch(
        headless=True,
        args=[
            "--disable-http2", "--disable-quic", "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )


def goto_retry(page, url, tries=3):
    for i in range(tries):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            return True
        except Exception as e:
            print(f"  [재시도 {i+1}/{tries}] 이동 실패: {str(e)[:70]}")
            page.wait_for_timeout(2000)
    return False


def parse_gazette_rows(page):
    """
    현재 화면에서 공보 행들을 추출한다.
    eGazette 목록/결과 테이블은 gvGazetteList 계열 또는 일반 테이블 행으로 나온다.
    여기서는 Gazette ID(CG-...) 패턴을 앵커로 삼아 각 행의 텍스트를 긁는다.
    """
    rows = []

    # 방법 A: gvGazetteList_lbl_UGID_N 패턴 (검색결과형)
    idx = 0
    found_structured = False
    while True:
        el = page.query_selector(f"#gvGazetteList_lbl_UGID_{idx}")
        if el is None:
            break
        found_structured = True

        def g(field):
            e = page.query_selector(f"#gvGazetteList_lbl_{field}_{idx}")
            return e.inner_text().strip() if e else ""

        rows.append({
            "gazette_id": el.inner_text().strip(),
            "subject": g("Subject"),
            "metadata": g("metadata"),
            "department": g("Department"),
            "ministry": g("Ministry"),
            "issue_date": g("IssueDate"),
            "publish_date": g("PublishDate"),
        })
        idx += 1

    if found_structured:
        return rows

    # 방법 B: 일반 테이블 행에서 Gazette ID 패턴으로 긁기 (홈/RecentUploads형)
    # 모든 <tr>을 훑어 CG-...-숫자 패턴이 있는 행을 공보로 간주
    trs = page.query_selector_all("tr")
    for tr in trs:
        try:
            txt = tr.inner_text()
        except Exception:
            continue
        m = re.search(r"CG-[A-Z]{2}-[A-Z]-\d{8}-\d+", txt)
        if not m:
            continue
        gid = m.group(0)
        # 셀 단위로 분해
        cells = [c.inner_text().strip() for c in tr.query_selector_all("td")]
        joined = " | ".join(cells)
        rows.append({
            "gazette_id": gid,
            "subject": joined,      # 홈형은 컬럼 구분이 약해 전체를 subject로
            "metadata": "",
            "department": "",
            "ministry": cells[0] if cells else "",
            "issue_date": "",
            "publish_date": "",
            "_rowtext": re.sub(r"\s+", " ", txt).strip(),
        })
    return rows


def collect_from_view_all(page, target):
    """홈에서 특정 View All(postback)을 클릭해 전체 목록을 긁는다. 최대 3회 재시도."""
    rows = []

    for attempt in range(3):
        # 매 시도마다 홈부터 새로 시작 (깨진 상태 초기화)
        if not goto_retry(page, HOME_URL):
            print(f"  [경고] 홈 접속 실패 (시도 {attempt+1})")
            continue

        # View All 클릭: 앵커 클릭 → 실패 시 JS postback
        el = page.query_selector(f"a[href*=\"{target}\"]")
        if not el:
            print(f"  [경고] '{target}' 링크 없음 (시도 {attempt+1})")
            continue
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=90000):
                el.click(timeout=45000)
        except Exception:
            try:
                page.evaluate(f"__doPostBack('{target}','')")
                page.wait_for_load_state("domcontentloaded", timeout=90000)
            except Exception as e2:
                print(f"  [postback 실패] {str(e2)[:50]} (시도 {attempt+1})")
                continue

        # ★핵심★ 목록 데이터가 실제로 나타날 때까지 안전하게 대기
        # (inner_text 폴링은 페이지 갱신 중 context destroyed 유발 → wait_for_selector 사용)
        got_data = False
        try:
            # 검색결과형(gvGazetteList) 또는 CG- 링크가 뜰 때까지 대기
            page.wait_for_function(
                """() => /CG-[A-Z]{2}-[A-Z]-\\d{8}-\\d+/.test(document.body ? document.body.innerText : '')""",
                timeout=30000,
            )
            got_data = True
        except Exception:
            got_data = False

        if got_data:
            page.wait_for_timeout(1500)  # 완전 안정화
            print(f"  [진단-{target}] 진입성공 (시도 {attempt+1}) URL={page.url}")
            break
        else:
            print(f"  [재시도] {target} 목록 안 뜸 (시도 {attempt+1}/3), 다시 시도")
    else:
        print(f"  [경고] '{target}' 3회 시도 후에도 목록 진입 실패")
        return rows

    page.wait_for_timeout(1500)
    # 결과 파싱 + 페이지네이션
    rows += parse_gazette_rows(page)

    # 페이저 구조 진단 (첫 페이지에서 한 번만)
    pager_texts = []
    for a in page.query_selector_all("tr.pager a, .pager a"):
        try:
            pager_texts.append(a.inner_text().strip())
        except Exception:
            pass
    print(f"  [진단-{target} 페이저] 링크들={pager_texts}")

    # 페이지네이션: 매번 페이저를 새로 조회(stale 방지),
    # 클릭 후 expect_navigation 또는 네트워크 유휴 대기로 context destroyed 회피
    visited_pages = {1}
    while True:
        # 현재 화면에서 아직 안 간 다음 페이지 번호 찾기
        next_num = None
        links = page.query_selector_all("tr.pager a, .pager a")
        candidates = []
        for a in links:
            t = a.inner_text().strip()
            if t.isdigit():
                candidates.append(int(t))
        candidates = sorted(set(candidates) - visited_pages)
        if not candidates:
            break
        next_num = candidates[0]

        # 다음 페이지 링크를 다시 조회해서 클릭 (stale 방지)
        target_link = None
        for a in page.query_selector_all("tr.pager a, .pager a"):
            if a.inner_text().strip() == str(next_num):
                target_link = a
                break
        if not target_link:
            break

        try:
            try:
                with page.expect_navigation(wait_until="domcontentloaded", timeout=60000):
                    target_link.click(timeout=30000)
            except Exception:
                # 네비게이션 이벤트 안 잡히면 postback 후 안정화 대기
                target_link.click(timeout=30000)
            page.wait_for_timeout(2500)  # DOM 안정화 (context destroyed 방지)
            # 새 페이지가 로드됐는지 gvGazetteList 또는 행 존재 확인
            page.wait_for_selector("tr", timeout=30000)
            rows += parse_gazette_rows(page)
            visited_pages.add(next_num)
            if len(visited_pages) > 30:
                break
        except Exception as e:
            print(f"  [경고] {next_num}p 이동 실패: {str(e)[:60]}")
            break

    print(f"  [진단-{target}] 수집 {len(rows)}건")
    return rows


def match_keywords(row):
    hay = f"{row.get('subject','')} {row.get('metadata','')} {row.get('department','')} {row.get('ministry','')} {row.get('_rowtext','')}".lower()
    return [kw for kw in KEYWORDS if kw.lower() in hay]


def build_pdf_url_candidates(row):
    gid = row["gazette_id"]
    m = re.search(r"-[EW]-\d{4}(\d{4})-", gid)
    year = m.group(1) if m else str(datetime.now().year)
    tail = gid.split("-")[-1]
    return [f"{PDF_BASE}/{year}/{tail}.pdf"]


def try_download_pdf(row, session):
    for url in build_pdf_url_candidates(row):
        try:
            resp = session.get(url, timeout=60)
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and (
                resp.content[:4] == b"%PDF" or "pdf" in ctype.lower()
            ):
                return url, resp.content
        except Exception as e:
            print(f"  [PDF 시도 실패] {url}: {str(e)[:50]}")
    return None, None


def enrich_with_pdf(matches, cookies):
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    for c in cookies:
        try:
            s.cookies.set(c["name"], c["value"], domain=c.get("domain", "egazette.gov.in"))
        except Exception:
            pass
    for m in matches:
        if not ATTACH_PDF:
            m["_pdf_url"] = build_pdf_url_candidates(m)[0]
            m["_pdf_bytes"] = None
            m["_pdf_size"] = 0
            continue
        url, data = try_download_pdf(m, s)
        m["_pdf_url"] = url or build_pdf_url_candidates(m)[0]
        m["_pdf_bytes"] = data
        m["_pdf_size"] = len(data) if data else 0
        print(f"  [PDF] {m['gazette_id']}: {f'{m[chr(95)+chr(112)+chr(100)+chr(102)+chr(95)+chr(115)+chr(105)+chr(122)+chr(101)]/1024/1024:.2f}MB' if data else '실패(링크)'}")


def decide_attachments(matches):
    total = 0
    to_attach, to_link = [], []
    for m in matches:
        if m.get("_pdf_bytes") is not None and total + m["_pdf_size"] <= ATTACH_SIZE_LIMIT:
            to_attach.append(m); total += m["_pdf_size"]
        else:
            to_link.append(m)
    return to_attach, to_link


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_state(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)


def pdf_filename(row):
    return re.sub(r"[^A-Za-z0-9_-]", "_", row["gazette_id"]) + ".pdf"


def send_email(matches):
    user = os.environ["GMAIL_USER"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)
    to_attach, to_link = decide_attachments(matches)

    parts = [
        f"인도 eGazette 신규 공보 {len(matches)}건이 키워드에 매칭되었습니다.",
        f"확인 시각: {datetime.now():%Y-%m-%d %H:%M}", "",
    ]
    if to_attach:
        parts.append(f"■ 첨부 PDF: {len(to_attach)}건")
        for m in to_attach:
            parts.append(f"  · [{', '.join(m['_matched'])}] {m['subject'][:120]}\n"
                         f"    부처: {m.get('ministry','')} / ID: {m['gazette_id']}")
        parts.append("")
    if to_link:
        parts.append(f"■ 링크 안내: {len(to_link)}건")
        for m in to_link:
            parts.append(f"  · [{', '.join(m['_matched'])}] {m['subject'][:120]}\n"
                         f"    부처: {m.get('ministry','')} / ID: {m['gazette_id']}\n"
                         f"    PDF: {m['_pdf_url']}")
        parts.append("")
    parts.append("홈: https://egazette.gov.in/")

    msg = MIMEMultipart()
    msg["Subject"] = f"[eGazette 알림] 인도 공보 {len(matches)}건 매칭"
    msg["From"] = user
    msg["To"] = to
    msg.attach(MIMEText("\n".join(parts), "plain", "utf-8"))
    for m in to_attach:
        part = MIMEApplication(m["_pdf_bytes"], _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=pdf_filename(m))
        msg.attach(part)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, app_pw)
        server.send_message(msg)
    print(f"[알림 발송] 총 {len(matches)}건 (첨부 {len(to_attach)}, 링크 {len(to_link)}) -> {to}")


def send_telegram(matches):
    """텔레그램으로 알림 발송 (PDF도 첨부)."""
    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[텔레그램] 토큰/chat_id 없음 → 건너뜀")
        return

    api = f"https://api.telegram.org/bot{token}"

    for m in matches:
        gazette_id = m.get("gazette_id", "")
        subject = m.get("subject", "")
        ministry = m.get("ministry", "")
        matched = m.get("_matched", [])
        pdf_url = m.get("_pdf_url", "")

        # Telegram HTML parse_mode 사용 시 사용자/사이트 텍스트는 escape 처리
        safe_matched = ", ".join(str(x) for x in matched)
        safe_subject = escape(subject[:200])
        safe_ministry = escape(ministry)
        safe_id = escape(gazette_id)
        safe_pdf_url = escape(pdf_url)

        text = (
            f"🔔 <b>인도 eGazette 신규 공보</b>\n\n"
            f"[{escape(safe_matched)}] 매칭\n"
            f"<b>{safe_subject}</b>\n"
            f"부처: {safe_ministry}\n"
            f"ID: {safe_id}\n"
            f"PDF: {safe_pdf_url}"
        )

        try:
            requests.post(
                f"{api}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )

            # PDF 있으면 문서로 첨부
            if m.get("_pdf_bytes"):
                requests.post(
                    f"{api}/sendDocument",
                    data={"chat_id": chat_id},
                    files={"document": (pdf_filename(m), m["_pdf_bytes"], "application/pdf")},
                    timeout=60,
                )
        except Exception as e:
            print(f"[텔레그램 전송 실패] {gazette_id}: {str(e)[:80]}")

    print(f"[텔레그램 발송] {len(matches)}건 → chat {chat_id}")


def main():
    all_rows = []
    seen_ids = set()
    cookies = []

    with sync_playwright() as p:
        browser = launch_browser(p)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
        )
        for target in VIEW_ALL_TARGETS:
            page = context.new_page()
            try:
                batch = collect_from_view_all(page, target)
            except Exception as e:
                print(f"[에러] {target} 수집 실패: {str(e)[:80]}")
                batch = []
            finally:
                try: page.close()
                except Exception: pass
            for row in batch:
                if row["gazette_id"] not in seen_ids:
                    seen_ids.add(row["gazette_id"])
                    all_rows.append(row)
        cookies = context.cookies()
        browser.close()

    print(f"[합계] 중복 제거 후 {len(all_rows)}건")

    seen = load_state()
    new_matches = []
    for row in all_rows:
        if row["gazette_id"] in seen:
            continue
        matched = match_keywords(row)
        if matched:
            row["_matched"] = matched
            new_matches.append(row)
        seen.add(row["gazette_id"])

    if new_matches:
        print(f"[매칭] 신규 {len(new_matches)}건 → PDF 확보")
        enrich_with_pdf(new_matches, cookies)
        send_email(new_matches)
        send_telegram(new_matches)
    else:
        print("[결과] 신규 매칭 없음")

    save_state(seen)


if __name__ == "__main__":
    main()
