# crypto_watch

Deribit のオプション建玉から BTC / ETH の「地形」(壁・支持・磁石・ガンマ) を読み、
**観測は毎時・考察は条件付き**で Claude に投げて Discord へ通知する。
通貨ごとに独立した timer / 状態 / 通知チャンネル (webhook) を持つ。

静かな日は定時の2通だけ、荒れた日は自動で増える。届いたものは全部読む価値がある、
という状態を保つのが設計方針。

## 構成

```
毎時  advisor.py --check     Deribit/Hyperliquid/CBOE/iShares/Coinbase/Binance を観測してスナップショット保存 (APIのみ・無料)
                             (米国ETF IBIT/ETHA のOIは日次更新なので18時間キャッシュ、
                              iShares の口数/NAV は3時間キャッシュ + 日次履歴を蓄積)
        └─ 発火条件を満たすか？
             ├─ No  → 何もしない
             └─ Yes → claude -p で考察 → notify_discord.py で Discord へ
```

| ファイル | 役割 |
|---|---|
| `advisor.py` | 建玉/GEX/ボラ構造(スキュー・期間構造)/米国ETF地形/米国需要(ETFフロー・Coinbaseプレミアム・先物ベーシス)の計算、レポート生成、発火判定 (`--check`)、作図の呼び出し |
| `plot_terrain.py` | 地形図PNGの描画と「図の読みどころ」の生成 (matplotlib) |
| `notify_discord.py` | テキストを embed に整形して Webhook へ送信。画像・元データを添付。`--mode summary` で要約 + リンクの1通 |
| `publish_pages.py` | 考察・解析データ・推移を gh-pages に積んで公開 (`PAGES_URL` 設定時) |
| `pages/` | サイトの静的ファイル (HTML/CSS/JS)。`publish_pages.py` がコピーして公開する |
| `watch_loop.sh` | 定期実行の本体。定時考察・クールダウン・日次上限を通貨別に管理 |
| `run_watch.sh` | 手動で1回だけ通す (自動運転の状態を触らない) |
| `crypto_bot.py` | Discord bot。`/crypto_status` で稼働状況、`/crypto_run` でその場の考察 (任意) |
| `install_service.sh` | systemd --user への登録/解除 (`btcwatch@BTC` / `btcwatch@ETH` / `cryptobot`) |
| `requirements.txt` | 作図 (matplotlib) と bot (discord.py)。どちらも無くても定期送信は動く |

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
`requirements.txt` は「あると増える機能」の分で、matplotlib は地形図を描く場合
(無ければ作図だけスキップして通知は続行)、discord.py は bot を動かす場合にだけ要る。

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
  `BTC_baseline.json` が無い場合、初回実行は必ず考察が走る。
  `BTC_etf_fund.json` / `ETH_etf_fund.json` は IBIT/ETHA の口数・NAVの日次履歴で、
  ETF資金流出入はこれの差分から出す (失うと流出入とσ評価がしばらく出なくなる)

## 米国の現物需要と先物ベーシス

「ETFに年初来2番目の流入があった日に、レポートがそれに触れていなかった」を防ぐための
3点セット。Web検索任せにせず、数値としてレポートに載せて発火条件にも入れている。

| 観点 | 出所 | 更新 | 読み方 |
|---|---|---|---|
| ETF資金流出入 | iShares 公式ページの IBIT/ETHA 発行済口数 × NAV | 日次 (米国引け後) | 創設/償還の確定値。IBIT/ETHA 単体で、全発行体の合計ではない (合計と年初来の位置づけは考察側の Web検索に任せる) |
| Coinbase プレミアム | Coinbase (USD) vs Binance (USDT) 現物 | 毎時 | 米国の現物買い (AP の創設買い) が乗ると当日中にプラスへ振れる。ETFフローの先行指標 |
| 先物ベーシス | Deribit 期日先物 vs 現物指数 (年率換算) | 毎時 | 「ETFロング/先物ショート」のキャリー妙味。高いほど流入にアービ資金が混じり、低ければ実需寄り |

口数の履歴は初回観測から蓄積するので、流出入は2回目の日次更新から、σ評価は10営業日分
溜まってから出る。

## Discord bot (任意)

定期送信とは独立した常駐プロセス。定期送信そのものには手を触れない。

