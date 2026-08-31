#!/usr/bin/env bash
# run_watch.sh — 手動で1回だけ: advisor.py → claude考察 → Discord通知(元データ添付)
# 定期実行は watch_loop.sh を使うこと
set -euo pipefail
cd "$(dirname "$0")"

# matplotlib を入れた venv があればそれを使う (作図に必要)
PY="${PY:-$([ -x .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3)}"

CURRENCY="${CURRENCY:-BTC}"
PROMPT="この${CURRENCY}のオプション地形データを考察して。まずWeb検索で次の4点を確認すること:
1) 米国経済指標の直近結果と今後1週間の予定 (CPI・雇用統計・FOMC・PCE・GDPなど)
2) ホワイトハウスの動きと大統領・閣僚の直近発言 (関税・財政・金融規制・暗号資産関連)
3) 世界で今話題になっている出来事のうち市場に波及しうるもの (地政学・大手企業・規制)
4) 暗号資産固有のニュース (ETF資金流出入・取引所・規制当局)
そのうえで、価格に効きそうな材料だけを選び、それが地形のどの水準 (壁・支持・ガンマフリップ・満期集中) を試しに来るかという形で統合した見解を出して。関係の薄い一般論は書かない"
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
