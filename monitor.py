"""
인도 eGazette 실시간 모니터링
────────────────────────────────────────────────────────
- 설정한 부처들의 신규 공보를 주기적으로 확인
- 설정한 키워드가 제목/메타데이터에 등장하면 Gmail로 알림
- 매칭 공보의 PDF를 내려받아 메일에 첨부 (25MB 초과분은 링크로 대체)
- state.json으로 중복 알림 방지 (한 번 본 공보는 다시 알리지 않음)
- 첫 실행 때부터, 현재 떠 있는 매칭 공보를 바로 알림
- 월 경계 대비: 현재 월 + 직전 월 동시 조회
- 페이지네이션: 검색 결과 전체 페이지 순회

* PDF는 저장소에 저장되지 않습니다. 실행 중 임시로만 받았다가 메일 발송 후
  러너와 함께 폐기됩니다. 저장소에는 state.json(수 KB)만 커밋됩니다.
"""
import os
import re
import json
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from bs4 import BeautifulSoup
from datetime import datetime

# ══════════════════════════════════════════════════════════
#  설정 구역 — 여기만 고치면 됩니다 (코드 로직은 손댈 필요 없음)
# ══════════════════════════════════════════════════════════

# ① 감시할 키워드 (대소문자 무시). 하나라도 걸리면 알림.
KEYWORDS = [
    "copper",
    # "brass",      # 예: 황동도 감시하려면 주석 해제
    # "동관",
]

# ② 감시할 부처 목록. {번호: "표시이름"} 형태. 여러 개 = 모두 감시.
MINISTRIES = {
    "9": "Commerce and Industry",
    # "34": "Steel",
    # "83": "Bureau of Indian Standards",
}

# ── 자주 쓰는 부처 번호 참조표 (동관 사업 관련) ──────────────
#   9  = Ministry of Commerce and Industry   (통상·반덤핑·품질규제)
#   34 = Ministry of Steel                   (철강·비철금속)
#   86 = Ministry of Mines                   (광물·원자재)
#   6  = Ministry of Chemicals and Fertilizers
#   18 = Ministry of Finance                 (관세)
#   83 = Bureau of Indian Standards (BIS)    (품질규격 인증)
# ────────────────────────────────────────────────────────

# ③ PDF 첨부 옵션
ATTACH_PDF = True                       # False면 링크만 보냄
ATTACH_SIZE_LIMIT = 20 * 1024 * 1024    # 첨부 누적 안전선(약 20MB, Gmail 25MB 대비 여유)

STATE_FILE = "state.json"
BASE_URL = "https://egazette.gov.in/SearchMinistry.aspx"
PDF_BASE = "https://egazette.gov.in/WriteReadData"

# ══════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}


def get_hidden_fields(soup):
    """ASP.NET 상태 토큰(__VIEWSTATE 등) 추출."""
    fields = {}
    for name in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
                 "__EVENTTARGET", "__EVENTARGUMENT", "__LASTFOCUS",
                 "__VIEWSTATEENCRYPTED"]:
        el = soup.find("input", {"name": name})
        fields[name] = el["value"] if el and el.has_attr("value") else ""
    return fields


def parse_rows(html, ministry_label):
    """gvGazetteList 테이블에서 공보 행들을 파싱. 페이지 HTML도 함께 보관."""
    soup = BeautifulSoup(html, "lxml")
    rows = []
    idx = 0
    while True:
        ugid = soup.find("span", id=f"gvGazetteList_lbl_UGID_{idx}")
        if ugid is None:
            break

        def txt(field):
            el = soup.find("span", id=f"gvGazetteList_lbl_{field}_{idx}")
            return el.get_text(strip=True) if el else ""

        rows.append({
            "gazette_id": ugid.get_text(strip=True),
            "subject": txt("Subject"),
            "metadata": txt("metadata"),
            "department": txt("Department"),
            "issue_date": txt("IssueDate"),
            "publish_date": txt("PublishDate"),
            "ministry": ministry_label,
            "_page_html": html,   # PDF URL 탐지에 사용 (임시)
        })
        idx += 1
    return rows


def find_next_pages(html):
    """페이저에서 추가 페이지 번호 목록 반환."""
    soup = BeautifulSoup(html, "lxml")
    pages = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"Page\$(\d+)", a["href"])
        if m:
            pages.add(int(m.group(1)))
    return sorted(pages)


