# crypto_watch

Deribit のオプション建玉から BTC の「地形」(壁・支持・磁石・ガンマ) を読み、
**観測は毎時・考察は条件付き**で Claude に投げて Discord へ通知する。

静かな日は定時の2通だけ、荒れた日は自動で増える。届いたものは全部読む価値がある、
という状態を保つのが設計方針。

## 構成

```
毎時  advisor.py --check     Deribit/Hyperliquid を観測してスナップショット保存 (APIのみ・無料)
        └─ 発火条件を満たすか？
             ├─ No  → 何もしない
             └─ Yes → claude -p で考察 → notify_discord.py で Discord へ
```

| ファイル | 役割 |
|---|---|
| `advisor.py` | 建玉/GEX の計算、レポート生成、発火判定 (`--check`)、作図の呼び出し |
| `plot_terrain.py` | 地形図PNGの描画と「図の読みどころ」の生成 (matplotlib) |
| `notify_discord.py` | テキストを embed に整形して Webhook へ送信。画像・元データを添付 |
| `watch_loop.sh` | 定期実行の本体。定時考察・クールダウン・日次上限を管理 |
| `run_watch.sh` | 手動で1回だけ通す (自動運転の状態を触らない) |
| `install_service.sh` | systemd --user の timer として登録/解除 |

## セットアップ

```bash
cp .env.example .env        # DISCORD_WEBHOOK を書く
python3 -m venv .venv       # 作図する場合のみ (matplotlib)
.venv/bin/pip install matplotlib
./install_service.sh install
```

`advisor.py` 本体は標準ライブラリのみで動く。matplotlib が無い場合は作図だけスキップする。

## 発火条件

前回**考察を送った時点**のスナップショットと比較する (直前の毎時観測ではない)。
じわじわ動いた分も積算で拾うため。

| 条件 | 既定 | 変数 |
|---|---|---|
| スポット変化 | ±2.0% | `TH_SPOT` |
| ABS GEX 変化 | ±15% | `TH_GEX` |
| ファンディング符号反転 | \|値\| ≥ 0.002% を伴うもの | — |
| 単一ストライクのOI変化 | ±3,000枚 | `TH_OI` |
| ガンマフリップをスポットが跨いだ | — | — |

発火してもシェル側で2段階に絞る:

- `COOLDOWN_MIN` (既定120分) — 直近の考察から近すぎるときは見送る。**基準は据え置く**ので、
  明けたら同じ変化でちゃんと発火する
- `MAX_PER_DAY` (既定8回)

定時考察 (`SCHEDULE`、既定 8時・20時) はどちらも無視する。PCが落ちていて時刻を跨いだ場合は
次回起動時に1回だけ拾う。

## 運用

```bash
./watch_loop.sh force        # 今すぐ考察させる
./run_watch.sh               # 状態を触らずに1回だけ
./install_service.sh status  # 次回実行時刻
journalctl --user -u btcwatch -f
```

閾値や定時の変更は `systemd/btcwatch.service` の `Environment=` 行を編集して再インストール。

## 通知の中身

- 先頭 embed: 地形図PNG + 図の読みどころ (数値から機械生成するので図と必ず一致する)
- 続く embed: Claude の考察を見出し単位で分割、キーワードで色付け
- 添付: `terrain-*.png` / `analysis-*.md` (embed化前の考察) / `report-*.txt` (生レポート)

## 注意

OIの偏りは「壁・磁石」の地図であって方向予測ではない。イベントの影響は考察側 (Web検索) が担う。
最終判断は自分で。
