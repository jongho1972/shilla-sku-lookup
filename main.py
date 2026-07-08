"""신라인터넷면세점 SKU 조회 웹앱.

SKU 번호 입력 → 국문/영문 브랜드명, 상품명, REF.NO, 상품문의 전화번호 조회.
신라 API: ajaxProducts (CSRF 토큰 필요) + 상품 상세 페이지 파싱.
"""

import asyncio
import json
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import List
from urllib.parse import quote

from bs4 import BeautifulSoup
from curl_cffi import requests as creq
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI(title="신라 SKU 조회")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SHILLA_SEARCH = "https://m.shilladfs.com/estore/kr/ko/search?query={q}"
SHILLA_AJAX = "https://m.shilladfs.com/estore/kr/ko/ajaxProducts"
SHILLA_DETAIL_M = "https://m.shilladfs.com/estore/kr/ko/p/{code}"   # 스크래핑용
SHILLA_DETAIL_PC = "https://www.shilladfs.com/estore/kr/ko/p/{code}"  # 링크 노출용

_PHONE_RE = re.compile(r"\d{2,3}-\d{3,4}-\d{4}")
_CSRF_RE = re.compile(r"CSRFToken['\"\s:=]+([0-9a-f-]{36})")


def _make_session() -> creq.Session:
    sess = creq.Session(impersonate="chrome")
    sess.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    return sess


def _get_csrf(sess: creq.Session, query: str) -> str:
    page = sess.get(SHILLA_SEARCH.format(q=query), timeout=15)
    m = _CSRF_RE.search(page.text)
    return m.group(1) if m else ""


def _extract_category(hit: dict) -> str:
    """검색 API 응답의 "코드:카테고리명" 목록에서 카테고리명 추출 (2뎁스가 더 구체적이면 우선)."""
    for cat_key in ("disp2DepthCategoryList", "disp1DepthCategoryList"):
        cat_items = hit.get(cat_key)
        if isinstance(cat_items, list) and cat_items:
            last = str(cat_items[-1])
            category = last.split(":", 1)[-1].strip() if ":" in last else last.strip()
            if category:
                return category
    return ""


def _fetch_product(sku: str) -> dict:
    sess = _make_session()

    # 1. 검색 페이지에서 CSRF 토큰 획득
    token = _get_csrf(sess, sku)

    # 2. SKU로 상품 검색
    body = {
        "json": json.dumps({
            "category": "", "size": "10", "page": 0,
            "text": sku, "within": "", "query": sku,
            "pagination": "", "condition": {"discountRate": "0"},
        }, ensure_ascii=False),
        "CSRFToken": token,
    }
    r = sess.post(SHILLA_AJAX, data=body,
                  headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])

    if not results:
        return {}

    # skuNo 필드로 정확 매칭, 없으면 미조회 처리
    hit = next((it for it in results if it.get("skuNo") == sku), None)
    if not hit:
        return {}

    code = hit.get("code", "")
    brand_cat = hit.get("brandCategory") or {}
    brand_kr = (hit.get("brandName") or brand_cat.get("brandName") or "").strip()
    brand_en = (brand_cat.get("enName") or "").strip()

    category = _extract_category(hit)

    product_name = hit.get("productNameForDisp") or hit.get("name") or ""

    # 3. 상세 페이지에서 영문 브랜드명, 전화번호 파싱 (REF.NO는 API 응답에서 직접 사용)
    #    - strong.info_brand: "한글명 | 영문명"
    #    - strong.number_title → 부모 li 텍스트: 상품 문의
    ref_no = hit.get("refNo", "") or code
    phone = ""
    if code:
        try:
            dr = sess.get(SHILLA_DETAIL_M.format(code=code), timeout=15)
            soup = BeautifulSoup(dr.text, "html.parser")

            # info_brand 로 영문명 보완 (brandCategory.enName 없는 경우 폴백)
            if not brand_en:
                info_brand = soup.select_one("strong.info_brand")
                if info_brand:
                    ib_text = info_brand.get_text(strip=True)
                    if " | " in ib_text:
                        brand_kr, brand_en = [b.strip() for b in ib_text.split(" | ", 1)]

            # 상품유형: API 응답에서 못 얻었을 때만 브레드크럼 마지막 활성 항목으로 보조
            if not category:
                bc_items = soup.select("ul.breadcrumb_box li.on")
                if bc_items:
                    category = bc_items[-1].get_text(strip=True)

            # 상품 문의 전화번호 파싱
            for s in soup.select("strong.number_title"):
                label = s.get_text(strip=True)
                li = s.parent
                value = li.get_text(strip=True).replace(label, "", 1).strip()
                if "REF" in label.upper() and not hit.get("refNo"):
                    ref_no = value or ref_no
                elif "문의" in label:
                    m2 = _PHONE_RE.search(value)
                    if m2:
                        phone = m2.group(0)

        except Exception as e:
            logger.warning("상세 페이지 파싱 실패 code=%s: %s", code, e)

    return {
        "sku": sku,
        "ref_no": ref_no,
        "brand_kr": brand_kr,
        "brand_en": brand_en,
        "category": category,
        "product_name": product_name,
        "phone": phone,
        "detail_url": SHILLA_DETAIL_PC.format(code=code) if code else "",
    }


@app.get("/shilla_logo.png")
async def logo():
    return FileResponse(Path(__file__).parent / "shilla_logo.png", media_type="image/png")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(Path(__file__).parent / "index.html")


