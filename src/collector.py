# -*- coding: utf-8 -*-
"""
쿠팡 Wing 매출 수집기
---------------------------------------------
쿠팡 Open API에서 매출/정산/주문/반품·취소 데이터를 받아
1) 이 폴더의 data.db(SQLite)에 누적 저장하고
2) 요약표를 만들어 회사 구글 시트로 전송합니다.

외부 라이브러리 없이 파이썬 기본 기능만 사용합니다.
실행:  python collector.py            (평소 자동 실행)
       python collector.py --backfill 180   (과거 180일 다시 받기)
       python collector.py --test     (API 연결만 확인)
"""
import argparse
import configparser
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))
API_HOST = "https://api-gateway.coupang.com"
SSL_CTX = ssl.create_default_context()

log = logging.getLogger("collector")
DUMP = False  # --dump 옵션: 쿠팡 응답 원문을 logs 폴더에 저장 (문제 확인용)
_dump_n = [0]


# ─────────────────────────────── 공통 유틸 ───────────────────────────────
def today_kst():
    return datetime.now(KST).date()


def d2s(d):
    return d.strftime("%Y-%m-%d")


def chunks(start, end, days):
    """start~end(포함)를 days일 단위 구간으로 나눔"""
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def money(v):
    """쿠팡 금액 필드: 숫자 또는 {units, nanos} 형태 모두 처리"""
    if v is None:
        return 0
    if isinstance(v, dict):
        return (v.get("units") or 0) + (v.get("nanos") or 0) / 1e9
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────── 쿠팡 API ───────────────────────────────
class CoupangAPIError(Exception):
    pass


class Coupang:
    def __init__(self, vendor_id, access_key, secret_key):
        self.vendor_id = vendor_id.strip()
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()

    def _auth(self, method, path, query):
        signed = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
        msg = signed + method + path + query
        sig = hmac.new(self.secret_key.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return (f"CEA algorithm=HmacSHA256, access-key={self.access_key}, "
                f"signed-date={signed}, signature={sig}")

    def get(self, path, params, retries=4, delay=0.25):
        query = urllib.parse.urlencode(params)
        url = f"{API_HOST}{path}?{query}" if query else f"{API_HOST}{path}"
        for attempt in range(retries):
            req = urllib.request.Request(url, method="GET")
            req.add_header("Content-Type", "application/json;charset=UTF-8")
            req.add_header("Authorization", self._auth("GET", path, query))
            req.add_header("X-Requested-By", self.vendor_id)
            try:
                with urllib.request.urlopen(req, context=SSL_CTX, timeout=60) as r:
                    body = r.read().decode("utf-8")
                time.sleep(delay)  # 호출 간격 (과다 호출 방지)
                if DUMP and _dump_n[0] < 40:
                    _dump_n[0] += 1
                    fn = os.path.join(BASE_DIR, "logs", f"raw_{_dump_n[0]:02d}.txt")
                    with open(fn, "w", encoding="utf-8") as f:
                        f.write(path + "?" + query + "\n\n" + body[:20000])
                return json.loads(body)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code in (429, 500, 502, 503, 504, 412) and attempt < retries - 1:
                    wait = (20 if e.code == 429 else 3) * (attempt + 1)
                    log.warning("쿠팡 응답 %s, %s초 후 재시도: %s", e.code, wait, detail)
                    time.sleep(wait)
                    continue
                if e.code in (401, 403):
                    raise CoupangAPIError(
                        f"인증 실패({e.code}). API 키가 만료됐거나, 이 PC의 IP가 쿠팡 Wing에 "
                        f"등록된 IP와 다를 수 있습니다. 상세: {detail}")
                raise CoupangAPIError(f"쿠팡 API 오류 {e.code}: {detail}")
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(5)
                    continue
                raise CoupangAPIError(f"네트워크 오류: {e}")
        raise CoupangAPIError("재시도 횟수 초과")

    # 매출내역 (매출인식일 기준, 어제까지만 조회 가능)
    def revenue_history(self, d_from, d_to):
        path = "/v2/providers/openapi/apis/api/v1/revenue-history"
        token = ""
        while True:
            res = self.get(path, {
                "vendorId": self.vendor_id,
                "recognitionDateFrom": d2s(d_from),
                "recognitionDateTo": d2s(d_to),
                "token": token,
                "maxPerPage": 50,
            })
            for row in res.get("data") or []:
                yield row
            token = res.get("nextToken") or ""
            if not res.get("hasNext") or not token:
                break

    # 발주서(주문) 목록 - 일단위
    def ordersheets(self, d_from, d_to, status):
        path = f"/v2/providers/openapi/apis/api/v5/vendors/{self.vendor_id}/ordersheets"
        token = ""
        while True:
            params = {
                "createdAtFrom": d2s(d_from) + "+09:00",
                "createdAtTo": d2s(d_to) + "+09:00",
                "status": status,
                "maxPerPage": 50,
            }
            if token:
                params["nextToken"] = token
            res = self.get(path, params)
            for row in res.get("data") or []:
                yield row
            token = res.get("nextToken") or ""
            if not token:
                break

    # 반품/취소 요청 목록
    def return_requests(self, d_from, d_to, cancel_type, status=None):
        path = f"/v2/providers/openapi/apis/api/v6/vendors/{self.vendor_id}/returnRequests"
        token = ""
        while True:
            params = {
                "createdAtFrom": d2s(d_from),
                "createdAtTo": d2s(d_to),
                "cancelType": cancel_type,
                "maxPerPage": 50,
            }
            if status:
                params["status"] = status
            if token:
                params["nextToken"] = token
            res = self.get(path, params)
            for row in res.get("data") or []:
                yield row
            token = res.get("nextToken") or ""
            if not token:
                break

    # 로켓그로스 주문 (결제일 기준, 최대 30일)
    def rg_orders(self, d_from, d_to):
        path = f"/v2/providers/rg_open_api/apis/api/v1/vendors/{self.vendor_id}/rg/orders"
        token = ""
        while True:
            params = {"paidDateFrom": d_from.strftime("%Y%m%d"), "paidDateTo": d_to.strftime("%Y%m%d")}
            if token:
                params["nextToken"] = token
            res = self.get(path, params, delay=1.2)
            for row in res.get("data") or []:
                yield row
            token = res.get("nextToken") or ""
            if not token:
                break

    # 로켓그로스 재고 요약 (분당 50회 제한)
    def rg_inventory(self):
        path = f"/v2/providers/rg_open_api/apis/api/v1/vendors/{self.vendor_id}/rg/inventory/summaries"
        token = ""
        while True:
            params = {"nextToken": token} if token else {}
            res = self.get(path, params, delay=1.3)
            for row in res.get("data") or []:
                yield row
            token = res.get("nextToken") or ""
            if not token:
                break

    # 상품 목록 / 상품 상세 (대표 이미지용)
    def seller_products(self):
        path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
        token = ""
        for _ in range(200):
            params = {"vendorId": self.vendor_id, "maxPerPage": 100}
            if token:
                params["nextToken"] = token
            res = self.get(path, params, delay=0.3)
            if not token:
                try:
                    with open(os.path.join(BASE_DIR, "logs", "product_list_sample.txt"), "w", encoding="utf-8") as f:
                        f.write(json.dumps(res, ensure_ascii=False)[:20000])
                except Exception:
                    pass
            data = res.get("data") if isinstance(res, dict) else None
            if isinstance(data, dict):   # 응답 모양이 다를 때 대비
                data = data.get("content") or data.get("data") or []
            for row in data or []:
                yield row
            token = str(res.get("nextToken") or "")
            if not token:
                break

    def seller_product(self, spid):
        path = f"/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/{spid}"
        return self.get(path, {}, delay=0.3)

    # 지급내역 (월 단위)
    def settlements(self, year_month):
        path = "/v2/providers/marketplace_openapi/apis/api/v1/settlement-histories"
        res = self.get(path, {"revenueRecognitionYearMonth": year_month})
        data = res if isinstance(res, list) else (res.get("data") or [])
        return data


# ─────────────────────────────── 저장소(SQLite) ───────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS revenue (
  recognition_date TEXT, sale_date TEXT, settlement_date TEXT,
  order_id TEXT, sale_type TEXT, line_type TEXT,
  product_id TEXT, product_name TEXT, vendor_item_id TEXT, vendor_item_name TEXT,
  sale_price REAL, quantity REAL, sale_amount REAL,
  coupang_coupon REAL, seller_coupon REAL, download_coupon REAL,
  service_fee REAL, settlement_amount REAL);
CREATE INDEX IF NOT EXISTS ix_rev_date ON revenue(recognition_date);

CREATE TABLE IF NOT EXISTS orders (
  ordered_date TEXT, ordered_at TEXT, order_id TEXT, status TEXT,
  vendor_item_id TEXT, product_name TEXT, item_name TEXT,
  quantity REAL, cancel_count REAL, sales_price REAL, order_price REAL, discount REAL);
CREATE INDEX IF NOT EXISTS ix_ord_date ON orders(ordered_date);

CREATE TABLE IF NOT EXISTS returns (
  created_date TEXT, created_at TEXT, receipt_id TEXT, order_id TEXT,
  receipt_type TEXT, status TEXT, fault_by TEXT,
  reason_cat1 TEXT, reason_cat2 TEXT, reason TEXT,
  vendor_item_id TEXT, product_name TEXT, item_name TEXT,
  quantity REAL, return_shipping_charge REAL);
CREATE INDEX IF NOT EXISTS ix_ret_date ON returns(created_date);

CREATE TABLE IF NOT EXISTS settlements (
  year_month TEXT, settlement_type TEXT, settlement_date TEXT,
  date_from TEXT, date_to TEXT, total_sale REAL, service_fee REAL,
  settlement_target REAL, deduction REAL, final_amount REAL, status TEXT);

