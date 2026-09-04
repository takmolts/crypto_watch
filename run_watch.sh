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
