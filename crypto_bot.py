#!/usr/bin/env python3
"""
crypto_bot.py — crypto_watch の Discord bot

    /crypto_status            定期送信の動作状況・次回時刻・蓄積データを見る
    /crypto_run [BTC|ETH]     いまの地形を評価して考察を1本投稿する (省略で両方)

定期送信そのものには手を触れない (watch_loop.sh + systemd timer のまま)。
/crypto_run は run_watch.sh を呼ぶだけで、advisor.py に --check を渡さないため
自動運転の基準 (baseline / 日次カウント / 定時の済み印) は動かない。手動で回した
せいで次の定時トリガーが鈍る、ということが起きないようにしている。

考察の投稿先は通貨別チャンネル (DISCORD_WEBHOOK_<通貨>)。定期送信と同じ
notify_discord.py を通すので、地形図・色付き embed・添付が定期分と揃う。
コマンドを叩いたチャンネルには進捗と結果だけ返す。
"""

import asyncio
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Optional
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

from notify_discord import load_env

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR", os.path.expanduser("~/.btc_oi_advisor"))
LOG_DIR = os.path.join(BASE, os.environ.get("LOG_DIR", "logs"))
WATCH_LOG = os.path.join(LOG_DIR, "watch.log")
RUN_WATCH = os.path.join(BASE, "run_watch.sh")

CURRENCIES = os.environ.get("CURRENCIES", "BTC ETH").split()
# claude の Web検索込みで数分かかる。systemd 側の TimeoutStartSec=900 に合わせる
RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "900"))

env = load_env(os.path.join(BASE, ".env"))


def conf(key, default=None):
    """環境変数を優先し、無ければ .env を見る"""
    return os.environ.get(key) or env.get(key) or default


TOKEN = conf("DISCORD_BOT_TOKEN")
# ギルドを指定すると slash コマンドが即時反映される (グローバルは最大1時間かかる)
GUILD_ID = conf("DISCORD_GUILD_ID")

STARTED = time.time()
# 実行中の通貨。二重起動を防ぐ (bot は単一プロセスなので集合で足りる)
RUNNING = set()


# ---------------------------------------------------------------- 表示の道具

def ago(dt):
    """過去の時刻を「3時間12分前」にする"""
    if dt is None:
        return "—"
    return span(datetime.now() - dt) + "前"


def until(dt):
    if dt is None:
        return "—"
    return span(dt - datetime.now()) + "後"


def span(td):
    s = int(abs(td.total_seconds()))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}日{h}時間"
    if h:
        return f"{h}時間{m}分"
    if m:
        return f"{m}分"
    return f"{s}秒"


def hhmm(dt):
    return dt.strftime("%H:%M") if dt else "—"


def mdhm(dt):
    return dt.strftime("%m/%d %H:%M") if dt else "—"


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


# ---------------------------------------------------------------- systemd

def unit_props(unit, *props):
    """systemctl show の結果を dict で返す。ユニットが無ければ空"""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", unit, *[f"-p{p}" for p in props]],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k:
            d[k] = v
    return d


# systemd は "Tue 2026-09-01 10:01:37 JST" の形で返す。
# 無効なタイマーでは "n/a" や "infinity" になるので取れないこともある
_SD_TIME = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def sd_time(value):
    m = _SD_TIME.search(value or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")


def timer_info(cur):
    """タイマーの有効/次回/前回と、ユニットに設定された SCHEDULE を読む"""
    t = unit_props(f"btcwatch@{cur}.timer",
                   "ActiveState", "NextElapseUSecRealtime", "LastTriggerUSec")
    s = unit_props(f"btcwatch@{cur}.service", "Environment", "ActiveState")
    schedule = []
    for tok in shlex.split(s.get("Environment", "")):
        k, _, v = tok.partition("=")
        if k == "SCHEDULE":
            schedule = [int(h) for h in v.split()]
    return {
        "installed": bool(t),
        "active": t.get("ActiveState") == "active",
        "next": sd_time(t.get("NextElapseUSecRealtime")),
        "last": sd_time(t.get("LastTriggerUSec")),
        "running": s.get("ActiveState") in ("active", "activating"),
        "schedule": schedule or [8, 20],
    }


# ---------------------------------------------------------------- 状態ファイル

def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def state(cur):
    """watch_loop.sh が置く状態ファイルを読む"""
    last_fire = read_text(os.path.join(STATE_DIR, f"last_fire.{cur}"))
    day = read_text(os.path.join(STATE_DIR, f"day_count.{cur}")).split()
    sched = read_text(os.path.join(STATE_DIR, f"last_sched.{cur}")).split()
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "last_fire": datetime.fromtimestamp(int(last_fire)) if last_fire.isdigit() else None,
        "count": int(day[1]) if len(day) > 1 and day[0] == today and day[1].isdigit() else 0,
        # 済み印は日付が変わると無効。watch_loop.sh の判定と同じ扱いにする
        "sched_done": [int(h) for h in sched[1:] if h.isdigit()] if sched[:1] == [today] else [],
        "alert": read_text(os.path.join(STATE_DIR, f"last_alert.{cur}")),
    }


