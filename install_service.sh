#!/usr/bin/env bash
# systemd --user への登録 / 解除 (通貨ごとのテンプレートインスタンス)
#   ./install_service.sh install     登録して起動 (既定: BTC ETH)
#   ./install_service.sh uninstall   停止して解除
#   ./install_service.sh status      状態と次回実行時刻
#
#   CURRENCIES="BTC" ./install_service.sh install   のように通貨を絞れる
set -euo pipefail
cd "$(dirname "$0")"
DEST="$HOME/.config/systemd/user"
CURRENCIES="${CURRENCIES:-BTC ETH}"

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
    systemctl --user daemon-reload
    for c in $CURRENCIES; do
      systemctl --user enable --now "btcwatch@$c.timer"
    done
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
    rm -f "$DEST/btcwatch@.service" "$DEST/btcwatch@.timer" \
          "$DEST/btcwatch.service" "$DEST/btcwatch.timer"
    systemctl --user daemon-reload
    echo "解除しました"
    ;;
  status)
    systemctl --user list-timers 'btcwatch@*.timer' --no-pager || true
    for c in $CURRENCIES; do
      systemctl --user status "btcwatch@$c.service" --no-pager -n 10 || true
    done
    ;;
  *) echo "usage: $0 [install|uninstall|status]" >&2; exit 2 ;;
esac
