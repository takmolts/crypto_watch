#!/usr/bin/env bash
# systemd --user への登録 / 解除 (通貨ごとのテンプレートインスタンス)
#   ./install_service.sh install     登録して起動 (既定: BTC ETH)
#   ./install_service.sh uninstall   停止して解除
#   ./install_service.sh status      状態と次回実行時刻
#
#   CURRENCIES="BTC" ./install_service.sh install   のように通貨を絞れる
#   BOT=0 ./install_service.sh install               Discord bot を入れない
set -euo pipefail
cd "$(dirname "$0")"
DEST="$HOME/.config/systemd/user"
CURRENCIES="${CURRENCIES:-BTC ETH}"
BOT="${BOT:-1}"

# bot は定期送信のおまけなので、前提が揃っていなければ理由を出して飛ばす。
# 定期送信 (timer) 側の登録は bot の有無に関係なく成立させる
bot_ready() {
  [ "$BOT" = "1" ] || { echo "bot: BOT=0 のためスキップ"; return 1; }
  [ -f crypto_bot.py ] || { echo "bot: crypto_bot.py が無いのでスキップ"; return 1; }
  if [ ! -x .venv/bin/python3 ]; then
    echo "bot: .venv が無いのでスキップ (python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)"
    return 1
  fi
  if ! .venv/bin/python3 -c 'import discord' 2>/dev/null; then
    echo "bot: discord.py 未導入のためスキップ (.venv/bin/pip install -r requirements.txt)"
    return 1
  fi
  if ! grep -qE '^DISCORD_BOT_TOKEN=.+' .env 2>/dev/null; then
    echo "bot: .env の DISCORD_BOT_TOKEN が空のためスキップ"
    return 1
  fi
  return 0
}

case "${1:-install}" in
  install)
    mkdir -p "$DEST"
    # ユニット内の %h/Programs/btcwatch を実際の配置先に合わせる
    case "$PWD" in
      "$HOME"/*) here="%h/${PWD#"$HOME"/}" ;;
      *)         here="$PWD" ;;
    esac
    sed "s|%h/Programs/btcwatch|$here|g" systemd/btcwatch@.service \
        > "$DEST/btcwatch@.service"
    cp systemd/btcwatch@.timer "$DEST/"
    # テンプレート化前の単一ユニットが残っていれば片付ける
    if [ -f "$DEST/btcwatch.timer" ]; then
      systemctl --user disable --now btcwatch.timer 2>/dev/null || true
      rm -f "$DEST/btcwatch.service" "$DEST/btcwatch.timer"
    fi
    if bot_ready; then
      sed "s|%h/Programs/btcwatch|$here|g" systemd/cryptobot.service \
          > "$DEST/cryptobot.service"
    fi
    systemctl --user daemon-reload
    for c in $CURRENCIES; do
      systemctl --user enable --now "btcwatch@$c.timer"
    done
    if [ -f "$DEST/cryptobot.service" ]; then
      systemctl --user enable --now cryptobot.service
      # 常駐なので、起動に失敗していればここで気づけるようにする
      sleep 2
      systemctl --user is-active --quiet cryptobot.service \
        && echo "bot: 起動しました (/crypto_status /crypto_run)" \
        || echo "bot: 起動に失敗 — journalctl --user -u cryptobot -n 30"
    fi
    # ログアウト後も動かす (既に有効なら何もしない)
    loginctl enable-linger "$USER" 2>/dev/null || true
    systemctl --user list-timers 'btcwatch@*.timer' --no-pager
    echo "ログ: journalctl --user -u 'btcwatch@*' -f  /  $PWD/logs/watch.log"
    ;;
  uninstall)
    for c in $CURRENCIES; do
      systemctl --user disable --now "btcwatch@$c.timer" 2>/dev/null || true
    done
    systemctl --user disable --now btcwatch.timer 2>/dev/null || true
    systemctl --user disable --now cryptobot.service 2>/dev/null || true
    rm -f "$DEST/btcwatch@.service" "$DEST/btcwatch@.timer" \
          "$DEST/btcwatch.service" "$DEST/btcwatch.timer" \
          "$DEST/cryptobot.service"
    systemctl --user daemon-reload
    echo "解除しました"
    ;;
  status)
    systemctl --user list-timers 'btcwatch@*.timer' --no-pager || true
    for c in $CURRENCIES; do
      systemctl --user status "btcwatch@$c.service" --no-pager -n 10 || true
    done
    systemctl --user status cryptobot.service --no-pager -n 10 2>/dev/null || true
    ;;
  *) echo "usage: $0 [install|uninstall|status]" >&2; exit 2 ;;
esac
