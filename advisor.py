#!/usr/bin/env python3
"""
btc_oi_advisor.py — Deribit オプションOIから BTC の「地形」を読んで助言文を出す

使い方:
    python3 btc_oi_advisor.py                  # BTC, 全満期
    python3 btc_oi_advisor.py --currency ETH
    python3 btc_oi_advisor.py --max-days 45    # 45日以内の満期だけで評価
    python3 btc_oi_advisor.py --json           # 生の計算結果もJSONで出す

注意: これは OI の偏りから「壁・支持・磁石」を機械的に読むだけの参考ツール。
      方向を保証するものではない。イベント(FOMC等)は一切考慮しない。
"""

import argparse
import html
import json
import math
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

API = "https://www.deribit.com/api/v2"

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


# ---------------------------------------------------------------- fetch

def api_get(path, params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API}/{path}?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "oi-advisor/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    if "result" not in data:
        raise RuntimeError(f"API error: {data}")
    return data["result"]


def fetch_spot(currency):
    r = api_get("public/get_index_price",
                {"index_name": f"{currency.lower()}_usd"})
    return float(r["index_price"])


def fetch_options(currency):
    """全オプションの板サマリ(OI含む)を取得"""
    return api_get("public/get_book_summary_by_currency",
                   {"currency": currency, "kind": "option"})


def fetch_funding(currency):
    """無期限先物のファンディングレート(8h, %)。失敗したらNone"""
    try:
        r = api_get("public/ticker",
                    {"instrument_name": f"{currency}-PERPETUAL"})
        return float(r["funding_8h"]) * 100.0
    except Exception:
        return None


HL_API = "https://api.hyperliquid.xyz/info"