def sched_status(cur, info, st):
    """定時考察の「今日の進み具合」と「次にいつ出るか」を watch_loop.sh と同じ規則で出す"""
    now = datetime.now()
    done, pending, waiting = [], [], []
    for h in info["schedule"]:
        if h in st["sched_done"]:
            done.append(h)
        elif now.hour >= h:
            pending.append(h)      # 時刻は過ぎているのに未送信 = 次回 catch-up 対象
        else:
            waiting.append(h)

    parts = []
    if done:
        parts.append("✅ " + "・".join(f"{h}時" for h in done))
    if pending:
        parts.append("⚠️ " + "・".join(f"{h}時" for h in pending) + " 未送信")
    if waiting:
        parts.append("🕐 " + "・".join(f"{h}時" for h in waiting) + " 待ち")

    if pending:
        # 済み印が無いので、次の毎時実行がそのまま出し直す
        nxt = f"次の観測 {hhmm(info['next'])} で出し直し"
    elif waiting:
        t = now.replace(hour=min(waiting), minute=0, second=0, microsecond=0)
        nxt = f"次は今日 {hhmm(t)} ({until(t)})"
    else:
        t = (now + timedelta(days=1)).replace(
            hour=min(info["schedule"]), minute=0, second=0, microsecond=0)
        nxt = f"次は明日 {hhmm(t)}"
    return " / ".join(parts) or "—", nxt


# ---------------------------------------------------------------- 蓄積データ

