#!/usr/bin/env python3
"""
notify_discord.py — 標準入力のテキストをセンテンス単位で Discord embed に整形して送る

使い方:
    python3 advisor.py 2>/dev/null \
      | claude -p "..." --allowedTools "WebSearch" \
      | python3 notify_discord.py

    python3 notify_discord.py --mode section   # 見出し単位でまとめる(embed数を節約)
    python3 notify_discord.py --dry-run        # 送らずにpayloadを表示
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

# Discord の制限
MAX_EMBEDS_PER_MSG = 10
MAX_DESC = 4096
MAX_TITLE = 256
MAX_AUTHOR = 256
MAX_TOTAL_CHARS = 6000
# payload_json が 10KiB を超えると Discord は HTTP 500 を返す (実測: 10,240 bytes
# ちょうどが境界)。日本語は 1文字=3バイトなので 6000文字制限より先にここへ当たる。
MAX_PAYLOAD_BYTES = 9000

# 色 (キーワードでトーンを付ける)
C_BULL = 0x2ECC71   # 上/強気
C_BEAR = 0xE74C3C   # 下/弱気
C_WARN = 0xF1C40F   # 警戒
C_INFO = 0x3498DB   # 中立・数値
C_HEAD = 0x9B59B6   # 見出し

BULL_WORDS = ("上昇", "強気", "買い", "上抜け", "ブレイク", "上値追い", "ロング")
BEAR_WORDS = ("下落", "弱気", "売り", "割れ", "崩れ", "下抜け", "ショート", "急落")
WARN_WORDS = ("注意", "警戒", "リスク", "不透明", "急変", "ボラ", "イベント", "空白",
              "加速", "止まりにくい")

HEAD_RE = re.compile(r"^\s*(?:#{1,6}\s+|─+\s*|\*\*)|^\s*【.+】|^\s*[■◆▼▲]\s*\S+")
SENT_SPLIT_RE = re.compile(r"(?<=[。！？])\s*")
BULLET_RE = re.compile(r"^\s*(?:[-*・>]|\d+[.)])\s+")


# ---------------------------------------------------------------- env

def load_env(path):
    """.env を読む (python-dotenv 不要)"""
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            env[k.strip()] = v
    return env


# ---------------------------------------------------------------- parse

def is_heading(line):
    s = line.strip()
    if not s:
        return False
    if HEAD_RE.match(s):
        return True
    # "── GEX ──" のような囲み見出し
    if s.startswith("──") and s.endswith("──"):
        return True
    return False


def clean_heading(line):
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = s.strip("─- ").strip()
    s = s.replace("**", "").strip()
    return s[:MAX_TITLE] or "—"


def split_units(text):
    """テキストを (見出し, センテンス) のリストに分解する。

    - 見出し行はセクション名として保持
    - 箇条書き行は分割せず1センテンス扱い
    - それ以外は 。！？ で分割
    """
    units = []          # [(section, sentence)]
    section = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if is_heading(line):
            section = clean_heading(line)
            continue
        if BULLET_RE.match(line):
            units.append((section, line.strip()))
            continue
        for s in SENT_SPLIT_RE.split(line.strip()):
            s = s.strip()
            if s:
                units.append((section, s))
    return units


def pick_color(text):
    t = text
    if any(w in t for w in WARN_WORDS):
        return C_WARN
    bull = sum(w in t for w in BULL_WORDS)
    bear = sum(w in t for w in BEAR_WORDS)
    if bear > bull:
        return C_BEAR
    if bull > bear:
        return C_BULL
    return C_INFO


# ---------------------------------------------------------------- embeds

def chunk(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def build_embeds_sentence(units, ts):
    """1センテンス = 1 embed"""
    embeds = []
    for section, sent in units:
        for piece in chunk(sent, MAX_DESC):
            e = {"description": piece, "color": pick_color(piece)}
            if section:
                e["author"] = {"name": section[:MAX_AUTHOR]}
            embeds.append(e)
    if embeds:
        embeds[-1]["timestamp"] = ts
        embeds[-1]["footer"] = {"text": "advisor.py × Claude / OI地形 + イベントリスク"}
    return embeds


def build_embeds_section(units, ts):
    """1見出し = 1 embed、センテンスを行として並べる"""
    embeds = []
    cur_sec, lines = None, []

    def flush():
        if not lines:
            return
        body = "\n".join(f"▸ {ln}" if not BULLET_RE.match(ln) else ln
                         for ln in lines)
        for i, piece in enumerate(chunk(body, MAX_DESC)):
            e = {"description": piece, "color": pick_color(piece)}
            title = cur_sec or "考察"
            if i:
                title = f"{title} (続き)"
            e["title"] = title[:MAX_TITLE]
            embeds.append(e)

    for section, sent in units:
        if section != cur_sec:
            flush()
            cur_sec, lines = section, []
        lines.append(sent)
    flush()
    if embeds:
        embeds[-1]["timestamp"] = ts
        embeds[-1]["footer"] = {"text": "advisor.py × Claude / OI地形 + イベントリスク"}
    return embeds


REASON_SEC = "── 今回の発火理由 ──"


def extract_reasons(report_text):
    """advisor.py の生レポートから発火理由の箇条書きを抜く"""
    if not report_text or REASON_SEC not in report_text:
        return []
    body = report_text.split(REASON_SEC, 1)[1]
    out = []
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("──"):
            break
        if s.startswith("・"):
            out.append(s[1:].strip())
    return out


def extract_overview(text):
    """考察の1章 (## 1. 地形サマリ …) の本文。無ければ先頭の数行"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^\s*#{1,2}\s*1[\.．)]", ln):
            start = i + 1
            break
    if start is None:
        body = [ln for ln in lines if ln.strip() and not is_heading(ln)][:10]
        return "\n".join(body)
    body = []
    for ln in lines[start:]:
        if re.match(r"^\s*#{1,2}\s", ln):
            break
        if ln.strip():
            body.append(ln.rstrip())
    return "\n".join(body)


def build_embeds_summary(text, ts, link, reasons, cur, kind_label=None):
    """要約1つだけ: 発火理由 + 1章 + リンク (全文は GitHub Pages 側)"""
    overview = extract_overview(text)
    parts = []
    if reasons:
        parts.append("**発火理由**\n" + "\n".join(f"・{r}" for r in reasons))
    if overview:
        parts.append(overview)
    body = "\n\n".join(parts)
    tail = f"\n\n📄 [レポート全文・解析データ・推移を見る]({link})"
    body = body[:MAX_DESC - len(tail) - 1] + tail
    title = f"{cur} 考察" + (f" ({kind_label})" if kind_label else "")
    e = {"title": title[:MAX_TITLE], "url": link, "description": body,
         "color": pick_color(overview or body), "timestamp": ts,
         "footer": {"text": "advisor.py × Claude / 全文は GitHub Pages"}}
    return [e]


def paginate(embeds):
    """Discord の 10個/6000文字/payload 10KiB 制限でメッセージに分割"""
    msgs, cur, cur_len, cur_bytes = [], [], 0, 0
    for e in embeds:
        n = len(e.get("description", "")) + len(e.get("title", "")) \
            + len(e.get("author", {}).get("name", "")) \
            + len(e.get("footer", {}).get("text", ""))
        b = len(json.dumps(e, ensure_ascii=False).encode("utf-8"))
        if cur and (len(cur) >= MAX_EMBEDS_PER_MSG
                    or cur_len + n > MAX_TOTAL_CHARS
                    or cur_bytes + b > MAX_PAYLOAD_BYTES):
            msgs.append(cur)
            cur, cur_len, cur_bytes = [], 0, 0
        cur.append(e)
        cur_len += n
        cur_bytes += b
    if cur:
        msgs.append(cur)
    return msgs


# ---------------------------------------------------------------- post

MAX_UPLOAD = 8 * 1024 * 1024      # 無印サーバのアップロード上限 8MB


def build_multipart(payload, files):
    """payload_json + files[n] の multipart/form-data を組む"""
    boundary = "----btcwatch" + uuid.uuid4().hex
    b = bytearray()
    sep = f"--{boundary}\r\n".encode()

    b += sep
    b += b'Content-Disposition: form-data; name="payload_json"\r\n'
    b += b"Content-Type: application/json\r\n\r\n"
    b += json.dumps(payload, ensure_ascii=False).encode("utf-8")
    b += b"\r\n"

    for i, (name, content, ctype) in enumerate(files):
        b += sep
        b += (f'Content-Disposition: form-data; name="files[{i}]"; '
              f'filename="{name}"\r\n').encode()
        b += f"Content-Type: {ctype}\r\n\r\n".encode()
        b += content
        b += b"\r\n"

    b += f"--{boundary}--\r\n".encode()
    return bytes(b), f"multipart/form-data; boundary={boundary}"


MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".json": "application/json"}


