#!/usr/bin/env python3
"""
publish_pages.py — 考察と観測データを GitHub Pages (gh-pages ブランチ) へ積む

使い方:
    publish_pages.py add --currency BTC \
        --report logs/report-BTC-20260904-085703.txt \
        --analysis logs/analysis-BTC-20260904-085703.md \
        [--png logs/terrain-BTC-....png] [--caption logs/terrain-BTC-....caption.txt] \
        [--kind sched|trigger|manual] [--no-push]
        → 公開URL (…/?c=BTC&r=<stamp>) を stdout に出す
    publish_pages.py rebuild [--no-push]
        静的ファイル (pages/) を差し替えて再公開。蓄積したデータはそのまま

設定 (.env か環境変数):
    PAGES_URL        公開URL (例 https://takmolts.github.io/crypto_watch/)。
                     未設定なら exit 2 で何もしない (呼び出し側は従来の全文通知に戻す)
    PAGES_DIR        サイトの作業コピー (既定 $STATE_DIR/pages)
    PAGES_BRANCH     公開ブランチ (既定 gh-pages)
    PAGES_REMOTE     push 先 (既定: このリポジトリの origin)
    PAGES_KEEP_DAYS  何日分の考察と推移を残すか (既定 7)

履歴は持たない: 毎回 orphan コミット1つを force-push する (データを積んでも
リポジトリが肥大しない)。データは PAGES_DIR に蓄積し、無ければ gh-pages から取り直す。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from notify_discord import load_env

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "pages")

KIND_LABEL = {"sched": "定時", "trigger": "発火", "manual": "手動"}

# スナップショットから公開する数値 (strikes 等の大きい項目は載せない)
SUMMARY_KEYS = ("ts", "spot", "pcr", "total_call", "total_put", "net_gex", "abs_gex",
                "flip", "funding", "dvol", "hl_funding_8h", "hl_oi", "hl_premium",
                "skew_25d", "term_slope", "cb_premium", "basis_name", "basis_ann",
                "basis_next_ann", "fut_oi_usd", "etf_fund")
SERIES_KEYS = ("spot", "dvol", "funding", "hl_funding_8h", "abs_gex", "net_gex",
               "skew_25d", "cb_premium", "basis_ann", "hl_oi")

SNAP_RE = re.compile(r"^([A-Z]+)_(\d{8})_(\d{6})\.json$")
SEC_RE = re.compile(r"^──\s*(.+?)\s*──$")
HEAD_RE = re.compile(r"^(#{1,2})\s+(.+?)\s*$")
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")


def log(msg):
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------- settings

def origin_url():
    try:
        r = subprocess.run(["git", "-C", BASE, "remote", "get-url", "origin"],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip() or None
    except Exception:
        return None


def settings():
    env = load_env(os.path.join(BASE, ".env"))

    def get(k, default=None):
        return os.environ.get(k) or env.get(k) or default

    state_dir = get("STATE_DIR", os.path.expanduser("~/.btc_oi_advisor"))
    url = get("PAGES_URL")
    return {
        "url": (url.rstrip("/") + "/") if url else None,
        "dir": get("PAGES_DIR", os.path.join(state_dir, "pages")),
        "branch": get("PAGES_BRANCH", "gh-pages"),
        "remote": get("PAGES_REMOTE") or origin_url(),
        "keep_days": float(get("PAGES_KEEP_DAYS", "7")),
        "state_dir": state_dir,
    }


# ---------------------------------------------------------------- git

def git(args, cwd, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r


def ensure_site(cfg):
    """作業コピーを用意する。無ければ init し、公開済みの gh-pages があればそれを取り直す"""
    d = cfg["dir"]
    os.makedirs(d, exist_ok=True)
    if os.path.isdir(os.path.join(d, ".git")):
        if cfg["remote"]:
            git(["remote", "set-url", "origin", cfg["remote"]], d, check=False)
        return
    git(["init", "-q"], d)
    if not cfg["remote"]:
        log("push 先が不明 (PAGES_REMOTE も origin も無い)。ローカルにだけ積みます")
        return
    git(["remote", "add", "origin", cfg["remote"]], d)
    r = git(["fetch", "-q", "--depth", "1", "origin", cfg["branch"]], d, check=False)
    if r.returncode == 0:
        git(["checkout", "-q", "-B", cfg["branch"], "FETCH_HEAD"], d)
        log(f"公開済みの {cfg['branch']} を取り直しました: {d}")


def commit_and_push(cfg, msg, push=True):
    """orphan コミット1つに作り直して force-push (履歴を持たない)"""
    d = cfg["dir"]
    git(["branch", "-D", "_publish"], d, check=False)
    git(["checkout", "-q", "--orphan", "_publish"], d)
    git(["add", "-A"], d)
    git(["-c", "user.name=crypto_watch", "-c", "user.email=crypto_watch@localhost",
         "commit", "-q", "-m", msg], d)
    git(["branch", "-M", cfg["branch"]], d)
    if push and cfg["remote"]:
        git(["push", "-q", "-f", "origin", cfg["branch"]], d)
        log(f"push: origin/{cfg['branch']}")
    elif push:
        log("push 先が無いので push はしていません")


# ---------------------------------------------------------------- parse

def parse_report(text):
    """advisor.py の生レポートを '── 見出し ──' 単位に分け、発火理由も抜く"""
    sections, title, buf = [], "地形 (壁・支持・磁石)", []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            sections.append({"title": title, "text": body})

    for line in text.splitlines():
        m = SEC_RE.match(line.strip())
        if m:
            flush()
            title, buf = m.group(1), []
        else:
            buf.append(line.rstrip())
    flush()

    reasons = []
    for s in sections:
        if s["title"].startswith("今回の発火理由"):
            reasons = [ln.strip()[1:].strip() for ln in s["text"].splitlines()
                       if ln.strip().startswith("・")]
    return sections, reasons


def parse_analysis(mdtext):
    """claude の考察を '## 章' 単位に分け、概要 (1章) と出典URLを抜く"""
    chapters, title, buf = [], "", []

    def flush():
        body = "\n".join(buf).strip()
        if body or title:
            chapters.append({"title": title, "md": body})

    for line in mdtext.splitlines():
        m = HEAD_RE.match(line)
        if m:
            flush()
            title, buf = m.group(2).strip(), []
        else:
            buf.append(line.rstrip())
    flush()

    overview = ""
    for c in chapters:
        t = c["title"]
        if re.match(r"^\s*1[\.．)]", t) or "サマリ" in t:
            overview = c["md"]
            break
    if not overview and chapters:
        overview = chapters[0]["md"]

    seen, sources = set(), []
    for u in URL_RE.findall(mdtext):
        u = u.rstrip(".,。、)")
        if u not in seen:
            seen.add(u)
            sources.append(u)
    return chapters, overview, sources


# ---------------------------------------------------------------- snapshots

def snapshot_files(state_dir, currency):
    """(datetime, path) を古い順に"""
    out = []
    try:
        names = os.listdir(state_dir)
    except OSError:
        return out
    for n in names:
        m = SNAP_RE.match(n)
        if not m or m.group(1) != currency:
            continue
        try:
            ts = datetime.strptime(m.group(2) + m.group(3), "%Y%m%d%H%M%S") \
                .replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append((ts, os.path.join(state_dir, n)))
    out.sort()
    return out


def latest_summary(state_dir, currency):
    files = snapshot_files(state_dir, currency)
    if not files:
        return {}
    try:
        with open(files[-1][1]) as f:
            snap = json.load(f)
    except Exception:
        return {}
    out = {k: snap.get(k) for k in SUMMARY_KEYS}
    etf = snap.get("etf") or {}
    if etf:
        out["etf_symbol"] = etf.get("symbol")
        out["etf_total_call"] = etf.get("total_call")
        out["etf_total_put"] = etf.get("total_put")
    return out


def build_series(state_dir, currency, keep_days):
    """直近 keep_days 日の毎時スナップショットを推移データに畳む"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    series = {"ts": []}
    for k in SERIES_KEYS:
        series[k] = []
    for ts, path in snapshot_files(state_dir, currency):
        if ts < cutoff:
            continue
        try:
            with open(path) as f:
                snap = json.load(f)
        except Exception:
            continue
        series["ts"].append(ts.isoformat())
        for k in SERIES_KEYS:
            v = snap.get(k)
            series[k].append(v if isinstance(v, (int, float)) else None)

    flows = []
    try:
        with open(os.path.join(state_dir, f"{currency}_etf_fund.json")) as f:
            hist = json.load(f).get("history") or {}
        dates = sorted(hist)
        for a, b in zip(dates, dates[1:]):
            ds = hist[b]["shares"] - hist[a]["shares"]
            flows.append([b, ds * hist[b]["nav"]])
        flows = flows[-30:]
    except Exception:
        pass
    return series, flows


