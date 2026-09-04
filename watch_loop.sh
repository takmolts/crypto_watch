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
#   TH_SPOT=2.0 TH_GEX=15 TH_OI=3000 TH_HL_OI=10 TH_SKEW=5 TH_ETF_OI=50000  発火閾値
#   TH_ETF_FLOW=400 TH_CB_PREM=0.15 TH_BASIS=3   ETF資金流出入(百万USD)・Coinbaseプレミアム(pt)・先物ベーシス(pt)
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
CURRENCY="${CURRENCY:-BTC}"
TH_SPOT="${TH_SPOT:-2.0}"
TH_GEX="${TH_GEX:-15}"
# 単一ストライクOI(枚)は通貨で桁が違う (ETHは1枚=1ETHで枚数が一桁多い)
TH_OI="${TH_OI:-$([ "$CURRENCY" = "ETH" ] && echo 20000 || echo 3000)}"
TH_HL_OI="${TH_HL_OI:-10}"
TH_SKEW="${TH_SKEW:-5}"
# 米国ETF(IBIT/ETHA)の単一ストライクOI(枚)。1枚=100株で通貨により規模が違う
TH_ETF_OI="${TH_ETF_OI:-$([ "$CURRENCY" = "ETH" ] && echo 25000 || echo 50000)}"
# 米国ETF(IBIT/ETHA)の1日の資金流出入(百万USD)。純資産の規模が違うので通貨別
TH_ETF_FLOW="${TH_ETF_FLOW:-$([ "$CURRENCY" = "ETH" ] && echo 150 || echo 400)}"
TH_CB_PREM="${TH_CB_PREM:-0.15}"   # Coinbaseプレミアムの変化(pt)
TH_BASIS="${TH_BASIS:-3}"          # 先物ベーシス(年率)の変化(pt)
MODE="${MODE:-section}"
NOTIFY="${NOTIFY:-1}"

STATE_DIR="${STATE_DIR:-$HOME/.btc_oi_advisor}"
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$STATE_DIR" "$LOG_DIR"
LOGFILE="$LOG_DIR/watch.log"
# 状態は通貨別に持つ (BTC/ETH を並走させるため)
LOCK="$STATE_DIR/watch.$CURRENCY.lock"
LAST_FIRE="$STATE_DIR/last_fire.$CURRENCY"    # epoch秒
DAY_COUNT="$STATE_DIR/day_count.$CURRENCY"    # "YYYY-MM-DD count"
SCHED_MARK="$STATE_DIR/last_sched.$CURRENCY"  # "YYYY-MM-DD 8 20" (今日済ませた定時)
ALERT_MARK="$STATE_DIR/last_alert.$CURRENCY"  # 継続中の障害の種別 (復旧するまで残す)
# 通貨サフィックスが無かった頃の状態ファイルを BTC 用として引き継ぐ
if [ "$CURRENCY" = "BTC" ]; then
    for f in last_fire day_count last_sched; do
        [ -f "$STATE_DIR/$f" ] && [ ! -e "$STATE_DIR/$f.BTC" ] \
            && mv "$STATE_DIR/$f" "$STATE_DIR/$f.BTC"
    done
fi

PROMPT="この${CURRENCY}のオプション地形データを考察して。まずWeb検索で次の4点を確認すること:
1) 米国経済指標の直近結果と今後1週間の予定 (CPI・雇用統計・FOMC・PCE・GDPなど)
2) ホワイトハウスの動きと大統領・閣僚の直近発言 (関税・財政・金融規制・暗号資産関連)
3) 世界で今話題になっている出来事のうち市場に波及しうるもの (地政学・大手企業・規制)
4) 暗号資産固有のニュース (現物ETFの資金流出入は全発行体の合計と年初来での位置づけまで・取引所・規制当局)

次の章立てで書くこと:
## 1. 地形サマリ — いまのレンジ・重要水準・ガンマ状態を数行で
## 2. 世界の動き — 効く材料の背景と因果。マクロのレジームが変わったならそれを最初に
## 3. イベントカレンダー — 今日と今週の予定を、なぜ・どちらに効くかとセットで
## 4. 地形との統合 — 各材料がどの水準 (壁・支持・フリップ・満期集中・ETFの壁) を試しに来るか
## 5. 需給とボラの温度感 — ファンディング・板・スキュー・DVOL・ETFフロー(IBIT/ETHAの口数変化)・Coinbaseプレミアム・先物ベーシスから違和感を拾う。ETF流入はベーシスの高低と合わせて、アービ資金か実需かを切り分ける
## 6. シナリオ — 今日/今週/それ以降の時系列で、確度と価格帯つき。強気弱気の両方
## 7. 見方を変えるトリガー — この数値がこうなったら前提が崩れる、という具体値で