def _mime(name):
    return MIME.get(os.path.splitext(name)[1].lower(), "text/plain; charset=utf-8")


def read_attachments(paths, source_text=None, source_name=None, image=None):
    """添付ファイルを (ファイル名, bytes, Content-Type) のリストで返す。

    image は embed に埋め込む画像。attachment:// で参照するため先頭に置く。
    """
    files = []
    for path in ([image] if image else []) + list(paths or []):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            print(f"添付を読めません: {path} ({e})", file=sys.stderr)
            continue
        name = os.path.basename(path)
        files.append((name, data, _mime(name)))
    if source_text is not None:
        name = source_name or "source.md"
        files.append((name, source_text.encode("utf-8"), _mime(name)))

    out, total = [], 0
    for name, data, ctype in files:
        if total + len(data) > MAX_UPLOAD:
            print(f"添付が上限(8MB)を超えるため除外: {name}", file=sys.stderr)
            continue
        total += len(data)
        out.append((name, data, ctype))
    return out


def post(webhook, payload, files=None, retries=3):
    if files:
        data, ctype = build_multipart(payload, files)
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ctype = "application/json"
    req = urllib.request.Request(
        webhook, data=data,
        headers={"Content-Type": ctype,
                 "User-Agent": "btcwatch-notifier/1.0"},
        method="POST")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            if e.code == 429:
                try:
                    wait = float(json.loads(body).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                print(f"rate limited: {wait}s 待機", file=sys.stderr)
                time.sleep(min(wait + 0.2, 30))
                continue
            if 500 <= e.code < 600 and attempt < retries - 1:
                wait = 2 * (attempt + 1)
                print(f"HTTP {e.code} — {wait}s 後に再試行 "
                      f"({attempt + 1}/{retries}): {body[:200]}", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from None
        except urllib.error.URLError as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("送信に失敗しました (retry上限)")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["sentence", "section", "summary"],
                    default="sentence",
                    help="sentence: 1文=1embed (既定) / section: 見出し単位でまとめる / "
                         "summary: 発火理由と1章だけの要約1つ + リンク (--link 必須)")
    ap.add_argument("--link", default=None, help="summary: 全文を置いた URL")
    ap.add_argument("--report", default=None, metavar="FILE",
                    help="summary: 発火理由を読む advisor.py の生レポート")
    ap.add_argument("--kind", default=None, help="summary: 定時/発火/手動 などの種別表記")
    ap.add_argument("--env", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".env"))
    ap.add_argument("--webhook", default=None, help="DISCORD_WEBHOOK を上書き")
    ap.add_argument("--content", default=None, help="embed の前に置く本文")
    ap.add_argument("--currency", default="BTC",
                    help="ヘッダ表記と DISCORD_WEBHOOK_<通貨> の選択に使う")
    ap.add_argument("--username", default=None,
                    help="既定: '<通貨> OI Advisor'")
    ap.add_argument("--attach", action="append", default=[], metavar="FILE",
                    help="テキストファイルを添付する (複数指定可)")
    ap.add_argument("--image", default=None, metavar="PNG",
                    help="画像を添付し、先頭の独立した embed に埋め込む")
    ap.add_argument("--image-caption", default=None, metavar="FILE",
                    help="図の読みどころを書いたテキストファイル")
    ap.add_argument("--attach-source", action="store_true",
                    help="embed化前の入力テキスト自体を添付する")
    ap.add_argument("--dry-run", action="store_true", help="送らずに payload を表示")
    ap.add_argument("--file", default=None, help="stdin の代わりに読むファイル")
    args = ap.parse_args()

    text = (open(args.file, encoding="utf-8").read() if args.file
            else sys.stdin.read())
    if not text.strip():
        print("入力が空です", file=sys.stderr)
        sys.exit(1)

    env = load_env(args.env)
    cur = args.currency.upper()
    # 通貨別チャンネル: DISCORD_WEBHOOK_<通貨> があれば優先、無ければ共通にフォールバック
    webhook = (args.webhook
               or os.environ.get(f"DISCORD_WEBHOOK_{cur}")
               or env.get(f"DISCORD_WEBHOOK_{cur}")
               or os.environ.get("DISCORD_WEBHOOK")
               or env.get("DISCORD_WEBHOOK"))
    if not webhook and not args.dry_run:
        print(f"DISCORD_WEBHOOK_{cur} / DISCORD_WEBHOOK が見つかりません "
              "(.env を確認)", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    ts = now.isoformat()
    if args.mode == "summary":
        if not args.link:
            print("--mode summary には --link が必要です", file=sys.stderr)
            sys.exit(1)
        report_text = ""
        if args.report:
            try:
                with open(args.report, encoding="utf-8") as f:
                    report_text = f.read()
            except OSError as e:
                print(f"レポートを読めません: {e}", file=sys.stderr)
        embeds = build_embeds_summary(text, ts, args.link, extract_reasons(report_text),
                                      cur, args.kind)
    else:
        units = split_units(text)
        build = build_embeds_sentence if args.mode == "sentence" else build_embeds_section
        embeds = build(units, ts)
    if not embeds:
        print("整形できるセンテンスがありませんでした", file=sys.stderr)
        sys.exit(1)

    header = args.content if args.content is not None else \
        f"**{cur} 地形 × イベントリスク考察**  `{now:%Y-%m-%d %H:%M UTC}`"
    if args.mode == "summary" and args.content is None:
        header += f"  <{args.link}>"
    username = args.username or f"{cur} OI Advisor"

    image = args.image if (args.image and os.path.exists(args.image)) else None
    if args.image and not image:
        print(f"画像が見つかりません: {args.image}", file=sys.stderr)

    # 図は考察の前に、独立した embed として置く。
    # 考察のどこか末尾にぶら下げると、その節と関係があるように見えてしまう
    if image and args.mode == "summary":
        embeds[0]["image"] = {"url": "attachment://" + os.path.basename(image)}
    elif image:
        cap = ""
        if args.image_caption:
            try:
                with open(args.image_caption, encoding="utf-8") as f:
                    cap = f.read().strip()
            except OSError as e:
                print(f"読みどころを読めません: {e}", file=sys.stderr)
        e = {"title": "地形図 — この図のどこを見るか",
             "color": C_HEAD,
             "image": {"url": "attachment://" + os.path.basename(image)}}
        if cap:
            e["description"] = cap[:MAX_DESC]
        embeds.insert(0, e)

    msgs = paginate(embeds)
    files = read_attachments(
        args.attach,
        source_text=text if args.attach_source else None,
        source_name=(os.path.basename(args.file) if args.file
                     else f"analysis-{now:%Y%m%d-%H%M%S}.md"),
        image=image)

    for i, group in enumerate(msgs):
        payload = {"username": username, "embeds": group}
        if i == 0 and header:
            payload["content"] = header[:2000]
        attach = files if i == 0 else None
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            if attach:
                for n, d, c in attach:
                    print(f"[attach] {n} ({len(d):,} bytes, {c})", file=sys.stderr)
            continue
        post(webhook, payload, files=attach)
        print(f"sent {i + 1}/{len(msgs)} ({len(group)} embeds)", file=sys.stderr)
        if i < len(msgs) - 1:
            time.sleep(0.8)


if __name__ == "__main__":
    main()
