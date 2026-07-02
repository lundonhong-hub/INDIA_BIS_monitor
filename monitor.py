"""
인도 eGazette 실시간 모니터링 (Playwright 브라우저 자동화 버전)
────────────────────────────────────────────────────────
requests 방식은 이 사이트의 세션 보안(ASPFIXATION)과 엄격한 페이지 흐름 강제
때문에 error.aspx로 튕긴다. 그래서 실제 브라우저(Chromium)를 자동 조종해
사람처럼 홈 → 검색 → 부처/월 선택 → 조회 순서로 접근한다.

- 설정한 부처들의 신규 공보를 주기적으로 확인
- 키워드가 제목/메타데이터에 등장하면 PDF 첨부해 Gmail 알림
- state.json으로 중복 알림 방지
- 첫 실행부터 현재 떠 있는 매칭 공보를 바로 알림
- 월 경계 대비: 현재 월 + 직전 월 동시 조회
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
from playwright.sync_api import sync_playwright

# ══════════════════════════════════════════════════════════
#  설정 구역 — 여기만 고치면 됩니다
# ══════════════════════════════════════════════════════════

# ① 감시할 키워드 (대소문자 무시)
KEYWORDS = [
    "copper",
    # "brass",
    # "동관",
]

# ② 감시할 부처 목록. {드롭다운value: "표시이름"}
MINISTRIES = {
    "9": "Commerce and Industry",
    # "34": "Steel",
    # "83": "Bureau of Indian Standards",
}

# ── 부처 번호 참조표 ──────────────────────────────
#   9=Commerce and Industry  34=Steel  86=Mines
#   6=Chemicals and Fertilizers  18=Finance  83=BIS
# ────────────────────────────────────────────────

# ③ PDF 첨부 옵션
ATTACH_PDF = True
ATTACH_SIZE_LIMIT = 20 * 1024 * 1024

STATE_FILE = "state.json"
HOME_URL = "https://egazette.gov.in/"
PDF_BASE = "https://egazette.gov.in/WriteReadData"

# ══════════════════════════════════════════════════════════


def fetch_ministry_month(page, ministry_value, ministry_label, month, year):
    """브라우저 page로 한 부처의 한 월치 공보를 조회해 행 리스트 반환."""
    rows = []

    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)

    # 상단 Search 메뉴 클릭 → SearchMenu
    # 홈페이지 Search는 __doPostBack('sgzt','') 방식. 정확히 그 링크를 우선 클릭.
    clicked_search = False
    for selector in [
        "a[href*=\"sgzt\"]",
        "a:has-text('Search Gazette')",
        "a:has-text('Search')",
    ]:
        try:
            el = page.query_selector(selector)
            if el:
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                clicked_search = True
                print(f"  [진입] Search 메뉴 클릭 성공: '{selector}'")
                break
        except Exception as e:
            print(f"  [Search시도 실패] {selector}: {e}")
    if not clicked_search:
        print(f"  [경고] Search 메뉴 클릭 실패")

    # ── 진단: SearchMenu 도달 후 클릭 가능한 링크/버튼 전부 나열 ──
    print(f"  [진단-SearchMenu] URL={page.url} 제목={page.title()}")
    clickables = page.query_selector_all("a, input[type=image], input[type=button], input[type=submit]")
    print(f"  [진단-클릭가능요소 {len(clickables)}개]")
    for i, el in enumerate(clickables[:40]):
        try:
            tag = el.evaluate("e => e.tagName")
            txt = (el.inner_text() or "").strip()
            href = el.get_attribute("href") or ""
            alt = el.get_attribute("alt") or ""
            elid = el.get_attribute("id") or ""
            name = el.get_attribute("name") or ""
            info = f"tag={tag} id='{elid}' name='{name}' text='{txt[:30]}' alt='{alt[:30]}' href='{href[:50]}'"
            print(f"    [{i}] {info}")
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────

    # SearchMenu에서 Ministry 검색 진입 (정확한 링크는 위 진단 로그로 확정)
    # 우선 'Ministry Wise' / 'Search by Ministry' 류를 우선 시도, 없으면 SearchMinistry href 링크
    entered = False
    for selector in [
        "a:has-text('Ministry Wise')",
        "a:has-text('Search by Ministry')",
        "a:has-text('Ministry wise')",
        "a[href*='SearchMinistry']",
        "input[alt*='Ministry']",
    ]:
        try:
            el = page.query_selector(selector)
            if el:
                el.click()
                page.wait_for_load_state("domcontentloaded", timeout=30000)
                entered = True
                print(f"  [진입] '{selector}' 로 Ministry 검색 진입 성공")
                break
        except Exception as e:
            print(f"  [진입시도 실패] {selector}: {e}")
    if not entered:
        print(f"  [경고] Ministry 검색 진입점을 못 찾음 — 위 클릭가능요소 목록 참고 필요")

    # ddlMinistry 있어야 정상
    if page.query_selector("#ddlMinistry") is None:
        print(f"  [진단] ddlMinistry 없음 — URL: {page.url} / 제목: {page.title()}")
        body = page.query_selector("body")
        if body:
            print(f"  [진단] body: {re.sub(chr(92)+'s+', ' ', body.inner_text())[:400]}")
        return rows

    page.select_option("#ddlMinistry", ministry_value)
    page.wait_for_load_state("domcontentloaded", timeout=30000)

    try:
        page.check("#rdb_Option_0", timeout=5000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    page.select_option("#ddlmonth", str(month))
    page.wait_for_load_state("domcontentloaded", timeout=15000)
    page.select_option("#ddlyear", str(year))
    page.wait_for_load_state("domcontentloaded", timeout=15000)

    page.click("#ImgSubmitDetails")
    page.wait_for_load_state("domcontentloaded", timeout=30000)

    total_el = page.query_selector("#lbl_Result")
    print(f"  [진단-결과] {total_el.inner_text().strip() if total_el else '결과라벨없음'}")

    rows += parse_current_page(page, ministry_label)

    # 페이지네이션 순회
    page_num = 2
    while True:
        pager_links = page.query_selector_all("tr.pager a, .pager a")
        target = None
        for a in pager_links:
            if a.inner_text().strip() == str(page_num):
                target = a
                break
        if not target:
            break
        try:
            target.click()
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            rows += parse_current_page(page, ministry_label)
            page_num += 1
            if page_num > 20:
                break
        except Exception as e:
            print(f"  [경고] {page_num}페이지 이동 실패: {e}")
            break

    return rows


def parse_current_page(page, ministry_label):
    """현재 화면의 gvGazetteList 테이블에서 공보 행 추출."""
    rows = []
    idx = 0
    while True:
        ugid_el = page.query_selector(f"#gvGazetteList_lbl_UGID_{idx}")
        if ugid_el is None:
            break

        def txt(field):
            el = page.query_selector(f"#gvGazetteList_lbl_{field}_{idx}")
            return el.inner_text().strip() if el else ""

        rows.append({
            "gazette_id": ugid_el.inner_text().strip(),
            "subject": txt("Subject"),
            "metadata": txt("metadata"),
            "department": txt("Department"),
            "issue_date": txt("IssueDate"),
            "publish_date": txt("PublishDate"),
            "ministry": ministry_label,
        })
        idx += 1
    return rows


def target_months():
    now = datetime.now()
    result = [(now.month, now.year)]
    if now.month == 1:
        result.append((12, now.year - 1))
    else:
        result.append((now.month - 1, now.year))
    return result


def match_keywords(row):
    haystack = f"{row['subject']} {row['metadata']}".lower()
    return [kw for kw in KEYWORDS if kw.lower() in haystack]


def build_pdf_url_candidates(row):
    gid = row["gazette_id"]
    m = re.search(r"-E-\d{4}(\d{4})-", gid)
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
            print(f"  [PDF 시도 실패] {url}: {e}")
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
        status = f"{m['_pdf_size']/1024/1024:.2f}MB" if data else "실패(링크로 대체)"
        print(f"  [PDF] {m['gazette_id']}: {status}")


def decide_attachments(matches):
    total = 0
    to_attach, to_link = [], []
    for m in matches:
        if m.get("_pdf_bytes") is not None and total + m["_pdf_size"] <= ATTACH_SIZE_LIMIT:
            to_attach.append(m)
            total += m["_pdf_size"]
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
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", row["gazette_id"])
    return f"{safe}.pdf"


def send_email(matches):
    user = os.environ["GMAIL_USER"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)

    to_attach, to_link = decide_attachments(matches)

    parts = [
        f"인도 eGazette 신규 공보 {len(matches)}건이 키워드에 매칭되었습니다.",
        f"확인 시각: {datetime.now():%Y-%m-%d %H:%M}",
        "",
    ]
    if to_attach:
        parts.append(f"■ 첨부 PDF: {len(to_attach)}건")
        for m in to_attach:
            parts.append(
                f"  · [{', '.join(m['_matched'])}] {m['subject']}\n"
                f"    부처: {m['ministry']} / 발행: {m['publish_date']} / ID: {m['gazette_id']}"
            )
        parts.append("")
    if to_link:
        parts.append(f"■ 링크 안내: {len(to_link)}건")
        for m in to_link:
            parts.append(
                f"  · [{', '.join(m['_matched'])}] {m['subject']}\n"
                f"    부처: {m['ministry']} / 발행: {m['publish_date']} / ID: {m['gazette_id']}\n"
                f"    PDF: {m['_pdf_url']}"
            )
        parts.append("")
    parts.append("검색 페이지: https://egazette.gov.in/")

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


def main():
    months = target_months()
    rows = []
    seen_ids = set()
    browser_cookies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        for mvalue, mlabel in MINISTRIES.items():
            for month, year in months:
                try:
                    batch = fetch_ministry_month(page, mvalue, mlabel, month, year)
                except Exception as e:
                    print(f"[에러] {mlabel} {year}-{month:02d} 조회 실패: {e}")
                    batch = []
                for row in batch:
                    if row["gazette_id"] not in seen_ids:
                        seen_ids.add(row["gazette_id"])
                        rows.append(row)
                print(f"[조회] {mlabel} {year}-{month:02d}: {len(batch)}건")

        browser_cookies = context.cookies()
        browser.close()

    print(f"[합계] 중복 제거 후 {len(rows)}건")

    seen = load_state()
    new_matches = []
    for row in rows:
        if row["gazette_id"] in seen:
            continue
        matched = match_keywords(row)
        if matched:
            row["_matched"] = matched
            new_matches.append(row)
        seen.add(row["gazette_id"])

    if new_matches:
        print(f"[매칭] 신규 {len(new_matches)}건 → PDF 확보 시도")
        enrich_with_pdf(new_matches, browser_cookies)
        send_email(new_matches)
    else:
        print("[결과] 신규 매칭 없음")

    save_state(seen)


if __name__ == "__main__":
    main()
