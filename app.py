"""
도서 카테고리 검색 서버
- 알라딘 / 교보문고 / 예스24 세 사이트에서 같은 책이 어떤 카테고리로
  분류되어 있는지 한 번에 긁어와서 비교해 주는 작은 백엔드입니다.
- 각 사이트별로 상위 5개 결과까지 가져옵니다.
- 저자명을 함께 입력하면 검색어에 합쳐서 더 정확한 결과를 찾습니다.
- 브라우저에서 바로 이 사이트들을 호출하면 CORS 때문에 막히기 때문에,
  파이썬 서버가 대신 호출해 주는 역할을 합니다.

실행 방법:
    pip install -r requirements.txt
    python app.py
    # 그러면 http://localhost:8000 주소로 사이트가 열려요.
"""

from __future__ import annotations

import html as html_mod
import json
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

# curl_cffi: Chrome TLS 핑거프린트를 흉내 내서 CloudFront/Cloudflare 같은 WAF 를
# 우회하는 용도. 교보문고 상세 페이지가 AWS CloudFront 뒤에 있어서 Render 의
# egress IP 를 봇으로 판정하는 문제가 있는데, curl_cffi 로 Chrome 을 impersonate
# 하면 통과하는 경우가 많아요. 설치 실패하면 조용히 requests 로 폴백합니다.
try:
    from curl_cffi import requests as cffi_requests  # type: ignore

    HAS_CFFI = True
except Exception:  # pragma: no cover
    cffi_requests = None  # type: ignore
    HAS_CFFI = False

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)  # 친구가 다른 주소에서 프론트엔드만 열어도 부를 수 있게

# ---- 공용 설정 ------------------------------------------------------------
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}
TIMEOUT = 12  # 초
MAX_RESULTS = 5  # 각 사이트별 최대 결과 개수


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def _get(
    url: str,
    referer: Optional[str] = None,
    session: Optional[requests.Session] = None,
) -> str:
    sess = session or requests
    headers = {}
    if referer:
        headers["Referer"] = referer
    r = sess.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text


def _unescape(s: str) -> str:
    """HTML 엔티티(&amp;, &lt; 등)를 원래 문자로 되돌립니다."""
    return html_mod.unescape(s).strip()


def _search(pattern: str, text: str, flags: int = 0) -> str:
    m = re.search(pattern, text, flags)
    return m.group(1) if m else ""


def _build_query(title: str, author: str = "") -> str:
    """도서명과 (선택) 저자명을 합쳐서 검색용 쿼리 문자열을 만듭니다."""
    title = (title or "").strip()
    author = (author or "").strip()
    if author:
        return f"{title} {author}".strip()
    return title


def _merge_fallback(book: Optional[dict], fallback: Optional[dict]) -> Optional[dict]:
    """상세 페이지에서 못 얻은 필드를 검색 페이지 정보로 채워 넣습니다."""
    if book is None:
        return fallback
    if not fallback:
        return book
    for k, v in fallback.items():
        if not book.get(k):
            book[k] = v
    return book


# ---- 알라딘 ---------------------------------------------------------------
def _fetch_aladin_book(
    item_id: str, search_url: str, session: requests.Session
) -> Optional[dict]:
    """알라딘 상세 페이지에서 책 1권 정보를 뽑아옵니다."""
    detail_url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
    try:
        detail = _get(detail_url, referer=search_url, session=session)
    except Exception:
        return None

    # JSON-LD 안의 genre / name / author / image 를 하나씩 빼냅니다.
    genre = _search(r'"genre"\s*:\s*"([^"]+)"', detail)
    name = _search(r'"name"\s*:\s*"([^"]+)"', detail)
    author = _search(r'"author"\s*:\s*"([^"]+)"', detail)
    publisher = _search(r'"publisher"\s*:\s*"([^"]+)"', detail)
    image = _search(r'"image"\s*:\s*"([^"]+)"', detail)

    # 알라딘은 한 책을 여러 카테고리에 등록해서 "한국소설, 테마문학, 해외 문학상"
    # 처럼 콤마로 나열해 줍니다. 대표 1개만 보여주도록 첫 값만 사용.
    if "," in genre:
        genre = genre.split(",")[0].strip()

    # 백업: 상세 페이지의 카테고리 경로 (ulCategory) 에서 첫 번째 경로만 뽑기
    full_path = _search(
        r'<ul[^>]+id="ulCategory"[^>]*>(.*?)</ul>', detail, flags=re.DOTALL
    )
    full = ""
    if full_path:
        parts = re.findall(r">([^<>]+)</a>", full_path)
        parts = [p.strip() for p in parts if p.strip()]
        # "국내도서"가 여러 번 나오면 두 번째 "국내도서" 직전에서 자릅니다.
        out: list[str] = []
        for p in parts:
            if p == "국내도서" and out:
                break
            out.append(p)
        full = " > ".join(out)

    return {
        "title": _unescape(name),
        "author": _unescape(author),
        "publisher": _unescape(publisher),
        "cover": image or "",
        "link": detail_url,
        "category": _unescape(genre),
        "category_full": full,
    }