```
/crypto_status            稼働状況・次回時刻・蓄積データを見る
/crypto_run               いまの地形を評価して考察を投稿する (省略で BTC/ETH 両方)
/crypto_run currency:BTC  通貨を指定する
```

`/crypto_status` が出すもの (通貨ごと):

- 観測タイマーの次回/前回、いま実行中か
- 定時考察の今日の進み具合 (✅済み / ⚠️未送信 / 🕐待ち) と、次にいつ出るか。
  未送信があれば「次の観測で出し直し」と catch-up の予定を出す
- 最終考察の時刻と今日の回数、**継続中の障害** (`last_alert` があれば)
- スナップショットの件数・期間・サイズ、いま何と比べて発火判定しているか (基準の時刻と spot)
- `watch.log` の直近の出来事

`/crypto_run` は `run_watch.sh` を呼ぶだけなので、**自動運転の基準を動かさない**。
`advisor.py` に `--check` を渡さないため baseline は書かれず、日次カウントや定時の
済み印にも触らない。手動で回したせいで次の定時トリガーが鈍る、ということが起きない。

考察の投稿先は通貨別チャンネル (`DISCORD_WEBHOOK_<通貨>`)。定期送信と同じ
`notify_discord.py` を通すので、地形図・色付き embed・添付が定期分と揃う。
コマンドを叩いたチャンネルには開始と完了の報告だけ返す。

数分かかるので応答は先に返し、結果は完了時にチャンネルへ流す (interaction の
15分制限に縛られないため)。同じ通貨の二重実行は弾く。

### bot のセットアップ

```bash
.venv/bin/pip install -r requirements.txt   # discord.py
```

`.env` に足す:

```
DISCORD_BOT_TOKEN=...    # Developer Portal の Bot > Token (webhook とは別物)
DISCORD_GUILD_ID=...     # 入れると slash コマンドが即時反映される (空だと最大1時間)
```

bot の招待には `bot` と `applications.commands` の両スコープが要る。権限は
「メッセージを送信」があれば足りる (考察は webhook 経由なので bot 自身は投稿しない)。

```bash
./install_service.sh install     # 前提が揃っていれば cryptobot.service も登録される
BOT=0 ./install_service.sh install   # bot を入れない
journalctl --user -u cryptobot -f
```

トークンが空・discord.py 未導入・`.venv` 無しのいずれでも、理由を出して bot だけ
飛ばす。定期送信の登録はそれと関係なく成立する。

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
| 25Δスキュー変化 | ±5pt | `TH_SKEW` |
| IV期間構造(90d-7d)の符号反転 | \|値\| ≥ 2pt を伴うもの | — |
| 米国ETFの単一ストライクOI変化 (日次) | IBIT ±50,000枚 / ETHA ±25,000枚 | `TH_ETF_OI` |
| 米国ETFの1日の資金流出入 (日次、口数×NAV) | IBIT ±$400M / ETHA ±$150M | `TH_ETF_FLOW` (百万USD) |
| Coinbaseプレミアムの変化 | ±0.15pt | `TH_CB_PREM` |
| 先物ベーシス(年率)の変化 / 符号反転 | ±3pt / \|値\| ≥ 1pt を伴うもの | `TH_BASIS` |
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
./install_service.sh status      # 次回実行時刻 (bot の状態も出る)
journalctl --user -u 'btcwatch@*' -f
```

閾値や定時の変更は `systemd/btcwatch@.service` の `Environment=` 行を編集して再インストール。

## 通知の中身

- 先頭 embed: 地形図PNG + 図の読みどころ (数値から機械生成するので図と必ず一致する)。
  左端に直近72時間の値動きパネル (無期限先物1h足、近傍の壁/支持を重ねる)、
  サブタイトルに HL 板の厚み (mid±2%のUSD建て) も出す
- 続く embed: Claude の考察を見出し単位で分割、キーワードで色付け
- 添付: `terrain-*.png` / `analysis-*.md` (embed化前の考察) / `report-*.txt` (生レポート)

## GitHub Pages に全文を公開する (任意)

考察が増えて Discord では追いきれなくなったので、全文・解析データ・推移はサイトに積み、
Discord は**要約 + リンクの1通**にできる。`.env` に `PAGES_URL` を書くと有効になる。

```
発火 → advisor.py → claude -p → publish_pages.py add  (gh-pages に積んで push)
                                   └─ 成功: notify_discord.py --mode summary --link URL
                                   └─ 失敗/未設定: 従来の全文通知
