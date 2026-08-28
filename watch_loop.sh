#!/usr/bin/env bash
# watch_loop.sh — 観測は毎時・考察は条件付き
#
#   ./watch_loop.sh          ループ常駐 (毎時ちょうどに実行)
#   ./watch_loop.sh once     1回だけ実行 (cron から呼ぶ場合はこちら)
#   ./watch_loop.sh force    トリガー無視で1回考察させる (動作確認用)
#
# 環境変数で調整:
#   INTERVAL=3600            観測間隔(秒)
#   SCHEDULE="8 20"          定時考察の時刻(ローカル時, 空で無効)
#   COOLDOWN_MIN=120         トリガー発火の最短間隔(分)。定時考察は対象外
#   MAX_PER_DAY=8            1日あたりの考察回数の上限
#   TH_SPOT=2.0 TH_GEX=15 TH_OI=3000   発火閾値
#   MODE=section             Discord embed の整形モード
#   NOTIFY=1                 0 にすると Discord へ送らない(ログのみ)
#
# 通知には元データを2点添付する:
#   report-*.txt   advisor.py の生レポート(Deribitから読んだ地形)
#   analysis-*.md  claude の考察本文(embed に落とす前のテキスト)

set -uo pipefail
cd "$(dirname "$0")"

# matplotlib を入れた venv があればそれを使う (作図に必要)
PY="${PY:-$([ -x .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3)}"

INTERVAL="${INTERVAL:-3600}"
SCHEDULE="${SCHEDULE-8 20}"   # 空文字を渡すと定時考察を無効化できる
COOLDOWN_MIN="${COOLDOWN_MIN:-120}"
MAX_PER_DAY="${MAX_PER_DAY:-8}"
TH_SPOT="${TH_SPOT:-2.0}"
TH_GEX="${TH_GEX:-15}"
TH_OI="${TH_OI:-3000}"
MODE="${MODE:-section}"
NOTIFY="${NOTIFY:-1}"
CURRENCY="${CURRENCY:-BTC}"

STATE_DIR="${STATE_DIR:-$HOME/.btc_oi_advisor}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$STATE_DIR" "$LOG_DIR"
LOGFILE="$LOG_DIR/watch.log"
LOCK="$STATE_DIR/watch.lock"
LAST_FIRE="$STATE_DIR/last_fire"        # epoch秒
DAY_COUNT="$STATE_DIR/day_count"        # "YYYY-MM-DD count"
SCHED_MARK="$STATE_DIR/last_sched"      # "YYYY-MM-DD 8 20" (今日済ませた定時)

PROMPT='このデータを考察して。今日の重要イベント(FOMC・雇用統計・要人発言など)をWeb検索で確認し、地形とイベントリスクを統合した見解を出して'

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOGFILE" >&2; }

# ---------------------------------------------------------------- 1回分