def _search_aladin_once(query: str, search_target: str, max_results: int) -> list[dict]:
    """알라딘을 특정 SearchTarget(Book, All 등)으로 한 번 긁어오는 내부 함수."""
    session = _new_session()
    search_url = (
        "https://www.aladin.co.kr/search/wsearchresult.aspx"
        f"?SearchTarget={search_target}&SearchWord={urllib.parse.quote(query)}"
    )
    html = _get(search_url, session=session)

    item_ids: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'<div[^>]+class="ss_book_box[^"]*"[^>]*>.*?ItemId=(\d+)',
        html,
        re.DOTALL,
    ):
        iid = m.group(1)
        if iid in seen:
            continue
        seen.add(iid)
        item_ids.append(iid)
        if len(item_ids) >= max_results:
            break

    if not item_ids:
        return []

    with ThreadPoolExecutor(max_workers=len(item_ids)) as pool:
        books = list(
            pool.map(lambda iid: _fetch_aladin_book(iid, search_url, session), item_ids)
        )
    return [b for b in books if b]


def search_aladin(query: str, max_results: int = MAX_RESULTS) -> dict:
    # 1차: 일반 도서(Book)만 — 대부분 여기서 찾아져요.
    books = _search_aladin_once(query, "Book", max_results)
    if books:
        return {"site": "aladin", "results": books}

    # 2차 폴백: ebook/기타 상품까지 포함 (SearchTarget=All)
    books = _search_aladin_once(query, "All", max_results)
    if books:
        return {"site": "aladin", "results": books, "expanded": True}

    return {"site": "aladin", "results": [], "error": "검색 결과 없음"}


# ---- 교보문고 -------------------------------------------------------------
def _pick_first_clean(candidates: list[str]) -> str:
    """후보 문자열 중 '[국내도서]' 같은 카테고리 뱃지(대괄호 감싼 것)는 건너뛰고
    실제 값으로 보이는 것을 고릅니다."""
    for c in candidates:
        c = (c or "").strip()
        if not c:
            continue
        if c.startswith("[") and c.endswith("]"):
            continue  # 뱃지성 라벨은 제목이 아니에요
        return c
    return ""


def _extract_kyobo_jsonld(html: str) -> Optional[dict]:
    """교보문고 상세 페이지의 application/ld+json 블록 중에서
    @type 이 'Book' 인 블록을 찾아 dict 로 돌려줍니다.

    이 블록 하나에 name / image / author.name / publisher.name / genre 가
    전부 들어 있어서 가장 안정적인 정보 출처예요.
    """
    for m in re.finditer(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    ):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        # 단일 객체일 수도 있고, @graph 배열로 여러 개 묶여 있을 수도 있어요.
        candidates = []
        if isinstance(data, list):
            candidates.extend(data)
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                candidates.extend(data["@graph"])
            else:
                candidates.append(data)
        for d in candidates:
            if isinstance(d, dict) and d.get("@type") == "Book":
                return d
    return None


