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


def search_aladin(query: str, max_results: int = MAX_RESULTS) -> dict:
    session = _new_session()
    search_url = (
        "https://www.aladin.co.kr/search/wsearchresult.aspx"
        f"?SearchTarget=Book&SearchWord={urllib.parse.quote(query)}"
    )
    html = _get(search_url, session=session)

    # 실제 검색 결과 카드(ss_book_box) 안의 ItemId 들을 순서대로 모읍니다.
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
        return {"site": "aladin", "results": [], "error": "검색 결과 없음"}

    # 같은 session 을 스레드들이 공유 — requests.Session 은 GET 에 대해 안전합니다.
    with ThreadPoolExecutor(max_workers=len(item_ids)) as pool:
        books = list(
            pool.map(lambda iid: _fetch_aladin_book(iid, search_url, session), item_ids)
        )
    books = [b for b in books if b]
    if not books:
        return {"site": "aladin", "results": [], "error": "상세 페이지를 불러오지 못했어요"}
    return {"site": "aladin", "results": books}


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
    교보문고 검색 결과 페이지에서 각 prod_item 블록마다
    {id, title, author, publisher, cover} 를 미리 뽑아 둡니다.
    상세 페이지 파싱이 실패해도 이 정보를 대신 보여줄 수 있어요.
    """
    items: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'<li[^>]*class="[^"]*prod_item[^"]*"[^>]*>(.*?)</li>\s*(?=<li|</ul>)',
        html,
        re.DOTALL,
    ):
        block = m.group(1)

        id_m = re.search(r"/detail/(S\d+)", block)
        if not id_m:
            continue
        iid = id_m.group(1)
        if iid in seen:
            continue
        seen.add(iid)

        # 제목: img alt → prod_name → a[title] 순으로 시도, 대괄호 뱃지는 스킵
        title = _pick_first_clean([
            _search(r'<img[^>]+alt="([^"]+?)(?:\s*표지)?"', block),
            _search(r'class="prod_name"[^>]*>([^<]+)<', block),
            _search(r'class="prod_title"[^>]*>([^<]+)<', block),
            _search(r'<a[^>]+href="[^"]*/detail/S\d+"[^>]+title="([^"]+)"', block),
        ])

        author = (
            _search(r'class="[^"]*prod_author[^"]*"[^>]*>([^<]+)<', block)
            or _search(r'class="[^"]*author[^"]*"[^>]*>([^<]+)<', block)
        )

        publisher = _search(r'class="[^"]*prod_publish[^"]*"[^>]*>([^<]+)<', block)

        # lazy-load 이미지들: data-src, data-original, kbbfn 전용 속성들 등
        cover = (
            _search(r'<img[^>]+data-src="([^"]+)"', block)
            or _search(r'<img[^>]+data-original="([^"]+)"', block)
            or _search(r'<img[^>]+data-kbbfn-src="([^"]+)"', block)
            or _search(r'<img[^>]+src="([^"]+)"', block)
        )
        # 빈 placeholder 이미지는 무시
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
        detail = _get(detail_url, referer=search_url, session=session)
    except Exception:
        return fallback  # 상세가 실패해도 검색 메타로 보여줌

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
    session = _new_session()
    # gbCode=TOT 추가 — 브라우저에서 실제로 보내는 파라미터와 맞춰서 더 안정적
    search_url = (
        "https://search.kyobobook.co.kr/search"
        f"?keyword={urllib.parse.quote(query)}&gbCode=TOT&target=total"
    )
    html = _get(search_url, session=session)

    items = _parse_kyobo_search_items(html, max_results)
    if not items:
        # 기존 방식 폴백 — prod_item 이 안 잡히면 그냥 /detail/S 링크 순으로
        seen: set[str] = set()
        for m in re.finditer(r"/detail/(S\d+)", html):
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


def search_yes24(query: str, max_results: int = MAX_RESULTS) -> dict:
    # 예스24 는 쿠키가 있어야만 검색이 제대로 동작해서, 홈페이지로 먼저
    # 세션을 워밍업해 쿠키(ASP.NET_SessionId 등)를 받아 둡니다.
    session = _new_session()
    try:
        _get("https://www.yes24.com/", session=session)
    except Exception:
        pass  # 워밍업 실패해도 검색은 시도

    search_url = (
        "https://www.yes24.com/Product/Search"
        f"?domain=BOOK&query={urllib.parse.quote(query)}"
    )
    html = _get(search_url, referer="https://www.yes24.com/", session=session)

    # 실제 검색결과 상품명 근처의 /product/goods/ (소문자) 링크들을 모읍니다.
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
        return {"site": "yes24", "results": [], "error": "검색 결과 없음"}

    # session 을 그대로 공유 — cookies dict 변환 과정에서 KeyError 가 나는 이슈를 피합니다.
    with ThreadPoolExecutor(max_workers=len(goods_ids)) as pool:
        books = list(
            pool.map(lambda gid: _fetch_yes24_book(gid, search_url, session), goods_ids)
        )
    books = [b for b in books if b]
    if not books:
        return {"site": "yes24", "results": [], "error": "상세 페이지를 불러오지 못했어요"}
    return {"site": "yes24", "results": books}


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