def _search_keyword(keyword: str, size: int = 50) -> list[dict]:
    sess = _make_session()
    token = _get_csrf(sess, keyword)

    body = {
        "json": json.dumps({
            "category": "", "size": str(size), "page": 0,
            "text": keyword, "within": "", "query": keyword,
            "pagination": "", "condition": {"discountRate": "0"},
        }, ensure_ascii=False),
        "CSRFToken": token,
    }
    r = sess.post(SHILLA_AJAX, data=body,
                  headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])

    items = []
    for it in results:
        brand_cat = it.get("brandCategory") or {}
        brand_kr = (it.get("brandName") or brand_cat.get("brandName") or "").strip()
        brand_en = (brand_cat.get("enName") or "").strip()
        code = it.get("code", "")
        items.append({
            "sku": it.get("skuNo", ""),
            "brand_kr": brand_kr,
            "brand_en": brand_en,
            "category": _extract_category(it),
            "product_name": it.get("productNameForDisp") or it.get("name") or "",
            "ref_no": it.get("refNo", "") or code,
            "soldout": it.get("soldOutYn") == "Y",
            "detail_url": SHILLA_DETAIL_PC.format(code=code) if code else "",
        })
    return items


@app.get("/api/search")
async def search_keyword(keyword: str):
    keyword = keyword.strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="검색어를 입력해주세요")
    if len(keyword) < 2:
        raise HTTPException(status_code=400, detail="검색어를 2자 이상 입력해주세요")

    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _search_keyword, keyword)
    return results


@app.get("/api/lookup")
async def lookup(sku: str):
    sku = sku.strip()
    if not sku:
        raise HTTPException(status_code=400, detail="SKU 번호를 입력해주세요")

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _fetch_product, sku)

    if not result:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다. SKU 번호를 확인해주세요.")

    return result


class BatchRequest(BaseModel):
    skus: List[str]


@app.post("/api/batch")
async def batch_lookup(req: BatchRequest):
    skus = [s.strip() for s in req.skus if s.strip()]
    if not skus:
        raise HTTPException(status_code=400, detail="SKU 번호를 입력해주세요")
    if len(skus) > 50:
        raise HTTPException(status_code=400, detail="한 번에 최대 50개까지 조회할 수 있습니다")

    loop = asyncio.get_running_loop()
    sem = asyncio.Semaphore(5)  # 최대 5개 동시 요청

    async def fetch_one(sku: str):
        async with sem:
            try:
                result = await loop.run_in_executor(None, _fetch_product, sku)
                return result if result else {"sku": sku, "error": True}
            except Exception as e:
                logger.warning("배치 조회 실패 sku=%s: %s", sku, e)
                return {"sku": sku, "error": True}

    results = await asyncio.gather(*[fetch_one(sku) for sku in skus])
    return list(results)


class ExportRequest(BaseModel):
    kind: str  # "batch" | "keyword"
    keyword: str = ""
    rows: List[dict]


def _display_width(s: str) -> float:
    """한글 등 전각 문자는 폭 2배로 계산해 엑셀 열 너비 근사치를 구한다."""
    import unicodedata
    return sum(1.7 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in s)


def _autofit_columns(ws, min_width: float = 8, max_width: float = 70) -> None:
    for col_cells in ws.columns:
        max_len = 0.0
        for cell in col_cells:
            if cell.value is None:
                continue
            max_len = max(max_len, _display_width(str(cell.value)))
        ws.column_dimensions[col_cells[0].column_letter].width = max(min_width, min(max_len + 2, max_width))


def _xlsx_response(wb, filename: str) -> Response:
    buf = BytesIO()
    wb.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=\"export.xlsx\"; filename*=UTF-8''{quote(filename)}"
        },
    )


@app.post("/api/export")
async def export_excel(req: ExportRequest):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    head_font = Font(bold=True, color="0B2E5C")
    link_font = Font(color="1F6FEB", underline="single")

    if req.kind == "keyword":
        ws.title = "키워드검색"
        headers = ["#", "국문 브랜드명", "영문 브랜드명", "상품유형", "상품명", "SKU.NO", "REF.NO", "상품 페이지"]
    else:
        ws.title = "SKU조회"
        headers = ["#", "국문 브랜드명", "영문 브랜드명", "상품유형", "상품명", "SKU.NO", "REF.NO", "상품 문의", "상품 페이지"]

    ws.append(headers)
    for c in ws[1]:
        c.font = head_font

    for i, r in enumerate(req.rows, start=1):
        if r.get("error"):
            ws.append([i, f"SKU {r.get('sku', '')} — 조회 실패"])
            continue
        if req.kind == "keyword":
            ws.append([i, r.get("brand_kr", ""), r.get("brand_en", ""), r.get("category", ""),
                       r.get("product_name", ""), r.get("sku", ""), r.get("ref_no", ""), ""])
            link_col = 8
        else:
            ws.append([i, r.get("brand_kr", ""), r.get("brand_en", ""), r.get("category", ""),
                       r.get("product_name", ""), r.get("sku", ""), r.get("ref_no", ""),
                       r.get("phone", ""), ""])
            link_col = 9

        url = r.get("detail_url")
        cell = ws.cell(row=ws.max_row, column=link_col)
        if url:
            cell.value = "상품 페이지"
            cell.hyperlink = url
            cell.font = link_font
        else:
            cell.value = "—"

    _autofit_columns(ws)
    ws.freeze_panes = "A2"

    filename = f"키워드검색_{req.keyword}.xlsx" if req.kind == "keyword" else "SKU조회.xlsx"
    return _xlsx_response(wb, filename)