def _get_kyobo(url: str, referer: str = "https://search.kyobobook.co.kr/") -> tuple[int, int, dict, str]:
    """
    교보문고(검색/상세 페이지 모두)를 가져옵니다. CloudFront WAF 가
    requests 의 TLS 지문으로 차단을 걸 수 있어서, curl_cffi 가 있으면
    Chrome 을 impersonate 해서 먼저 시도하고, 그래도 본문이 비면 requests 로
    한 번 더 시도합니다.

    검색 페이지도 동일 WAF 에 걸리는 것으로 확인돼서, 검색과 상세 모두
    이 헬퍼를 경유합니다.

    반환: (status_code, raw_bytes_len, headers_dict, text)
    """
    # 실제 브라우저에서 보내는 전체 헤더 세트. 단순히 UA 만 바꾸는 것보다
    # Sec-Fetch-* / Sec-CH-UA 까지 맞추면 WAF 통과율이 올라갑니다.
    full_headers = {
        **BROWSER_HEADERS,
        "Referer": referer,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }

    # 1) curl_cffi 로 Chrome TLS 핑거프린트 흉내
    if HAS_CFFI:
        try:
            r = cffi_requests.get(
                url,
                headers=full_headers,
                impersonate="chrome120",
                timeout=TIMEOUT,
            )
            body = r.content or b""
            if len(body) > 0:
                text = body.decode(r.encoding or "utf-8", errors="replace")
                return (r.status_code, len(body), dict(r.headers), text)
        except Exception:
            pass  # 실패하면 아래 requests 폴백

    # 2) requests 폴백
    r2 = requests.get(url, headers=full_headers, timeout=TIMEOUT)
    body2 = r2.content or b""
    r2.encoding = r2.apparent_encoding or r2.encoding
    return (r2.status_code, len(body2), dict(r2.headers), r2.text)


def _clean_kyobo_author(raw: str) -> str:
    """ '알랭 드 보통 지음 | 정영목 번역' → '알랭 드 보통' 처럼
    여러 명이 묶여 있는 author 문자열에서 대표 저자만 깔끔하게 뽑아냅니다."""
    if not raw:
        return ""
    # '|' 또는 ',' 로 묶인 첫 인물만
    first = raw.split("|")[0].split(",")[0].strip()
    # '지음', '저자', '저', '엮음', '편저', '옮김', '번역', '글' 같은 꼬리표 제거
    first = re.sub(r"\s+(지음|저자|저|엮음|편저|옮김|번역|글)\s*$", "", first).strip()
    return first


def _parse_kyobo_search_items(html: str, max_results: int) -> list[dict]:
    """
    교보문고 검색 결과 페이지에서 /detail/S... 링크를 순서대로 찾고,
    각 링크 주변 HTML 윈도우에서 제목/표지를 긁어 옵니다.

    기존에는 li.prod_item 블록에만 의존했는데, 교보문고가 class 이름을
    조금만 바꿔도 전부 깨져서 — 지금은 class 구조에 덜 의존하는 방식으로
    바꿨습니다. 실제 제목/카테고리는 상세 페이지의 JSON-LD 에서
    최종적으로 덮어써지므로, 여기서는 '못 해도 빈값 대신 뭔가라도 보여주기'
    용 fallback 정도의 역할만 합니다.
    """
    items: list[dict] = []
    seen: set[str] = set()

    for m in re.finditer(r"/detail/([A-Z]?\d+)", html):
        iid = m.group(1)
        if iid in seen:
            continue
        seen.add(iid)

        # 링크 위치 양옆으로 ~3000자 윈도우를 떠서 이 제품에 속한 마크업만 본다고 가정.
        start = max(0, m.start() - 3000)
        end = min(len(html), m.end() + 3000)
        window = html[start:end]

        # 제목: 교보문고는 대개 <img alt="책제목 표지"> 형태를 써요.
        #       '표지' 접미사가 붙은 alt 를 먼저 시도합니다.
        title = _pick_first_clean([
            _search(r'<img[^>]+alt="([^"]+?)\s*표지"', window),
            _search(r'<img[^>]+alt="([^"]+?)"', window),
            _search(r'class="prod_name"[^>]*>([^<]+)<', window),
            _search(r'class="prod_title"[^>]*>([^<]+)<', window),
            _search(r'<a[^>]+href="[^"]*/detail/S\d+"[^>]+title="([^"]+)"', window),
        ])

        author = (
            _search(r'class="[^"]*prod_author[^"]*"[^>]*>([^<]+)<', window)
            or _search(r'class="[^"]*author[^"]*"[^>]*>([^<]+)<', window)
        )
        publisher = _search(r'class="[^"]*prod_publish[^"]*"[^>]*>([^<]+)<', window)

        # 표지: lazy-load 속성들 → 일반 src 순서
        cover = (
            _search(r'<img[^>]+data-kbbfn-src="([^"]+)"', window)
            or _search(r'<img[^>]+data-src="([^"]+)"', window)
            or _search(r'<img[^>]+data-original="([^"]+)"', window)
            or _search(r'<img[^>]+src="(https?://[^"]+pdt[^"]+)"', window)
        )
        if cover and ("placeholder" in cover.lower() or "blank" in cover.lower()):
            cover = ""

        items.append(
            {
                "id": iid,
                "title": _unescape(title),
                "author": _unescape(author),
                "publisher": _unescape(publisher),
                "cover": cover or "",
                "link": f"https://product.kyobobook.co.kr/detail/{iid}",
            }
        )
        if len(items) >= max_results:
            break
    return items


