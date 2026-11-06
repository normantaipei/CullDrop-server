# Culldrop Server

[Culldrop](https://normantaipei.github.io/CullDrop-server/zh-Hant/) iOS app 的自架伺服器與返圖頁。

[English](README.md) · **繁體中文** · [日本語](README.ja.md)

一台伺服器放多本相簿。app 用裝置 token 管理相簿，客戶用分享連結看圖、下載，不需要帳號或 token。app 不接伺服器也能用，要返圖連結或把照片存到手機以外才需要。

## 安裝

Debian 或 Ubuntu，用 root 跑，最後會印出伺服器網址和裝置 token。

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install.sh | bash
```

把兩個值填進 app 的「設定 → 伺服器」，網址要連 `http://` 或 `https://` 一起填。

重跑就是更新程式，token 和照片不會動。

### Proxmox

在 Proxmox 主機上跑，會開一台 LXC 再裝進去。磁碟預設 200 GB，照片都存在裡面。

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install-lxc.sh | CT_DISK_GB=200 bash
```

要固定 IP 就加 `CT_NET=name=eth0,bridge=vmbr0,ip=192.168.1.50/24,gw=192.168.1.1`。

### Docker

```bash
git clone https://github.com/normantaipei/CullDrop-server.git && cd CullDrop-server
CULLDROP_DEVICE_TOKENS="iphone:$(openssl rand -hex 24)" docker compose -f deploy/docker-compose.yml up -d
```

### 手動

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CULLDROP_DEVICE_TOKENS="iphone:$(openssl rand -hex 24)" CULLDROP_DATA=./data \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 從外部連線

分享連結要有對外網址，而 iOS 在區網以外只允許 `https://`。在 8000 port 前面放 Caddy 或 Nginx 這類反向代理，再這樣安裝：

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install.sh | CULLDROP_PUBLIC_URL=https://photos.example.com CULLDROP_TRUSTED_PROXIES=192.168.1.10 bash
```

`CULLDROP_TRUSTED_PROXIES` 填反向代理的 IP，登入封鎖才看得到真實來源。

## NAS 備份

收到的檔案會複製到 `<NAS>/<相簿>/<原檔名>`，星等與標籤寫進 JPG，RAW 則寫進同名 `.xmp`。NAS 收到後 app 才刪本機檔。在 app 刪除不會動到 NAS。

在 Proxmox 主機上跑，會把 SMB 掛在主機再綁進 LXC，過程會問 NAS 密碼。

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/setup-nas.sh | NAS_SHARE=//192.168.1.10/share/photos NAS_USER=photos bash
```

其他系統請自己掛載，在裡面建立空檔案 `.culldrop-nas`，再設定 `CULLDROP_NAS_DIR`。

## 語言

返圖頁依訪客的瀏覽器語言顯示（English、繁體中文、日本語），也可以用 `?lang=ja` 指定。安裝提示依 `CULLDROP_LANG` 或系統語言，並存成伺服器預設語言，用於連結預覽。

## 設定

寫在 `/etc/culldrop.env`，改完跑 `systemctl restart culldrop`。

| 變數 | 用途 | 預設 |
|---|---|---|
| `CULLDROP_DEVICE_TOKENS` | `名稱:token`，多組用逗號分隔 | 必填 |
| `CULLDROP_DATA` | 資料目錄 | `./data` |
| `CULLDROP_PUBLIC_URL` | 對外網址，連結預覽會用到 | |
| `CULLDROP_LANG` | 請求沒帶語言時用：`en`、`ja`、`zh-Hant` | `zh-Hant` |
| `CULLDROP_SITE_NAME` | 返圖頁標題後綴 | 依語言 |
| `CULLDROP_MIN_FREE_GB` | 空間低於此值就停止收件 | `2` |
| `CULLDROP_MAX_INFLIGHT` | 每台裝置的同時上傳數 | `4` |
| `CULLDROP_DEVICE_NETS` | 允許使用裝置 API 的網段 | 不限 |
| `CULLDROP_TRUSTED_PROXIES` | 反向代理的 IP 或 CIDR | |
| `CULLDROP_NAS_DIR` | NAS 備份目錄 | |

## 你的責任

伺服器由你自己架設，上面的照片和照片裡的人都由你負責。開發者無法存取你的伺服器。拿到分享連結的人都看得到該相簿，請妥善保管，案子結束後記得停止分享。

## 協助

問題請開 [Issue](https://github.com/normantaipei/CullDrop-server/issues)。貼內容前請拿掉 token 和私人網址。

## 授權

[MIT](LICENSE)