def fetch_hyperliquid(currency):
    """Hyperliquid の perp コンテキスト。失敗したら None (キー不要)"""
    try:
        body = json.dumps({"type": "metaAndAssetCtxs"}).encode()
        req = urllib.request.Request(
            HL_API, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "oi-advisor/1.0"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            meta, ctxs = json.loads(r.read().decode())
        for u, c in zip(meta["universe"], ctxs):
            if u["name"] != currency.upper():
                continue
            oi = float(c["openInterest"])                # 枚 (BTC)
            mark = float(c["markPx"])
            # HL のファンディングは1時間ごと。Deribit(8h)に揃える
            f8 = float(c["funding"]) * 8 * 100
            return {
                "oi": oi,
                "oi_usd": oi * mark,
                "funding_8h": f8,
                "funding_apr": f8 * 3 * 365,
                "mark": mark,
                "oracle": float(c["oraclePx"]),
                "premium": float(c["premium"]) * 100,
                "day_volume": float(c["dayNtlVlm"]),
            }
    except Exception:
        return None
    return None


def _fetch_hl_l2book(currency, n_sig_figs):
    """Hyperliquid の板 (L2)。nSigFigs で価格を丸めて集約した段が返る"""
    body = json.dumps({"type": "l2Book", "coin": currency.upper(),
                       "nSigFigs": n_sig_figs}).encode()
    req = urllib.request.Request(
        HL_API, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "oi-advisor/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())["levels"]   # [bids, asks]


def fetch_hl_depth(currency):
    """
    HL板の厚み(USD建て)を mid±2% / ±5% で集計。失敗したら None。

    l2Book は最大20段/片側しか返さないため、細かい丸め(3桁)で±2%を、
    粗い丸め(2桁)で±5%をカバーする。スナップショット値なので見せ板は拾いうる。
    """
    try:
        def depth_within(levels, pct):
            bids, asks = levels
            mid = (float(bids[0]["px"]) + float(asks[0]["px"])) / 2
            lo, hi = mid * (1 - pct), mid * (1 + pct)
            bid = sum(float(l["px"]) * float(l["sz"])
                      for l in bids if float(l["px"]) >= lo)
            ask = sum(float(l["px"]) * float(l["sz"])
                      for l in asks if float(l["px"]) <= hi)
            return mid, bid, ask

        mid, bid2, ask2 = depth_within(_fetch_hl_l2book(currency, 3), 0.02)
        _, bid5, ask5 = depth_within(_fetch_hl_l2book(currency, 2), 0.05)
        return {"mid": mid, "bid2": bid2, "ask2": ask2,
                "bid5": bid5, "ask5": ask5}
    except Exception:
        return None


ETF_MAP = {"BTC": "IBIT", "ETH": "ETHA"}
CBOE_API = "https://cdn.cboe.com/api/global/delayed_quotes/options/"


def parse_occ_symbol(sym):
    """'IBIT281215P00020000' -> (expiry_date, strike, 'C'/'P')。不正なら None"""
    try:
        strike = int(sym[-8:]) / 1000.0
        cp = sym[-9]
        if cp not in "CP":
            return None
        y = 2000 + int(sym[-15:-13])
        mo, dd = int(sym[-13:-11]), int(sym[-11:-9])
        # 満期は米国市場の引け後
        return datetime(y, mo, dd, 21, 0, tzinfo=timezone.utc), strike, cp
    except Exception:
        return None


def fetch_etf_terrain(currency, d, max_age_h=18.0):
    """
    米国現物ETF (IBIT/ETHA) のオプション建玉地形。CBOEの15分遅延データ。
    OIは日次更新なので d にキャッシュし、max_age_h より新しければ再取得しない。
    取得失敗時は古いキャッシュで続行。対象外通貨・完全失敗は None。
    """
    sym = ETF_MAP.get(currency.upper())
    if not sym:
        return None
    cache = os.path.join(d, f"{currency}_etf.json")
    old = None
    try:
        with open(cache) as f:
            old = json.load(f)
        age = time.time() - datetime.fromisoformat(old["fetched"]).timestamp()
        if age < max_age_h * 3600:
            return old
    except Exception:
        pass

    try:
        req = urllib.request.Request(CBOE_API + f"{sym}.json",
                                     headers={"User-Agent": "oi-advisor/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())["data"]
        now = datetime.now(timezone.utc)
        price = float(data["current_price"])
        strikes = defaultdict(lambda: {"call": 0.0, "put": 0.0,
                                       "call_w": 0.0, "put_w": 0.0})
        expiries = defaultdict(lambda: {"call": 0.0, "put": 0.0})
        tc = tp = 0.0
        for o in data.get("options", []):
            parsed = parse_occ_symbol(o.get("option", ""))
            oi = float(o.get("open_interest") or 0)
            if not parsed or oi <= 0:
                continue
            exp, strike, cp = parsed
            days = max((exp - now).total_seconds() / 86400.0, 0.05)
            w = 1.0 / math.sqrt(days / 7.0 + 1.0)   # 遠いLEAPSを減点 (book と同じ)
            s = strikes[str(strike)]
            key = "call" if cp == "C" else "put"
            s[key] += oi
            s[key + "_w"] += oi * w
            expiries[exp.strftime("%Y-%m-%d")][key] += oi
            if cp == "C":
                tc += oi
            else:
                tp += oi
        if not strikes:
            return old
        etf = {"symbol": sym, "fetched": now.isoformat(), "price": price,
               "total_call": tc, "total_put": tp, "strikes": dict(strikes),
               "expiries": dict(expiries)}
        tmp = cache + ".tmp"
        with open(tmp, "w") as f:
            json.dump(etf, f)
        os.replace(tmp, cache)
        return etf
    except Exception:
        return old


def fetch_price_history(currency, hours=72):
    """無期限先物の1時間足 (直近hours時間)。失敗したら None"""
    try:
        end = int(time.time() * 1000)
        start = end - hours * 3600 * 1000
        r = api_get("public/get_tradingview_chart_data",
                    {"instrument_name": f"{currency}-PERPETUAL",
                     "start_timestamp": start, "end_timestamp": end,
                     "resolution": 60})
        if r.get("status") != "ok" or not r.get("ticks"):
            return None
        return {"t": r["ticks"], "close": r["close"],
                "high": r["high"], "low": r["low"]}
    except Exception:
        return None


def fetch_dvol(currency):
    """DVOL(ボラティリティ指数)の直近値。失敗したらNone"""
    try:
        end = int(time.time() * 1000)
        start = end - 6 * 3600 * 1000
        r = api_get("public/get_volatility_index_data",
                    {"currency": currency, "start_timestamp": start,
                     "end_timestamp": end, "resolution": 3600})
        data = r.get("data") or []
        return float(data[-1][4]) if data else None   # 最後のcandleのclose
    except Exception:
        return None


# ---------------------------------------------------------------- 米国需要・ベーシス
#
# ETF流入 (現物の実需) を「見落とさない」ための3点セット。
#   ・iShares 公式の IBIT/ETHA 発行済口数 → 口数の増減×NAV = 創設/償還 = その日の資金流出入
#     (日次・米国引け後の確定値。前日の需要の裏取りであって先行指標ではない)
#   ・Coinbase プレミアム: 米国の現物買い (APの創設買いを含む) が乗ると当日中にプラスへ振れる
#   ・Deribit 期日先物のベーシス: 「ETFロング/先物ショート」のキャリー妙味そのもの。
#     高いほどETF流入に方向性の薄いアービ資金が混じりやすく、低ければ実需寄り

def parse_future_expiry(name):
    """'BTC-25SEP26' -> 満期 datetime (08:00 UTC)。無期限・不正なら None"""
    try:
        _, expiry = name.split("-")
        day = int(expiry[:-5])
        mon = MONTHS[expiry[-5:-2]]
        year = 2000 + int(expiry[-2:])
        return datetime(year, mon, day, 8, 0, tzinfo=timezone.utc)
    except Exception:
        return None


def fetch_futures_basis(currency, spot, now):
    """
    Deribit 期日先物のベーシス (対現物指数, 年率換算) と先物建玉。失敗したら None。
    front = 20日以上先で建玉が最大の限月 (主力)。next = その30日以上先で建玉最大の限月。
    満期が近い限月は年率換算が僅かな値差で暴れる (残り8日なら0.1%の差が年率4.6pt) ので外す。
    """
    try:
        rows = api_get("public/get_book_summary_by_currency",
                       {"currency": currency, "kind": "future"})
        contracts, total_oi, perp_oi = [], 0.0, 0.0
        for r in rows:
            name = r.get("instrument_name", "")
            oi = float(r.get("open_interest") or 0.0)     # インバース先物: USD建て
            total_oi += oi
            if name.endswith("-PERPETUAL"):
                perp_oi += oi
                continue
            exp = parse_future_expiry(name)
            mark = float(r.get("mark_price") or 0.0)
            if not exp or mark <= 0 or spot <= 0:
                continue
            days = (exp - now).total_seconds() / 86400.0
            if days < 1.0:      # 満期直前は年率換算が暴れるので外す
                continue
            basis = (mark / spot - 1.0) * 100.0
            contracts.append({"name": name, "days": days, "oi": oi,
                              "basis": basis, "ann": basis * 365.0 / days})
        if not contracts:
            return None
        contracts.sort(key=lambda c: c["days"])
        eligible = [c for c in contracts if c["days"] >= 20]
        front = max(eligible, key=lambda c: c["oi"]) if eligible else contracts[-1]
        later = [c for c in contracts if c["days"] >= front["days"] + 30]
        nxt = max(later, key=lambda c: c["oi"]) if later else None
        return {"front": front, "next": nxt, "contracts": contracts,
                "total_oi": total_oi, "perp_oi": perp_oi}
    except Exception:
        return None


COINBASE_API = "https://api.exchange.coinbase.com/products/{c}-USD/ticker"
BINANCE_API = "https://api.binance.com/api/v3/ticker/price?symbol={c}USDT"
BYBIT_API = "https://api.bybit.com/v5/market/tickers?category=spot&symbol={c}USDT"


def _get_json(url, timeout=15, ua="oi-advisor/1.0"):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_coinbase_premium(currency):
    """
    Coinbase (USD建て) と Binance (USDT建て) の現物価格差 (%)。失敗したら None。
    Binance が取れなければ Bybit で代用。USDT/USD の乖離も混じる点は注意。
    """
    c = currency.upper()
    try:
        cb = float(_get_json(COINBASE_API.format(c=c))["price"])
    except Exception:
        return None
    ref = ref_name = None
    try:
        ref, ref_name = float(_get_json(BINANCE_API.format(c=c))["price"]), "Binance"
    except Exception:
        try:
            lst = _get_json(BYBIT_API.format(c=c))["result"]["list"]
            ref, ref_name = float(lst[0]["lastPrice"]), "Bybit"
        except Exception:
            return None
    if not ref:
        return None
    return {"coinbase": cb, "ref": ref, "ref_name": ref_name,
            "premium": (cb / ref - 1.0) * 100.0}


ISHARES_MAP = {
    "BTC": ("IBIT", "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust-etf"),
    "ETH": ("ETHA", "https://www.ishares.com/us/products/337614/ishares-ethereum-trust-etf"),
}
ISHARES_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 oi-advisor/1.0"
ETF_FUND_KEEP = 120     # 日次履歴の保持数


def _ishares_datapoint(page, key):
    """ページ埋め込みJSONから "key":{...} を1つ取り出す (波括弧の対応で切る)。無ければ None"""
    m = re.search(r'"%s":\{' % re.escape(key), page)
    if not m:
        return None
    i, depth = m.end() - 1, 0
    for j in range(i, min(i + 4000, len(page))):
        ch = page[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(page[i:j + 1])
                except ValueError:
                    return None
    return None


def _dp_value(dp):
    """データ点の数値。value が無ければ formattedValue ('1,375,120,000') を読む"""
    if not dp:
        return None
    v = dp.get("value")
    if isinstance(v, (int, float)):
        return float(v)
    s = dp.get("formattedValue")
    try:
        return float(str(s).replace(",", "")) if s else None
    except ValueError:
        return None


def _dp_date(dp):
    """データ点の基準日 'Sep 03, 2026' -> '2026-09-03'"""
    if not dp:
        return None
    s = dp.get("formattedAsOfDate")
    try:
        return datetime.strptime(s, "%b %d, %Y").strftime("%Y-%m-%d") if s else None
    except ValueError:
        return None


def summarize_etf_fund(data):
    """蓄積した日次履歴から、直近の流出入・5回分累計・NAV乖離をまとめる。履歴なしは None"""
    hist = (data or {}).get("history") or {}
    if not hist:
        return None
    dates = sorted(hist)
    latest = hist[dates[-1]]
    out = {"symbol": data["symbol"], "asof": dates[-1], "fetched": data.get("fetched"),
           "shares": latest["shares"], "nav": latest["nav"],
           "nav_change": latest.get("nav_change"),
           "net_assets": latest.get("net_assets"), "volume": latest.get("volume"),
           "n_hist": len(dates), "prev_asof": None, "flow_shares": None,
           "flow_usd": None, "flow5_usd": None, "flow_z": None,
           "premium": None, "flows": []}
    flows = []
    for a, b in zip(dates, dates[1:]):
        ds = hist[b]["shares"] - hist[a]["shares"]
        flows.append((b, ds, ds * hist[b]["nav"]))    # 創設/償還はNAVで値付けされる
    if flows:
        out["prev_asof"], out["flow_shares"], out["flow_usd"] = \
            dates[-2], flows[-1][1], flows[-1][2]
        out["flow5_usd"] = sum(f[2] for f in flows[-5:])
        out["flows"] = [(b, usd) for b, _, usd in flows[-10:]]
        if len(flows) >= 10:
            vals = [f[2] for f in flows[-30:]]
            mu = sum(vals) / len(vals)
            sd = (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
            out["flow_z"] = (flows[-1][2] - mu) / sd if sd > 0 else None
    close = latest.get("close")
    if close and latest.get("close_asof") == dates[-1] and latest["nav"]:
        out["premium"] = (close / latest["nav"] - 1.0) * 100.0
    return out


def fetch_etf_fund(currency, d, max_age_h=3.0):
    """
    iShares 公式ページから IBIT/ETHA の発行済口数・NAV・純資産を読み、日次履歴を d に蓄積する。
    公式の数字は米国引け後に日次更新。max_age_h より新しいキャッシュがあれば再取得しない。
    取得失敗時は古い履歴で続行。対象外通貨・履歴なしは None。
    """
    if currency.upper() not in ISHARES_MAP:
        return None
    sym, url = ISHARES_MAP[currency.upper()]
    cache = os.path.join(d, f"{currency}_etf_fund.json")
    old = None
    try:
        with open(cache) as f:
            old = json.load(f)
        age = time.time() - datetime.fromisoformat(old["fetched"]).timestamp()
        if age < max_age_h * 3600:
            return summarize_etf_fund(old)
    except Exception:
        pass

    hist = dict((old or {}).get("history") or {})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": ISHARES_UA})
        with urllib.request.urlopen(req, timeout=40) as r:
            page = html.unescape(r.read().decode("utf-8", "replace"))
        shares = _ishares_datapoint(page, "sharesOutstanding")
        nav = _ishares_datapoint(page, "navAmount")
        asof = _dp_date(shares) or _dp_date(nav)
        sh_v, nav_v = _dp_value(shares), _dp_value(nav)
        if not asof or not sh_v or not nav_v:
            raise ValueError("iShares ページから口数/NAVを読めない")
        close = _ishares_datapoint(page, "closingPrice")
        hist[asof] = {
            "shares": sh_v, "nav": nav_v,
            "nav_change": _dp_value(_ishares_datapoint(page, "navAmountChange")),
            "net_assets": _dp_value(_ishares_datapoint(page, "totalNetAssetsFundLevel")),
            "close": _dp_value(close), "close_asof": _dp_date(close),
            "volume": _dp_value(_ishares_datapoint(page, "consolidatedVolume")),
        }
        hist = dict(sorted(hist.items())[-ETF_FUND_KEEP:])
        data = {"symbol": sym, "fetched": datetime.now(timezone.utc).isoformat(),
                "history": hist}
        tmp = cache + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, cache)
        return summarize_etf_fund(data)
    except Exception:
        return summarize_etf_fund(old) if old else None


# ---------------------------------------------------------------- parse

def parse_instrument(name):
    """'BTC-26SEP26-80000-C' -> (expiry_date, strike, 'C'/'P')"""
    try:
        _, expiry, strike, cp = name.split("-")
        day = int(expiry[:-5])
        mon = MONTHS[expiry[-5:-2]]
        year = 2000 + int(expiry[-2:])
        exp = datetime(year, mon, day, 8, 0, tzinfo=timezone.utc)  # 満期はUTC8時
        return exp, float(strike), cp
    except Exception:
        return None


def build_book(raw, now, max_days):
    """
    strike -> {"call": OI, "put": OI, "call_w": 重み付きOI, "put_w": ...}
    重み = 時間重み(満期が近いほど大) 。ガンマが 1/sqrt(T) に比例することの近似。
    """
    book = defaultdict(lambda: {"call": 0.0, "put": 0.0,
                                "call_w": 0.0, "put_w": 0.0})
    expiries = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    instruments = []   # GEX計算用: (strike, cp, oi, iv, years)

    for item in raw:
        parsed = parse_instrument(item.get("instrument_name", ""))
        if not parsed:
            continue
        exp, strike, cp = parsed
        oi = float(item.get("open_interest") or 0.0)
        if oi <= 0:
            continue
        days = max((exp - now).total_seconds() / 86400.0, 0.05)
        if max_days and days > max_days:
            continue
        t_weight = 1.0 / math.sqrt(days / 7.0 + 1.0)   # 1週間で~0.7, 90日で~0.27
        key = "call" if cp == "C" else "put"
        book[strike][key] += oi
        book[strike][key + "_w"] += oi * t_weight
        expiries[exp.strftime("%d%b%y").upper()][key] += oi

        iv = float(item.get("mark_iv") or 0.0) / 100.0   # Deribitは%表記
        if iv > 0:
            instruments.append((strike, cp, oi, iv, days / 365.0))

    return dict(book), dict(expiries), instruments


# ---------------------------------------------------------------- metrics

def bs_gamma(spot, strike, iv, years):
    """Black-Scholesのガンマ (Call/Put共通)。金利0近似。"""
    if years <= 0 or iv <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * years) / (iv * math.sqrt(years))
    phi = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    return phi / (spot * iv * math.sqrt(years))


def net_gex_at(price, instruments):
    """
    仮定価格priceでのネットGEX(USD, 1%変動あたり)。
    符号規約: ディーラーはCallロング/Putショートと仮定する慣習的な近似
    (Laevitas等と同じナイーブ規約)。Call: +, Put: -。
    GEX_1% = Γ × OI × price × (price × 0.01)
    """
    total = 0.0
    for strike, cp, oi, iv, years in instruments:
        g = bs_gamma(price, strike, iv, years)
        sign = 1.0 if cp == "C" else -1.0
        total += sign * g * oi * price * price * 0.01
    return total


def compute_gex(instruments, spot):
    """ネットGEX / ABS GEX / ストライク別GEX / ガンマフリップ水準"""
    per_strike = defaultdict(lambda: {"call": 0.0, "put": 0.0})
    net, abs_ = 0.0, 0.0
    for strike, cp, oi, iv, years in instruments:
        g = bs_gamma(spot, strike, iv, years)
        gex = g * oi * spot * spot * 0.01
        abs_ += gex
        if cp == "C":
            per_strike[strike]["call"] += gex
            net += gex
        else:
            per_strike[strike]["put"] += gex
            net -= gex

    # ガンマフリップ: net GEX(S) の符号が変わる価格をグリッド探索し、現値に最も近い交点を採用
    crossings = []
    lo, hi = spot * 0.6, spot * 1.4
    steps = 80
    prev_p, prev_v = None, None
    for i in range(steps + 1):
        p = lo + (hi - lo) * i / steps
        v = net_gex_at(p, instruments)
        if prev_v is not None and prev_v * v < 0:
            crossings.append(prev_p + (p - prev_p) * (0 - prev_v) / (v - prev_v))
        prev_p, prev_v = p, v
    flip = min(crossings, key=lambda x: abs(x - spot)) if crossings else None

    top = sorted(per_strike.items(),
                 key=lambda kv: -(kv[1]["call"] + kv[1]["put"]))[:3]
    return {"net": net, "abs": abs_, "flip": flip,
            "top_strikes": [(k, v["call"] + v["put"]) for k, v in top]}


def norm_cdf(x):
    """標準正規分布の累積分布関数"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_d1(spot, strike, iv, years):
    return ((math.log(spot / strike) + 0.5 * iv * iv * years)
            / (iv * math.sqrt(years)))


def compute_vol_structure(instruments, spot):
    """
    ATM IVの期間構造(7d/30d/90d)と25Δスキュー(30d近辺のリスクリバーサル)。
    mark IVからの自前計算。データ不足なら None。
    """
    exps = defaultdict(list)
    for strike, cp, oi, iv, years in instruments:
        if iv > 0 and years > 0.003:        # 満期1日未満はIVが歪むので除外
            exps[round(years, 6)].append((strike, cp, iv))
    if not exps:
        return None

    def atm_iv(rows):
        near = sorted(rows, key=lambda r: abs(r[0] - spot))[:4]
        return sum(r[2] for r in near) / len(near)

    def iv_at_delta(rows, years, cp, target):
        best_err, best_iv = None, None
        for strike, c, iv in rows:
            if c != cp:
                continue
            d1 = bs_d1(spot, strike, iv, years)
            delta = norm_cdf(d1) if cp == "C" else norm_cdf(d1) - 1.0
            err = abs(delta - target)
            if best_err is None or err < best_err:
                best_err, best_iv = err, iv
        return best_iv

    tenors, skew = {}, None
    for label, days in (("7d", 7), ("30d", 30), ("90d", 90)):
        yrs = min(exps, key=lambda y: abs(y * 365 - days))
        if abs(yrs * 365 - days) > days:    # 目標テナーから離れすぎなら欠測
            continue
        rows = exps[yrs]
        tenors[label] = {"days": yrs * 365, "atm": atm_iv(rows) * 100}
        if label == "30d":
            p = iv_at_delta(rows, yrs, "P", -0.25)
            c = iv_at_delta(rows, yrs, "C", 0.25)
            if p and c:
                skew = (p - c) * 100        # volポイント。正=Put割高
    if not tenors:
        return None
    out = {"tenors": tenors, "skew_25d": skew}
    if "7d" in tenors and "90d" in tenors:
        out["term_slope"] = tenors["90d"]["atm"] - tenors["7d"]["atm"]
    else:
        out["term_slope"] = None
    return out


def proximity(spot, strike, band=0.25):
    """現値からの近さ(0..1)。band=25%離れるとほぼ0。ATMガンマ集中の近似。"""
    x = abs(strike - spot) / (spot * band)
    return math.exp(-x * x)


def analyze(book, spot):
    strikes = sorted(book)
    total_call = sum(v["call"] for v in book.values())
    total_put = sum(v["put"] for v in book.values())
    pcr = total_put / total_call if total_call else float("nan")

    # --- 壁と支持: 重み付きOIで上位を拾い、現値に近い順に並べ替える
    call_walls = sorted(
        sorted(((k, v["call_w"], v["call"]) for k, v in book.items()
                if k > spot and v["call"] > 0),
               key=lambda x: -x[1])[:3],
        key=lambda x: x[0])                      # 近い(低い)順
    put_supports = sorted(
        sorted(((k, v["put_w"], v["put"]) for k, v in book.items()
                if k < spot and v["put"] > 0),
               key=lambda x: -x[1])[:3],
        key=lambda x: -x[0])                     # 近い(高い)順

    # --- 磁石スコア: (Call+Put)重み付きOI × 現値への近さ
    magnets = sorted(
        ((k, (v["call_w"] + v["put_w"]) * proximity(spot, k))
         for k, v in book.items()),
        key=lambda x: -x[1])[:3]

    # --- 上下の「引力」: 磁石成分を上下で合算して非対称性を見る
    pull_up = sum((v["call_w"] + v["put_w"]) * proximity(spot, k)
                  for k, v in book.items() if k > spot)
    pull_dn = sum((v["call_w"] + v["put_w"]) * proximity(spot, k)
                  for k, v in book.items() if k < spot)
    tilt = (pull_up - pull_dn) / (pull_up + pull_dn) if (pull_up + pull_dn) else 0.0

    # --- 直近の空白地帯: 現値の上下で重み付きOIが薄い区間
    def nearest_wall_dist(walls):
        return (walls[0][0] - spot) / spot if walls else None

    # --- Max Pain (全体の近似): 各仮定価格での買い手ペイオフ合計が最小の点
    grid = [s for s in strikes if 0.5 * spot < s < 1.6 * spot]
    max_pain, best = None, float("inf")
    for p in grid:
        pain = sum(v["call"] * max(p - k, 0) + v["put"] * max(k - p, 0)
                   for k, v in book.items())
        if pain < best:
            best, max_pain = pain, p

    return {
        "spot": spot,
        "pcr": pcr,
        "total_call": total_call,
        "total_put": total_put,
        "call_walls": call_walls,
        "put_supports": put_supports,
        "magnets": magnets,
        "tilt": tilt,
        "max_pain": max_pain,
    }


# ---------------------------------------------------------------- snapshots

def snap_dir(base=None):
    d = base or os.path.join(os.path.expanduser("~"), ".btc_oi_advisor")
    os.makedirs(d, exist_ok=True)
    return d


def build_snapshot(currency, m, g, funding, dvol, book, now, hl=None,
                   depth=None, vs=None, etf=None, fund=None, cb=None, basis=None):
    front = (basis or {}).get("front") or {}
    nxt = (basis or {}).get("next") or {}
    return {
        "ts": now.isoformat(),
        "currency": currency,
        "spot": m["spot"],
        "pcr": m["pcr"],
        "total_call": m["total_call"],
        "total_put": m["total_put"],
        "net_gex": g["net"] if g else None,
        "abs_gex": g["abs"] if g else None,
        "flip": g["flip"] if g else None,
        "funding": funding,
        "dvol": dvol,
        "hl_funding_8h": (hl or {}).get("funding_8h"),
        "hl_oi": (hl or {}).get("oi"),
        "hl_premium": (hl or {}).get("premium"),
        "hl_depth": depth,
        "skew_25d": (vs or {}).get("skew_25d"),
        "term_slope": (vs or {}).get("term_slope"),
        "etf": etf,
        # 米国需要・ベーシス (発火判定と前回比に使う分だけ)
        "cb_premium": (cb or {}).get("premium"),
        "basis_name": front.get("name"),
        "basis_ann": front.get("ann"),
        "basis_next_ann": nxt.get("ann"),
        "fut_oi_usd": (basis or {}).get("total_oi"),
        "etf_fund": ({"symbol": fund["symbol"], "asof": fund["asof"],
                      "shares": fund["shares"], "nav": fund["nav"],
                      "flow_usd": fund["flow_usd"]} if fund else None),
        "strikes": {str(int(k)): {"call": v["call"], "put": v["put"]}
                    for k, v in book.items()},
    }


def save_snapshot(d, currency, m, g, funding, dvol, book, now, snap=None, hl=None):
    snap = snap or build_snapshot(currency, m, g, funding, dvol, book, now, hl)
    path = os.path.join(d, f"{currency}_{now:%Y%m%d_%H%M%S}.json")
    with open(path, "w") as f:
        json.dump(snap, f)
    return path


def is_snapshot_file(name, currency):
    """'BTC_20260904_080000.json' のような時刻付きスナップショットか"""
    return re.fullmatch(rf"{re.escape(currency)}_\d{{8}}_\d{{6}}\.json", name) is not None


def prune_snapshots(d, currency, keep_days):
    """古いスナップショットを削除 (keep_days<=0 なら何もしない)"""
    if not keep_days or keep_days <= 0:
        return 0
    cutoff = time.time() - keep_days * 86400
    n = 0
    for f in os.listdir(d):
        # 時刻付きスナップショットだけ。baseline や各種キャッシュ (_etf.json 等) は触らない
        if not is_snapshot_file(f, currency):
            continue
        path = os.path.join(d, f)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                n += 1
        except OSError:
            pass
    return n


def load_prev_snapshot(d, currency):
    """最新のスナップショットを返す(なければNone)"""
    files = sorted(f for f in os.listdir(d) if is_snapshot_file(f, currency))
    if not files:
        return None
    try:
        with open(os.path.join(d, files[-1])) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------- triggers

def baseline_path(d, currency):
    return os.path.join(d, f"{currency}_baseline.json")


def load_baseline(d, currency):
    """前回 claude 考察を発火させた時点のスナップショット"""
    try:
        with open(baseline_path(d, currency)) as f:
            return json.load(f)
    except Exception:
        return None


def pending_path(d, currency):
    return os.path.join(d, f"{currency}_baseline.pending.json")


def promote_baseline(d, currency):
    """保留中のスナップショットを基準に昇格 (考察が実際に走った後に呼ぶ)"""
    src = pending_path(d, currency)
    if not os.path.exists(src):
        return False
    os.replace(src, baseline_path(d, currency))
    return True


def save_baseline(d, currency, snap):
    tmp = baseline_path(d, currency) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f)
    os.replace(tmp, baseline_path(d, currency))


def evaluate_triggers(base, cur, book, now, th_spot, th_gex, th_oi, th_hl_oi,
                      th_skew, th_etf_oi, th_basis=3.0, th_cb_prem=0.15,
                      th_etf_flow=400.0):
    """前回発火時点(base)と今(cur)を比べて、発火理由のリストを返す"""
    reasons = []

    # 米国ETFの資金流出入 (日次。iShares の口数が更新された日だけ比較する)
    bfund, cfund = base.get("etf_fund") or {}, cur.get("etf_fund") or {}
    if cfund.get("flow_usd") is not None and cfund.get("asof") != bfund.get("asof"):
        flow = cfund["flow_usd"]
        if abs(flow) >= th_etf_flow * 1e6:
            side = "流入" if flow > 0 else "流出"
            reasons.append(f"{cfund['symbol']} {side} {fmt_usd(flow)} "
                           f"({cfund['asof']} 口数ベース)")

    # Coinbase プレミアムの急変 = 米国の現物需要の向きが変わった
    bcb, ccb = base.get("cb_premium"), cur.get("cb_premium")
    if bcb is not None and ccb is not None and abs(ccb - bcb) >= th_cb_prem:
        reasons.append(f"Coinbaseプレミアム {ccb - bcb:+.2f}pt "
                       f"({bcb:+.2f}% → {ccb:+.2f}%)")

    # 先物ベーシス (年率) の急変。主力限月が変わった (ロール) 直後は比べない
    bb, cbs = base.get("basis_ann"), cur.get("basis_ann")
    if bb is not None and cbs is not None \
            and base.get("basis_name") == cur.get("basis_name"):
        if abs(cbs - bb) >= th_basis:
            reasons.append(f"先物ベーシス(年率) {cbs - bb:+.1f}pt "
                           f"({bb:+.1f}% → {cbs:+.1f}%, {cur['basis_name']})")
        elif bb * cbs < 0 and max(abs(bb), abs(cbs)) >= 1.0:
            side = "逆ザヤ化" if cbs < 0 else "順ザヤ回復"
            reasons.append(f"先物ベーシスの符号反転: {side} "
                           f"({bb:+.1f}% → {cbs:+.1f}%)")

    if base.get("spot") and cur.get("spot"):
        chg = (cur["spot"] - base["spot"]) / base["spot"] * 100
        if abs(chg) >= th_spot:
            reasons.append(f"スポット {chg:+.1f}% "
                           f"(${base['spot']:,.0f} → ${cur['spot']:,.0f})")

    if base.get("abs_gex") and cur.get("abs_gex"):
        chg = (cur["abs_gex"] - base["abs_gex"]) / base["abs_gex"] * 100
        if abs(chg) >= th_gex:
            reasons.append(f"ABS GEX {chg:+.1f}% "
                           f"({fmt_usd(base['abs_gex'])} → {fmt_usd(cur['abs_gex'])})")

    bf, cf = base.get("funding"), cur.get("funding")
    if bf is not None and cf is not None:
        # ノイズ除け: どちらかが十分な大きさを持つ符号反転だけ拾う
        if bf * cf < 0 and max(abs(bf), abs(cf)) >= 0.002:
            reasons.append(f"ファンディング符号反転 ({bf:+.4f}% → {cf:+.4f}%)")

    # HL無期限の建玉急変 (レバレッジ勢の参入/退出)
    if base.get("hl_oi") and cur.get("hl_oi"):
        chg = (cur["hl_oi"] - base["hl_oi"]) / base["hl_oi"] * 100
        if abs(chg) >= th_hl_oi:
            reasons.append(f"HL建玉 {chg:+.1f}% "
                           f"({base['hl_oi']:,.0f} → {cur['hl_oi']:,.0f}枚)")

    # HLプレミアム(mark-oracle乖離)の符号反転 = 足元の圧力の向きが変わった
    bp, cp_ = base.get("hl_premium"), cur.get("hl_premium")
    if bp is not None and cp_ is not None:
        # ノイズ除け: どちらかが十分な大きさを持つ反転だけ拾う
        if bp * cp_ < 0 and max(abs(bp), abs(cp_)) >= 0.03:
            reasons.append(f"HLプレミアム符号反転 ({bp:+.3f}% → {cp_:+.3f}%)")

    # 25Δスキューの急変 (下方ヘッジ需要の増減)
    bsk, csk = base.get("skew_25d"), cur.get("skew_25d")
    if bsk is not None and csk is not None and abs(csk - bsk) >= th_skew:
        reasons.append(f"25Δスキュー {csk - bsk:+.1f}pt "
                       f"({bsk:+.1f} → {csk:+.1f})")

    # IV期間構造の反転 (符号が変わる反転だけ。ノイズ除けに片側2pt以上)
    bts, cts = base.get("term_slope"), cur.get("term_slope")
    if bts is not None and cts is not None:
        if bts * cts < 0 and max(abs(bts), abs(cts)) >= 2.0:
            side = "逆ザヤ化(短期警戒)" if cts < 0 else "順ザヤ回復"
            reasons.append(f"IV期間構造の反転: {side} "
                           f"({bts:+.1f} → {cts:+.1f}pt)")

    # 米国ETFの単一ストライクOI急変 (日次。キャッシュが同一なら比較しない)
    be, ce = base.get("etf"), cur.get("etf")
    if be and ce and be.get("fetched") != ce.get("fetched"):
        movers = []
        keys = set(be.get("strikes", {})) | set(ce.get("strikes", {}))
        for ks in keys:
            o = be["strikes"].get(ks, {})
            c = ce["strikes"].get(ks, {})
            for side in ("call", "put"):
                dv = c.get(side, 0.0) - o.get(side, 0.0)
                if abs(dv) >= th_etf_oi:
                    movers.append((abs(dv), float(ks), side.capitalize(), dv))
        movers.sort(reverse=True)
        for _, k, side, dv in movers[:2]:
            reasons.append(f"{ce['symbol']} ${k:g} {side} OI {dv:+,.0f}枚")

    # ガンマフリップをスポットが跨いだ
    bflip, cflip = base.get("flip"), cur.get("flip")
    if bflip and cflip and base.get("spot") and cur.get("spot"):
        was_above = base["spot"] >= bflip
        now_above = cur["spot"] >= cflip
        if was_above != now_above:
            side = "上抜け(ガンマ+圏へ)" if now_above else "下抜け(ガンマ-圏へ)"
            reasons.append(f"ガンマフリップ{side} (${cflip:,.0f})")

    # ストライク別OIの急変
    bs = base.get("strikes", {})
    movers = []
    for ks in set(bs) | {str(int(k)) for k in book}:
        k = float(ks)
        c = book.get(k, {"call": 0.0, "put": 0.0})
        o = bs.get(ks, {"call": 0.0, "put": 0.0})
        for side in ("call", "put"):
            dv = c.get(side, 0.0) - o.get(side, 0.0)
            if abs(dv) >= th_oi:
                movers.append((abs(dv), k, side.capitalize(), dv))
    movers.sort(reverse=True)
    for _, k, side, dv in movers[:3]:
        reasons.append(f"${k:,.0f} {side} OI {dv:+,.0f}枚")

    return reasons


def verbalize_diff(prev, m, g, funding, dvol, book, now, hl=None, depth=None,
                   vs=None, etf=None, fund=None, cb=None, basis=None):
    out = []
    ts = datetime.fromisoformat(prev["ts"])
    hours = (now - ts).total_seconds() / 3600.0
    out.append(f"── 前回比 ({hours:.1f}時間前のスナップショットと比較) ──")

    if prev.get("spot"):
        out.append(f"・スポット: {m['spot'] - prev['spot']:+,.0f} "
                   f"(${prev['spot']:,.0f} → ${m['spot']:,.0f})")
    if g and prev.get("abs_gex"):
        chg = (g["abs"] - prev["abs_gex"]) / prev["abs_gex"] * 100
        out.append(f"・ABS GEX: {chg:+.1f}% "
                   f"({fmt_usd(prev['abs_gex'])} → {fmt_usd(g['abs'])})")
    if funding is not None and prev.get("funding") is not None:
        out.append(f"・ファンディング(8h): "
                   f"{prev['funding']:+.4f}% → {funding:+.4f}%")
    if dvol is not None and prev.get("dvol") is not None:
        out.append(f"・DVOL: {prev['dvol']:.1f} → {dvol:.1f} "
                   f"({dvol - prev['dvol']:+.1f})")
    if hl and prev.get("hl_oi"):
        chg = (hl["oi"] - prev["hl_oi"]) / prev["hl_oi"] * 100
        out.append(f"・HL建玉: {chg:+.1f}% "
                   f"({prev['hl_oi']:,.0f} → {hl['oi']:,.0f}枚)")
    if hl and prev.get("hl_funding_8h") is not None:
        out.append(f"・HLファンディング(8h換算): {prev['hl_funding_8h']:+.4f}% "
                   f"→ {hl['funding_8h']:+.4f}%")
    pd = prev.get("hl_depth")
    if depth and pd:
        out.append(f"・HL板(±2%): 買い {fmt_usd(pd['bid2'])} → {fmt_usd(depth['bid2'])}"
                   f" / 売り {fmt_usd(pd['ask2'])} → {fmt_usd(depth['ask2'])}")
    if vs and vs.get("skew_25d") is not None \
            and prev.get("skew_25d") is not None:
        out.append(f"・25Δスキュー: {prev['skew_25d']:+.1f} → "
                   f"{vs['skew_25d']:+.1f}pt")
    pe = prev.get("etf")
    if etf and pe and pe.get("fetched") != etf.get("fetched"):
        dtc = etf["total_call"] - pe.get("total_call", 0.0)
        dtp = etf["total_put"] - pe.get("total_put", 0.0)
        out.append(f"・{etf['symbol']} 建玉(日次): Call {dtc:+,.0f}枚 / "
                   f"Put {dtp:+,.0f}枚")
    pf = prev.get("etf_fund") or {}
    if fund and fund.get("flow_usd") is not None and pf.get("asof") != fund["asof"]:
        out.append(f"・{fund['symbol']} 資金流出入(日次更新あり): {fmt_usd(fund['flow_usd'])} "
                   f"({fund['asof']} 口数ベース)")
    if cb and prev.get("cb_premium") is not None:
        out.append(f"・Coinbaseプレミアム: {prev['cb_premium']:+.2f}% → "
                   f"{cb['premium']:+.2f}%")
    if basis and prev.get("basis_ann") is not None \
            and prev.get("basis_name") == basis["front"]["name"]:
        out.append(f"・先物ベーシス(年率, {basis['front']['name']}): "
                   f"{prev['basis_ann']:+.1f}% → {basis['front']['ann']:+.1f}%")

    # ストライク別OIの増減 上位
    movers = []
    prev_strikes = prev.get("strikes", {})
    keys = set(prev_strikes) | {str(int(k)) for k in book}
    for ks in keys:
        k = float(ks)
        cur = book.get(k, {"call": 0.0, "put": 0.0})
        old = prev_strikes.get(ks, {"call": 0.0, "put": 0.0})
        dc = cur["call"] - old.get("call", 0.0)
        dp = cur["put"] - old.get("put", 0.0)
        if abs(dc) > 1:
            movers.append((abs(dc), k, "Call", dc))
        if abs(dp) > 1:
            movers.append((abs(dp), k, "Put", dp))
    movers.sort(reverse=True)
    if movers:
        out.append("・OI増減 上位:")
        for _, k, cp, dv in movers[:4]:
            out.append(f"    ${k:,.0f} {cp} {dv:+,.0f}枚")

    # 差分からの示唆
    hints = []
    if movers:
        _, k, cp, dv = movers[0]
        if dv > 0 and cp == "Call" and k > m["spot"]:
            hints.append(f"${k:,.0f}のCall積み増し(壁強化 or 上値期待の買い)")
        elif dv > 0 and cp == "Put" and k < m["spot"]:
            hints.append(f"${k:,.0f}のPut積み増し(下値警戒 or 支持強化)")
        elif dv < 0:
            hints.append(f"${k:,.0f}の{cp}解消が進行(壁/支持の弱体化)")
    if funding is not None and prev.get("funding") is not None:
        df = funding - prev["funding"]
        if df > 0.005:
            hints.append("ファンディング上昇=ロング過熱方向")
        elif df < -0.005:
            hints.append("ファンディング低下=ロング解消/ショート優勢方向")
    if hints:
        out.append("【差分からの示唆】 " + "。".join(hints) + "。")
    return "\n".join(out)


def verbalize_vol(vs):
    """ボラ構造 (期間構造・スキュー) の文章化"""
    out = ["── ボラ構造 (Deribit mark IVからの自前計算) ──"]
    tenors = sorted(vs["tenors"].items(), key=lambda kv: kv[1]["days"])
    out.append("・ATM IV: " + " / ".join(
        f"{lbl} {v['atm']:.1f}%" for lbl, v in tenors))

    slope = vs.get("term_slope")
    if slope is not None:
        if slope >= 2:
            note = "順ザヤ(コンタンゴ)。平時の形"
        elif slope <= -2:
            note = "逆ザヤ(バックワーデーション)。短期のイベント警戒が強い"
        else:
            note = "ほぼフラット"
        out.append(f"・期間構造 (90d-7d): {slope:+.1f}pt — {note}")

    skew = vs.get("skew_25d")
    if skew is not None:
        if skew >= 8:
            note = "Putが大幅に割高。下方ヘッジ需要が強い"
        elif skew >= 3:
            note = "Put優位。下方警戒がやや強い"
        elif skew <= -3:
            note = "Call優位。上方向の投機・追いかけ買いが目立つ"
        else:
            note = "中立圏"
        out.append(f"・25Δスキュー (30d, Put-Call): {skew:+.1f}pt — {note}")
    return "\n".join(out)


def verbalize_etf(etf, spot):
    """米国現物ETF側の建玉地形。ストライクは原資産換算で表記する"""
    sym, px = etf["symbol"], etf["price"]
    ratio = spot / px if px else None
    out = [f"── {sym} (米国現物ETF) オプション地形 ──"]
    tc, tp = etf["total_call"], etf["total_put"]
    pcr = tp / tc if tc else float("nan")
    out.append(f"・建玉: Call {tc:,.0f}枚 / Put {tp:,.0f}枚 (PCR {pcr:.2f}) "
               f"※1枚=100株, {sym} ${px:,.2f}")

    if ratio:
        walls = sorted(((float(k), v["call_w"], v["call"])
                        for k, v in etf["strikes"].items()
                        if float(k) > px and v["call"] > 0),
                       key=lambda x: -x[1])[:2]
        sups = sorted(((float(k), v["put_w"], v["put"])
                       for k, v in etf["strikes"].items()
                       if float(k) < px and v["put"] > 0),
                      key=lambda x: -x[1])[:2]
        if walls:
            s = ", ".join(f"${k * ratio:,.0f} ({sym} ${k:g}, {o:,.0f}枚)"
                          for k, _, o in sorted(walls))
            out.append(f"・上の壁 (原資産換算): {s}")
        if sups:
            s = ", ".join(f"${k * ratio:,.0f} ({sym} ${k:g}, {o:,.0f}枚)"
                          for k, _, o in sorted(sups, reverse=True))
            out.append(f"・下の支持 (原資産換算): {s}")

    # 月次満期 (第3金曜)。ETFオプションはここにOIが集中しやすい
    exps = etf.get("expiries") or {}
    total = etf["total_call"] + etf["total_put"]
    if exps and total:
        now = datetime.now(timezone.utc)
        monthlies = []
        for ds, v in exps.items():
            try:
                dt = datetime.strptime(ds, "%Y-%m-%d").replace(
                    hour=21, tzinfo=timezone.utc)
            except ValueError:
                continue
            if dt.weekday() == 4 and 15 <= dt.day <= 21 and dt >= now:
                monthlies.append((dt, v))
        if monthlies:
            dt, v = min(monthlies)
            share = (v["call"] + v["put"]) / total * 100
            days = (dt - now).total_seconds() / 86400.0
            line = (f"・次の月次満期 (第3金曜): {dt:%Y-%m-%d} ({days:.0f}日後) — "
                    f"全OIの{share:.0f}%がここで満期")
            if days <= 7:
                line += ("。満期週につき集中ストライクへの吸着と"
                         "ロール(建て直し)で需給が動きやすい")
            out.append(line)

    ts = etf.get("fetched", "")[:16].replace("T", " ")
    out.append(f"・データ: CBOE 15分遅延 / OIは日次更新・週末は静止 "
               f"(取得 {ts} UTC)")
    out.append("【ETFからの示唆】 米国の機関・規制市場勢のポジション地図。"
               "Deribitの壁と換算水準が重なる価格帯は攻防が固くなりやすい。"
               "ETFは第3金曜の月次満期が主戦場で、満期通過でOIの景色が変わる点に注意。")
    return "\n".join(out)


def verbalize_flow(fund, cb, basis, currency):
    """米国の現物需要 (ETFフロー・Coinbaseプレミアム) と先物ベーシスの文章化。材料ゼロなら空文字"""
    if not (fund or cb or basis):
        return ""
    out = ["── 米国の現物需要と先物ベーシス ──"]
    hints = []

    # 米国現物ETF (IBIT/ETHA) の資金流出入: 口数の増減 × NAV
    if fund:
        sym = fund["symbol"]
        line = (f"・{sym} 発行済口数: {fund['shares']:,.0f}口 ({fund['asof']}) "
                f"/ NAV ${fund['nav']:,.2f}")
        if fund.get("nav_change") is not None and fund["nav"]:
            line += f" (前日比 {fund['nav_change'] / (fund['nav'] - fund['nav_change']) * 100:+.1f}%)"
        out.append(line)
        if fund.get("flow_usd") is None:
            out.append(f"・{sym} 資金流出入: 履歴を蓄積中 (初回)。口数が次に更新されてから出る")
        else:
            flow = fund["flow_usd"]
            side = "流入" if flow > 0 else ("流出" if flow < 0 else "変化なし")
            line = (f"・{sym} 資金流出入 ({fund['prev_asof']}→{fund['asof']}): "
                    f"{side} {fmt_usd(abs(flow))} ({fund['flow_shares']:+,.0f}口)")
            if fund.get("flow_z") is not None:
                line += f" — 直近30回の平均から {fund['flow_z']:+.1f}σ"
            out.append(line)
            if fund.get("flow5_usd") is not None and fund["n_hist"] >= 3:
                out.append(f"・{sym} 直近{min(5, fund['n_hist'] - 1)}回分の累計: "
                           f"{fmt_usd(fund['flow5_usd'])}")
            if fund.get("net_assets"):
                ratio = flow / fund["net_assets"] * 100
                if abs(ratio) >= 1.0:
                    hints.append(f"{sym}の1日の{side}が純資産の{abs(ratio):.1f}%と大きい")
        extra = []
        if fund.get("net_assets"):
            extra.append(f"純資産 {fmt_usd(fund['net_assets'])}")
        if fund.get("volume"):
            extra.append(f"前日出来高 {fund['volume'] / 1e6:,.1f}M株")
        if fund.get("premium") is not None:
            p = fund["premium"]
            extra.append(f"NAV乖離 {p:+.2f}%")
            if p >= 0.3:
                hints.append("ETFがNAVに対してプレミアム。創設(現物買い)が続きやすい")
            elif p <= -0.3:
                hints.append("ETFがNAVに対してディスカウント。償還(現物売り)が出やすい")
        if extra:
            out.append("・" + " / ".join(extra))
        out.append(f"・データ: iShares 公式の日次更新 (米国引け後。日本時間の朝に前日分が確定)。"
                   f"{sym} 単体の創設/償還で、全発行体の合計ではない")

    # Coinbase プレミアム: 米国の現物買いが乗ると当日中にプラスへ振れる
    if cb:
        p = cb["premium"]
        if p >= 0.10:
            note = "米国の現物買いが先行。ETF創設 (APの買い) の前兆になりやすい"
        elif p <= -0.10:
            note = "米国から現物売り。ETF償還や利確が出ている可能性"
        elif p >= 0.04:
            note = "やや米国買い優勢"
        elif p <= -0.04:
            note = "やや米国売り優勢"
        else:
            note = "中立圏"
        out.append(f"・Coinbaseプレミアム (vs {cb['ref_name']} USDT): {p:+.3f}% — {note} "
                   f"(Coinbase ${cb['coinbase']:,.0f} / {cb['ref_name']} ${cb['ref']:,.0f})")

    # Deribit 期日先物ベーシス: キャリー取引の妙味
    if basis:
        f, n = basis["front"], basis.get("next")
        line = f"・先物ベーシス (対現物, 年率): {f['name']} ({f['days']:.0f}日) {f['ann']:+.1f}%"
        if n:
            line += f" / {n['name']} ({n['days']:.0f}日) {n['ann']:+.1f}%"
        out.append(line)
        if basis["total_oi"]:
            share = basis["perp_oi"] / basis["total_oi"] * 100
            out.append(f"・Deribit先物建玉: {fmt_usd(basis['total_oi'])} "
                       f"(うち無期限 {share:.0f}%)")
        ann = f["ann"]
        if ann >= 10:
            note = ("キャリー(ETFロング/先物ショート)の妙味が大きい水準。"
                    "ETF流入に方向性の薄いアービ資金が混じりやすく、"
                    "ベーシスが潰れると一斉に巻き戻る")
        elif ann >= 5:
            note = "平時の水準。アービと実需が混在"
        elif ann >= 0:
            note = "キャリー妙味が薄い。この状態でのETF流入は実需寄りと読める"
        else:
            note = "逆ザヤ。先物が現物を下回るストレス局面で、ロング解消かヘッジ売りが強い"
        out.append(f"・ベーシスの読み: {note}")
        if fund and fund.get("flow_usd") and fund["flow_usd"] > 0:
            if ann >= 10:
                hints.append("流入とベーシス高が同時 → 流入の一部はベーシス取引の可能性。"
                             "現物需要として割り引いて見る")
            elif ann < 5:
                hints.append("流入があるのにベーシスが低い → アービでは説明しにくく、"
                             "実需(方向性のあるロング)寄り")
        if cb and cb["premium"] >= 0.10 and ann < 5:
            hints.append("Coinbase買い先行 + ベーシス低位 → 米国の現物実需が主導")

    if hints:
        out.append("【米国需要からの示唆】 " + "。".join(hints) + "。")
    return "\n".join(out)


def verbalize_extras(funding, dvol, hl=None, depth=None):
    out = []
    if funding is not None:
        if funding > 0.03:
            note = "ロング側がかなり過密。逆行時の投げに注意"
        elif funding > 0.01:
            note = "ややロング優勢"
        elif funding < -0.01:
            note = "ショート優勢。踏み上げ燃料あり"
        else:
            note = "中立圏"
        out.append(f"・ファンディング(8h): {funding:+.4f}% — {note}")
    if dvol is not None:
        if dvol >= 70:
            note = "高ボラ環境。オプションは割高、値幅は大きめに"
        elif dvol >= 45:
            note = "中程度"
        else:
            note = "低ボラ環境。圧縮が続くと放れに警戒"
        out.append(f"・DVOL: {dvol:.1f} — {note}")

    # Hyperliquid (レバレッジ側の需給)
    if hl:
        out.append(f"・HL建玉: {hl['oi']:,.0f}枚 ({fmt_usd(hl['oi_usd'])}) / "
                   f"24h出来高 {fmt_usd(hl['day_volume'])}")
        out.append(f"・HLファンディング(8h換算): {hl['funding_8h']:+.4f}% "
                   f"(年率 {hl['funding_apr']:+.1f}%)")
        if funding is not None:
            div = hl["funding_8h"] - funding
            if abs(div) < 0.005:
                note = "取引所間の偏りなし"
            elif abs(div) < 0.02:
                side = "HL" if div > 0 else "Deribit"
                note = f"軽度の偏り({side}側のロングがコスト高)"
            else:
                side = "HL" if div > 0 else "Deribit"
                note = (f"明確な偏り。{side}に投機的なロングが寄っている。"
                        "巻き戻し時はこちら側から崩れやすい")
            out.append(f"・ファンディング乖離 (HL-Deribit): {div:+.4f}%/8h — {note}")
        # mark と oracle の乖離 = 今この瞬間の圧力
        if abs(hl["premium"]) >= 0.02:
            d = "買い" if hl["premium"] > 0 else "売り"
            out.append(f"・HLプレミアム: {hl['premium']:+.3f}% — "
                       f"現物指数から乖離、足元は{d}圧力")

    # HL板の厚み (瞬間値。HL単体の吸収力であって市場全体ではない)
    if depth:
        out.append(f"・HL板の厚み: ±2%以内 買い {fmt_usd(depth['bid2'])} / "
                   f"売り {fmt_usd(depth['ask2'])} ・ "
                   f"±5%以内 買い {fmt_usd(depth['bid5'])} / "
                   f"売り {fmt_usd(depth['ask5'])}")
        b, a = depth["bid5"], depth["ask5"]
        if b > 0 and a > 0:
            r = b / a
            if r >= 1.6:
                note = "買い板が厚い。下は受け止められやすく、上に動くほうが軽い"
            elif r <= 1 / 1.6:
                note = "売り板が厚い。上値は重く、下の受けが薄い"
            else:
                note = "上下の板は概ね均衡"
            out.append(f"・HL板バランス(±5%): 買い/売り = {r:.2f} — {note}"
                       "(瞬間値のため見せ板を含みうる)")
    return "\n".join(out)


# ---------------------------------------------------------------- verbalize

def pct(a, b):
    return (a - b) / b * 100


def fmt_usd(x):
    a = abs(x)
    if a >= 1e9:
        return f"${x/1e9:,.2f}B"
    if a >= 1e6:
        return f"${x/1e6:,.1f}M"
    return f"${x:,.0f}"


def verbalize_gex(g, spot):
    out = []
    out.append("── GEX (自前計算: Deribit mark IVベース) ──")
    out.append(f"・ネットGEX: {fmt_usd(g['net'])} / 1%変動  "
               f"(ABS GEX: {fmt_usd(g['abs'])})")
    if g["flip"]:
        d = pct(g["flip"], spot)
        out.append(f"・ガンマフリップ推定: ${g['flip']:,.0f} ({d:+.1f}%)")
    if g["top_strikes"]:
        tops = ", ".join(f"${k:,.0f}({fmt_usd(v)})" for k, v in g["top_strikes"])
        out.append(f"・ガンマ集中ストライク: {tops}")

    if g["net"] > 0:
        msg = ("ネットガンマはプラス圏。ディーラーヘッジは逆張り方向"
               "(上がれば売り・下がれば買い)に働き、値動きは抑制されやすい。"
               "レンジ・平均回帰想定が優勢。")
        if g["flip"] and g["flip"] < spot:
            msg += (f" ただし${g['flip']:,.0f}を割るとマイナス圏に転落し、"
                    "下落が加速しやすくなる点に注意。")
    else:
        msg = ("ネットガンマはマイナス圏。ディーラーヘッジは順張り方向"
               "(上がれば買い・下がれば売り)に働き、値動きが増幅されやすい。"
               "トレンドが出たら伸びやすく、SLは広めに取るか触らないのが無難。")
        if g["flip"] and g["flip"] > spot:
            msg += (f" ${g['flip']:,.0f}を回復すればプラス圏に戻り、"
                    "値動きは落ち着きやすい。")
    out.append(f"【ガンマからの示唆】 {msg}")
    return "\n".join(out)


def verbalize(m):
    spot = m["spot"]
    out = []
    out.append(f"現在値: ${spot:,.0f}")
    out.append("")

    # 壁
    if m["call_walls"]:
        w = m["call_walls"][0]
        out.append(
            f"▲ 上値: 最初の厚いCall壁は ${w[0]:,.0f} "
            f"(OI {w[2]:,.0f}枚, 現値から{pct(w[0], spot):+.1f}%)。")
        if len(m["call_walls"]) > 1:
            rest = ", ".join(f"${x[0]:,.0f}" for x in m["call_walls"][1:])
            out.append(f"   その先の壁: {rest}")
    if m["put_supports"]:
        s = m["put_supports"][0]
        out.append(
            f"▼ 下値: 最初の厚いPut支持は ${s[0]:,.0f} "
            f"(OI {s[2]:,.0f}枚, 現値から{pct(s[0], spot):+.1f}%)。")
        if len(m["put_supports"]) > 1:
            rest = ", ".join(f"${x[0]:,.0f}" for x in m["put_supports"][1:])
            out.append(f"   その下の支持: {rest}")
    out.append("")

    # 空白地帯の警告
    if m["call_walls"] and m["put_supports"]:
        up_d = pct(m["call_walls"][0][0], spot)
        dn_d = -pct(m["put_supports"][0][0], spot)
        if dn_d > up_d * 2:
            out.append(f"⚠ 下方向はPutの積みが薄く、支持まで{dn_d:.1f}%の空白。"
                       "下に走り出すと止まりにくい地形。")
        elif up_d > dn_d * 2:
            out.append(f"⚠ 上方向はCallの積みが薄く、壁まで{up_d:.1f}%の空白。"
                       "上に走り出すと軽い地形。")

    # 磁石
    if m["magnets"]:
        mg = m["magnets"][0][0]
        d = pct(mg, spot)
        out.append(f"◎ 最も磁力の強いストライク: ${mg:,.0f} ({d:+.1f}%)。"
                   "満期接近時はここに吸着しやすい。")

    # Max pain
    if m["max_pain"]:
        out.append(f"・Max Pain(全満期近似): ${m['max_pain']:,.0f}")

    # PCR
    out.append(f"・Put/Call Ratio: {m['pcr']:.2f} "
               f"(Call {m['total_call']:,.0f} / Put {m['total_put']:,.0f})")
    out.append("")

    # 総合バイアス
    t = m["tilt"]
    if t > 0.15:
        bias = ("OIの重心は現値より上にあり、磁石は上方向。"
                "レンジ上限方向へじり上がりやすい地形。ただし壁到達後は減速しやすい。")
    elif t < -0.15:
        bias = ("OIの重心は現値より下にあり、磁石は下方向。"
                "戻りは重く、下のOI集中帯へ引かれやすい地形。")
    else:
        bias = ("上下の引力が拮抗。OIからは方向感なし。"
                "壁と支持のレンジ内での往復を想定するのが無難。")
    out.append(f"【地形からの示唆】 {bias}")
    out.append("")
    out.append("※OIの偏りは「壁・磁石」の地図であって方向予測ではない。"
               "ガンマ総量が少ない時期(オプションカット直後など)は壁自体が柔らかい。"
               "イベント(FOMC・雇用統計等)はこのツールの外側。最終判断は自分で。")
    return "\n".join(out)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--currency", default="BTC")
    ap.add_argument("--max-days", type=float, default=None,
                    help="この日数以内の満期だけで評価 (例: 45)")
    ap.add_argument("--json", action="store_true", help="計算結果をJSONでも出力")
    ap.add_argument("--snap-dir", default=None,
                    help="スナップショット保存先 (既定: ~/.btc_oi_advisor)")
    ap.add_argument("--no-save", action="store_true",
                    help="スナップショットを保存しない (読み比較はする)")
    ap.add_argument("--check", action="store_true",
                    help="観測のみ。前回発火時点から有意な変化があれば exit 0、"
                         "なければ exit 1 (レポートは stdout に出さない)")
    ap.add_argument("--out", default=None,
                    help="レポート本文をこのファイルに書く (--check と併用)")
    ap.add_argument("--force", action="store_true",
                    help="--check でも必ず発火扱いにする (定時実行用)")
    ap.add_argument("--th-spot", type=float, default=2.0,
                    help="発火閾値: スポット変化率%% (既定 2.0)")
    ap.add_argument("--th-gex", type=float, default=15.0,
                    help="発火閾値: ABS GEX 変化率%% (既定 15.0)")
    ap.add_argument("--th-oi", type=float, default=3000.0,
                    help="発火閾値: 単一ストライクのOI変化(枚) (既定 3000)")
    ap.add_argument("--th-hl-oi", type=float, default=10.0,
                    help="発火閾値: HL無期限の建玉変化率%% (既定 10.0)")
    ap.add_argument("--th-skew", type=float, default=5.0,
                    help="発火閾値: 25Δスキュー変化(volポイント) (既定 5.0)")
    ap.add_argument("--th-etf-oi", type=float, default=50000.0,
                    help="発火閾値: 米国ETFの単一ストライクOI変化(枚) (既定 50000)")
    ap.add_argument("--th-etf-flow", type=float, default=400.0,
                    help="発火閾値: 米国ETF(IBIT/ETHA)の1日の資金流出入(百万USD) (既定 400)")
    ap.add_argument("--th-cb-prem", type=float, default=0.15,
                    help="発火閾値: Coinbaseプレミアム変化(pt) (既定 0.15)")
    ap.add_argument("--th-basis", type=float, default=3.0,
                    help="発火閾値: 先物ベーシス(年率)変化(pt) (既定 3.0)")
    ap.add_argument("--plot", default=None, metavar="PNG",
                    help="地形図をPNGに描く (matplotlib が必要)")
    ap.add_argument("--defer-baseline", action="store_true",
                    help="--check の発火時、基準を即更新せず保留にする。"
                         "考察が実際に走った後で --promote-baseline を呼ぶこと")
    ap.add_argument("--promote-baseline", action="store_true",
                    help="保留中の基準を確定させて終了 (通信しない)")
    ap.add_argument("--prune-days", type=float, default=60.0,
                    help="この日数より古いスナップショットを削除 (0で無効)")
    args = ap.parse_args()

    if args.promote_baseline:
        d = snap_dir(args.snap_dir)
        ok = promote_baseline(d, args.currency)
        print("baseline 更新" if ok else "保留中の baseline なし", file=sys.stderr)
        sys.exit(0 if ok else 1)

    now = datetime.now(timezone.utc)
    print(f"[{now:%Y-%m-%d %H:%M UTC}] Deribitから取得中...", file=sys.stderr)
    spot = fetch_spot(args.currency)
    raw = fetch_options(args.currency)
    book, expiries, instruments = build_book(raw, now, args.max_days)
    if not book:
        print("OIデータが取れませんでした", file=sys.stderr)
        sys.exit(1)

    funding = fetch_funding(args.currency)
    dvol = fetch_dvol(args.currency)
    hl = fetch_hyperliquid(args.currency)
    if hl is None:
        print("Hyperliquid の取得に失敗 (その分は省略)", file=sys.stderr)
    depth = fetch_hl_depth(args.currency)
    if depth is None:
        print("Hyperliquid 板の取得に失敗 (その分は省略)", file=sys.stderr)

    m = analyze(book, spot)
    g = compute_gex(instruments, spot) if instruments else None
    vs = compute_vol_structure(instruments, spot) if instruments else None

    d = snap_dir(args.snap_dir)
    etf = fetch_etf_terrain(args.currency, d)
    if etf is None:
        print("米国ETFオプションの取得に失敗 (その分は省略)", file=sys.stderr)

    # 米国の現物需要 (ETFフロー・Coinbaseプレミアム) と先物ベーシス
    fund = fetch_etf_fund(args.currency, d)
    if fund is None:
        print("米国ETF(iShares)の口数/NAVの取得に失敗 (その分は省略)", file=sys.stderr)
    cb = fetch_coinbase_premium(args.currency)
    if cb is None:
        print("Coinbaseプレミアムの取得に失敗 (その分は省略)", file=sys.stderr)
    basis = fetch_futures_basis(args.currency, spot, now)
    if basis is None:
        print("Deribit先物ベーシスの取得に失敗 (その分は省略)", file=sys.stderr)

    # ---- レポート本文の組み立て ----
    parts = [verbalize(m)]
    if g:
        parts.append(verbalize_gex(g, spot))
    if vs:
        parts.append(verbalize_vol(vs))
    extras = verbalize_extras(funding, dvol, hl, depth)
    if extras:
        parts.append("── 需給・ボラ補助指標 ──\n" + extras)
    if etf:
        parts.append(verbalize_etf(etf, spot))
    flow = verbalize_flow(fund, cb, basis, args.currency)
    if flow:
        parts.append(flow)

    prev = load_prev_snapshot(d, args.currency)
    if prev:
        parts.append(verbalize_diff(prev, m, g, funding, dvol, book, now, hl,
                                    depth, vs, etf, fund, cb, basis))
    else:
        print("(初回実行: 次回から前回比が出ます)", file=sys.stderr)

    snap = build_snapshot(args.currency, m, g, funding, dvol, book, now, hl,
                          depth, vs, etf, fund, cb, basis)

    # ---- トリガー判定 ----
    fired, reasons = True, []
    if args.check:
        base = load_baseline(d, args.currency)
        if base is None:
            reasons = ["初回実行(基準スナップショットなし)"]
        else:
            reasons = evaluate_triggers(base, snap, book, now,
                                        args.th_spot, args.th_gex, args.th_oi,
                                        args.th_hl_oi, args.th_skew,
                                        args.th_etf_oi, args.th_basis,
                                        args.th_cb_prem, args.th_etf_flow)
            fired = bool(reasons)
        if args.force and not fired:
            fired, reasons = True, ["定時実行(--force)"]
        elif args.force:
            reasons.append("定時実行(--force)")
        if fired:
            hours = None
            if base and base.get("ts"):
                hours = (now - datetime.fromisoformat(base["ts"])).total_seconds() / 3600
            head = "【発火】" + (f" 前回考察から{hours:.1f}時間" if hours else "")
            parts.append("── 今回の発火理由 ──\n" + head + "\n"
                         + "\n".join(f"・{r}" for r in reasons))

    report = "\n\n".join(parts)

    # ---- 作図 ----
    if args.plot:
        try:
            import plot_terrain
            hist = fetch_price_history(args.currency)
            if hist is None:
                print("価格履歴の取得に失敗 (値動きパネルは省略)", file=sys.stderr)
            plot_terrain.render(args.plot, spot, book, m, g, instruments, now,
                                net_gex_at=net_gex_at, hl_depth=depth,
                                price_hist=hist, currency=args.currency)
            cap = plot_terrain.caption(spot, book, m, g)
            cap_path = os.path.splitext(args.plot)[0] + ".caption.txt"
            with open(cap_path, "w", encoding="utf-8") as f:
                f.write(cap + "\n")
            print(f"plot saved: {args.plot} (+caption)", file=sys.stderr)
        except ImportError as e:
            print(f"作図をスキップ (matplotlib 未導入: {e})", file=sys.stderr)
        except Exception:
            import traceback
            print("作図に失敗:", file=sys.stderr)
            traceback.print_exc()

    # ---- 出力 ----
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    if not args.check:
        print(report)

    # ---- 保存 ----
    if not args.no_save:
        path = save_snapshot(d, args.currency, m, g, funding, dvol, book, now,
                             snap=snap)
        print(f"snapshot saved: {path}", file=sys.stderr)
        n = prune_snapshots(d, args.currency, args.prune_days)
        if n:
            print(f"pruned {n} old snapshots", file=sys.stderr)
    if args.check and fired and not args.no_save:
        if args.defer_baseline:
            with open(pending_path(d, args.currency), "w") as f:
                json.dump(snap, f)
        else:
            save_baseline(d, args.currency, snap)

    print("\n--- 満期別OI ---", file=sys.stderr)
    for e, v in sorted(expiries.items(),
                       key=lambda kv: datetime.strptime(kv[0], "%d%b%y")):
        print(f"  {e}: Call {v['call']:>10,.0f} / Put {v['put']:>10,.0f}",
              file=sys.stderr)

    if args.json:
        out = dict(m)
        out["gex"] = g
        out["funding"] = funding
        out["dvol"] = dvol
        if args.check:
            out["fired"] = fired
            out["reasons"] = reasons
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))

    if args.check:
        if fired:
            print("TRIGGER: " + " / ".join(reasons), file=sys.stderr)
        else:
            print("no trigger (静穏)", file=sys.stderr)
        sys.exit(0 if fired else 1)


if __name__ == "__main__":
    main()