def _extract_kyobo_category(detail: str) -> tuple[str, str]:
    """교보문고 상세 페이지에서 카테고리 경로를 여러 패턴으로 시도해 뽑습니다.
    반환: (대표 카테고리, 전체 경로)"""
    parts: list[str] = []

    # 1) breadcrumb_list (예전 구조)
    for pattern in (
        r'<ol[^>]*class="[^"]*breadcrumb_list[^"]*"[^>]*>(.*?)</ol>',
        r'<[ou]l[^>]*class="[^"]*breadcrumb[^"]*"[^>]*>(.*?)</[ou]l>',
        r'<nav[^>]*class="[^"]*breadcrumb[^"]*"[^>]*>(.*?)</nav>',
    ):
        bc = _search(pattern, detail, re.DOTALL)
        if bc:
            found = re.findall(r">([^<>]+)</a>", bc)
            parts = [p.strip() for p in found if p.strip() and p.upper() != "HOME"]
            if parts:
                break

    # 2) "카테고리 분류/위치" 섹션 텍스트
    if not parts:
        cat_area = _search(
            r"카테고리\s*(?:분류|위치)[^<]*<[^>]+>(.*?)</(?:ul|ol|div|section)",
            detail,
            re.DOTALL,
        )
        if cat_area:
            found = re.findall(r">([^<>]+)</a>", cat_area)
            parts = [p.strip() for p in found if p.strip() and p.upper() != "HOME"]

    # 3) JSON-LD BreadcrumbList
    if not parts:
        ld = _search(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            detail,
            re.DOTALL,
        )
        if ld and "BreadcrumbList" in ld:
            names = re.findall(r'"name"\s*:\s*"([^"]+)"', ld)
            parts = [n.strip() for n in names if n.strip() and n.upper() != "HOME"]

    if not parts:
        return "", ""
    return parts[-1], " > ".join(parts)


