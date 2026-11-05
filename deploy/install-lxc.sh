#!/usr/bin/env bash
# 在 Proxmox 主機上開一台 LXC 並裝好 Culldrop 伺服器（用 root 跑）：
#   curl -fsSL https://raw.githubusercontent.com/normantaipei/culldrop-server/main/deploy/install-lxc.sh | bash
# 重跑 = 更新同一台（hostname 相同就沿用），token 和照片不會動。
# 可覆寫：CT_NAME、CT_DISK_GB（照片都存這裡）、CT_CORES、CT_MEMORY、CT_NET、CT_STORAGE、CT_TEMPLATE_STORAGE
# 對外網址：CULLDROP_PUBLIC_URL=https://…（反向代理時再加 CULLDROP_TRUSTED_PROXIES=代理 IP），會寫進 LXC 的設定
#   例：curl -fsSL … | CT_DISK_GB=500 CT_NET=name=eth0,bridge=vmbr0,ip=192.168.1.50/24,gw=192.168.1.1 bash
set -euo pipefail

# 提示語言：CULLDROP_LANG=zh-Hant|en|ja，沒給就看系統語言。也會寫成伺服器的預設語言。
case "${CULLDROP_LANG:-${LC_ALL:-${LANG:-}}}" in zh*) UI_LANG=zh-Hant ;; ja*) UI_LANG=ja ;; *) UI_LANG=en ;; esac
t() { case "$UI_LANG" in zh-Hant) printf '%s' "$1" ;; ja) printf '%s' "$3" ;; *) printf '%s' "$2" ;; esac; }
die() { t "$1" "$2" "$3" >&2; echo >&2; exit 1; }

NAME=${CT_NAME:-culldrop}
DISK_GB=${CT_DISK_GB:-200}
CORES=${CT_CORES:-2}
MEMORY=${CT_MEMORY:-2048}
NET=${CT_NET:-name=eth0,bridge=vmbr0,ip=dhcp}
INSTALL_URL=${CULLDROP_INSTALL_URL:-https://raw.githubusercontent.com/normantaipei/culldrop-server/main/deploy/install.sh}

# 支援這種內容、可用空間最大的儲存
storage_for() {
  pvesm status --content "$1" | awk 'NR>1 && $3=="active" {print $6, $1}' | sort -n | tail -1 | awk '{print $2}'
}

# 同一版有 amd64 / arm64 兩種，要挑主機的架構
latest_template() {
  pveam available --section system | awk -v v="debian-$1-standard" -v a="_${ARCH}." 'index($2, v) && index($2, a) {print $2}' | sort -V | tail -1
}

find_ct() { pct list | awk -v n="$NAME" 'NR>1 && $NF==n {id=$1} END {print id}'; }

create_ct() {
  local rootfs tmpl_store tpl id
  rootfs=${CT_STORAGE:-$(storage_for rootdir)}
  tmpl_store=${CT_TEMPLATE_STORAGE:-$(storage_for vztmpl)}
  [ -n "$rootfs" ] || die "找不到能放容器的儲存，用 CT_STORAGE 指定（看 pvesm status）" "No storage for containers found. Set CT_STORAGE (see pvesm status)." "コンテナ用のストレージが見つかりません。CT_STORAGE で指定してください(pvesm status を確認)。"
  [ -n "$tmpl_store" ] || die "找不到能放範本的儲存，用 CT_TEMPLATE_STORAGE 指定" "No storage for templates found. Set CT_TEMPLATE_STORAGE." "テンプレート用のストレージが見つかりません。CT_TEMPLATE_STORAGE で指定してください。"

  pveam update >/dev/null
  tpl=$(latest_template 13)
  [ -n "$tpl" ] || tpl=$(latest_template 12)
  [ -n "$tpl" ] || die "找不到 ${ARCH} 的 Debian 範本" "No Debian template found for ${ARCH}." "${ARCH} 用の Debian テンプレートが見つかりません。"
  if ! pveam list "$tmpl_store" | awk -v t="$tpl" 'index($0, t) {f=1} END {exit !f}'; then
    pveam download "$tmpl_store" "$tpl" >&2
  fi

  id=$(pvesh get /cluster/nextid)
  t "建立 LXC ${id}（${NAME}）：$rootfs ${DISK_GB}GB，$tpl" "Creating LXC ${id} (${NAME}): $rootfs ${DISK_GB}GB, $tpl" "LXC ${id}(${NAME})を作成します: $rootfs ${DISK_GB}GB、$tpl" >&2; echo >&2
  pct create "$id" "$tmpl_store:vztmpl/$tpl" --hostname "$NAME" --cores "$CORES" --memory "$MEMORY" \
    --rootfs "$rootfs:$DISK_GB" --net0 "$NET" --unprivileged 1 --features nesting=1 --onboot 1 >&2
  echo "$id"
}

main() {
  [ "$(id -u)" -eq 0 ] || die "請用 root 執行" "Run this as root." "root で実行してください。"
  command -v pct >/dev/null || die "這支要在 Proxmox 主機上跑；LXC 或一般主機用 deploy/install.sh" "Run this on a Proxmox host. For an LXC or a regular server, use deploy/install.sh." "Proxmox ホストで実行してください。LXC や通常のサーバーでは deploy/install.sh を使います。"

  ARCH=$(dpkg --print-architecture)
  local id
  id=$(find_ct)
  if [ -n "$id" ]; then
    local ct_arch
    ct_arch=$(pct config "$id" | awk '$1=="arch:" {print $2}')
    if [ -n "$ct_arch" ] && [ "$ct_arch" != "$ARCH" ]; then
      die "LXC ${id} 是 ${ct_arch}，主機是 ${ARCH}，跑不起來。確定裡面沒資料後刪掉再重跑：pct destroy ${id}" "LXC ${id} is ${ct_arch} but the host is ${ARCH}, so it can't run. If it holds no data, delete it and run this again: pct destroy ${id}" "LXC ${id} は ${ct_arch}、ホストは ${ARCH} のため実行できません。データがないことを確認して削除し、再実行してください: pct destroy ${id}"
    fi
    t "沿用 LXC ${id}（${NAME}）" "Using existing LXC ${id} (${NAME})" "既存の LXC ${id}(${NAME})を使用します"; echo
  else
    id=$(create_ct)
  fi
  pct status "$id" | grep -q running || pct start "$id"

  local online=""
  for _ in $(seq 1 60); do
    if pct exec "$id" -- getent hosts deb.debian.org >/dev/null 2>&1; then online=1; break; fi
    sleep 2
  done
  [ -n "$online" ] || die "LXC $id 連不上網路，檢查 CT_NET（bridge、DHCP）" "LXC $id has no network. Check CT_NET (bridge, DHCP)." "LXC $id がネットワークに接続できません。CT_NET(ブリッジ、DHCP)を確認してください。"

  pct exec "$id" -- bash -c "set -o pipefail; apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates && curl -fsSL '$INSTALL_URL' | CULLDROP_LANG='$UI_LANG' CULLDROP_PUBLIC_URL='${CULLDROP_PUBLIC_URL:-}' CULLDROP_TRUSTED_PROXIES='${CULLDROP_TRUSTED_PROXIES:-}' bash"
  echo "  $(t "進去看" "Open a shell" "シェルを開く")：pct enter $id"
}

main "$@"
