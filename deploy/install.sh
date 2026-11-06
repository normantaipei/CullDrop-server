#!/usr/bin/env bash
# Culldrop 伺服器一行安裝（Debian / Ubuntu，用 root 跑）：
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install.sh | bash
# 重跑 = 更新程式；/etc/culldrop.env（token）和 /srv/culldrop（照片）不會動。
# 可給：CULLDROP_LANG、CULLDROP_PUBLIC_URL、CULLDROP_TRUSTED_PROXIES（有給才寫進 env，重跑也會更新）
set -euo pipefail

REPO=${CULLDROP_REPO:-https://github.com/normantaipei/CullDrop-server.git}
APP=/opt/culldrop
DATA=/srv/culldrop
ENV_FILE=/etc/culldrop.env

# 提示語言：CULLDROP_LANG=zh-Hant|en|ja，沒給就看系統語言。也會寫成伺服器的預設語言。
case "${CULLDROP_LANG:-${LC_ALL:-${LANG:-}}}" in zh*) UI_LANG=zh-Hant ;; ja*) UI_LANG=ja ;; *) UI_LANG=en ;; esac
t() { case "$UI_LANG" in zh-Hant) printf '%s' "$1" ;; ja) printf '%s' "$3" ;; *) printf '%s' "$2" ;; esac; }
die() { t "$1" "$2" "$3" >&2; echo >&2; exit 1; }

# 設定一個值：有就改，沒有就加
set_env() {
  [ -n "$2" ] || return 0
  if grep -q "^$1=" "$ENV_FILE"; then
    local kept
    kept=$(grep -v "^$1=" "$ENV_FILE")
    printf '%s\n%s=%s\n' "$kept" "$1" "$2" > "$ENV_FILE"
  else
    echo "$1=$2" >> "$ENV_FILE"
  fi
}

main() {
  [ "$(id -u)" -eq 0 ] || die "請用 root 執行" "Run this as root." "root で実行してください。"
  [ ! -d /etc/pve ] || die "這是 Proxmox 主機，不要裝在這裡；改跑 deploy/install-lxc.sh 開一台 LXC" "This is a Proxmox host. Don't install here; run deploy/install-lxc.sh to create an LXC." "ここは Proxmox ホストです。ここにはインストールせず、deploy/install-lxc.sh で LXC を作成してください。"

  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv openssl curl libimage-exiftool-perl

  id culldrop >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin culldrop
  # 不用 pull：換過來源時歷史不相關，pull 會失敗。.venv 沒進版控，不受影響
  if [ -d "$APP/.git" ]; then
    git -C "$APP" remote set-url origin "$REPO"
    git -C "$APP" fetch -q --depth 1 origin HEAD
    git -C "$APP" reset -q --hard FETCH_HEAD
  else
    git clone -q --depth 1 "$REPO" "$APP"
  fi
  [ -x "$APP/server/.venv/bin/python" ] || python3 -m venv "$APP/server/.venv"
  "$APP/server/.venv/bin/pip" install -q -r "$APP/server/requirements.txt"
  mkdir -p "$DATA"
  chown culldrop:culldrop "$DATA"

  if [ ! -f "$ENV_FILE" ]; then
    local ip token
    ip=$(hostname -I | tr ' ' '\n' | grep -m1 '\.' || true)   # IPv6 可能排在前面
    [ -n "$ip" ] || die "拿不到 IPv4，檢查網路" "No IPv4 address. Check the network." "IPv4 アドレスを取得できません。ネットワークを確認してください。"
    token=$(openssl rand -hex 24)
    (umask 077; cat > "$ENV_FILE" <<EOF
CULLDROP_DATA=$DATA
CULLDROP_DEVICE_TOKENS=iphone:$token
CULLDROP_PUBLIC_URL=http://$ip:8000
CULLDROP_LANG=$UI_LANG
CULLDROP_MIN_FREE_GB=2
CULLDROP_MAX_INFLIGHT=4
EOF
    )
  fi
  set_env CULLDROP_LANG "${CULLDROP_LANG:-}"
  set_env CULLDROP_PUBLIC_URL "${CULLDROP_PUBLIC_URL:-}"
  set_env CULLDROP_TRUSTED_PROXIES "${CULLDROP_TRUSTED_PROXIES:-}"

  cp "$APP/deploy/culldrop.service" /etc/systemd/system/culldrop.service
  systemctl daemon-reload
  systemctl enable -q culldrop
  systemctl restart culldrop

  local url token
  url=$(sed -n 's/^CULLDROP_PUBLIC_URL=//p' "$ENV_FILE")
  token=$(sed -n 's/^CULLDROP_DEVICE_TOKENS=[^:]*:\([^,]*\).*/\1/p' "$ENV_FILE")
  for _ in $(seq 1 30); do
    if curl -fsS -o /dev/null -H "Authorization: Bearer $token" http://127.0.0.1:8000/api/manage/me 2>/dev/null; then
      echo
      echo "✔ $(t "Culldrop 伺服器已啟動" "Culldrop server is running" "Culldrop サーバーを起動しました")"
      echo "  $(t "伺服器網址" "Server URL" "サーバー URL")：$url"
      echo "  $(t "裝置 token" "Device token" "デバイストークン")：$token"
      return
    fi
    sleep 1
  done
  die "✘ 伺服器沒有回應，看 journalctl -u culldrop -n 50" "✘ The server isn't responding. See journalctl -u culldrop -n 50" "✘ サーバーが応答しません。journalctl -u culldrop -n 50 を確認してください"
}

main "$@"