def _fetch_kyobo_book(
    item: dict, search_url: str, session: requests.Session
) -> Optional[dict]:
    """
    교보문고 상세 페이지에서 카테고리/제목/저자/표지를 뽑아옵니다.
    실패하거나 필드가 비어 있으면 검색 페이지에서 먼저 뽑아 둔 fallback 으로 채웁니다.
    """
    iid = item["id"]
    detail_url = f"https://product.kyobobook.co.kr/detail/{iid}"

    fallback = {
        "title": item.get("title", ""),
        "author": item.get("author", ""),
        "publisher": item.get("publisher", ""),
        "cover": item.get("cover", ""),
        "link": detail_url,
        "category": "",
        "category_full": "",
    }

    try:
        status, raw_len, _hdrs, detail = _get_kyobo(detail_url, referer=search_url)
    except Exception as e:
        fb = dict(fallback)
        fb["category_full"] = f"(상세 페이지 접근 실패: {type(e).__name__})"
        return fb

    # CloudFront 가 200 + 빈 본문으로 조용히 차단하는 경우 처리
    if raw_len == 0 or not detail:
        fb = dict(fallback)
        fb["category_full"] = f"(CloudFront 차단 의심: status={status}, 본문 0바이트)"
        return fb

    # ① 1순위: JSON-LD Book 블록 — name/image/author/publisher/genre 가
    #          전부 깔끔하게 들어 있어서 여기서 거의 모든 필드를 채울 수 있어요.
    title = ""
    author = ""
    publisher = ""
    cover = ""
    category = ""
    category_full = ""

    ld_book = _extract_kyobo_jsonld(detail)
    if ld_book:
        title = (ld_book.get("name") or "").strip()
        cover = (ld_book.get("image") or "").strip()

        raw_author = ld_book.get("author") or ""
        if isinstance(raw_author, dict):
            raw_author = raw_author.get("name", "")
        elif isinstance(raw_author, list) and raw_author:
            first = raw_author[0]
            raw_author = first.get("name", "") if isinstance(first, dict) else str(first)
        author = _clean_kyobo_author(str(raw_author))

        raw_pub = ld_book.get("publisher") or ""
        if isinstance(raw_pub, dict):
            raw_pub = raw_pub.get("name", "")
        publisher = str(raw_pub).strip()

        # 교보문고에서는 genre 가 곧 카테고리(예: "시/에세이")여서 그대로 씁니다.
        genre = (ld_book.get("genre") or "").strip()
        if genre:
            category = genre
            category_full = genre

    # ② 2순위 보강: og:title / og:image — JSON-LD 가 없을 때만 사용
    if not title or not cover:
        # 교보문고의 og:title 포맷 → "책제목 | 저자 - 교보문고"
        og_title = _search(r'<meta property="og:title" content="([^"]+)"', detail)
        if not title and og_title:
            if " | " in og_title:
                left, _, right = og_title.partition(" | ")
                title = left.strip()
                if not author:
                    right = re.sub(r"\s*-\s*교보문고\s*$", "", right).strip()
                    author = right
            else:
                title = og_title.strip()
        if not cover:
            cover = (
                _search(r'<meta property="og:image" content="([^"]+)"', detail)
                or _search(r'<meta name="twitter:image" content="([^"]+)"', detail)
            )

    if not title:
        title = _pick_first_clean([
            _search(r'<span[^>]*class="[^"]*prod_title[^"]*"[^>]*>([^<]+)</span>', detail),
            _search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</h1>', detail),
            _search(r'<title>([^<|]+)', detail),
        ])

    if not author:
        author = (
            _search(r'<meta name="author" content="([^"]+)"', detail)
            or _search(r'data-author="([^"]+)"', detail)
        )
        author = _clean_kyobo_author(author)
    if not publisher:
        publisher = _search(r'data-publisher="([^"]+)"', detail)

    # ③ 카테고리: JSON-LD genre 가 없을 때만 breadcrumb 등으로 폴백
    if not category:
        category, category_full = _extract_kyobo_category(detail)

    book = {
        "title": _unescape(title),
        "author": _unescape(author),
        "publisher": _unescape(publisher),
        "cover": cover,
        "link": detail_url,
        "category": category,
        "category_full": category_full,
    }
    return _merge_fallback(book, fallback)


