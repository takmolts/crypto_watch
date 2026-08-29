# crypto_watch

Deribit のオプション建玉から BTC / ETH の「地形」(壁・支持・磁石・ガンマ) を読み、
**観測は毎時・考察は条件付き**で Claude に投げて Discord へ通知する。
通貨ごとに独立した timer / 状態 / 通知チャンネル (webhook) を持つ。

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
| `watch_loop.sh` | 定期実行の本体。定時考察・クールダウン・日次上限を通貨別に管理 |
| `run_watch.sh` | 手動で1回だけ通す (自動運転の状態を触らない) |
| `install_service.sh` | systemd --user の timer として登録/解除 (`btcwatch@BTC` / `btcwatch@ETH`) |
| `requirements.txt` | 作図に必要なものだけ (matplotlib) |

## セットアップ

```bash
git clone git@github.com:takmolts/crypto_watch.git
cd crypto_watch
cp .env.example .env    # DISCORD_WEBHOOK_BTC / DISCORD_WEBHOOK_ETH を書く
                        # (通貨別が無ければ DISCORD_WEBHOOK にフォールバック)

python3 -m venv .venv                       # 作図する場合のみ
.venv/bin/pip install -r requirements.txt

./install_service.sh install                # BTC/ETH の timer を登録
CURRENCIES="BTC" ./install_service.sh install   # 通貨を絞る場合
```

`advisor.py` / `notify_discord.py` / シェル側は**標準ライブラリのみ**で動く。
`requirements.txt` の matplotlib は地形図を描く場合にだけ必要で、無ければ作図だけ
スキップして通知は続行する。

シェルスクリプトは `.venv/bin/python3` があればそれを使い、無ければ `python3` に
フォールバックする。

### 別マシンに移すときの前提

- `claude` CLI にログイン済みであること (`claude -p` を使う)
- 図に日本語を出すなら CJK フォント。Debian/Ubuntu なら
  `sudo apt install fonts-noto-cjk` (無い場合は自動で DejaVu Sans にフォールバック
  するが、日本語が豆腐になる)
- 配置先は任意。`install_service.sh` が systemd ユニットのパスを実際の場所に
  書き換える (ホーム配下なら `%h/...` として記録する)
- 前のマシンの観測履歴を引き継ぐなら `~/.btc_oi_advisor/` をコピーする。
  `BTC_baseline.json` が無い場合、初回実行は必ず考察が走る

## 発火条件

前回**考察を送った時点**のスナップショットと比較する (直前の毎時観測ではない)。
じわじわ動いた分も積算で拾うため。

| 条件 | 既定 | 変数 |
|---|---|---|
| スポット変化 | ±2.0% | `TH_SPOT` |
| ABS GEX 変化 | ±15% | `TH_GEX` |
| ファンディング符号反転 | \|値\| ≥ 0.002% を伴うもの | — |
| 単一ストライクのOI変化 | BTC ±3,000枚 / ETH ±20,000枚 | `TH_OI` |
| HL無期限の建玉変化 | ±10% | `TH_HL_OI` |
| HLプレミアム符号反転 | \|値\| ≥ 0.03% を伴うもの | — |
| ガンマフリップをスポットが跨いだ | — | — |

発火してもシェル側で2段階に絞る:

- `COOLDOWN_MIN` (既定120分) — 直近の考察から近すぎるときは見送る。**基準は据え置く**ので、
  明けたら同じ変化でちゃんと発火する
- `MAX_PER_DAY` (既定8回)

定時考察 (`SCHEDULE`、既定 8時・20時) はどちらも無視する。PCが落ちていて時刻を跨いだ場合は
次回起動時に1回だけ拾う。

## 運用

```bash
./watch_loop.sh force            # 今すぐ考察させる (BTC)
CURRENCY=ETH ./watch_loop.sh force
./run_watch.sh                   # 状態を触らずに1回だけ (CURRENCY=ETH も可)
./install_service.sh status      # 次回実行時刻
journalctl --user -u 'btcwatch@*' -f
```

閾値や定時の変更は `systemd/btcwatch@.service` の `Environment=` 行を編集して再インストール。

## 通知の中身

- 先頭 embed: 地形図PNG + 図の読みどころ (数値から機械生成するので図と必ず一致する)。
  左端に直近72時間の値動きパネル (無期限先物1h足、近傍の壁/支持を重ねる)、
  サブタイトルに HL 板の厚み (mid±2%のUSD建て) も出す
- 続く embed: Claude の考察を見出し単位で分割、キーワードで色付け
- 添付: `terrain-*.png` / `analysis-*.md` (embed化前の考察) / `report-*.txt` (生レポート)

## 注意

OIの偏りは「壁・磁石」の地図であって方向予測ではない。イベントの影響は考察側 (Web検索) が担う。
最終判断は自分で。