```

サイトの中身 (通貨はタブで切替、考察は直近 `PAGES_KEEP_DAYS` 日分を選んで振り返れる):

1. **概要** — 主要数値のタイル、発火理由、考察の1章、地形図。
   「最新データを取得」でブラウザから Deribit / Coinbase / Binance を直接読み、考察時点との差を重ねる
2. **分析** — 考察の2〜7章
3. **推移** — 毎時スナップショットからの数日分の折れ線 (現物・DVOL・ファンディング・ベーシス・
   Coinbaseプレミアム・スキュー・GEX・HL建玉) と ETF 資金流出入の棒
4. **解析データ** — advisor.py の生レポートを節ごとに折りたたみ
5. **出典** — 考察中の URL

「Claude に相談」「ChatGPT に相談」は、概要と URL を入れたプロンプト付きで各サービスを開く。
「レポートをコピー」は考察と生データをクリップボードへ (貼り付けて相談する用)。
フルの再考察は静的サイトからは起動できないので、Discord の `/crypto_run` を使う。

### セットアップ

```bash
# .env
PAGES_URL=https://takmolts.github.io/crypto_watch/
PAGES_KEEP_DAYS=7
```

1. 初回だけ手で公開して gh-pages ブランチを作る:
   `./run_watch.sh` か、既存のログから
   `python3 publish_pages.py add --currency BTC --report logs/report-….txt --analysis logs/analysis-….md --png logs/terrain-….png --kind manual`
2. GitHub のリポジトリ設定 → Pages → Source を「Deploy from a branch」、Branch を `gh-pages` / `/ (root)` にする
3. 数分後に `PAGES_URL` で見えるようになる

注意:

- **Pages は公開サイト**。無料プランでは private リポジトリの Pages は使えない。
  載るのは市場データと考察だけで、webhook 等の秘密は含まない (`.env` は git 管理外)
- 履歴は持たない: 毎回 orphan コミット1つを force-push するので、データを積んでもリポジトリは
  肥大しない。蓄積の実体は `~/.btc_oi_advisor/pages/` (`PAGES_DIR`)。
  失っても `publish_pages.py` が gh-pages から取り直す
- 静的ファイル (`pages/`) を直したら `python3 publish_pages.py rebuild` で差し替えられる
- push は `origin` に SSH/HTTPS で行うので、その認証が通っている必要がある

## 失敗したとき

考察や送信が落ちても今までは無言だったので、**届かないこと**でしか気づけなかった。
いまは落ちた時点で短い死活通知を投げる。

| 落ちた場所 | 通知の種別 | その後 |
|---|---|---|
| `advisor.py` (観測) | `advisor` | 次の毎時観測で復帰 |
| `claude -p` (考察) | `claude` / `claude-empty` | レポートは残る。定時なら次の時刻で出し直す |
| `publish_pages.py` (公開) | `pages` | Discord には全文で送る (通知は止まらない) |
| Discord 送信 | `discord` | 考察は `analysis-*.md` に残る。定時なら次の時刻で出し直す |

定時考察の「済み」印 (`~/.btc_oi_advisor/last_sched.<通貨>`) は**送信できたときだけ**書く。
送れていなければ印を書かないので、次の毎時実行が catch-up として拾い直す。古い考察を
再送するのではなく、その時点の地形を読み直して出し直す (地形もイベントも数時間で変わる)。

死活通知は復旧するまで1通だけ (`~/.btc_oi_advisor/last_alert.<通貨>` で抑止)。
定時は復旧まで毎時リトライするので、そのたびに鳴らすと通知の価値が落ちるため。
障害の種別が変わったときと、復旧してから再発したときは改めて鳴る。

`claude -p` の認証切れ (`Failed to authenticate: OAuth session expired`) が原因のことが多い。
その場合は `claude` にログインし直せば、次の毎時実行が自動で拾い直す。

## 注意

OIの偏りは「壁・磁石」の地図であって方向予測ではない。イベントの影響は考察側 (Web検索) が担う。
最終判断は自分で。