# ---------------------------------------------------------------- site

def stamp_from(path, currency):
    m = re.search(rf"({currency}-\d{{8}}-\d{{6}})", os.path.basename(path))
    return m.group(1) if m else None


def stamp_ts(stamp):
    try:
        return datetime.strptime(stamp.split("-", 1)[1], "%Y%m%d-%H%M%S") \
            .replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def copy_static(site):
    for n in os.listdir(STATIC_DIR):
        src = os.path.join(STATIC_DIR, n)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(site, n))
    open(os.path.join(site, ".nojekyll"), "w").close()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def write_site_json(cfg, site):
    data_dir = os.path.join(site, "data")
    curs = sorted(n for n in os.listdir(data_dir)
                  if os.path.isfile(os.path.join(data_dir, n, "index.json"))) \
        if os.path.isdir(data_dir) else []
    write_json(os.path.join(data_dir, "site.json"), {
        "currencies": curs,
        "updated": datetime.now(timezone.utc).isoformat(),
        "keep_days": cfg["keep_days"],
        "url": cfg["url"],
    })


def add_run(cfg, args):
    site = cfg["dir"]
    cur = args.currency.upper()
    stamp = args.stamp or stamp_from(args.report, cur) or stamp_from(args.analysis, cur) \
        or f"{cur}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    ts = stamp_ts(stamp)

    with open(args.report, encoding="utf-8") as f:
        report = f.read()
    with open(args.analysis, encoding="utf-8") as f:
        analysis = f.read()
    caption = ""
    if args.caption and os.path.exists(args.caption):
        with open(args.caption, encoding="utf-8") as f:
            caption = f.read().strip()

    sections, reasons = parse_report(report)
    chapters, overview, sources = parse_analysis(analysis)
    summary = latest_summary(cfg["state_dir"], cur)

    image = None
    if args.png and os.path.exists(args.png):
        img_dir = os.path.join(site, "img")
        os.makedirs(img_dir, exist_ok=True)
        image = f"img/terrain-{stamp}.png"
        shutil.copy2(args.png, os.path.join(site, image))

    run = {
        "currency": cur, "stamp": stamp, "ts": ts.isoformat(),
        "kind": args.kind, "kind_label": KIND_LABEL.get(args.kind, args.kind),
        "reasons": reasons, "summary": summary,
        "overview_md": overview, "chapters": chapters, "sources": sources,
        "report_sections": sections, "image": image, "caption": caption,
        "analysis_md": analysis, "report_txt": report,
    }
    cur_dir = os.path.join(site, "data", cur)
    write_json(os.path.join(cur_dir, "runs", f"{stamp}.json"), run)

    # 一覧: 古いものを落として更新
    idx_path = os.path.join(cur_dir, "index.json")
    try:
        with open(idx_path, encoding="utf-8") as f:
            runs = json.load(f).get("runs") or []
    except Exception:
        runs = []
    runs = [r for r in runs if r.get("stamp") != stamp]
    runs.append({"stamp": stamp, "ts": ts.isoformat(), "kind": args.kind,
                 "kind_label": KIND_LABEL.get(args.kind, args.kind),
                 "reasons": reasons, "spot": summary.get("spot")})
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["keep_days"])
    keep, dropped = [], []
    for r in runs:
        (keep if datetime.fromisoformat(r["ts"]) >= cutoff else dropped).append(r)
    for r in dropped:
        for p in (os.path.join(cur_dir, "runs", f"{r['stamp']}.json"),
                  os.path.join(site, "img", f"terrain-{r['stamp']}.png")):
            try:
                os.remove(p)
            except OSError:
                pass
    keep.sort(key=lambda r: r["ts"], reverse=True)

    series, flows = build_series(cfg["state_dir"], cur, cfg["keep_days"])
    write_json(idx_path, {"currency": cur,
                          "updated": datetime.now(timezone.utc).isoformat(),
                          "runs": keep, "series": series, "etf_flows": flows})
    log(f"追加: {cur} {stamp} ({len(keep)}件を保持, {len(dropped)}件を削除)")
    return stamp


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="考察を1件追加して公開")
    a.add_argument("--currency", default="BTC")
    a.add_argument("--report", required=True)
    a.add_argument("--analysis", required=True)
    a.add_argument("--png", default=None)
    a.add_argument("--caption", default=None)
    a.add_argument("--kind", choices=sorted(KIND_LABEL), default="manual")
    a.add_argument("--stamp", default=None, help="既定はファイル名から (BTC-YYYYMMDD-HHMMSS)")
    a.add_argument("--no-push", action="store_true")
    b = sub.add_parser("rebuild", help="静的ファイルだけ差し替えて公開")
    b.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    cfg = settings()
    if not cfg["url"]:
        log("PAGES_URL が未設定のため公開しません")
        sys.exit(2)

    ensure_site(cfg)
    site = cfg["dir"]
    stamp = None
    if args.cmd == "add":
        stamp = add_run(cfg, args)
    copy_static(site)
    write_site_json(cfg, site)
    msg = f"publish {stamp}" if stamp else "rebuild static"
    commit_and_push(cfg, msg, push=not args.no_push)
    if stamp:
        print(f"{cfg['url']}?c={args.currency.upper()}&r={stamp}")


if __name__ == "__main__":
    main()