書き方の注意:
- 材料は羅列せず、背景と因果を説明したうえで水準まで掘り下げる。採用した材料は読者がこのレポートだけで状況を把握できる深さで書く。省くのは関係の薄い一般論だけ
- Discordに流すため表 (markdownテーブル) は使わず、見出しと箇条書きで書く
- 最後に出典URLを並べる"

log() { printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$CURRENCY" "$*" \
        | tee -a "$LOGFILE" >&2; }

# ---------------------------------------------------------------- 死活通知
#
# 考察が落ちても今までは無言で、届かないことでしか気づけなかった。
# ただし定時は復旧するまで毎時リトライするので、そのたびに鳴らすと
# 「届いたものは全部読む価値がある」が壊れる。障害の種別が変わるか
# 復旧するまでの1通だけに絞る。
alert() {
    local kind="$1" body="$2"
    [ "$NOTIFY" = "1" ] || return 0
    [ "$(cat "$ALERT_MARK" 2>/dev/null)" = "$kind" ] && return 0
    if printf '%s\n' "$body" | "$PY" notify_discord.py --mode sentence \
            --currency "$CURRENCY" --username "$CURRENCY Watch (障害)" \
            --content "⚠️ **${CURRENCY} の定期通知が止まっています** (\`$kind\`)" \
            >/dev/null 2>>"$LOGFILE"; then
        echo "$kind" > "$ALERT_MARK"
    else
        # Discord 自体が落ちている場合はここも失敗する。mark を書かないので次回また試す
        log "死活通知の送信にも失敗 ($kind)"
    fi
}

# 引数を与えると、継続中の障害がその種別だったときだけ復帰扱いにする。
# 「観測は直ったが考察はまだ落ちている」を取り違えないため
alert_clear() {
    local cur k hit=""
    cur=$(cat "$ALERT_MARK" 2>/dev/null) || return 0
    [ -n "$cur" ] || return 0
    if [ $# -gt 0 ]; then
        for k in "$@"; do [ "$k" = "$cur" ] && hit=1; done
        [ -n "$hit" ] || return 0
    fi
    rm -f "$ALERT_MARK"
    log "障害から復帰 ($cur)"
}

# ---------------------------------------------------------------- 1回分

run_once() {
    local force_flag="${1:-}"
    local stamp rpt force="" reason_sched=""
    stamp="$CURRENCY-$(date -u +%Y%m%d-%H%M%S)"
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
        --th-hl-oi "$TH_HL_OI" --th-skew "$TH_SKEW" --th-etf-oi "$TH_ETF_OI" \
        --th-etf-flow "$TH_ETF_FLOW" --th-cb-prem "$TH_CB_PREM" --th-basis "$TH_BASIS" \
        >/dev/null 2>>"$LOGFILE"
    local rc=$?

    if [ $rc -ne 0 ] && [ $rc -ne 1 ]; then
        log "advisor.py がエラー終了 (rc=$rc) — 今回はスキップ"
        alert advisor "観測に失敗しました (advisor.py rc=$rc)。詳細は $LOGFILE を見てください。"
        return 0
    fi
    alert_clear advisor
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
        alert claude "考察の生成に失敗しました (claude -p)。観測とレポート自体は取れています。
$(tail -c 400 "$analysis" 2>/dev/null)
レポート: $rpt"
        return 0
    fi
    if [ ! -s "$analysis" ]; then
        log "claude の出力が空 — 通知をスキップ"
        alert claude-empty "claude の出力が空でした。レポート: $rpt"
        return 0
    fi
    alert_clear claude claude-empty

    local sent=1   # NOTIFY=0 は「送らないのが正常」なので済み扱いにする
    if [ "$NOTIFY" = "1" ]; then
        img=()
        if [ -s "$png" ]; then
            img=(--image "$png")
            [ -s "${png%.png}.caption.txt" ] && \
                img+=(--image-caption "${png%.png}.caption.txt")
        fi
        if "$PY" notify_discord.py --mode "$MODE" --currency "$CURRENCY" \
                --file "$analysis" \
                "${img[@]}" --attach-source --attach "$rpt" 2>>"$LOGFILE"; then
            log "Discord へ送信: $analysis"
            alert_clear
        else
            sent=0
            log "Discord 送信に失敗 (考察は $analysis に保存済み)"
            # 図とレポートを抱えた本体が弾かれても、短いテキストなら通ることが多い
            alert discord "考察は生成できましたが Discord へ送れませんでした。
本文: $analysis
次の定時実行で出し直します。"
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
    # 定時は「送れたら済み」。送れていなければ mark を書かず次回に持ち越す。
    # 古い考察を再送するのではなく、その時点の地形を読み直して出し直す
    # (地形もイベントも数時間で変わるので、鮮度のほうが価値が高い)
    if [ -n "$reason_sched" ] && [ "$sent" = "1" ]; then
        echo "$reason_sched" > "$SCHED_MARK"
    fi
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