run_once() {
    local force_flag="${1:-}"
    local stamp rpt force="" reason_sched=""
    stamp=$(date -u +%Y%m%d-%H%M%S)
    rpt="$LOG_DIR/report-$stamp.txt"
    local png="$LOG_DIR/terrain-$stamp.png"

    # 定時考察の時刻か。SCHED_MARK ("YYYY-MM-DD 8 20") に済んだ時刻を記録する。
    # PCが落ちていて時刻を跨いだ場合は次の起動時に1回だけ拾う(catch-up)。
    local today hh mark_date mark_hours pending=""
    today=$(date +%F); hh=$(date +%-H)
    if [ -n "$SCHEDULE" ]; then
        read -r mark_date mark_hours < <(cat "$SCHED_MARK" 2>/dev/null; echo)
        [ "${mark_date:-}" != "$today" ] && mark_hours=""
        for h in $SCHEDULE; do
            case " ${mark_hours:-} " in *" $h "*) continue ;; esac
            [ "$hh" -ge "$h" ] && pending="${pending:+$pending }$h"
        done
        if [ -n "$pending" ]; then
            # 複数溜まっていても考察は1回。全部まとめて済み扱いにする
            force="--force"
            reason_sched="$today ${mark_hours:+$mark_hours }$pending"
        fi
    fi
    [ "$force_flag" = "force" ] && force="--force"

    "$PY" advisor.py --currency "$CURRENCY" --check --defer-baseline $force \
        --out "$rpt" --plot "$png" \
        --th-spot "$TH_SPOT" --th-gex "$TH_GEX" --th-oi "$TH_OI" \
        >/dev/null 2>>"$LOGFILE"
    local rc=$?

    if [ $rc -ne 0 ] && [ $rc -ne 1 ]; then
        log "advisor.py がエラー終了 (rc=$rc) — 今回はスキップ"
        return 0
    fi
    if [ $rc -eq 1 ]; then
        log "観測のみ: 有意な変化なし (snapshot 保存済み)"
        rm -f "$rpt" "$png"
        return 0
    fi

    # --- ここから発火 ---
    # 定時考察でなければクールダウンと日次上限を見る
    if [ -z "$force" ]; then
        local last now gap
        last=$(cat "$LAST_FIRE" 2>/dev/null || echo 0)
        now=$(date +%s); gap=$(( (now - last) / 60 ))
        if [ "$gap" -lt "$COOLDOWN_MIN" ]; then
            log "トリガー発火したがクールダウン中 (${gap}分 < ${COOLDOWN_MIN}分) — 考察を見送り"
            rm -f "$rpt" "$png"
            return 0
        fi
        local d c
        read -r d c < <(cat "$DAY_COUNT" 2>/dev/null || echo "$today 0")
        [ "$d" != "$today" ] && c=0
        if [ "$c" -ge "$MAX_PER_DAY" ]; then
            log "本日の考察が上限 ($MAX_PER_DAY 回) に到達 — 見送り"
            rm -f "$rpt" "$png"
            return 0
        fi
    fi

    # 古いログ/画像の掃除 (既定30日)
    find "$LOG_DIR" -maxdepth 1 -type f \( -name 'report-*.txt' -o -name 'analysis-*.md' \
        -o -name 'terrain-*.png' \) -mtime "+${LOG_KEEP_DAYS:-30}" -delete 2>/dev/null || true

    log "考察を実行${force:+ (定時)}: $rpt"
    local analysis="$LOG_DIR/analysis-$stamp.md" img
    if ! claude -p "$PROMPT" --allowedTools "WebSearch" < "$rpt" > "$analysis" 2>>"$LOGFILE"; then
        log "claude -p が失敗 — 通知をスキップ (レポートは $rpt に残っています)"
        return 0
    fi
    if [ ! -s "$analysis" ]; then
        log "claude の出力が空 — 通知をスキップ"
        return 0
    fi

    if [ "$NOTIFY" = "1" ]; then
        img=()
        if [ -s "$png" ]; then
            img=(--image "$png")
            [ -s "${png%.png}.caption.txt" ] && \
                img+=(--image-caption "${png%.png}.caption.txt")
        fi
        if "$PY" notify_discord.py --mode "$MODE" --file "$analysis" \
                "${img[@]}" --attach-source --attach "$rpt" 2>>"$LOGFILE"; then
            log "Discord へ送信: $analysis"
        else
            log "Discord 送信に失敗 (考察は $analysis に保存済み)"
        fi
    else
        log "NOTIFY=0 のため送信せず: $analysis"
    fi

    # 状態更新 (考察が実際に走ったときだけ基準を進める)
    "$PY" advisor.py --currency "$CURRENCY" --promote-baseline \
        >/dev/null 2>>"$LOGFILE" || log "baseline の昇格に失敗"
    date +%s > "$LAST_FIRE"
    local d c
    read -r d c < <(cat "$DAY_COUNT" 2>/dev/null || echo "$today 0")
    [ "$d" != "$today" ] && c=0
    echo "$today $((c + 1))" > "$DAY_COUNT"
    [ -n "$reason_sched" ] && echo "$reason_sched" > "$SCHED_MARK"
}

# ---------------------------------------------------------------- 排他

with_lock() {
    exec 9>"$LOCK"
    if ! flock -n 9; then
        log "別のインスタンスが実行中 — スキップ"
        return 0
    fi
    "$@"
}

# ---------------------------------------------------------------- entry

case "${1:-loop}" in
    once)  with_lock run_once ;;
    force) with_lock run_once force ;;
    loop)
        log "watch_loop 開始 (間隔 ${INTERVAL}s / 定時 ${SCHEDULE:-なし}時 / 上限 ${MAX_PER_DAY}回/日)"
        trap 'log "watch_loop 停止"; exit 0' INT TERM
        while true; do
            with_lock run_once
            # 次の区切り(既定は毎時ちょうど)まで待つ
            now=$(date +%s)
            sleep $(( INTERVAL - now % INTERVAL ))
        done
        ;;
    *) echo "usage: $0 [loop|once|force]" >&2; exit 2 ;;
esac