def fetch_ministry_month(ministry_value, ministry_label, month, year):
    """한 부처의 한 월치 공보를 전체 페이지 순회하며 가져온다."""
    s = requests.Session()
    s.headers.update(HEADERS)

    r = s.get(BASE_URL, timeout=30)
    r.raise_for_status()
    fields = get_hidden_fields(BeautifulSoup(r.text, "lxml"))

    # ── 진단: 최초 GET 응답 상태 확인 (차단 여부를 여기서 먼저 가늠) ──
    raw0 = r.text
    has_form0 = 'id="Form1"' in raw0
    has_table0 = "gvGazetteList" in raw0
    print(f"  [진단-최초GET] HTTP상태={r.status_code} 응답길이={len(raw0)} "
          f"Form1존재={has_form0} 테이블존재={has_table0}")
    # ─────────────────────────────────────────────────────────

    fields.update({
        "__EVENTTARGET": "ddlMinistry", "ddlMinistry": ministry_value,
        "rdb_Option": "0", "ddlmonth": str(month), "ddlyear": str(year),
    })
    r = s.post(BASE_URL, data=fields, timeout=30)
    r.raise_for_status()
    fields = get_hidden_fields(BeautifulSoup(r.text, "lxml"))

    fields.update({
        "__EVENTTARGET": "", "ddlMinistry": ministry_value,
        "rdb_Option": "0", "ddlmonth": str(month), "ddlyear": str(year),
        "ImgSubmitDetails.x": "20", "ImgSubmitDetails.y": "10",
    })
    r = s.post(BASE_URL, data=fields, timeout=30)
    r.raise_for_status()

    # ── 진단 로그: 사이트가 실제로 뭘 돌려줬는지 확인 (문제 생기면 이걸로 원인 파악) ──
    raw = r.text
    total_match = re.search(r"Total No\.? of Gazettes\s*:\s*(\d+)", raw)
    has_table = "gvGazetteList" in raw
    has_form = 'id="Form1"' in raw or "id='Form1'" in raw
    diag = BeautifulSoup(raw, "lxml")
    sel = diag.find("select", id="ddlMinistry")
    selected_text = None
    if sel:
        opt = sel.find("option", selected=True)
        selected_text = opt.get_text(strip=True) if opt else None

    block_keywords = ["captcha", "access denied", "rejected", "blocked",
                      "forbidden", "not authorized", "temporarily unavailable"]
    found_block = [k for k in block_keywords if k in raw.lower()]

    print(f"  [진단] HTTP상태={r.status_code} 응답길이={len(raw)} "
          f"Form1존재={has_form} 테이블존재={has_table} "
          f"'Total No' 매칭={total_match.group(1) if total_match else None} "
          f"실제선택부처='{selected_text}' (요청: 부처값={ministry_value}, 월/년={month}/{year})")
    print(f"  [진단-헤더] Server={r.headers.get('Server')} "
          f"Content-Type={r.headers.get('Content-Type')} "
          f"Set-Cookie존재={'Set-Cookie' in r.headers}")
    if found_block:
        print(f"  [진단-차단의심] 발견된 키워드: {found_block}")
    if not has_form or not has_table:
        snippet = re.sub(r"\s+", " ", raw)
        print(f"  [진단-응답스니펫-앞부분] {snippet[:800]}")
        print(f"  [진단-응답스니펫-중간부분] {snippet[len(snippet)//2:len(snippet)//2+800]}")
    # ─────────────────────────────────────────────────────────────────

    all_rows = parse_rows(raw, ministry_label)
    visited = {1}
    to_visit = [p for p in find_next_pages(r.text) if p not in visited]

    while to_visit:
        page = to_visit.pop(0)
        if page in visited:
            continue
        fields = get_hidden_fields(BeautifulSoup(r.text, "lxml"))
        fields.update({
            "__EVENTTARGET": "gvGazetteList", "__EVENTARGUMENT": f"Page${page}",
            "ddlMinistry": ministry_value, "rdb_Option": "0",
            "ddlmonth": str(month), "ddlyear": str(year),
        })
        r = s.post(BASE_URL, data=fields, timeout=30)
        r.raise_for_status()
        all_rows += parse_rows(r.text, ministry_label)
        visited.add(page)
        for p in find_next_pages(r.text):
            if p not in visited and p not in to_visit:
                to_visit.append(p)

    return all_rows