def search_kyobo(query: str, max_results: int = MAX_RESULTS) -> dict:
    # 검색 페이지도 CloudFront 뒤에 있어서, Render IP 에서 오는 요청에는
    # "결과 0건" 으로 응답하는 현상이 관측됐습니다. 그래서 curl_cffi 를
    # 거치는 _get_kyobo 로 Chrome 처럼 위장해서 가져옵니다.
    search_url = (
        "https://search.kyobobook.co.kr/search"
        f"?keyword={urllib.parse.quote(query)}&gbCode=TOT&target=total"
    )
    _status, _raw, _hdrs, html = _get_kyobo(
        search_url, referer="https://www.kyobobook.co.kr/"
    )
    session = _new_session()  # 상세 페이지 fallback 용 (거의 안 쓰이지만 시그니처 유지)

    items = _parse_kyobo_search_items(html, max_results)
    if not items:
        # 기존 방식 폴백 — prod_item 이 안 잡히면 그냥 /detail/S 링크 순으로
        seen: set[str] = set()
        for m in re.finditer(r"/detail/([A-Z]?\d+)", html):
            iid = m.group(1)
            if iid in seen:
                continue
            seen.add(iid)
            items.append(
                {
                    "id": iid,
                    "title": "",
                    "author": "",
                    "publisher": "",
                    "cover": "",
                    "link": f"https://product.kyobobook.co.kr/detail/{iid}",
                }
            )
            if len(items) >= max_results:
                break

    if not items:
        return {"site": "kyobo", "results": [], "error": "검색 결과 없음"}

    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        books = list(
            pool.map(lambda it: _fetch_kyobo_book(it, search_url, session), items)
        )
    books = [b for b in books if b]
    if not books:
        return {"site": "kyobo", "results": [], "error": "상세 페이지를 불러오지 못했어요"}
    return {"site": "kyobo", "results": books}


# ---- 예스24 ---------------------------------------------------------------
def _fetch_yes24_book(
    goods_id: str, search_url: str, session: requests.Session
) -> Optional[dict]:
    detail_url = f"https://www.yes24.com/Product/Goods/{goods_id}"
    try:
        detail = _get(detail_url, referer=search_url, session=session)
    except Exception:
        return None

    # "카테고리 분류" 섹션을 찾아서 첫 번째 <li> 안의 <a> 들을 순서대로 뽑기
    cat_section = _search(
        r"카테고리 분류.*?<ul[^>]*class=\"yesAlertLi\"[^>]*>(.*?)</ul>",
        detail,
        flags=re.DOTALL,
    )
    first_li = ""
    if cat_section:
        li_match = re.search(r"<li[^>]*>(.*?)</li>", cat_section, re.DOTALL)
        first_li = li_match.group(1) if li_match else ""
    parts: list[str] = []
    if first_li:
        parts = re.findall(r">([^<>]+)</a>", first_li)
        parts = [p.strip() for p in parts if p.strip() and p.strip() != "국내도서"]

    category = parts[-1] if parts else ""
    full = " > ".join(parts)

    # 제목/저자/출판사/이미지
    title = _search(r'<meta property="og:title" content="([^"]+)"', detail)
    cover = _search(r'<meta property="og:image" content="([^"]+)"', detail)
    # og:title 은 보통 "책제목 | 저자 | 출판사" 형태
    author = ""
    publisher = ""
    if title and " | " in title:
        bits = [b.strip() for b in title.split("|")]
        if len(bits) >= 3:
            title, author, publisher = bits[0], bits[1], bits[2]
        elif len(bits) == 2:
            title, author = bits[0], bits[1]

    return {
        "title": _unescape(title),
        "author": _unescape(author),
        "publisher": _unescape(publisher),
        "cover": cover,
        "link": detail_url,
        "category": category,
        "category_full": full,
    }


def _search_yes24_once(
    query: str, domain: str, session: requests.Session, max_results: int
) -> list[dict]:
    """예스24 를 특정 domain(BOOK, ALL 등)으로 한 번 긁어오는 내부 함수."""
    search_url = (
        "https://www.yes24.com/Product/Search"
        f"?domain={domain}&query={urllib.parse.quote(query)}"
    )
    html = _get(search_url, referer="https://www.yes24.com/", session=session)

    goods_ids: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'class="gd_name"[^>]*href="[^"]*?/product/goods/(\d+)',
        html,
    ):
        gid = m.group(1)
        if gid in seen:
            continue
        seen.add(gid)
        goods_ids.append(gid)
        if len(goods_ids) >= max_results:
            break

    # gd_name 못 찾으면 아무 /product/goods/ 링크나 폴백
    if not goods_ids:
        for m in re.finditer(r"/product/goods/(\d+)", html):
            gid = m.group(1)
            if gid in seen:
                continue
            seen.add(gid)
            goods_ids.append(gid)
            if len(goods_ids) >= max_results:
                break

    if not goods_ids:
        return []

    with ThreadPoolExecutor(max_workers=len(goods_ids)) as pool:
        books = list(
            pool.map(lambda gid: _fetch_yes24_book(gid, search_url, session), goods_ids)
        )
    return [b for b in books if b]