def data_stats(cur):
    files = sorted(glob.glob(os.path.join(STATE_DIR, f"{cur}_2*.json")))
    total = sum(os.path.getsize(f) for f in files) if files else 0

    def stamp(path):
        m = re.search(r"_(\d{8})_(\d{6})\.json$", path)
        if not m:
            return None
        # スナップショットのファイル名は UTC。表示はローカルに直す
        utc = datetime.strptime(m.group(1) + m.group(2),
                                "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return utc.astimezone().replace(tzinfo=None)

    base_at, base_spot = None, None
    try:
        with open(os.path.join(STATE_DIR, f"{cur}_baseline.json")) as f:
            b = json.load(f)
        base_at = datetime.fromisoformat(b["ts"]).astimezone().replace(tzinfo=None)
        base_spot = b.get("spot")
    except (OSError, ValueError, KeyError):
        pass

    return {
        "count": len(files),
        "bytes": total,
        "oldest": stamp(files[0]) if files else None,
        "newest": stamp(files[-1]) if files else None,
        "baseline": base_at,
        "baseline_spot": base_spot,
    }


# ---------------------------------------------------------------- ログ

LOG_LINE = re.compile(r"^\[([\d\-]+ [\d:]+)\] \[(\w+)\] (.*)$")
# 状況として意味のある行だけ拾う (観測のみ・クールダウンは出さない)
NOTABLE = ("Discord へ送信", "Discord 送信に失敗", "claude -p が失敗",
           "claude の出力が空", "advisor.py がエラー終了", "障害から復帰",
           "本日の考察が上限")


def recent_events(cur, limit=3):
    """watch.log の末尾から、その通貨の目立つ出来事を新しい順に拾う"""
    try:
        with open(WATCH_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    found = []
    for line in reversed(tail.splitlines()):
        m = LOG_LINE.match(line)
        if not m or m.group(2) != cur:
            continue
        if any(k in m.group(3) for k in NOTABLE):
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            # パスは長いので落として本文だけ見せる
            msg = re.split(r"[:：]| — | \(", m.group(3))[0].strip()
            found.append((ts, msg[:40]))
            if len(found) >= limit:
                break
    return found


# ---------------------------------------------------------------- /crypto_status

def status_embed():
    e = discord.Embed(title="📊 crypto_watch 稼働状況", color=0x3498DB)
    notes = [f"bot 稼働 {span(timedelta(seconds=time.time() - STARTED))}"]
    notes.append("手動実行: " + ("・".join(sorted(RUNNING)) if RUNNING else "なし"))
    e.description = " / ".join(notes)

    for cur in CURRENCIES:
        info, st, d = timer_info(cur), state(cur), data_stats(cur)
        lines = []

        if not info["installed"]:
            lines.append("⛔ timer 未登録 (`./install_service.sh install`)")
        elif not info["active"]:
            lines.append("⛔ timer 停止中")
        else:
            now_run = " ・実行中" if info["running"] else ""
            lines.append(f"**観測** 次回 {hhmm(info['next'])} ({until(info['next'])})"
                         f" ・前回 {hhmm(info['last'])}{now_run}")

        prog, nxt = sched_status(cur, info, st)
        lines.append(f"**定時** {prog}")
        lines.append(f"　　　 {nxt}")
        lines.append(f"**考察** 最終 {mdhm(st['last_fire'])} ({ago(st['last_fire'])})"
                     f" ・今日 {st['count']}回")

        if st["alert"]:
            lines.append(f"⚠️ **障害継続中** `{st['alert']}` — 復旧すると自動で解除")

        lines.append(f"**データ** {d['count']}件 "
                     f"{mdhm(d['oldest'])}〜{mdhm(d['newest'])} ({human_bytes(d['bytes'])})")
        spot = f" (spot {d['baseline_spot']:,.0f})" if d["baseline_spot"] else ""
        lines.append(f"**基準** {mdhm(d['baseline'])}{spot} と比べて発火判定")

        ev = recent_events(cur)
        if ev:
            lines.append("**直近** " + " / ".join(f"{hhmm(t)} {msg}" for t, msg in ev))

        e.add_field(name=f"── {cur} ──",
                    value="\n".join(lines)[:1024], inline=False)

    if os.path.exists(WATCH_LOG):
        e.set_footer(text=f"watch.log {human_bytes(os.path.getsize(WATCH_LOG))}"
                          f" ・{LOG_DIR}")
    return e


# ---------------------------------------------------------------- /crypto_run

async def run_currency(cur):
    """run_watch.sh を1本回す。(成功, 経過秒, 表示用テキスト) を返す"""
    t0 = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            RUN_WATCH, cwd=BASE,
            env={**os.environ, "CURRENCY": cur},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        return False, 0.0, f"起動できません: {exc}"

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, time.time() - t0, f"{RUN_TIMEOUT}秒で打ち切りました"

    took = time.time() - t0
    stderr = err.decode("utf-8", "replace")
    if proc.returncode == 0:
        m = re.search(r"^log: (\S+)", stderr, re.M)
        return True, took, (m.group(1) if m else "")
    # 失敗の理由は claude や advisor の stderr に出る。末尾だけ見せる
    tail = (stderr.strip() or out.decode("utf-8", "replace").strip())[-800:]
    return False, took, tail


async def run_and_report(channel, targets, user):
    try:
        results = await asyncio.gather(*(run_currency(c) for c in targets),
                                       return_exceptions=True)
    finally:
        RUNNING.difference_update(targets)

    lines = []
    for cur, res in zip(targets, results):
        if isinstance(res, BaseException):
            lines.append(f"❌ **{cur}** 想定外のエラー\n```\n{res!r}\n```")
            continue
        ok, took, detail = res
        if ok:
            lines.append(f"✅ **{cur}** 完了 ({span(timedelta(seconds=took))}) "
                         f"→ {cur} のチャンネルに投稿しました"
                         + (f"\n　`{detail}`" if detail else ""))
        else:
            lines.append(f"❌ **{cur}** 失敗 ({span(timedelta(seconds=took))})\n"
                         f"```\n{detail}\n```")
    await channel.send(f"{user.mention}\n" + "\n".join(lines))


# ---------------------------------------------------------------- bot

intents = discord.Intents.none()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@tree.command(name="crypto_status",
              description="定期送信の動作状況・次回時刻・蓄積データを見る")
async def crypto_status(interaction: discord.Interaction):
    # systemctl を通貨ごとに叩くので、イベントループを塞がないよう別スレッドで組む。
    # 3秒以内に返せない可能性を消すため defer してから送る
    await interaction.response.defer()
    embed = await asyncio.to_thread(status_embed)
    await interaction.followup.send(embed=embed)


@tree.command(name="crypto_run",
              description="いまの地形を評価して考察を投稿する (3〜5分かかる)")
@app_commands.describe(currency="対象通貨。省略すると BTC と ETH の両方")
@app_commands.choices(currency=[
    app_commands.Choice(name="BTC", value="BTC"),
    app_commands.Choice(name="ETH", value="ETH"),
    app_commands.Choice(name="両方", value="BOTH"),
])
async def crypto_run(interaction: discord.Interaction,
                     currency: Optional[app_commands.Choice[str]] = None):
    want = currency.value if currency else "BOTH"
    targets = CURRENCIES if want == "BOTH" else [want]

    busy = [c for c in targets if c in RUNNING]
    if busy:
        await interaction.response.send_message(
            f"⏳ {'・'.join(busy)} は実行中です。終わるまで待ってください。",
            ephemeral=True)
        return

    RUNNING.update(targets)
    # claude の Web検索込みで数分かかる。3秒以内に返しておき、
    # 結果は完了時にチャンネルへ流す (defer の15分制限に縛られないため)
    await interaction.response.send_message(
        f"🔄 **{'・'.join(targets)}** の考察を開始しました (3〜5分)。"
        f"\n考察は通貨別チャンネルへ、完了報告はここに出します。"
        f"\n自動運転の基準は触りません。")

    channel = client.get_partial_messageable(interaction.channel_id)
    asyncio.create_task(run_and_report(channel, targets, interaction.user))


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        where = f"guild {GUILD_ID}"
    else:
        await tree.sync()
        where = "global (反映に最大1時間)"
    print(f"{client.user} で接続。コマンドを {where} に同期しました", flush=True)


def main():
    if not TOKEN:
        sys.exit("DISCORD_BOT_TOKEN が設定されていません (.env を確認)")
    if not os.access(RUN_WATCH, os.X_OK):
        sys.exit(f"{RUN_WATCH} が実行できません")
    client.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