def target_months():
    """현재 월 + 직전 월 (월 경계 대비)."""
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
    """공보 PDF의 실제 URL 후보들을 우선순위대로 생성."""
    candidates = []
    gid = row["gazette_id"]
    page_html = row.get("_page_html", "")

    m = re.search(r"-E-\d{4}(\d{4})-", gid)   # DDMMYYYY 중 연도
    year = m.group(1) if m else str(datetime.now().year)

    # 방법 1: 페이지 HTML에 박힌 실제 WriteReadData 경로 (가장 정확)
    for mm in re.finditer(r"WriteReadData/(\d{4})/(\d+)\.pdf", page_html):
        url = f"{PDF_BASE}/{mm.group(1)}/{mm.group(2)}.pdf"
        if url not in candidates:
            candidates.append(url)

    # 방법 2: Gazette ID 끝번호로 조합 (폴백)
    tail = gid.split("-")[-1]
    fallback = f"{PDF_BASE}/{year}/{tail}.pdf"
    if fallback not in candidates:
        candidates.append(fallback)

    return candidates


def try_download_pdf(row, session):
    """후보 URL들을 순서대로 시도해 PDF를 받는다. 성공 시 (url, bytes) 반환."""
    for url in build_pdf_url_candidates(row):
        try:
            resp = session.get(url, timeout=60)
            ctype = resp.headers.get("Content-Type", "")
            # PDF 시그니처(%PDF) 또는 content-type으로 유효성 확인
            if resp.status_code == 200 and (
                resp.content[:4] == b"%PDF" or "pdf" in ctype.lower()
            ):
                return url, resp.content
        except Exception as e:
            print(f"  [PDF 시도 실패] {url}: {e}")
    return None, None


def enrich_with_pdf(matches):
    """매칭 공보에 PDF(bytes/size/url)를 채운다."""
    if not ATTACH_PDF:
        for m in matches:
            m["_pdf_url"] = build_pdf_url_candidates(m)[0]
            m["_pdf_bytes"] = None
            m["_pdf_size"] = 0
        return

    s = requests.Session()
    s.headers.update(HEADERS)
    for m in matches:
        url, data = try_download_pdf(m, s)
        m["_pdf_url"] = url or build_pdf_url_candidates(m)[0]
        m["_pdf_bytes"] = data
        m["_pdf_size"] = len(data) if data else 0
        status = f"{m['_pdf_size']/1024/1024:.2f}MB" if data else "실패(링크로 대체)"
        print(f"  [PDF] {m['gazette_id']}: {status}")


def decide_attachments(matches):
    """25MB 한도 내에서 첨부/링크 분배."""
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
    """첨부 파일명: Gazette ID 기반 안전한 이름."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", row["gazette_id"])
    return f"{safe}.pdf"


def send_email(matches):
    """Gmail SMTP로 알림 발송 (PDF 첨부 + 초과분 링크)."""
    user = os.environ["GMAIL_USER"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("ALERT_TO", user)

    to_attach, to_link = decide_attachments(matches)

    # 본문 구성
    parts = [
        f"인도 eGazette 신규 공보 {len(matches)}건이 키워드에 매칭되었습니다.",
        f"확인 시각: {datetime.now():%Y-%m-%d %H:%M}",
        "",
    ]
    if to_attach:
        parts.append(f"■ 첨부 PDF: {len(to_attach)}건 (이 메일에 첨부됨)")
        for m in to_attach:
            parts.append(
                f"  · [{', '.join(m['_matched'])}] {m['subject']}\n"
                f"    부처: {m['ministry']} / 발행: {m['publish_date']} / ID: {m['gazette_id']}"
            )
        parts.append("")
    if to_link:
        parts.append(f"■ 링크 안내: {len(to_link)}건 (용량 초과 또는 다운로드 실패 → 아래 링크 확인)")
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

    # PDF 첨부
    for m in to_attach:
        part = MIMEApplication(m["_pdf_bytes"], _subtype="pdf")
        part.add_header("Content-Disposition", "attachment",
                        filename=pdf_filename(m))
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, app_pw)
        server.send_message(msg)
    print(f"[알림 발송] 총 {len(matches)}건 (첨부 {len(to_attach)}, 링크 {len(to_link)}) -> {to}")


def main():
    months = target_months()

    rows = []
    seen_ids = set()
    for mvalue, mlabel in MINISTRIES.items():
        for month, year in months:
            try:
                batch = fetch_ministry_month(mvalue, mlabel, month, year)
            except Exception as e:
                print(f"[에러] {mlabel} {year}-{month:02d} 조회 실패: {e}")
                raise
            for row in batch:
                if row["gazette_id"] not in seen_ids:
                    seen_ids.add(row["gazette_id"])
                    rows.append(row)
            print(f"[조회] {mlabel} {year}-{month:02d}: {len(batch)}건")

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
        enrich_with_pdf(new_matches)
        send_email(new_matches)
    else:
        print("[결과] 신규 매칭 없음")

    save_state(seen)


if __name__ == "__main__":
    main()
