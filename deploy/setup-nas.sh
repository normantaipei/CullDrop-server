#!/usr/bin/env bash
# 在 Proxmox 主機上把 NAS 接給 Culldrop（用 root 跑）：掛 SMB 到主機、綁進 LXC、打開備份。
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/culldrop-server/main/deploy/setup-nas.sh | NAS_SHARE=//192.168.1.10/share/photos NAS_USER=photos bash
# 無特權 LXC 不能自己掛 SMB，所以掛在主機再用 mp0 綁進去。密碼存在主機的 /etc/culldrop-nas.cred（600），不進 repo。
# 可覆寫：CT_NAME、NAS_PASSWORD（不給就問）、NAS_MOUNT
set -euo pipefail

# 提示語言：CULLDROP_LANG=zh-Hant|en|ja，沒給就看系統語言。也會寫成伺服器的預設語言。
case "${CULLDROP_LANG:-${LC_ALL:-${LANG:-}}}" in zh*) UI_LANG=zh-Hant ;; ja*) UI_LANG=ja ;; *) UI_LANG=en ;; esac
t() { case "$UI_LANG" in zh-Hant) printf '%s' "$1" ;; ja) printf '%s' "$3" ;; *) printf '%s' "$2" ;; esac; }
die() { t "$1" "$2" "$3" >&2; echo >&2; exit 1; }

NAME=${CT_NAME:-culldrop}
MOUNT=${NAS_MOUNT:-/mnt/culldrop-nas}
CRED=${NAS_CRED:-/etc/culldrop-nas.cred}
FSTAB=${NAS_FSTAB:-/etc/fstab}
CT_MOUNT=/mnt/nas
INSTALL_URL=${CULLDROP_INSTALL_URL:-https://raw.githubusercontent.com/normantaipei/culldrop-server/main/deploy/install.sh}

main() {
  [ "$(id -u)" -eq 0 ] || die "請用 root 執行" "Run this as root." "root で実行してください。"
  command -v pct >/dev/null || die "這支要在 Proxmox 主機上跑" "Run this on a Proxmox host." "Proxmox ホストで実行してください。"
  command -v mount.cifs >/dev/null || die "主機沒有 mount.cifs（cifs-utils）" "mount.cifs (cifs-utils) is missing on the host." "ホストに mount.cifs(cifs-utils)がありません。"
  [ -n "${NAS_SHARE:-}" ] && [ -n "${NAS_USER:-}" ] || die "要給 NAS_SHARE=//IP/共用/子資料夾 和 NAS_USER=帳號" "Set NAS_SHARE=//IP/share/subfolder and NAS_USER=account." "NAS_SHARE=//IP/共有/サブフォルダ と NAS_USER=アカウント を指定してください。"

  local id
  id=$(pct list | awk -v n="$NAME" 'NR>1 && $NF==n {id=$1} END {print id}')
  [ -n "$id" ] || die "找不到 LXC ${NAME}，先跑 install-lxc.sh" "LXC ${NAME} not found. Run install-lxc.sh first." "LXC ${NAME} が見つかりません。先に install-lxc.sh を実行してください。"
  pct status "$id" | grep -q running || pct start "$id"

  local password=${NAS_PASSWORD:-}
  if [ -z "$password" ]; then
    read -rsp "$(t "NAS 密碼（${NAS_USER}）：" "NAS password (${NAS_USER}): " "NAS パスワード(${NAS_USER}): ")" password </dev/tty
    echo
  fi
  (umask 077; printf 'username=%s\npassword=%s\n' "$NAS_USER" "$password" > "$CRED")

  # 無特權 LXC 的 uid 在主機上要加 100000，culldrop 才寫得進去
  local uid host_id opts
  uid=$(pct exec "$id" -- id -u culldrop)
  host_id=$((100000 + uid))
  opts="credentials=${CRED},uid=${host_id},gid=${host_id},file_mode=0664,dir_mode=0775,iocharset=utf8,_netdev,nofail"

  mkdir -p "$MOUNT"
  if mountpoint -q "$MOUNT"; then umount "$MOUNT"; fi
  local kept
  kept=$(awk -v m=" ${MOUNT} cifs " 'index($0, m) == 0' "$FSTAB")
  printf '%s\n%s\n' "$kept" "${NAS_SHARE} ${MOUNT} cifs ${opts} 0 0" > "$FSTAB"
  systemctl daemon-reload
  mount "$MOUNT" || die "掛不上 ${NAS_SHARE}，檢查路徑、帳號密碼" "Can't mount ${NAS_SHARE}. Check the path, account, and password." "${NAS_SHARE} をマウントできません。パス、アカウント、パスワードを確認してください。"
  touch "${MOUNT}/.culldrop-nas"   # 伺服器看到這個才算 NAS 掛上

  # 先存起來再比：grep -q 找到就關管線，pipefail 下可能誤判成沒找到
  local cfg
  cfg=$(pct config "$id")
  if ! grep -q "^mp0: ${MOUNT}," <<<"$cfg"; then
    if grep -q '^mp0:' <<<"$cfg"; then
      die "LXC ${id} 的 mp0 已經被用掉了，手動綁 ${MOUNT} 到 ${CT_MOUNT}" "mp0 on LXC ${id} is already in use. Bind ${MOUNT} to ${CT_MOUNT} manually." "LXC ${id} の mp0 は使用中です。${MOUNT} を ${CT_MOUNT} に手動でバインドしてください。"
    fi
    pct set "$id" -mp0 "${MOUNT},mp=${CT_MOUNT}"
  fi
  pct reboot "$id"
  local ok=""
  for _ in $(seq 1 60); do
    if pct exec "$id" -- test -f "${CT_MOUNT}/.culldrop-nas" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  [ -n "$ok" ] || die "LXC 裡看不到 ${CT_MOUNT}/.culldrop-nas" "${CT_MOUNT}/.culldrop-nas isn't visible inside the LXC." "LXC 内に ${CT_MOUNT}/.culldrop-nas が見えません。"

  # 掛載點比網路早好：重開後要等連得到 install.sh
  ok=""
  for _ in $(seq 1 60); do
    if pct exec "$id" -- curl -fsS -o /dev/null -m 5 "$INSTALL_URL" >/dev/null 2>&1; then ok=1; break; fi
    sleep 2
  done
  [ -n "$ok" ] || die "LXC ${id} 連不到 ${INSTALL_URL}" "LXC ${id} can't reach ${INSTALL_URL}." "LXC ${id} から ${INSTALL_URL} に接続できません。"

  pct exec "$id" -- bash -c "set -o pipefail; grep -q '^CULLDROP_NAS_DIR=' /etc/culldrop.env || echo 'CULLDROP_NAS_DIR=${CT_MOUNT}' >> /etc/culldrop.env; curl -fsSL '$INSTALL_URL' | CULLDROP_LANG='$UI_LANG' bash"
  echo "✔ $(t "NAS 備份已開啟" "NAS backup is on" "NAS バックアップをオンにしました")：${NAS_SHARE}"
}

main "$@"
