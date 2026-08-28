#!/usr/bin/env bash
# systemd --user への登録 / 解除
#   ./install_service.sh install     登録して起動
#   ./install_service.sh uninstall   停止して解除
#   ./install_service.sh status      状態と次回実行時刻
set -euo pipefail
cd "$(dirname "$0")"
DEST="$HOME/.config/systemd/user"

case "${1:-install}" in
  install)
    mkdir -p "$DEST"
    cp systemd/btcwatch.service systemd/btcwatch.timer "$DEST/"
    systemctl --user daemon-reload
    systemctl --user enable --now btcwatch.timer
    # ログアウト後も動かす (既に有効なら何もしない)
    loginctl enable-linger "$USER" 2>/dev/null || true
    systemctl --user list-timers btcwatch.timer --no-pager
    echo "ログ: journalctl --user -u btcwatch -f  /  $PWD/logs/watch.log"
    ;;
  uninstall)
    systemctl --user disable --now btcwatch.timer 2>/dev/null || true
    rm -f "$DEST/btcwatch.service" "$DEST/btcwatch.timer"
    systemctl --user daemon-reload
    echo "解除しました"
    ;;
  status)
    systemctl --user list-timers btcwatch.timer --no-pager || true
    systemctl --user status btcwatch.service --no-pager -n 20 || true
    ;;
  *) echo "usage: $0 [install|uninstall|status]" >&2; exit 2 ;;
esac