CREATE TABLE IF NOT EXISTS inventory (
  vendor_item_id TEXT, external_sku TEXT, orderable_qty REAL, sales_30d REAL, updated_at TEXT);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS images (vendor_item_id TEXT PRIMARY KEY, url TEXT, seller_product_id TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS seller_products (seller_product_id TEXT PRIMARY KEY, name TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS option_products (vendor_item_id TEXT PRIMARY KEY, seller_product_id TEXT, product_id TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS ads (
  ad_date TEXT, campaign_id TEXT, campaign TEXT, ad_group TEXT, ad_vid TEXT, conv_vid TEXT, placement TEXT, audience TEXT,
  ad_name TEXT, impressions REAL, clicks REAL, spend REAL, orders14 REAL, qty14 REAL, sales14 REAL, sales1 REAL, source TEXT);
CREATE INDEX IF NOT EXISTS ix_ads_date ON ads(ad_date);
CREATE TABLE IF NOT EXISTS option_grades (vendor_item_id TEXT PRIMARY KEY, grade TEXT, option_name TEXT);
"""


def open_db():
    db = sqlite3.connect(os.path.join(BASE_DIR, "data.db"))
    db.executescript(SCHEMA)
    return db


def meta_get(db, k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(db, k, v):
    db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, str(v)))


# ─────────────────────────────── 수집 ───────────────────────────────
def collect_revenue(api, db, d_from, d_to):
    yday = today_kst() - timedelta(days=1)
    d_to = min(d_to, yday)
    if d_from > d_to:
        return 0
    rows = []
    for a, b in chunks(d_from, d_to, 25):
        for r in api.revenue_history(a, b):
            sign = -1 if str(r.get("saleType", "")).upper() == "REFUND" else 1
            base = (r.get("recognitionDate"), r.get("saleDate"), r.get("settlementDate"),
                    str(r.get("orderId", "")), r.get("saleType"))
            for it in r.get("items") or []:
                q = num(it.get("quantity"))
                rows.append(base + (
                    "상품", str(it.get("productId", "")), it.get("productName"),
                    str(it.get("vendorItemId", "")), it.get("vendorItemName"),
                    num(it.get("salePrice")), sign * abs(q),
                    sign * abs(num(it.get("saleAmount"))),
                    num(it.get("coupangDiscountCoupon")), num(it.get("sellerDiscountCoupon")),
                    num(it.get("downloadableCoupon")),
                    sign * abs(num(it.get("serviceFee")) + num(it.get("serviceFeeVat"))),
                    sign * abs(num(it.get("settlementAmount"))),
                ))
            df = r.get("deliveryFee") or {}
            if num(df.get("amount")) or num(df.get("settlementAmount")):
                rows.append(base + (
                    "배송비", "", "배송비", "", "배송비", 0, 0,
                    sign * abs(num(df.get("amount"))), 0, 0, 0,
                    sign * abs(num(df.get("fee")) + num(df.get("feeVat"))),
                    sign * abs(num(df.get("settlementAmount"))),
                ))
    with db:
        db.execute("DELETE FROM revenue WHERE recognition_date BETWEEN ? AND ?", (d2s(d_from), d2s(d_to)))
        db.executemany(f"INSERT INTO revenue VALUES ({','.join('?' * 18)})", rows)
    return len(rows)


ORDER_STATUSES = ["ACCEPT", "INSTRUCT", "DEPARTURE", "DELIVERING", "FINAL_DELIVERY", "NONE_TRACKING"]


def collect_orders(api, db, d_from, d_to):
    rows = []
    for a, b in chunks(d_from, d_to, 25):
        for st in ORDER_STATUSES:
            for o in api.ordersheets(a, b, st):
                ordered_at = (o.get("orderedAt") or o.get("paidAt") or "")[:19]
                od = ordered_at[:10] or d2s(a)
                for it in o.get("orderItems") or []:
                    rows.append((
                        od, ordered_at, str(o.get("orderId", "")), st,
                        str(it.get("vendorItemId", "")),
                        it.get("sellerProductName") or it.get("vendorItemPackageName"),
                        it.get("sellerProductItemName") or it.get("vendorItemName"),
                        num(it.get("shippingCount")), num(it.get("cancelCount")),
                        money(it.get("salesPrice")), money(it.get("orderPrice")),
                        money(it.get("discountPrice")),
                    ))
    with db:
        db.execute("DELETE FROM orders WHERE status<>'RG' AND ordered_date BETWEEN ? AND ?", (d2s(d_from), d2s(d_to)))
        db.executemany(f"INSERT INTO orders VALUES ({','.join('?' * 12)})", rows)
    return len(rows)


def collect_rg_orders(api, db, d_from, d_to):
    rows = []
    for a, b in chunks(d_from, d_to, 25):
        for o in api.rg_orders(a, b):
            paid = o.get("paidAt")
            try:
                dt = datetime.fromtimestamp(int(paid) / 1000, KST)
            except (TypeError, ValueError):
                dt = datetime.combine(a, datetime.min.time(), KST)
            for it in o.get("orderItems") or []:
                q = num(it.get("salesQuantity"))
                price = num(it.get("unitSalesPrice"))
                rows.append((
                    d2s(dt.date()), dt.strftime("%Y-%m-%dT%H:%M:%S"), str(o.get("orderId", "")), "RG",
                    str(it.get("vendorItemId", "")), it.get("productName"), "",
                    q, 0, price, price * q, 0,
                ))
    # 쿠팡 API가 페이지를 넘길 때 같은 주문을 두 번 주는 경우가 있어 똑같은 줄은 하나만 남김
    seen, uniq = set(), []
    for r in rows:
        k = (r[1], r[2], r[4], r[7], r[10])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    with db:
        db.execute("DELETE FROM orders WHERE status='RG' AND ordered_date BETWEEN ? AND ?", (d2s(d_from), d2s(d_to)))
        db.executemany(f"INSERT INTO orders VALUES ({','.join('?' * 12)})", uniq)
        # 예전에 이미 두 번 들어간 줄도 정리
        db.execute("DELETE FROM orders WHERE status='RG' AND rowid NOT IN (SELECT MIN(rowid) FROM orders "
                   "WHERE status='RG' GROUP BY ordered_at, order_id, vendor_item_id, quantity, order_price)")
    if len(uniq) < len(rows):
        log.info("  로켓그로스 주문 중복 %d줄 제외", len(rows) - len(uniq))
    return len(uniq)


def collect_rg_inventory(api, db):
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    rows = []
    for r in api.rg_inventory():
        inv = r.get("inventoryDetails") or {}
        sc = r.get("salesCountMap") or {}
        rows.append((str(r.get("vendorItemId", "")), r.get("externalSkuId") or "",
                     num(inv.get("totalOrderableQuantity")), num(sc.get("SALES_COUNT_LAST_THIRTY_DAYS")), now))
    prev = db.execute("SELECT COUNT(*) FROM inventory").fetchone()[0]
    if not rows and prev:
        # 쿠팡이 가끔 빈 목록을 돌려줌 → 지난번 재고를 지우지 않고 그대로 둠
        log.warning("  로켓그로스 재고가 빈 목록으로 와서 지난번 재고(%d줄)를 유지", prev)
        return prev
    with db:
        db.execute("DELETE FROM inventory")
        db.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", rows)
    return len(rows)


IMG_CDN = "https://thumbnail6.coupangcdn.com/thumbnails/remote/492x492ex/image/"


def _find_vendor_item_ids(obj, out):
    """상품 상세 응답 안의 vendorItemId 를 모두 찾기 (마켓/로켓그로스 구조가 달라서 넓게 찾음)"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "vendorItemId" and v:
                out.add(str(v))
            elif isinstance(v, (dict, list)):
                _find_vendor_item_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_vendor_item_ids(v, out)
    return out


def _image_url(images):
    imgs = sorted([i for i in (images or []) if isinstance(i, dict)], key=lambda i: (
        0 if i.get("imageType") == "REPRESENTATION" else 1, num(i.get("imageOrder"))))
    for i in imgs:
        vp, cp = str(i.get("vendorPath") or ""), str(i.get("cdnPath") or "")
        if vp.startswith("http"):
            return vp
        if cp:
            return cp if cp.startswith("http") else IMG_CDN + cp.lstrip("/")
    return ""


def probe_rg_cancels(api, db):
    """취소·반품 확인 방법 찾기용: 쿠팡 응답 원문을 logs/rg_cancel_probe.txt 에 남김 (하루 한 번)"""
    fn = os.path.join(BASE_DIR, "logs", "rg_cancel_probe.txt")
    if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < 20 * 3600:
        return
    out = []
    t = today_kst()
    d1 = d2s(t - timedelta(days=1))
    oids = [r[0] for r in db.execute(
        "SELECT DISTINCT order_id FROM orders WHERE ordered_date IN (?, ?) ORDER BY ordered_at LIMIT 40",
        (d1, "2026-10-19"))]
    base = f"/v2/providers/rg_open_api/apis/api/v1/vendors/{api.vendor_id}"
    for oid in oids:
        try:
            res = api.get(f"{base}/rg/order/{oid}", {}, retries=2, delay=1.2)
            out.append(f"### rg/order/{oid}\n" + json.dumps(res, ensure_ascii=False)[:3000])
        except Exception as e:
            out.append(f"### rg/order/{oid}\nERROR {e}")
    ret = f"/v2/providers/openapi/apis/api/v6/vendors/{api.vendor_id}/returnRequests"
    a, b = t - timedelta(days=3), t
    tries = [
        ("cancel day", ret, {"createdAtFrom": d2s(a), "createdAtTo": d2s(b), "cancelType": "CANCEL", "maxPerPage": 50}),
        ("cancel timeFrame", ret, {"searchType": "timeFrame", "createdAtFrom": a.strftime("%Y-%m-%dT00:00"),
                                   "createdAtTo": b.strftime("%Y-%m-%dT23:59"), "cancelType": "CANCEL"}),
        ("return timeFrame", ret, {"searchType": "timeFrame", "createdAtFrom": a.strftime("%Y-%m-%dT00:00"),
                                   "createdAtTo": b.strftime("%Y-%m-%dT23:59"), "cancelType": "RETURN"}),
        ("rg returns", f"{base}/rg/returns", {"createdAtFrom": a.strftime("%Y%m%d"), "createdAtTo": b.strftime("%Y%m%d")}),
        ("rg cancels", f"{base}/rg/cancels", {"createdAtFrom": a.strftime("%Y%m%d"), "createdAtTo": b.strftime("%Y%m%d")}),
    ]
    for name, path, params in tries:
        try:
            res = api.get(path, params, retries=1, delay=0.5)
            out.append(f"### {name} {params}\n" + json.dumps(res, ensure_ascii=False)[:6000])
        except Exception as e:
            out.append(f"### {name} {params}\nERROR {str(e)[:300]}")
    with open(fn, "w", encoding="utf-8") as f:
        f.write("\n\n".join(out))


def probe_products(api, db):
    """이미지 조회 방법 확인용: 쿠팡 응답 원문을 logs/product_probe.txt 에 남김"""
    base = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
    sku = (db.execute("SELECT external_sku FROM inventory WHERE external_sku<>'' LIMIT 1").fetchone() or [""])[0]
    vid = (db.execute("SELECT vendor_item_id FROM inventory LIMIT 1").fetchone() or [""])[0]
    tries = [
        ("list nextToken=1", base, {"vendorId": api.vendor_id, "nextToken": "1", "maxPerPage": 10}),
        ("list APPROVED", base, {"vendorId": api.vendor_id, "maxPerPage": 10, "status": "APPROVED"}),
        ("time-frame", base + "/time-frame", {"vendorId": api.vendor_id, "createdAtFrom": "2026-01-01T00:00:00",
                                               "createdAtTo": datetime.now(KST).strftime("%Y-%m-%dT%H:%M:%S")}),
        ("detail by sku", f"{base}/{sku}", {}),
        ("external sku", f"{base}/external-vendor-sku-codes/{sku}", {}),
        ("vendor item inventory", f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vid}/inventories", {}),
    ]
    out = []
    for name, path, params in tries:
        try:
            res = api.get(path, params, retries=1, delay=0.3)
            out.append(f"### {name} {path} {params}\n" + json.dumps(res, ensure_ascii=False)[:6000])
        except Exception as e:
            out.append(f"### {name} {path} {params}\nERROR {e}")
    try:
        with open(os.path.join(BASE_DIR, "logs", "product_probe.txt"), "w", encoding="utf-8") as f:
            f.write("\n\n".join(out))
    except Exception:
        pass


def collect_images(api, db, limit=250):
    """대표 이미지 주소 받아오기 - 새 상품만 상세 조회 (한 번 받은 상품은 30일 뒤 다시 확인)"""
    now = datetime.now(KST)
    with db:
        for r in api.seller_products():
            spid = str(r.get("sellerProductId") or "")
            if spid:
                db.execute("INSERT OR IGNORE INTO seller_products VALUES (?,?,NULL)", (spid, r.get("sellerProductName") or ""))
        # 로켓그로스 재고의 externalSkuId 도 상품번호 후보로 시도
        for (sku,) in db.execute("SELECT DISTINCT external_sku FROM inventory WHERE external_sku<>''"):
            db.execute("INSERT OR IGNORE INTO seller_products VALUES (?,?,NULL)", (str(sku), "(재고 SKU)"))
    if db.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0 and \
            not os.path.exists(os.path.join(BASE_DIR, "logs", "product_probe.txt")):
        probe_products(api, db)
    old = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    todo = [x[0] for x in db.execute(
        "SELECT seller_product_id FROM seller_products WHERE checked_at IS NULL OR checked_at<? "
        "ORDER BY checked_at IS NOT NULL, seller_product_id DESC LIMIT ?", (old, limit))]
    got, fails = 0, 0
    for i, spid in enumerate(todo):
        try:
            res = api.seller_product(spid)
        except CoupangAPIError as e:
            fails += 1
            if fails <= 3:
                log.warning("  상품 %s 조회 실패: %s", spid, str(e)[:200])
            with db:
                db.execute("UPDATE seller_products SET checked_at=? WHERE seller_product_id=?", (now.strftime("%Y-%m-%d"), spid))
            continue
        if i == 0:
            try:
                with open(os.path.join(BASE_DIR, "logs", "product_sample.txt"), "w", encoding="utf-8") as f:
                    f.write(json.dumps(res, ensure_ascii=False)[:30000])
            except Exception:
                pass
        data = res.get("data") if isinstance(res, dict) else None
        data = data or {}
        rows = []
        for it in data.get("items") or []:
            url = _image_url(it.get("images"))
            if not url:
                continue
            for vid in _find_vendor_item_ids(it, set()):
                rows.append((vid, url, spid, now.strftime("%Y-%m-%d %H:%M")))
        # 엑셀로 연결된 옵션 중 상세 응답에서 못 찾은 옵션은 상품 대표 이미지로 채움
        first = next((it for it in (data.get("items") or []) if _image_url(it.get("images"))), None)
        rep = _image_url(first.get("images")) if first else ""
        if rep:
            have = {r[0] for r in rows}
            for (ov,) in db.execute("SELECT vendor_item_id FROM option_products WHERE seller_product_id=?", (spid,)):
                if ov not in have:
                    rows.append((ov, rep, spid, now.strftime("%Y-%m-%d %H:%M")))
        with db:
            db.executemany("INSERT OR REPLACE INTO images VALUES (?,?,?,?)", rows)
            db.execute("UPDATE seller_products SET checked_at=? WHERE seller_product_id=?", (now.strftime("%Y-%m-%d"), spid))
        got += len(rows)
    return got


FAULT_KO = {"COUPANG": "쿠팡", "VENDOR": "판매자", "CUSTOMER": "고객", "WMS": "물류", "GENERAL": "일반"}


def collect_returns(api, db, d_from, d_to):
    rows, seen = [], set()
    jobs = [("RETURN", s) for s in ("RU", "UC", "CC", "PR")] + [("CANCEL", None)]
    for a, b in chunks(d_from, d_to, 25):
        for ctype, st in jobs:
            try:
                for r in api.return_requests(a, b, ctype, st):
                    created_at = (r.get("createdAt") or "")[:19]
                    rtype = "취소" if (r.get("receiptType") or ctype) == "CANCEL" else "반품"
                    for it in r.get("returnItems") or [{}]:
                        key = (r.get("receiptId"), it.get("vendorItemId"))
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append((
                            created_at[:10], created_at, str(r.get("receiptId", "")),
                            str(r.get("orderId", "")), rtype, r.get("receiptStatus"),
                            FAULT_KO.get(r.get("faultByType"), r.get("faultByType")),
                            r.get("cancelReasonCategory1"), r.get("cancelReasonCategory2"),
                            r.get("reasonCodeText") or r.get("cancelReason"),
                            str(it.get("vendorItemId", "")), it.get("sellerProductName"),
                            it.get("vendorItemName"),
                            num(it.get("cancelCount") or r.get("cancelCountSum")),
                            money(r.get("returnShippingCharge")),
                        ))
            except CoupangAPIError as e:
                if "인증 실패" in str(e):
                    raise
                log.warning("반품/취소(%s %s) 조회 건너뜀: %s", ctype, st, e)
    with db:
        db.execute("DELETE FROM returns WHERE created_date BETWEEN ? AND ?", (d2s(d_from), d2s(d_to)))
        db.executemany(f"INSERT INTO returns VALUES ({','.join('?' * 15)})", rows)
    return len(rows)


def collect_settlements(api, db, months):
    n = 0
    for ym in months:
        data = api.settlements(ym)
        rows = [(
            ym, s.get("settlementType"), s.get("settlementDate"),
            s.get("revenueRecognitionDateFrom"), s.get("revenueRecognitionDateTo"),
            num(s.get("totalSale")), num(s.get("serviceFee")),
            num(s.get("settlementTargetAmount")), num(s.get("deductionAmount")),
            num(s.get("finalAmount")), s.get("status"),
        ) for s in data]
        with db:
            db.execute("DELETE FROM settlements WHERE year_month=?", (ym,))
            db.executemany(f"INSERT INTO settlements VALUES ({','.join('?' * 11)})", rows)
        n += len(rows)
    return n


# ─────────────────────────────── 정산 엑셀 자동 반영 ───────────────────────────────
import glob
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def read_xlsx_rows(path, sheet=0):
    """외부 라이브러리 없이 xlsx 시트(기본 첫 시트)를 [[값...]] 로 읽기"""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        sheets = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        if len(sheets) <= sheet:
            return []
        root = ET.fromstring(z.read(sheets[sheet]))
    rows = []
    for r in root.iter(f"{{{_NS['m']}}}row"):
        vals = {}
        for c in r.findall("m:c", _NS):
            ref = c.get("r", "")
            col = 0
            for ch in re.match(r"[A-Z]+", ref).group(0) if ref else "A":
                col = col * 26 + (ord(ch) - 64)
            t = c.get("t")
            v = c.find("m:v", _NS)
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(f"{{{_NS['m']}}}t"))
            elif v is not None:
                val = v.text
                try:
                    f = float(val)
                    val = int(f) if f.is_integer() and "." not in val and "E" not in val.upper() else f
                except ValueError:
                    pass
            else:
                val = None
            vals[col] = val
        if vals:
            rows.append([vals.get(i) for i in range(1, max(vals) + 1)])
    return rows


SETTLE_COLS = {
    "정산주기(종료일)": "period_end", "매출인식일": "recog_date", "발생일(결제완료일)": "paid_date",
    "주문ID": "order_id", "거래유형": "tx_type", "옵션ID": "vendor_item_id", "등록상품명": "product_name",
    "옵션명": "item_name", "판매가(A)": "price", "판매수량(B)": "qty", "판매액(A*B)": "gross",
    "쿠팡지원할인(C)": "coupang_disc", "매출금액(A*B-C)": "sales", "판매자할인쿠폰(D+E)": "seller_coupon",
    "정산대상액": "target", "판매수수료": "fee", "판매수수료 VAT": "fee_vat",
}

SETTLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settle_lines (
  row_hash TEXT PRIMARY KEY, period_end TEXT, recog_date TEXT, paid_date TEXT, order_id TEXT, tx_type TEXT,
  vendor_item_id TEXT, product_name TEXT, item_name TEXT, price REAL, qty REAL, gross REAL,
  coupang_disc REAL, sales REAL, seller_coupon REAL, target REAL, fee REAL, fee_vat REAL, source TEXT);
CREATE INDEX IF NOT EXISTS ix_settle_recog ON settle_lines(recog_date);
CREATE TABLE IF NOT EXISTS imported_files (name TEXT PRIMARY KEY, imported_at TEXT, rows INTEGER);
CREATE TABLE IF NOT EXISTS cfs_lines (
  row_hash TEXT PRIMARY KEY, kind TEXT, period_end TEXT, occur_date TEXT, tx_type TEXT, order_id TEXT,
  vendor_item_id TEXT, product_name TEXT, item_name TEXT, size TEXT, fee_a REAL, fee_b REAL, fee_c REAL, source TEXT);
CREATE INDEX IF NOT EXISTS ix_cfs_date ON cfs_lines(occur_date);
"""


def import_cfs_workbook(db, path, name):
    """쿠팡풀필먼트 입출고비·배송비 정산 엑셀(WAREHOUSING_SHIPPING) → cfs_lines. 반영한 줄 수 (형식이 아니면 None)"""
    n, found = 0, False
    for si in range(4):
        rows = read_xlsx_rows(path, si)
        if not rows:
            break
        title = " ".join(str(v) for r in rows[:3] for v in (r or []) if v)
        kind = "입출고비" if "입출고비 정산" in title else ("배송비" if "배송비 정산" in title else None)
        hi = next((i for i, r in enumerate(rows[:12]) if r and r[0] == "정산유형"), None)
        if not kind or hi is None:
            continue
        found = True
        head, sub = rows[hi], (rows[hi + 1] if hi + 1 < len(rows) else [])
        col = {h: i for i, h in enumerate(head) if h}
        ia = next((i for i, v in enumerate(sub or []) if v == "발생비용(A)"), None)
        if ia is None:
            continue
        ic = ia + 2   # 할인적용가(A-B) (배송비는 그 뒤에 추가비용·최종비용)
        ifin = next((i for i, v in enumerate(sub or []) if v == "최종비용"), None)
        for r in rows[hi + 2:]:
            if not r or r[0] not in ("주정산", "월정산"):
                continue
            g = lambda h: r[col[h]] if h in col and col[h] < len(r) else None
            v = lambda i: num(r[i]) if i is not None and i < len(r) else 0
            a, b = v(ia), v(ia + 1)
            c = v(ifin) if ifin is not None else v(ic)
            h = hashlib.sha1(json.dumps([kind] + list(r), ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
            cur = db.execute("INSERT OR IGNORE INTO cfs_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (h, kind, str(g("정산주기(종료일)") or ""), str(g("발생일(배송완료일)") or "")[:10],
                              g("거래유형") or "", str(g("주문ID") or ""), str(g("옵션ID") or ""),
                              g("등록상품명") or "", g("옵션명") or "", g("개별포장 상품 사이즈") or "", a, b, c, name))
            n += cur.rowcount
    return n if found else None


def prune_archives(days=30):
    """보관 폴더의 오래된 엑셀은 지움 (내용은 이미 data.db에 들어가 있음)"""
    cut = time.time() - days * 86400
    for sub in ("정산엑셀_보관", "광고엑셀_보관", "상품엑셀_보관"):
        for f in glob.glob(os.path.join(BASE_DIR, sub, "*.xlsx")):
            try:
                if os.path.getmtime(f) < cut:
                    os.remove(f)
            except OSError:
                pass


def _excel_folders(opt):
    raw = opt.get("excel_folders", "") if opt else ""
    folders = [p.strip() for p in raw.split(";") if p.strip()] or [
        os.path.join(os.path.expanduser("~"), "Downloads"),
        os.path.join(os.path.expanduser("~"), "Desktop"),
    ]
    folders.append(os.path.join(BASE_DIR, "정산엑셀_넣는곳"))
    return [os.path.expandvars(os.path.expanduser(f)) for f in folders]


def import_product_excels(db, vendor_id, opt=None):
    """Wing [상품 조회/수정 > 엑셀 대량 수정 > 엑셀 파일 다운로드] 파일에서 업체상품ID ↔ 옵션ID 연결을 읽음
    (로켓그로스 상품은 쿠팡 API 상품목록에 안 나와서, 이 연결이 있어야 대표 이미지를 받을 수 있음)"""
    db.executescript(SCHEMA)
    files = []
    for folder in _excel_folders(opt):
        files += glob.glob(os.path.join(folder, "*.xlsx"))
    n_total = 0
    for path in sorted(set(files)):
        name = os.path.basename(path)
        if name.startswith(vendor_id + "-") or name.startswith("~$"):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_size > 3_000_000:
            continue
        key = f"상품엑셀:{name}:{st.st_size}:{int(st.st_mtime)}"
        if db.execute("SELECT 1 FROM imported_files WHERE name=?", (key,)).fetchone():
            continue
        try:
            rows = read_xlsx_rows(path)
        except Exception:
            continue
        # 두 가지 엑셀 지원: ① 상품 조회/수정 > 엑셀 대량 수정(업체상품 ID)  ② 로켓그로스 재고 현황(등록상품 ID, 상품등급)
        hi = next((i for i, r in enumerate(rows[:10]) if r and "옵션 ID" in [str(x).strip() for x in r]
                   and any(("업체상품" in str(x) or "등록상품 ID" in str(x)) for x in r)), None)
        n = 0
        if hi is not None:
            head = [str(x).strip() for x in rows[hi]]
            col = lambda name: head.index(name) if name in head else None
            i_sp = next(i for i, h in enumerate(head) if ("업체상품" in h or "등록상품 ID" in h) and "ID" in h)
            i_op = head.index("옵션 ID")
            i_pid = col("Product ID")
            i_nm = col("쿠팡 노출 상품명")
            i_pn, i_on, i_gr = col("등록상품명"), col("옵션명"), col("상품등급")
            cell = lambda r, i: str(r[i] or "").strip() if i is not None and i < len(r) and r[i] is not None else ""
            for r in rows[hi + 1:]:
                if not r or len(r) <= max(i_sp, i_op):
                    continue
                sp, op = cell(r, i_sp), cell(r, i_op)
                if not sp.isdigit() or not op.isdigit():
                    continue
                pid = cell(r, i_pid)
                nm = cell(r, i_nm) or (cell(r, i_pn) + " " + cell(r, i_on)).strip()
                if i_gr is not None:
                    db.execute("INSERT OR REPLACE INTO option_grades VALUES (?,?,?)", (op, cell(r, i_gr), cell(r, i_on)))
                db.execute("INSERT OR REPLACE INTO option_products VALUES (?,?,?,?)", (op, sp, pid, nm))
                db.execute("INSERT OR IGNORE INTO seller_products VALUES (?,?,NULL)", (sp, nm))
                db.execute("UPDATE seller_products SET checked_at=NULL WHERE seller_product_id=? AND "
                           "NOT EXISTS (SELECT 1 FROM images WHERE seller_product_id=?)", (sp, sp))
                n += 1
            log.info("  상품 엑셀 반영: %s (옵션 %d개)", name, n)
        db.execute("INSERT OR REPLACE INTO imported_files VALUES (?,?,?)", (key, datetime.now(KST).isoformat(), n))
        db.commit()
        if n:   # 반영한 상품 엑셀은 다운로드 폴더에서 치움 (연결 정보는 data.db에 남음)
            arch = os.path.join(BASE_DIR, "상품엑셀_보관")
            os.makedirs(arch, exist_ok=True)
            _archive(path, arch, name)
        n_total += n
    return n_total


AD_VAT = 1.1   # 쿠팡 광고 보고서의 광고비는 VAT 별도 → 이익 계산 때 10% 더함


def import_ad_reports(db, vendor_id, opt=None):
    """쿠팡 광고센터 [광고 보고서 > 매출 성장 광고 보고서] 일별·상품 단위 파일(…_pa_daily_…xlsx) 반영
    같은 날짜·캠페인·상품 줄은 나중 파일 값으로 덮어씀 (14일 전환 매출이 뒤늦게 늘어나기 때문)"""
    db.executescript(SCHEMA)
    archive = os.path.join(BASE_DIR, "광고엑셀_보관")
    os.makedirs(archive, exist_ok=True)
    files = []
    for folder in _excel_folders(opt):
        files += glob.glob(os.path.join(folder, f"{vendor_id}_pa_daily_*.xlsx"))
    files.sort(key=lambda p: os.path.basename(p).split("_")[-1])   # 기간 끝 날짜 순서대로
    total = 0
    for path in files:
        name = os.path.basename(path)
        try:
            rows = read_xlsx_rows(path)
        except Exception as e:
            log.warning("  광고 보고서 읽기 실패(%s): %s", name, e)
            continue
        if not rows or "광고비" not in rows[0] or "날짜" not in rows[0]:
            log.info("  광고 보고서 형식이 달라 건너뜀: %s", name)
            continue
        h = {k: i for i, k in enumerate(rows[0])}
        def g(r, k, d=""):
            i = h.get(k)
            return r[i] if i is not None and i < len(r) and r[i] is not None else d
        def idstr(v):
            try:
                return str(int(float(v)))
            except (TypeError, ValueError):
                return str(v or "").strip()
        n = 0
        recs = []
        for r in rows[1:]:
            if not r or not g(r, "날짜"):
                continue
            ds = idstr(g(r, "날짜"))
            if len(ds) != 8:
                continue
            d = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
            recs.append((
                d, idstr(g(r, "캠페인 ID")), str(g(r, "캠페인명")), str(g(r, "광고그룹")),
                idstr(g(r, "광고집행 옵션ID")), idstr(g(r, "광고전환매출발생 옵션ID")), str(g(r, "광고 노출 지면")),
                str(g(r, "오디언스 플러스(외부 채널)")), str(g(r, "광고집행 상품명")),
                num(g(r, "노출수", 0)), num(g(r, "클릭수", 0)), num(g(r, "광고비", 0)),
                num(g(r, "총 주문수(14일)", 0)), num(g(r, "총 판매수량(14일)", 0)),
                num(g(r, "총 전환매출액(14일)", 0)), num(g(r, "총 전환매출액(1일)", 0)), name))
        # 파일 기간(파일명 끝의 시작_끝 날짜)에 해당하는 예전 줄은 지우고 이 파일 내용으로 바꿈
        parts = name.rsplit(".", 1)[0].split("_")
        try:
            a, b = parts[-2], parts[-1]
            a, b = f"{a[:4]}-{a[4:6]}-{a[6:]}", f"{b[:4]}-{b[4:6]}-{b[6:]}"
        except (IndexError, ValueError):
            a = min((x[0] for x in recs), default="")
            b = max((x[0] for x in recs), default="")
        with db:
            if a and b:
                db.execute("DELETE FROM ads WHERE ad_date BETWEEN ? AND ?", (a, b))
            db.executemany("INSERT INTO ads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", recs)
        n = len(recs)
        dest = os.path.join(archive, name)
        try:
            if os.path.abspath(path) != os.path.abspath(dest):
                if os.path.exists(dest):
                    os.remove(dest)
                shutil.move(path, dest)
        except OSError as e:
            log.warning("  광고 보고서 보관 폴더로 옮기기 실패(%s): %s", name, e)
        log.info("  광고 보고서 반영: %s (%d줄)", name, n)
        total += n
    return total


def _archive(path, archive, name):
    """다운로드 폴더에서 보관 폴더로 옮김 (같은 이름이 이미 있으면 다운로드 쪽 파일만 지움)"""
    dest = os.path.join(archive, name)
    try:
        if os.path.abspath(path) != os.path.abspath(dest):
            if os.path.exists(dest):
                os.remove(path)
            else:
                shutil.move(path, dest)
    except OSError as e:
        log.warning("  보관 폴더로 옮기기 실패(%s): %s", name, e)


def import_settlement_excels(db, vendor_id, opt=None):
    """다운로드·바탕화면 등에서 쿠팡 정산 리포트(xlsx)를 찾아 반영하고 보관 폴더로 옮김"""
    db.executescript(SETTLE_SCHEMA)
    inbox = os.path.join(BASE_DIR, "정산엑셀_넣는곳")
    archive = os.path.join(BASE_DIR, "정산엑셀_보관")
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(archive, exist_ok=True)
    files = []
    for folder in _excel_folders(opt):
        files += glob.glob(os.path.join(folder, f"{vendor_id}-*.xlsx"))
    total = 0
    for path in sorted(set(files)):
        name = os.path.basename(path)
        try:
            rows = read_xlsx_rows(path)
        except Exception as e:
            log.warning("  정산 엑셀 읽기 실패(%s): %s", name, e)
            continue
        hi = next((i for i, r in enumerate(rows[:10]) if r and "주문ID" in r and "거래유형" in r), None)
        if hi is None or "판매수수료" not in rows[hi]:
            try:
                nc = import_cfs_workbook(db, path, name)
            except Exception as e:
                log.warning("  물류비 엑셀 읽기 실패(%s): %s", name, e)
                continue
            if nc is not None:
                db.execute("INSERT OR REPLACE INTO imported_files VALUES (?,?,?)", (name, datetime.now(KST).isoformat(), nc))
                db.commit()
                log.info("  물류비 엑셀 반영: %s (새 줄 %d)", name, nc)
            elif "PRODUCT_SIZE_COMPARISON" in name:
                log.info("  쓰지 않는 리포트라 보관 폴더로만 옮김: %s", name)
            else:
                log.info("  아직 지원하지 않는 리포트라 건너뜀: %s", name)
                continue
            _archive(path, archive, name)
            continue
        head = rows[hi]
        idx = {SETTLE_COLS[h]: i for i, h in enumerate(head) if h in SETTLE_COLS}
        n = 0
        for r in rows[hi + 1:]:
            if not r or not any(v not in (None, "") for v in r):
                continue
            g = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else None
            if not g("order_id"):
                continue
            h = hashlib.sha1(json.dumps(r, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
            cur = db.execute(
                "INSERT OR IGNORE INTO settle_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (h, str(g("period_end") or ""), str(g("recog_date") or ""), str(g("paid_date") or ""),
                 str(g("order_id")), g("tx_type") or "", str(g("vendor_item_id") or ""), g("product_name") or "",
                 g("item_name") or "", num(g("price")), num(g("qty")), num(g("gross")), num(g("coupang_disc")),
                 num(g("sales")), num(g("seller_coupon")), num(g("target")), num(g("fee")), num(g("fee_vat")), name))
            n += cur.rowcount
        db.execute("INSERT OR REPLACE INTO imported_files VALUES (?,?,?)",
                   (name, datetime.now(KST).isoformat(), n))
        db.commit()
        _archive(path, archive, name)
        log.info("  정산 엑셀 반영: %s (새 줄 %d)", name, n)
        total += n
    return total


# ─────────────────────────────── 구글 시트 연동 ───────────────────────────────
class Sheets:
    def __init__(self, url, token):
        self.url = url.strip()
        self.token = token.strip()

    def _call(self, payload, retries=4):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last = ""
        for attempt in range(retries):
            try:
                req = urllib.request.Request(self.url, data=data, method="POST")
                req.add_header("Content-Type", "application/json; charset=utf-8")
                with urllib.request.urlopen(req, context=SSL_CTX, timeout=300) as r:
                    body = r.read().decode("utf-8", "replace")
                try:
                    res = json.loads(body)
                except ValueError:
                    # 구글 쪽 일시 오류(HTML 오류 페이지) → 잠시 후 재시도
                    last = "구글 시트 응답을 읽을 수 없습니다(일시 오류 또는 웹앱 배포 설정 확인): " + body[:200]
                    log.warning("구글 시트 일시 오류, %d초 후 재시도", 10 * (attempt + 1))
                    time.sleep(10 * (attempt + 1))
                    continue
                if not res.get("ok"):
                    raise RuntimeError("구글 시트 오류: " + str(res.get("error")))
                return res
            except (urllib.error.URLError, TimeoutError) as e:
                last = f"구글 시트 전송 실패: {e}"
                time.sleep(10 * (attempt + 1))
        raise RuntimeError(last or "구글 시트 전송 실패")

    def get_costs(self):
        return self._call({"token": self.token, "action": "get_costs"}).get("costs", [])

    def sync_options(self, options):
        return self._call({"token": self.token, "action": "sync_options", "rows": options})

    def write_table(self, name, header, rows, formats=None, hidden=False, chunk=4000):
        first = True
        for i in range(0, max(len(rows), 1), chunk):
            part = rows[i:i + chunk]
            self._call({
                "token": self.token, "action": "write", "sheet": name,
                "header": header, "rows": part, "clear": first,
                "formats": formats or {}, "hidden": hidden,
                "total": len(rows),
            })
            first = False

    def finish(self):
        return self._call({"token": self.token, "action": "finish"})


# ─────────────────────────────── 요약 계산 ───────────────────────────────
def load_costs(sheets):
    """원가 시트: [옵션ID, 상품명, 옵션명, 원가] → {옵션ID: 원가}"""
    costs = {}
    for r in sheets.get_costs():
        if len(r) >= 4 and str(r[0]).strip() and str(r[3]).strip() != "":
            try:
                costs[str(r[0]).strip().split(".")[0]] = float(str(r[3]).replace(",", ""))
            except ValueError:
                pass
    return costs


def r0(x):
    return round(x or 0)


def build_tables(db, costs, fee_rate=0.0, saver_fee=0):
    """요약표 만들기. 매출은 '주문(결제) 기준', 정산·반품은 쿠팡 정산 엑셀(로켓그로스)이 있을 때 표시."""
    db.executescript(SETTLE_SCHEMA)
    t = today_kst()
    yday = t - timedelta(days=1)
    tables = []
    names = {}
    for vid, pn, inm in db.execute("SELECT vendor_item_id, MAX(product_name), MAX(item_name) FROM orders GROUP BY vendor_item_id"):
        names[vid] = (pn or "", inm or "")
    for vid, pn, inm in db.execute("SELECT vendor_item_id, MAX(product_name), MAX(vendor_item_name) FROM revenue "
                                   "WHERE line_type='상품' GROUP BY vendor_item_id"):
        names.setdefault(vid, (pn or "", inm or ""))
    try:   # Wing 상품 엑셀에서 읽은 이름 (한 번도 안 팔린 옵션도 이름이 나오게)
        for vid, nm in db.execute("SELECT vendor_item_id, name FROM option_products"):
            if not names.get(vid, ("", ""))[0] and nm:
                names[vid] = (nm, "")
    except sqlite3.Error:
        pass
    try:   # 반품 재판매 옵션은 옵션명 뒤에 등급 표시 (예: 반품-상)
        for vid, gr in db.execute("SELECT vendor_item_id, grade FROM option_grades WHERE grade<>'' AND grade<>'NEW'"):
            pn, inm = names.get(vid, ("", ""))
            if pn and gr not in inm:
                names[vid] = (pn, (inm + f" [{gr}]").strip())
    except sqlite3.Error:
        pass

    # 일별 집계 (주문 기준) --------------------------------------------------
    daily = defaultdict(lambda: defaultdict(float))
    for d, oid, vid, q, amt in db.execute(
            "SELECT ordered_date, order_id, vendor_item_id, quantity, order_price FROM orders"):
        x = daily[d]
        x["qty"] += q
        x["sales"] += amt
        x["cost"] += costs.get(vid, 0) * q
    for d, cnt in db.execute("SELECT ordered_date, COUNT(DISTINCT order_id) FROM orders GROUP BY ordered_date"):
        daily[d]["cnt"] = cnt
    for d, stl in db.execute("SELECT recognition_date, SUM(settlement_amount) FROM revenue GROUP BY recognition_date"):
        daily[d]["stl"] += stl or 0
    # 로켓그로스 정산 엑셀: 정산대상액(판매수수료 차감 후) 과 취소(반품) 건수
    for d, vid, q, tgt, tx in db.execute(
            "SELECT recog_date, vendor_item_id, qty, target, tx_type FROM settle_lines"):
        x = daily[d]
        x["stl"] += tgt
        x["stl_cost"] += costs.get(vid, 0) * q
        if q and vid not in costs:
            x["stl_nocost"] += 1
        if "취소" in (tx or ""):
            x["ret"] += abs(q)
    # 취소·반품 금액 (정산 엑셀: 실제) + 정산 전 날짜는 과거 취소율로 추정
    for d, vid, q, gross in db.execute(
            "SELECT recog_date, vendor_item_id, qty, gross FROM settle_lines WHERE tx_type LIKE '%취소%'"):
        x = daily[d]
        x["refund"] += abs(gross or 0)
        x["refund_cost"] += costs.get(vid, 0) * abs(q or 0)
    settled_until = db.execute("SELECT MAX(recog_date) FROM settle_lines").fetchone()[0] or ""
    rate_vid, rate_all = {}, 0.0
    s_sold = s_canc = 0.0
    for vid, sold, canc in db.execute(
            "SELECT vendor_item_id, SUM(CASE WHEN qty>0 THEN gross ELSE 0 END), SUM(CASE WHEN qty<0 THEN -gross ELSE 0 END) "
            "FROM settle_lines GROUP BY vendor_item_id"):
        sold, canc = sold or 0, canc or 0
        s_sold += sold
        s_canc += canc
        if sold >= 50000:   # 판매가 어느 정도 있는 옵션만 개별 비율 사용
            rate_vid[vid] = min(canc / sold, 0.9)
    rate_all = (s_canc / s_sold) if s_sold else 0.0
    settled_days = {r[0] for r in db.execute("SELECT DISTINCT recog_date FROM settle_lines")}
    for d, vid, q, amt in db.execute("SELECT ordered_date, vendor_item_id, quantity, order_price FROM orders"):
        if d in settled_days:
            continue
        r = rate_vid.get(vid, rate_all)
        x = daily[d]
        x["refund_est"] += (amt or 0) * r
        x["refund_est_cost"] += costs.get(vid, 0) * (q or 0) * r
    for d, typ, cnt in db.execute(
            "SELECT created_date, receipt_type, COUNT(DISTINCT receipt_id) FROM returns GROUP BY created_date, receipt_type"):
        daily[d]["ret" if typ == "반품" else "can"] += cnt
    # 광고 (쿠팡 광고 보고서) - 광고비는 VAT 포함으로 이익에서 뺌
    for d, sp, s14, imp, clk in db.execute(
            "SELECT ad_date, SUM(spend), SUM(sales14), SUM(impressions), SUM(clicks) FROM ads GROUP BY ad_date"):
        x = daily[d]
        x["ad_raw"], x["ad"], x["adsales"], x["imp"], x["clk"] = sp or 0, (sp or 0) * AD_VAT, s14 or 0, imp or 0, clk or 0
    # Wing 판매분석 매출(판매-취소)이 있는 날은 순매출을 Wing 숫자와 똑같이 맞춤 (오늘은 실시간)
    wing = {}
    try:
        wing = {d: g for d, g in db.execute("SELECT sale_date, gmv FROM wing_daily")}
    except sqlite3.OperationalError:
        pass
    for d, g in wing.items():
        x = daily[d]
        if x["sales"] < g + x["refund"]:          # 주문 API가 늦게 들어온 날: 모자란 주문 금액을 채움
            x["sales"] = g + x["refund"]
        x["refund_est"] = x["sales"] - x["refund"] - g
        x["wing"] = g
    for x in daily.values():
        x["fee"] = x["sales"] * fee_rate
        # 순매출 = 주문 매출 − 취소·반품 (정산 엑셀이 있는 날은 실제, 없는 날은 추정)
        x["net"] = x["sales"] - x["refund"] - x["refund_est"]
        x["profit"] = (x["sales"] - x["cost"] - x["fee"] - x["ad"]
                       - (x["refund"] - x["refund_cost"]) - (x["refund_est"] - x["refund_est_cost"]))
        x["stl_profit"] = x["stl"] - x["stl_cost"] - x["ad"] if x["stl"] else 0

    head = ["주문건수", "판매수량", "매출액", "원가", "예상수수료", "추정이익", "정산금액", "정산기준이익",
            "반품건수", "취소건수", "광고비", "광고매출", "취소반품액", "순매출", "취소반품구분"]

    def row(label, x):
        return [label, r0(x["cnt"]), r0(x["qty"]), r0(x["sales"]), r0(x["cost"]), r0(x["fee"]),
                r0(x["profit"]), r0(x["stl"]),
                (r0(x["stl_profit"]) if x["stl"] and not x["stl_nocost"] else ""), r0(x["ret"]), r0(x["can"]),
                r0(x["ad"]), r0(x["adsales"]), r0(x["refund"] + x["refund_est"]), r0(x["net"]),
                ("예상" if x["refund_est"] and not x["refund"] else ("일부 예상" if x["refund_est"] else ("실제" if x["refund"] else "")))]

    fmt = {str(i): "#,##0" for i in range(1, 15)}
    days_sorted = sorted(k for k in daily if k)
    tables.append(("일별요약", ["날짜"] + head, [row(d, daily[d]) for d in reversed(days_sorted)], fmt, False))
    monthly = defaultdict(lambda: defaultdict(float))
    for d in days_sorted:
        for k, v in daily[d].items():
            monthly[d[:7]][k] += v
    tables.append(("월별요약", ["월"] + head, [row(m, monthly[m]) for m in sorted(monthly, reverse=True)], fmt, False))

    # 재고 ---------------------------------------------------------------
    inv = {}
    for vid, sku, qty, s30, upd in db.execute("SELECT * FROM inventory"):
        inv[vid] = (sku, qty, s30, upd)

    # 상품·옵션별 (최근 30일) ------------------------------------------------
    since30 = d2s(t - timedelta(days=29))
    prod = {}
    for vid, q, amt in db.execute(
            "SELECT vendor_item_id, SUM(quantity), SUM(order_price) FROM orders WHERE ordered_date>=? GROUP BY vendor_item_id",
            (since30,)):
        prod[vid] = {"q": q or 0, "amt": amt or 0, "ret": 0}
    since90 = d2s(t - timedelta(days=90))
    ret_stats = {}  # 옵션별 최근 90일 (판매수량, 취소수량) - 정산 엑셀 기준
    for vid, sold, canc in db.execute(
            "SELECT vendor_item_id, SUM(CASE WHEN qty>0 THEN qty ELSE 0 END), SUM(CASE WHEN qty<0 THEN -qty ELSE 0 END) "
            "FROM settle_lines WHERE recog_date>=? GROUP BY vendor_item_id", (since90,)):
        ret_stats[vid] = (sold or 0, canc or 0)
    for vid, q in db.execute("SELECT vendor_item_id, SUM(quantity) FROM returns WHERE receipt_type='반품' "
                             "AND created_date>=? GROUP BY vendor_item_id", (since30,)):
        prod.setdefault(vid, {"q": 0, "amt": 0, "ret": 0})["ret"] = q or 0
    prod_rows = []
    for vid, p in prod.items():
        has = vid in costs
        cost = costs.get(vid, 0) * p["q"]
        profit = p["amt"] - cost - p["amt"] * fee_rate
        stock = inv.get(vid, (None, None))[1]
        per_day = p["q"] / 30.0
        days_left = round(stock / per_day) if (stock is not None and per_day > 0) else ""
        pn, inm = names.get(vid, ("", ""))
        if vid in ret_stats:
            sold90, canc90 = ret_stats[vid]
            ret_q, ret_rate = canc90, (canc90 / sold90 if sold90 > 0 else 0)
        else:
            ret_q, ret_rate = p["ret"], (p["ret"] / p["q"] if p["q"] > 0 else 0)
        prod_rows.append([pn, inm, vid, r0(p["q"]), r0(p["amt"]), r0(cost) if has else "원가 미입력",
                          r0(profit) if has else "", r0(stock) if stock is not None else "", days_left,
                          r0(ret_q), round(ret_rate, 4)])
    prod_rows.sort(key=lambda r: r[4], reverse=True)
    prod_rows = [[i + 1] + r for i, r in enumerate(prod_rows)]
    tables.append(("상품별순위(최근30일)",
                   ["순위", "상품명", "옵션명", "옵션ID", "판매수량", "매출액", "원가", "추정이익", "현재고",
                    "재고일수", "반품수량", "반품률"], prod_rows,
                   {**{str(i): "#,##0" for i in (4, 5, 6, 7, 8, 9, 10)}, "11": "0.0%"}, False))

    # 옵션별 일별 실적 (사이트에서 기간을 골라 합산) ---------------------------
    od = {}
    for d, vid, q, amt in db.execute(
            "SELECT ordered_date, vendor_item_id, SUM(quantity), SUM(order_price) FROM orders "
            "WHERE ordered_date<>'' GROUP BY ordered_date, vendor_item_id"):
        od[(d, vid)] = [q or 0, amt or 0, 0]
    for d, vid, q in db.execute("SELECT created_date, vendor_item_id, SUM(quantity) FROM returns WHERE receipt_type='반품' "
                                "GROUP BY created_date, vendor_item_id"):
        if (d, vid) in od:
            od[(d, vid)][2] = q or 0
    od_rows = []
    for (d, vid), (q, amt, rq) in sorted(od.items(), reverse=True):
        if not q and not amt:
            continue
        has = vid in costs
        cost = costs.get(vid, 0) * q
        od_rows.append([d, vid, r0(q), r0(amt), r0(cost) if has else "", r0(amt - cost - amt * fee_rate) if has else "", r0(rq)])
    # 쿠팡 물류비 (입출고비·배송비: 정가 / 프로모션 / 세이버 할인 / 실제) --------------
    try:
        lm = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0, 0])
        for kind, d, tx, a, b, c in db.execute(
                "SELECT kind, occur_date, tx_type, fee_a, fee_b, fee_c FROM cfs_lines WHERE occur_date<>''"):
            x = lm[(d[:7], kind)]
            x[0] += a or 0
            if a and abs(b or 0) >= abs(a) - 0.5:
                x[1] += b or 0          # 전액 할인 = 프로모션
            else:
                x[2] += b or 0          # 일부 할인 = 세이버 등
            x[3] += c or 0
            if "취소" not in (tx or ""):
                x[4] += 1
        lm_rows = [[m, k, r0(x[0]), r0(x[1]), r0(x[2]), r0(x[3]), x[4]] for (m, k), x in sorted(lm.items(), reverse=True)]
        if lm_rows:
            tables.append(("물류비월별", ["월", "구분", "정가", "프로모션할인", "세이버할인", "청구", "건수"], lm_rows, {}, False))
            since = d2s(t - timedelta(days=60))
            lp = {}
            for kind, vid, pn, inm, size, a, c in db.execute(
                    "SELECT kind, vendor_item_id, product_name, item_name, size, fee_a, fee_c FROM cfs_lines "
                    "WHERE occur_date>=? AND tx_type NOT LIKE '%취소%'", (since,)):
                o = lp.setdefault(vid, {"n": 0, "sn": 0, "wa": 0, "wc": 0, "sa": 0, "sc": 0, "pn": pn, "on": inm, "sz": size})
                if kind == "입출고비":
                    o["n"] += 1; o["wa"] += a or 0; o["wc"] += c or 0
                else:
                    o["sn"] += 1; o["sa"] += a or 0; o["sc"] += c or 0
            lp_rows = []
            for vid, o in sorted(lp.items(), key=lambda kv: -kv[1]["n"]):
                if not o["n"]:
                    continue
                sn = o["sn"] or 1
                lp_rows.append([vid, o["pn"], o["on"], o["sz"], o["n"], r0(o["wa"] / o["n"]), r0(o["wc"] / o["n"]),
                                r0(o["sa"] / sn), r0(o["sc"] / sn)])
            tables.append(("물류비상품별", ["옵션ID", "상품명", "옵션명", "사이즈", "건수", "입출고정가", "입출고실제",
                                         "배송정가", "배송실제"], lp_rows, {}, False))
    except sqlite3.Error as e:
        log.warning("  물류비 표 건너뜀: %s", e)

    tables.append(("옵션일별", ["날짜", "옵션ID", "판매수량", "매출액", "원가", "추정이익", "반품수량"], od_rows, {}, False))
    used = {r[1] for r in od_rows}
    tables.append(("옵션이름", ["옵션ID", "상품명", "옵션명"],
                   [[vid, names.get(vid, ("", ""))[0], names.get(vid, ("", ""))[1]] for vid in sorted(used)], {}, False))

    # 재고현황 ------------------------------------------------------------
    inv_rows = []
    for vid, (sku, qty, s30, upd) in inv.items():
        per_day = (s30 or 0) / 30.0
        days_left = round(qty / per_day) if per_day > 0 else ""
        if qty <= 0:
            st = "품절"
        elif days_left != "" and days_left <= 14:
            st = "부족"
        else:
            st = "정상"
        pn, inm = names.get(vid, ("", ""))
        if not pn:
            pn = f"(이름 확인 안 됨 · 옵션ID {vid})"
        inv_rows.append([st, pn, inm, vid, sku, r0(qty), r0(s30), round(per_day, 1), days_left, upd])
    order_key = {"품절": 0, "부족": 1, "정상": 2}
    inv_rows.sort(key=lambda r: (order_key[r[0]], -(r[6] or 0)))
    tables.append(("재고현황", ["상태", "상품명", "옵션명", "옵션ID", "판매자SKU", "주문가능재고", "최근30일판매",
                               "일평균판매", "소진예상(일)", "조회시각"], inv_rows,
                   {"5": "#,##0", "6": "#,##0"}, False))

    # 반품(취소) 현황 - 로켓그로스 정산 엑셀 기준, 최근 90일 ---------------------------
    has_settle = db.execute("SELECT COUNT(*) FROM settle_lines").fetchone()[0] > 0
    reason_rows = []
    for vid, (sold90, canc90) in ret_stats.items():
        if canc90 <= 0:
            continue
        amt = db.execute("SELECT COALESCE(SUM(-sales),0) FROM settle_lines WHERE vendor_item_id=? AND qty<0 "
                         "AND recog_date>=?", (vid, since90)).fetchone()[0]
        pn, inm = names.get(vid, ("", ""))
        if not pn:
            r = db.execute("SELECT product_name, item_name FROM settle_lines WHERE vendor_item_id=? LIMIT 1",
                           (vid,)).fetchone()
            pn, inm = (r[0], r[1]) if r else ("", "")
        reason_rows.append([pn, inm, vid, r0(sold90), r0(canc90), round(canc90 / sold90, 4) if sold90 else 0, r0(amt)])
    reason_rows.sort(key=lambda r: (-r[4], -r[5]))
    tables.append(("반품취소현황(최근90일)", ["상품명", "옵션명", "옵션ID", "판매수량", "반품·취소수량", "반품률",
                                         "반품·취소금액"], reason_rows,
                   {"3": "#,##0", "4": "#,##0", "5": "0.0%", "6": "#,##0"}, False))

    # 정산 현황 - 로켓그로스 정산 엑셀(주차별) ----------------------------------------
    st_rows = []
    for pe, a1, a2, oc, sq, cq, sales, canc, fee, tgt in db.execute(
            "SELECT period_end, MIN(recog_date), MAX(recog_date), COUNT(DISTINCT CASE WHEN qty>0 THEN order_id END), "
            "SUM(CASE WHEN qty>0 THEN qty ELSE 0 END), SUM(CASE WHEN qty<0 THEN -qty ELSE 0 END), "
            "SUM(CASE WHEN qty>0 THEN sales ELSE 0 END), SUM(CASE WHEN qty<0 THEN -sales ELSE 0 END), "
            "SUM(fee+fee_vat), SUM(target) FROM settle_lines GROUP BY period_end ORDER BY period_end DESC"):
        cost_rows = db.execute("SELECT vendor_item_id, SUM(qty) FROM settle_lines WHERE period_end=? GROUP BY 1",
                               (pe,)).fetchall()
        known = all(v in costs for v, q in cost_rows if q)
        cost = sum(costs.get(v, 0) * q for v, q in cost_rows)
        st_rows.append([pe, a1, a2, oc, r0(sq), r0(cq), r0(sales), r0(canc), r0(fee), r0(tgt),
                        r0(cost), r0(tgt - cost) if known else "원가 일부 미입력"])
    if not st_rows:
        st_rows = [list(r) for r in db.execute(
            "SELECT date_to, date_from, date_to, '', '', '', total_sale, '', service_fee, final_amount, '', '' "
            "FROM settlements ORDER BY settlement_date DESC")]
    tables.append(("정산내역", ["정산주기(종료일)", "매출인식(시작)", "매출인식(끝)", "주문건수", "판매수량", "취소수량",
                               "매출금액", "취소금액", "판매수수료(VAT포함)", "정산대상액", "원가", "정산기준이익"],
                   st_rows, {str(i): "#,##0" for i in range(3, 12)}, False))

    # 상세 원본 ------------------------------------------------------------
    since400 = d2s(t - timedelta(days=400))
    status_ko = {"ACCEPT": "결제완료", "INSTRUCT": "상품준비중", "DEPARTURE": "배송지시",
                 "DELIVERING": "배송중", "FINAL_DELIVERY": "배송완료", "NONE_TRACKING": "업체직송", "RG": "로켓그로스"}
    ord_detail = []
    for r in db.execute(
            "SELECT ordered_date, ordered_at, order_id, status, vendor_item_id, product_name, item_name, quantity, "
            "sales_price, order_price FROM orders ORDER BY ordered_at DESC"):   # 보증 조회(최대 5년+)를 위해 전체 기간
        c = costs.get(r[4])
        ord_detail.append([r[0], r[1].replace("T", " "), r[2], status_ko.get(r[3], r[3]), r[4], r[5] or "",
                           r[6] or "", r[7], r[8], r[9], r0(c * r[7]) if c is not None else ""])
    tables.append(("주문상세", ["주문일", "주문시각", "주문번호", "구분", "옵션ID", "상품명", "옵션명", "수량",
                              "단가", "결제금액", "원가"], ord_detail,
                   {str(i): "#,##0" for i in (7, 8, 9, 10)}, False))
    rev_detail = [list(r) for r in db.execute(
        "SELECT recognition_date, sale_date, settlement_date, order_id, "
        "CASE sale_type WHEN 'REFUND' THEN '환불' ELSE '판매' END, line_type, product_name, vendor_item_name, "
        "vendor_item_id, sale_price, quantity, sale_amount, service_fee, settlement_amount "
        "FROM revenue WHERE recognition_date>=? ORDER BY recognition_date DESC", (since400,))]
    tables.append(("매출상세(마켓정산)", ["매출인식일", "판매일", "정산예정일", "주문번호", "구분", "항목", "상품명",
                                     "옵션명", "옵션ID", "판매가", "수량", "매출액", "수수료", "정산금액"], rev_detail,
                   {str(i): "#,##0" for i in (9, 10, 11, 12, 13)}, False))
    ret_detail = [list(r) for r in db.execute(
        "SELECT recog_date, paid_date, order_id, product_name, item_name, vendor_item_id, -qty, -sales "
        "FROM settle_lines WHERE qty<0 AND recog_date>=? ORDER BY recog_date DESC", (since400,))]
    tables.append(("반품취소상세", ["매출인식일", "결제일", "주문번호", "상품명", "옵션명", "옵션ID", "수량", "금액"],
                   ret_detail, {"6": "#,##0", "7": "#,##0"}, False))

    # 핵심 지표 -----------------------------------------------------------
    def period(a, b, key):
        return sum(daily[d][key] for d in days_sorted if a <= d <= b)

    this_m = d2s(t.replace(day=1))
    last_m_end = t.replace(day=1) - timedelta(days=1)
    last_m = d2s(last_m_end.replace(day=1))
    last_recog = db.execute("SELECT MAX(recog_date) FROM settle_lines").fetchone()[0]
    if last_recog:
        start4 = d2s(datetime.strptime(last_recog, "%Y-%m-%d").date() - timedelta(days=27))
        sold30, ret30 = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN qty>0 THEN qty ELSE 0 END),0), COALESCE(SUM(CASE WHEN qty<0 THEN -qty ELSE 0 END),0) "
            "FROM settle_lines WHERE recog_date BETWEEN ? AND ?", (start4, last_recog)).fetchone()
    else:
        sold30 = period(since30, d2s(t), "qty")
        ret30 = db.execute("SELECT COALESCE(SUM(quantity),0) FROM returns WHERE receipt_type='반품' AND created_date>=?",
                           (since30,)).fetchone()[0]
    kpi = [
        ["어제 매출", r0(period(d2s(yday), d2s(yday), "sales"))],
        ["어제 순매출", r0(period(d2s(yday), d2s(yday), "net"))],
        ["오늘 순매출", r0(period(d2s(t), d2s(t), "net"))],
        ["이번 달 순매출", r0(period(this_m, d2s(t), "net"))],
        ["지난 달 순매출", r0(period(last_m, d2s(last_m_end), "net"))],
        ["취소반품 추정률", round(rate_all, 4)],
        ["정산 반영일", settled_until],
        ["오늘 매출", r0(period(d2s(t), d2s(t), "sales"))],
        ["이번 달 매출", r0(period(this_m, d2s(t), "sales"))],
        ["지난 달 매출", r0(period(last_m, d2s(last_m_end), "sales"))],
        ["이번 달 추정이익", r0(period(this_m, d2s(t), "profit"))],
        ["최근 7일 매출", r0(period(d2s(t - timedelta(days=6)), d2s(t), "sales"))],
        ["이번 달 주문건수", r0(period(this_m, d2s(t), "cnt"))],
        ["최근 30일 반품률", round(ret30 / sold30, 4) if sold30 > 0 else 0],
        ["품절 옵션 수", sum(1 for r in inv_rows if r[0] == "품절" and (r[6] or 0) > 0)],   # 최근 30일 팔린 옵션 중 품절
        ["이번 달 광고비", r0(period(this_m, d2s(t), "ad"))],
        ["지난 달 광고비", r0(period(last_m, d2s(last_m_end), "ad"))],
        ["이번 달 광고매출", r0(period(this_m, d2s(t), "adsales"))],
        ["이번 달 광고비(VAT별도)", r0(period(this_m, d2s(t), "ad_raw"))],
        ["광고 데이터 있음", 1 if db.execute("SELECT COUNT(*) FROM ads").fetchone()[0] else 0],
        ["광고 마지막 날짜", (db.execute("SELECT MAX(ad_date) FROM ads").fetchone()[0] or "")],
        ["재고 부족 옵션 수", sum(1 for r in inv_rows if r[0] == "부족")],
        ["원가 미입력 옵션 수", sum(1 for v, p in prod.items() if p["q"] > 0 and v not in costs)],
        ["예상수수료율", fee_rate],
        ["정산 데이터 있음", 1 if (has_settle or db.execute("SELECT COUNT(*) FROM revenue").fetchone()[0]) else 0],
        ["반품 데이터 있음", 1 if (has_settle or db.execute("SELECT COUNT(*) FROM returns").fetchone()[0]) else 0],
        ["정산 반영 마지막 매출인식일", last_recog or ""],
        ["마지막 업데이트", datetime.now(KST).strftime("%Y-%m-%d %H:%M")],
        ["세이버 월 구독료", saver_fee],
        ["위안 환율", float(meta_get(db, "cny_rate", 0) or 0)],
        ["위안 환율 시각", meta_get(db, "cny_rate_at", "") or ""],
    ]
    tables.append(("_요약지표", ["항목", "값"], kpi, {}, True))
    if db.execute("SELECT COUNT(*) FROM ads").fetchone()[0]:
        ad_rows = []
        for d in reversed([k for k in days_sorted if daily[k].get("ad_raw") or daily[k].get("adsales")]):
            x = daily[d]
            ad_rows.append([d, r0(x["ad_raw"]), r0(x["ad"]), r0(x["adsales"]),
                            round(x["adsales"] / x["ad_raw"], 4) if x["ad_raw"] else "", r0(x["imp"]), r0(x["clk"]),
                            r0(x["sales"]), round(x["adsales"] / x["sales"], 4) if x["sales"] else ""])
        tables.append(("광고_일별", ["날짜", "광고비(VAT별도)", "광고비(VAT포함)", "광고매출(14일)", "ROAS", "노출수", "클릭수",
                                  "전체매출", "광고매출비중"], ad_rows, {}, False))
        a30 = []
        for vid, nm, sp, s14, clk, q14 in db.execute(
                "SELECT ad_vid, MAX(ad_name), SUM(spend), SUM(sales14), SUM(clicks), SUM(qty14) FROM ads "
                "WHERE ad_date>=? GROUP BY ad_vid", (since30,)):
            pn, inm = names.get(vid, (nm or "", ""))
            a30.append([pn or nm, inm, vid, r0(sp), r0(s14), round(s14 / sp, 4) if sp else "", r0(clk), r0(q14),
                        r0(prod.get(vid, {}).get("amt", 0))])
        a30.sort(key=lambda r: -(r[3] or 0))
        tables.append(("광고_상품별(최근30일)", ["상품명", "옵션명", "옵션ID", "광고비(VAT별도)", "광고매출(14일)", "ROAS",
                                            "클릭수", "광고판매수량", "전체매출"], a30, {}, False))
    tables.append(option_stats(db, names, inv, t))
    return tables


def import_cost_book(sheets):
    """이 폴더에 '원가대장_가져오기.json' 이 있으면 사이트 원가·마진 계산기로 한 번 가져오기 (이미 내용이 있으면 건너뜀)"""
    fn = os.path.join(BASE_DIR, "원가대장_가져오기.json")
    if not os.path.exists(fn):
        return
    try:
        with open(fn, encoding="utf-8") as f:
            req = json.load(f)
        req.update(token=sheets.token, action="import_book")
        try:
            res = sheets._call(req)
            log.info("원가대장 가져오기 완료: 차수 %s개, 상품 %s개", res.get("batches"), res.get("items"))
        except RuntimeError as e:
            if "이미" not in str(e):   # 서버가 아직 새 버전이 아니면 다음 실행 때 다시 시도
                log.warning("원가대장 가져오기 다음에 다시 시도: %s", e)
                return
            log.warning("원가대장 가져오기 건너뜀: %s", e)
        os.replace(fn, fn + ".완료")
    except Exception as e:
        log.warning("원가대장 가져오기 실패: %s", e)


def import_cost_adds(sheets):
    """수입 원가 계산기에서 보낸 '원가추가_*.json' → 원가·마진에 새 차수와 상품 추가 (처리 후 .완료)"""
    for fn in sorted(glob.glob(os.path.join(BASE_DIR, "원가추가_*.json"))):
        try:
            with open(fn, encoding="utf-8") as f:
                req = json.load(f)
            req.update(token=sheets.token, action="add_book")
            res = sheets._call(req)
            log.info("원가·마진에 차수 '%s' 추가 (상품 %s개)", res.get("batch"), res.get("items"))
            os.replace(fn, fn + ".완료")
        except Exception as e:
            log.warning("원가추가 파일 처리 실패(%s): %s", os.path.basename(fn), e)


def option_stats(db, names, inv, t):
    """원가 계산기용 옵션별 실적 (숨김 시트 _옵션실적)"""
    since30, since90 = d2s(t - timedelta(days=29)), d2s(t - timedelta(days=89))
    st = defaultdict(lambda: defaultdict(float))
    for vid, q, amt in db.execute("SELECT vendor_item_id, SUM(quantity), SUM(order_price) FROM orders "
                                  "WHERE ordered_date>=? GROUP BY vendor_item_id", (since30,)):
        st[vid]["q30"], st[vid]["a30"] = q or 0, amt or 0
    for vid, q in db.execute("SELECT vendor_item_id, SUM(quantity) FROM orders WHERE ordered_date>=? "
                             "GROUP BY vendor_item_id", (since90,)):
        st[vid]["q90"] = q or 0
    for vid, sold, canc, fee, gross in db.execute(
            "SELECT vendor_item_id, SUM(CASE WHEN qty>0 THEN qty ELSE 0 END), SUM(CASE WHEN qty<0 THEN -qty ELSE 0 END), "
            "SUM(CASE WHEN qty>0 THEN fee+fee_vat ELSE 0 END), SUM(CASE WHEN qty>0 THEN gross ELSE 0 END) "
            "FROM settle_lines WHERE recog_date>=? GROUP BY vendor_item_id", (since90,)):
        x = st[vid]
        x["sold"], x["canc"], x["fee"], x["gross"] = sold or 0, canc or 0, fee or 0, gross or 0
    imgs = {v: u for v, u in db.execute("SELECT vendor_item_id, url FROM images")}
    for vid, sp, s14 in db.execute("SELECT ad_vid, SUM(spend), SUM(sales14) FROM ads WHERE ad_date>=? GROUP BY ad_vid", (since30,)):
        st[vid]["ad30"], st[vid]["adsales30"] = sp or 0, s14 or 0
    rows = []
    for vid in set(st) | set(inv) | set(names):
        x = st.get(vid, {})
        pn, inm = names.get(vid, ("", ""))
        stock = inv.get(vid, (None, None))[1]
        q30, a30 = x.get("q30", 0), x.get("a30", 0)
        rows.append([vid, pn, inm, r0(q30), r0(a30), r0(a30 / q30) if q30 else "", r0(x.get("q90", 0)),
                     round(x["canc"] / x["sold"], 4) if x.get("sold") else "",
                     round(x["fee"] / x["gross"], 4) if x.get("gross") else "",
                     r0(stock) if stock is not None else "", imgs.get(vid, ""),
                     r0(x.get("ad30", 0)), r0(x.get("adsales30", 0))])
    return ("_옵션실적", ["옵션ID", "상품명", "옵션명", "판매수량30", "매출액30", "평균판매가30", "판매수량90",
                      "반품률90", "실제수수료율", "현재고", "이미지", "광고비30", "광고매출30"], rows, {}, True)


def option_list(db):
    return [list(r) for r in db.execute(
        "SELECT vendor_item_id, MAX(product_name), MAX(vendor_item_name) FROM revenue "
        "WHERE line_type='상품' AND vendor_item_id<>'' GROUP BY vendor_item_id "
        "UNION SELECT vendor_item_id, MAX(product_name), MAX(item_name) FROM orders "
        "WHERE vendor_item_id<>'' GROUP BY vendor_item_id "
        # 새로 등록해 아직 안 팔린 옵션도 원가 연결 목록에 바로 나오게 (Wing 상품 엑셀 이름 → 없으면 임시 이름)
        "UNION SELECT vendor_item_id, name, '' FROM option_products WHERE vendor_item_id<>'' "
        "AND vendor_item_id NOT IN (SELECT vendor_item_id FROM orders) "
        "UNION SELECT vendor_item_id, '(이름 확인 전 · 새 옵션 ' || vendor_item_id || ')', '' FROM inventory "
        "WHERE vendor_item_id NOT IN (SELECT vendor_item_id FROM orders) "
        "AND vendor_item_id NOT IN (SELECT vendor_item_id FROM option_products)")]


# ─────────────────────────────── 메인 ───────────────────────────────
def setup_logging():
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    fn = os.path.join(BASE_DIR, "logs", f"collector_{datetime.now(KST):%Y%m}.log")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(fn, encoding="utf-8")
    fh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    if sys.stdout is not None:  # pythonw(창 없이 실행)일 때는 화면 출력 생략
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def load_config():
    cfg = configparser.ConfigParser()
    path = os.path.join(BASE_DIR, "config.ini")
    if not cfg.read(path, encoding="utf-8"):
        raise SystemExit("config.ini 파일이 없습니다. config.example.ini를 복사해 config.ini로 만들고 값을 채워주세요.")
    return cfg


def run(args):
    cfg = load_config()
    api = Coupang(cfg["coupang"]["vendor_id"], cfg["coupang"]["access_key"], cfg["coupang"]["secret_key"])
    opt = cfg["options"] if cfg.has_section("options") else {}
    t = today_kst()

    if args.test:
        yday = t - timedelta(days=1)
        n = sum(1 for _ in api.rg_orders(t - timedelta(days=6), t))
        log.info("쿠팡 API 연결 성공! 최근 7일 로켓그로스 주문 %d건", n)
        return

    db = open_db()
    first = meta_get(db, "initialized") is None
    backfill = args.backfill or (int(opt.get("first_run_days", 365)) if first else 0)
    rev_days = max(int(opt.get("revenue_refresh_days", 45)), backfill)
    ord_days = max(int(opt.get("order_refresh_days", 14)), min(backfill, 90))
    ret_days = max(int(opt.get("return_refresh_days", 45)), backfill)

    rg_first = meta_get(db, "rg_initialized") is None
    rg_days = max(int(opt.get("order_refresh_days", 14)),
                  args.backfill or (int(opt.get("first_run_days", 365)) if rg_first else 0))

    def step(label, fn):
        """한 항목이 실패해도 나머지는 계속 수집 (인증 오류만 중단)"""
        try:
            n = fn()
            log.info("  %s %d줄", label, n)
        except CoupangAPIError as e:
            if "인증 실패" in str(e):
                raise
            log.warning("  %s 건너뜀: %s", label, e)

    log.info("수집 시작 (로켓그로스 주문 %d일, 마켓 매출 %d일, 마켓 주문 %d일, 반품/취소 %d일)",
             rg_days, rev_days, ord_days, ret_days)
    auth_err = None
    try:
        collect_all(api, db, opt, t, rg_days, rev_days, ord_days, ret_days, backfill, step)
    except CoupangAPIError as e:
        if "인증 실패" not in str(e):
            raise
        auth_err = e   # 쿠팡 인증이 막혀도 사이트 화면·원가·지난 데이터 표는 계속 보냄
        log.error("오류로 중단: %s (쿠팡 수집만 건너뛰고 사이트 갱신은 계속)", str(e)[:120])
    db.commit()
    send_to_sheets(cfg, db, opt)
    if auth_err:
        log.info("완료 (쿠팡 인증 실패로 새 주문·재고는 못 받음)")
    else:
        log.info("완료")


def collect_all(api, db, opt, t, rg_days, rev_days, ord_days, ret_days, backfill, step):
    step("로켓그로스 주문", lambda: collect_rg_orders(api, db, t - timedelta(days=rg_days), t))
    step("로켓그로스 재고", lambda: collect_rg_inventory(api, db))
    try:
        n = import_product_excels(db, api.vendor_id, opt)
        if n:
            log.info("  상품 엑셀 옵션 %d개 연결", n)
    except Exception as e:
        log.warning("  상품 엑셀 읽기 건너뜀: %s", e)
    step("상품 이미지", lambda: collect_images(api, db))
    try:
        probe_rg_cancels(api, db)
    except Exception as e:
        log.warning("  취소 조회 확인 건너뜀: %s", e)
    step("마켓 매출내역", lambda: collect_revenue(api, db, t - timedelta(days=rev_days), t))
    step("마켓 주문", lambda: collect_orders(api, db, t - timedelta(days=ord_days), t))
    step("반품/취소", lambda: collect_returns(api, db, t - timedelta(days=ret_days), t))
    months = []
    y, m = t.year, t.month
    for _ in range(max(3, backfill // 30 + 1)):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    step("정산내역", lambda: collect_settlements(api, db, months))
    try:
        n = import_ad_reports(db, api.vendor_id, opt)
        if n:
            log.info("  광고 보고서 %d줄 반영", n)
    except Exception as e:
        log.warning("  광고 보고서 반영 건너뜀: %s", e)
    try:
        n = import_settlement_excels(db, api.vendor_id, opt)
        log.info("  정산 엑셀 새 줄 %d", n)
    except Exception as e:
        log.warning("  정산 엑셀 반영 건너뜀: %s", e)
    try:
        import_wing_sales(db, api.vendor_id, opt)
    except Exception as e:
        log.warning("  Wing 매출 반영 건너뜀: %s", e)
    try:
        fix_settle_and_orders(db)
    except Exception as e:
        log.warning("  정산/주문 보정 건너뜀: %s", e)
    prune_archives(30)
    meta_set(db, "rg_initialized", datetime.now(KST).isoformat())
    meta_set(db, "initialized", datetime.now(KST).isoformat())
    db.commit()


def import_wing_sales(db, vendor_id, opt=None):
    """크롬 확장이 10분마다 저장하는 Wing 판매분석 매출(판매-취소, 최근 15일)을 읽음"""
    db.execute("CREATE TABLE IF NOT EXISTS wing_daily (sale_date TEXT PRIMARY KEY, gmv REAL, units REAL, fetched_at TEXT)")
    files = []
    for folder in _excel_folders(opt):
        files += glob.glob(os.path.join(folder, f"{vendor_id}-wing매출*.json"))
    files = sorted(set(files), key=os.path.getmtime)
    if not files:
        return 0
    newest = files[-1]
    for old in files[:-1]:
        try:
            _archive(old, os.path.join(BASE_DIR, "정산엑셀_보관"), os.path.basename(old))
        except Exception:
            pass
    key = f"{os.path.getmtime(newest):.0f}"
    if meta_get(db, "wing_file") == key:
        return 0
    j = json.load(open(newest, encoding="utf-8"))
    rows = [(str(r["date"])[:10], float(r.get("gmv") or 0), float(r.get("unitsSold") or 0), j.get("fetchedAt", ""))
            for r in j.get("cardData", []) if r.get("date")]
    with db:
        db.executemany("INSERT OR REPLACE INTO wing_daily VALUES (?,?,?,?)", rows)
    meta_set(db, "wing_file", key)
    db.commit()
    log.info("  Wing 판매분석 매출 %d일 반영 (받은 시각 %s)", len(rows), j.get("fetchedAt", "")[:16])
    return len(rows)


def fix_settle_and_orders(db):
    """1) 같은 주 정산 엑셀을 여러 번 받아 생긴 중복 줄 제거
       2) 쿠팡 주문 API에 빠진(또는 늦게 들어오는) 주문을 정산 엑셀로 채움 (status='RGS')"""
    with db:
        n1 = db.execute(
            "DELETE FROM settle_lines WHERE EXISTS (SELECT 1 FROM settle_lines b "
            "WHERE b.order_id=settle_lines.order_id AND b.vendor_item_id=settle_lines.vendor_item_id "
            "AND b.tx_type=settle_lines.tx_type AND b.qty=settle_lines.qty AND b.gross=settle_lines.gross "
            "AND b.recog_date=settle_lines.recog_date AND b.source<>settle_lines.source "
            "AND b.rowid<settle_lines.rowid)").rowcount
        db.execute("DELETE FROM orders WHERE status='RGS'")
        n2 = db.execute(
            "INSERT INTO orders SELECT s.recog_date, s.recog_date||'T00:00:00', s.order_id, 'RGS', s.vendor_item_id, "
            "s.product_name, s.item_name, s.qty, 0, s.price, s.gross, 0 FROM settle_lines s "
            "WHERE s.qty>0 AND s.recog_date<>'' AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id=s.order_id "
            "AND o.vendor_item_id=s.vendor_item_id)").rowcount
    if n1:
        log.info("  정산 엑셀 중복 %d줄 제거", n1)
    log.info("  주문 API에 없는 주문 %d줄을 정산 엑셀로 보충", n2)


def update_cny_rate(db):
    """네이버 환율(하나은행 매매기준율) 위안 → 원. 1시간에 한 번만 확인하고 '위안환율.json'으로도 남김"""
    last = meta_get(db, "cny_checked_at")
    if last and time.time() - float(last) < 3600:
        return
    meta_set(db, "cny_checked_at", str(time.time()))
    rate, src = None, ""
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://finance.naver.com/"}
    try:   # 1) 네이버 증권 모바일 API
        req = urllib.request.Request("https://m.stock.naver.com/front-api/marketIndex/productDetail?category=exchange&reutersCode=FX_CNYKRW", headers=hdr)
        with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as r:
            j = json.loads(r.read().decode("utf-8"))
        res = j.get("result", j)
        v = res.get("closePrice") or res.get("calcPrice")
        if v:
            rate, src = float(str(v).replace(",", "")), "네이버(m.stock)"
    except Exception as e:
        log.info("  네이버 환율 API 실패: %s", e)
    if rate is None:
        try:   # 2) 네이버 금융 환율 목록 (하나은행 매매기준율)
            req = urllib.request.Request("https://finance.naver.com/marketindex/exchangeList.naver", headers=hdr)
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=15) as r:
                html = r.read().decode("euc-kr", "ignore")
            m = re.search(r"중국\s*CNY.*?<td class=\"sale\">\s*([\d,\.]+)", html, re.S)
            if m:
                rate, src = float(m.group(1).replace(",", "")), "네이버(하나은행 매매기준율)"
        except Exception as e:
            log.info("  네이버 환율 목록 실패: %s", e)
    if not rate or not (100 < rate < 400):
        log.warning("  위안 환율을 못 받음 (지난 값 유지)")
        return
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    meta_set(db, "cny_rate", str(rate))
    meta_set(db, "cny_rate_at", now)
    db.commit()
    try:
        with open(os.path.join(BASE_DIR, "위안환율.json"), "w", encoding="utf-8") as f:
            json.dump({"rate": rate, "at": now, "source": src}, f, ensure_ascii=False)
    except OSError:
        pass
    log.info("  위안 환율 %.2f원 (%s)", rate, src)


def send_to_sheets(cfg, db, opt):
    url = cfg["google"].get("webapp_url", "").strip() if cfg.has_section("google") else ""
    if not url or "여기에" in url:
        log.warning("구글 시트 주소(webapp_url)가 없어 시트 전송은 건너뜁니다.")
        return
    try:
        update_cny_rate(db)
    except Exception as e:
        log.warning("  환율 확인 건너뜀: %s", e)
    sheets = Sheets(url, cfg["google"]["token"])
    sheets.sync_options(option_list(db))
    import_cost_book(sheets)
    import_cost_adds(sheets)
    costs = load_costs(sheets)
    log.info("원가 %d개 옵션 반영", len(costs))
    site = os.path.join(BASE_DIR, "site.html")
    if os.path.exists(site):
        html = open(site, encoding="utf-8").read()
        h = hashlib.md5(html.encode("utf-8")).hexdigest()
        if meta_get(db, "site_hash") != h:
            sheets._call({"token": sheets.token, "action": "set_html", "html": html})
            meta_set(db, "site_hash", h)
            db.commit()
            log.info("사이트 화면 업데이트 반영")
    fee_rate = float(opt.get("estimated_fee_rate", 0) or 0)
    sent = 0
    for name, header, rows, fmts, hidden in build_tables(db, costs, fee_rate, float(opt.get("saver_monthly_fee", 99000) or 0)):
        # 10분마다 돌기 때문에, 바뀐 표만 보냄 (안 바뀐 표는 건너뜀)
        h = hashlib.md5(json.dumps([header, rows, fmts], ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
        if meta_get(db, "tbl_" + name) == h:
            continue
        sheets.write_table(name, header, rows, fmts, hidden)
        meta_set(db, "tbl_" + name, h)
        db.commit()
        sent += 1
        log.info("  시트 '%s' %d줄 전송", name, len(rows))
    if not sent:
        log.info("  바뀐 표 없음")
    sheets.finish()


def report_failure(msg):
    """실패 시 구글 시트의 '_요약지표'에 상태를 남겨 직원들이 볼 수 있게 함"""
    try:
        cfg = load_config()
        sheets = Sheets(cfg["google"]["webapp_url"], cfg["google"]["token"])
        sheets._call({"token": sheets.token, "action": "status",
                      "message": f"{datetime.now(KST):%Y-%m-%d %H:%M} 수집 실패: {msg[:200]}"})
    except Exception:
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--backfill", type=int, default=0, help="과거 N일 데이터를 다시 받기")
    p.add_argument("--test", action="store_true", help="쿠팡 API 연결만 확인")
    p.add_argument("--dump", action="store_true", help="쿠팡 응답 원문 저장(문제 확인용)")
    a = p.parse_args()
    DUMP = a.dump
    setup_logging()
    try:
        run(a)
    except Exception as e:
        log.exception("오류로 중단: %s", e)
        if not a.test:
            report_failure(str(e))
        sys.exit(1)
