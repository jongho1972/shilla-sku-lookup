"""신라인터넷면세점 SKU 조회 웹앱.

SKU 번호 입력 → 국문/영문 브랜드명, 상품명, REF.NO, 상품문의 전화번호 조회.
신라 API: ajaxProducts (CSRF 토큰 필요) + 상품 상세 페이지 파싱.
"""

import json
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup
from curl_cffi import requests as creq
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

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


def _fetch_product(sku: str) -> dict:
    sess = creq.Session(impersonate="chrome")
    sess.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})

    # 1. 검색 페이지에서 CSRF 토큰 획득
    page = sess.get(SHILLA_SEARCH.format(q=sku), timeout=15)
    m = _CSRF_RE.search(page.text)
    token = m.group(1) if m else ""

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

    # SKU가 응답 JSON에 그대로 포함된 항목을 우선 선택, 없으면 첫 번째
    hit = next((it for it in results if sku in json.dumps(it)), results[0])

    code = hit.get("code", "")
    brand_raw = (
        hit.get("brandName")
        or (hit.get("brandCategory") or {}).get("brandName", "")
        or ""
    )
    product_name = hit.get("productNameForDisp") or hit.get("name") or ""

    # brandName이 "한글명 | 영문명" 형태인 경우 분리
    if " | " in brand_raw:
        brand_kr, brand_en = [b.strip() for b in brand_raw.split(" | ", 1)]
    else:
        brand_kr = brand_raw.strip()
        brand_en = ""

    # 3. 상세 페이지에서 영문 브랜드명, REF.NO, 전화번호 파싱
    #    - strong.info_brand: "한글명 | 영문명"
    #    - strong.number_title → 부모 li 텍스트: REF.NO / SKU.NO / 상품 문의
    ref_no = code
    phone = ""
    if code:
        try:
            dr = sess.get(SHILLA_DETAIL_M.format(code=code), timeout=15)
            soup = BeautifulSoup(dr.text, "html.parser")

            # 브랜드명 (영문 포함)
            info_brand = soup.select_one("strong.info_brand")
            if info_brand:
                brand_raw = info_brand.get_text(strip=True)
                if " | " in brand_raw:
                    brand_kr, brand_en = [b.strip() for b in brand_raw.split(" | ", 1)]

            # REF.NO / SKU.NO / 상품 문의
            for s in soup.select("strong.number_title"):
                label = s.get_text(strip=True)
                li = s.parent
                value = li.get_text(strip=True).replace(label, "", 1).strip()
                if "REF" in label.upper():
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
        "product_name": product_name,
        "phone": phone,
        "detail_url": SHILLA_DETAIL_PC.format(code=code) if code else "",
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def root():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/api/lookup")
async def lookup(sku: str):
    sku = sku.strip()
    if not sku:
        raise HTTPException(status_code=400, detail="SKU 번호를 입력해주세요")

    import asyncio
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _fetch_product, sku)

    if not result:
        raise HTTPException(status_code=404, detail="상품을 찾을 수 없습니다. SKU 번호를 확인해주세요.")

    return result
