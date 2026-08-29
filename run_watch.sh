#!/usr/bin/env bash
# run_watch.sh — 手動で1回だけ: advisor.py → claude考察 → Discord通知(元データ添付)
# 定期実行は watch_loop.sh を使うこと
set -euo pipefail
cd "$(dirname "$0")"

# matplotlib を入れた venv があればそれを使う (作図に必要)
PY="${PY:-$([ -x .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3)}"

CURRENCY="${CURRENCY:-BTC}"
PROMPT="この${CURRENCY}のデータを考察して。今日の重要イベント(FOMC・雇用統計・要人発言など)をWeb検索で確認し、地形とイベントリスクを統合した見解を出して"
MODE="${MODE:-section}"           # section(読みやすさ重視) | sentence(1文=1embed)
LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR"
STAMP="$CURRENCY-$(date -u +%Y%m%d-%H%M%S)"
RPT="$LOG_DIR/report-$STAMP.txt"
PNG="$LOG_DIR/terrain-$STAMP.png"
ANALYSIS="$LOG_DIR/analysis-$STAMP.md"

"$PY" advisor.py --currency "$CURRENCY" --out "$RPT" --plot "$PNG" >/dev/null
cat "$RPT"
claude -p "$PROMPT" --allowedTools "WebSearch" < "$RPT" | tee "$ANALYSIS"

if [ ! -s "$ANALYSIS" ]; then
  echo "claude の出力が空。通知をスキップ" >&2
  exit 1
fi

IMG=()
if [ -s "$PNG" ]; then
    IMG=(--image "$PNG")
    [ -s "${PNG%.png}.caption.txt" ] && IMG+=(--image-caption "${PNG%.png}.caption.txt")
fi
"$PY" notify_discord.py --mode "$MODE" --currency "$CURRENCY" --file "$ANALYSIS" \
    "${IMG[@]}" --attach-source --attach "$RPT" "$@"
echo "log: $ANALYSIS / $RPT" >&2