def search_yes24(query: str, max_results: int = MAX_RESULTS) -> dict:
    # 예스24 는 쿠키가 있어야만 검색이 제대로 동작해서, 홈페이지로 먼저
    # 세션을 워밍업해 쿠키(ASP.NET_SessionId 등)를 받아 둡니다.
    session = _new_session()
    try:
        _get("https://www.yes24.com/", session=session)
    except Exception:
        pass

    # 1차: 도서(BOOK)만
    books = _search_yes24_once(query, "BOOK", session, max_results)
    if books:
        return {"site": "yes24", "results": books}

    # 2차 폴백: 전체(ALL) — ebook 포함
    books = _search_yes24_once(query, "ALL", session, max_results)
    if books:
        return {"site": "yes24", "results": books, "expanded": True}

    return {"site": "yes24", "results": [], "error": "검색 결과 없음"}


# ---- 라우팅 헬퍼 ---------------------------------------------------------
SCRAPERS = {
    "aladin": search_aladin,
    "kyobo": search_kyobo,
    "yes24": search_yes24,
}


def _safe_call(name: str, query: str) -> dict:
    try:
        return SCRAPERS[name](query)
    except Exception as e:  # 한 사이트가 실패해도 다른 사이트는 계속 보여주려고
        return {"site": name, "results": [], "error": f"{type(e).__name__}: {e}"}


