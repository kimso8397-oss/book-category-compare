"""
도서 카테고리 검색 서버
- 알라딘 / 교보문고 / 예스24 세 사이트에서 같은 책이 어떤 카테고리로
  분류되어 있는지 한 번에 긁어와서 비교해 주는 작은 백엔드입니다.
- 브라우저에서 바로 이 사이트들을 호출하면 CORS 때문에 막히기 때문에,
  파이썬 서버가 대신 호출해 주는 역할을 합니다.

실행 방법:
    pip install -r requirements.txt
    python app.py
    # 그러면 http://localhost:5000 주소로 사이트가 열려요.
"""

from __future__ import annotations

import html as html_mod
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


# ---- 알라딘 ---------------------------------------------------------------
def search_aladin(query: str) -> dict:
    session = _new_session()
    search_url = (
        "https://www.aladin.co.kr/search/wsearchresult.aspx"
        f"?SearchTarget=Book&SearchWord={urllib.parse.quote(query)}"
    )
    html = _get(search_url, session=session)

    # 헤더/배너가 아닌 실제 검색 결과 카드(ss_book_box) 에서 첫 번째 ItemId 를 뽑기
    m = re.search(
        r'<div[^>]+class="ss_book_box[^"]*"[^>]*>.*?ItemId=(\d+)',
        html,
        re.DOTALL,
    )
    if not m:
        # 폴백: 처음에 나오는 ItemId
        m = re.search(r"ItemId=(\d+)", html)
    if not m:
        return {"site": "aladin", "error": "검색 결과 없음"}
    item_id = m.group(1)

    detail_url = f"https://www.aladin.co.kr/shop/wproduct.aspx?ItemId={item_id}"
    detail = _get(detail_url, referer=search_url, session=session)

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
        "site": "aladin",
        "title": _unescape(name or query),
        "author": _unescape(author),
        "publisher": _unescape(publisher),
        "cover": image or "",
        "link": detail_url,
        "category": _unescape(genre),
        "category_full": full,
    }


# ---- 교보문고 -------------------------------------------------------------
def search_kyobo(query: str) -> dict:
    session = _new_session()
    search_url = (
        "https://search.kyobobook.co.kr/search"
        f"?keyword={urllib.parse.quote(query)}&target=total"
    )
    html = _get(search_url, session=session)

    # 실제 상품 카드 <li class="prod_item"> 안의 첫 번째 detail 링크
    m = re.search(
        r'<li class="prod_item"[^>]*>.*?/detail/(S\d+)',
        html,
        re.DOTALL,
    )
    if not m:
        m = re.search(r"/detail/(S\d+)", html)
    if not m:
        return {"site": "kyobo", "error": "검색 결과 없음"}
    item_id = m.group(1)

    detail_url = f"https://product.kyobobook.co.kr/detail/{item_id}"
    detail = _get(detail_url, referer=search_url, session=session)

    # breadcrumb_list 안의 모든 <a>를 순서대로 모아요. (HOME > 국내도서 > 소설 > ...)
    breadcrumb_html = _search(
        r'<ol[^>]*class="breadcrumb_list"[^>]*>(.*?)</ol>',
        detail,
        flags=re.DOTALL,
    )
    parts: list[str] = []
    if breadcrumb_html:
        parts = re.findall(r">([^<>]+)</a>", breadcrumb_html)
        parts = [p.strip() for p in parts if p.strip()]
        # HOME 같은 빈 경로는 제외
        parts = [p for p in parts if p.upper() != "HOME"]

    category = parts[-1] if parts else ""
    full = " > ".join(parts)

    # 상세에서 제목/작가/출판사/커버 꺼내기
    title = _search(r'<span class="prod_title">([^<]+)</span>', detail) or query
    author = _search(r'data-author="([^"]+)"', detail)
    publisher = _search(r'data-publisher="([^"]+)"', detail)
    cover = _search(r'<meta property="og:image" content="([^"]+)"', detail)

    return {
        "site": "kyobo",
        "title": _unescape(title),
        "author": _unescape(author),
        "publisher": _unescape(publisher),
        "cover": cover,
        "link": detail_url,
        "category": category,
        "category_full": full,
    }


# ---- 예스24 ---------------------------------------------------------------
def search_yes24(query: str) -> dict:
    # 예스24 는 쿠키가 있어야만 검색이 제대로 동작해서, 홈페이지로 먼저
    # 세션을 워밍업해 쿠키(ASP.NET_SessionId 등)를 받아 둡니다.
    session = _new_session()
    _get("https://www.yes24.com/", session=session)

    search_url = (
        "https://www.yes24.com/Product/Search"
        f"?domain=BOOK&query={urllib.parse.quote(query)}"
    )
    html = _get(search_url, referer="https://www.yes24.com/", session=session)

    # 실제 검색결과 상품명 근처의 /product/goods/ (소문자) 링크
    m = re.search(
        r'class="gd_name"[^>]*href="[^"]*?/product/goods/(\d+)',
        html,
    )
    if not m:
        m = re.search(r"/product/goods/(\d+)", html)
    if not m:
        return {"site": "yes24", "error": "검색 결과 없음"}
    goods_id = m.group(1)

    detail_url = f"https://www.yes24.com/Product/Goods/{goods_id}"
    detail = _get(detail_url, referer=search_url, session=session)

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
    title = (
        _search(r'<meta property="og:title" content="([^"]+)"', detail) or query
    )
    cover = _search(r'<meta property="og:image" content="([^"]+)"', detail)
    # og:title 은 보통 "책제목 | 저자 | 출판사" 형태
    author = ""
    publisher = ""
    if " | " in title:
        bits = [b.strip() for b in title.split("|")]
        if len(bits) >= 3:
            title, author, publisher = bits[0], bits[1], bits[2]
        elif len(bits) == 2:
            title, author = bits[0], bits[1]

    return {
        "site": "yes24",
        "title": _unescape(title),
        "author": _unescape(author),
        "publisher": _unescape(publisher),
        "cover": cover,
        "link": detail_url,
        "category": category,
        "category_full": full,
    }


# ---- 유틸 -----------------------------------------------------------------
def _search(pattern: str, text: str, flags: int = 0) -> str:
    m = re.search(pattern, text, flags)
    return m.group(1) if m else ""


SCRAPERS = {
    "aladin": search_aladin,
    "kyobo": search_kyobo,
    "yes24": search_yes24,
}


def _safe_call(name: str, query: str) -> dict:
    try:
        return SCRAPERS[name](query)
    except Exception as e:  # 한 사이트가 실패해도 다른 사이트는 계속 보여주려고
        return {"site": name, "error": f"{type(e).__name__}: {e}"}


# ---- Flask 라우트 ---------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify({"error": "검색어가 비어 있어요."}), 400

    # 세 사이트를 동시에 병렬로 긁어옵니다. (순차 호출보다 훨씬 빨라요)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(_safe_call, name, query) for name in SCRAPERS}
        results = {name: f.result() for name, f in futures.items()}

    return jsonify({"query": query, "results": results})


if __name__ == "__main__":
    import os
    # Mac 의 AirPlay 수신자가 5000 포트를 쓰고 있어서 기본은 8000 번.
    # Render 등 배포 환경에서는 PORT 환경변수가 자동으로 들어옵니다.
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