# ---- Flask 라우트 ---------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/debug/kyobo")
def api_debug_kyobo():
    """
    교보문고가 서버(Render)에 뭘 돌려주고 있는지 그대로 보여주는 진단 엔드포인트.
    브라우저에서 /api/debug/kyobo?q=불안 으로 호출하면,
    - 검색 페이지 HTTP 상태/길이/앞 1500자
    - 찾은 /detail/S... 링크 개수
    - 첫 상세 페이지 HTTP 상태/길이/앞 2000자
    - JSON-LD Book 블록을 찾았는지, og:title 이 있는지
    를 JSON 으로 돌려줍니다. 이 결과만 보면 교보문고가 봇 차단을
    하는 건지, 구조가 바뀐 건지 바로 판별 가능합니다.
    """
    q = (request.args.get("q") or "불안").strip()
    info: dict = {"query": q}
    search_url = (
        "https://search.kyobobook.co.kr/search"
        f"?keyword={urllib.parse.quote(q)}&gbCode=TOT&target=total"
    )
    info["search_url"] = search_url
    try:
        status, raw, hdrs, search_html = _get_kyobo(
            search_url, referer="https://www.kyobobook.co.kr/"
        )
        info["search_status"] = status
        info["search_raw_bytes"] = raw
        info["search_length"] = len(search_html)
        info["search_head_1500"] = search_html[:1500]
        info["search_headers"] = {
            k: v for k, v in hdrs.items()
            if k.lower() in ("content-length", "content-type", "server", "x-cache")
        }
        ids = list(dict.fromkeys(re.findall(r"/detail/([A-Z]?\d+)", search_html)))[:10]
        info["detail_ids_found"] = ids
        info["any_detail_links"] = list(dict.fromkeys(
            re.findall(r'/detail/([^\s"\'<>?#]+)', search_html)
        ))[:20]
        info["search_no_result_hint"] = any(
            kw in search_html
            for kw in ("검색결과가 없", "검색 결과가 없", "일치하는 상품이 없")
        )

        # --- 상품 카드 구조 진단 ---
        # body 안에서 책과 관련돼 보이는 href 샘플을 수집합니다.
        body_start = search_html.find("<body")
        body_section = (
            search_html[body_start : body_start + 40000]
            if body_start > -1
            else search_html[:40000]
        )
        hrefs = re.findall(r'href="([^"]+)"', body_section)
        book_hrefs = [
            h for h in hrefs
            if any(k in h for k in ("kyobobook", "/detail", "/ebook", "/sam", "/product", "/store"))
            and not h.endswith((".css", ".js", ".png", ".jpg", ".svg", ".ico"))
        ]
        info["book_href_samples"] = list(dict.fromkeys(book_hrefs))[:30]

        # 첫 번째 상품 카드 블록 통째로. prod_item 또는 유사한 class 를 가진
        # li/div 블록 하나만 잘라서 보여줍니다. 이걸 보면 실제 상품 URL 패턴,
        # data-* 속성, 제목 마크업이 모두 드러납니다.
        first_prod = None
        for pattern in (
            r'<li[^>]*class="[^"]*prod_item[^"]*"[^>]*>.*?</li>',
            r'<li[^>]*class="[^"]*prod[^"]*"[^>]*>.*?</li>',
            r'<div[^>]*class="[^"]*prod_list_area[^"]*"[^>]*>.*?</div>\s*</section>',
            r'<article[^>]*>.*?</article>',
            r'<div[^>]*class="[^"]*item[^"]*"[^>]*>.*?</div>',
        ):
            m = re.search(pattern, body_section, re.DOTALL)
            if m:
                first_prod = m.group(0)[:3500]
                break
        info["first_prod_block"] = first_prod

        # 검색 결과 영역으로 추정되는 컨테이너만 잘라서도 한번:
        result_area = re.search(
            r'<(?:section|div)[^>]*class="[^"]*(?:search_result|result_area|prod_list_area|prod_area)[^"]*"[^>]*>(.*?)</(?:section|div)>',
            body_section,
            re.DOTALL,
        )
        info["result_area_head"] = (
            result_area.group(1)[:3000] if result_area else None
        )
    except Exception as e:
        info["search_error"] = f"{type(e).__name__}: {e}"
        return jsonify(info)

    if not ids:
        return jsonify(info)

    first_id = ids[0]
    detail_url = f"https://product.kyobobook.co.kr/detail/{first_id}"
    info["detail_url"] = detail_url
    info["has_curl_cffi"] = HAS_CFFI
    try:
        status, raw_len, hdrs, detail_html = _get_kyobo(detail_url, search_url)
        info["detail_status"] = status
        info["detail_raw_bytes"] = raw_len
        info["detail_headers"] = {
            k: v
            for k, v in hdrs.items()
            if k.lower()
            in (
                "content-encoding",
                "content-length",
                "content-type",
                "server",
                "x-cache",
                "x-bot-check",
                "cf-ray",
            )
        }
        info["detail_length"] = len(detail_html)
        info["detail_head_2000"] = detail_html[:2000]
        info["has_jsonld_book"] = _extract_kyobo_jsonld(detail_html) is not None
        og = _search(r'<meta property="og:title" content="([^"]+)"', detail_html)
        info["og_title"] = og
        info["has_breadcrumb_keyword"] = "breadcrumb" in detail_html.lower()
        info["has_captcha_keyword"] = any(
            kw in detail_html.lower()
            for kw in ("captcha", "접속이 차단", "접속이차단", "bot", "blocked", "access denied")
        )
    except Exception as e:
        info["detail_error"] = f"{type(e).__name__}: {e}"
    return jsonify(info)


@app.route("/api/search")
def api_search():
    title = (request.args.get("q") or "").strip()
    author = (request.args.get("author") or "").strip()
    if not title:
        return jsonify({"error": "검색어가 비어 있어요."}), 400

    query = _build_query(title, author)

    # 세 사이트를 동시에 병렬로 긁어옵니다. (순차 호출보다 훨씬 빨라요)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(_safe_call, name, query) for name in SCRAPERS}
        results = {name: f.result() for name, f in futures.items()}

    return jsonify({
        "query": query,
        "title": title,
        "author": author,
        "results": results,
    })


if __name__ == "__main__":
    import os
    # Mac 의 AirPlay 수신자가 5000 포트를 쓰고 있어서 기본은 8000 번.
    # Render 등 배포 환경에서는 PORT 환경변수가 자동으로 들어옵니다.
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
