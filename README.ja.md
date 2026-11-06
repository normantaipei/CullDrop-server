# Culldrop Server

iOS アプリ [Culldrop](https://normantaipei.github.io/CullDrop-server/ja/) のセルフホスト用サーバーとギャラリーです。

[English](README.md) · [繁體中文](README.zh-Hant.md) · **日本語**

1 台のサーバーで複数のアルバムを扱えます。アプリはデバイストークンでアルバムを管理し、クライアントは共有リンクから写真を閲覧・ダウンロードできます(アカウントやトークンは不要です)。アプリはサーバーなしでも使えます。ギャラリーリンクや端末外への保存が必要なときだけ設置してください。

## インストール

Debian または Ubuntu で、root として実行します。最後にサーバー URL とデバイストークンが表示されます。

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install.sh | bash
```

アプリの「設定 → サーバー」に両方を入力してください。URL には `http://` または `https://` も含めます。

再実行するとプログラムを更新します。トークンと写真はそのままです。

### Proxmox

Proxmox ホストで実行すると、LXC を作成してインストールします。ディスクの初期値は 200 GB で、写真はここに保存されます。

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install-lxc.sh | CT_DISK_GB=200 bash
```

固定 IP にするには `CT_NET=name=eth0,bridge=vmbr0,ip=192.168.1.50/24,gw=192.168.1.1` を追加します。

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

## 外部からの接続

共有リンクには公開 URL が必要で、iOS はローカルネットワーク外では `https://` のみ許可します。ポート 8000 の前に Caddy や Nginx などのリバースプロキシを置き、次のようにインストールしてください。

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install.sh | CULLDROP_PUBLIC_URL=https://photos.example.com CULLDROP_TRUSTED_PROXIES=192.168.1.10 bash
```

`CULLDROP_TRUSTED_PROXIES` にはプロキシの IP を指定します。これによりログインのブロックが実際の接続元を判別できます。

## NAS バックアップ

受信したファイルは `<NAS>/<アルバム>/<元のファイル名>` にコピーされ、星評価とタグは JPG に、RAW は同名の `.xmp` に書き込まれます。アプリは NAS の受信後に端末のファイルを削除します。アプリで削除しても NAS には影響しません。

Proxmox ホストで実行すると、SMB をホストにマウントして LXC にバインドします。途中で NAS のパスワードを尋ねます。

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/setup-nas.sh | NAS_SHARE=//192.168.1.10/share/photos NAS_USER=photos bash
```

その他のシステムでは共有を自分でマウントし、空のファイル `.culldrop-nas` を作成して `CULLDROP_NAS_DIR` を設定してください。

## 言語

ギャラリーは閲覧者のブラウザの言語(English、繁體中文、日本語)で表示され、`?lang=ja` で指定もできます。インストーラーのメッセージは `CULLDROP_LANG` またはシステムの言語に従い、その言語をリンクプレビュー用のサーバー既定値として保存します。

## 設定

`/etc/culldrop.env` に記述し、`systemctl restart culldrop` を実行します。

| 変数 | 用途 | 初期値 |
|---|---|---|
| `CULLDROP_DEVICE_TOKENS` | `名前:トークン`(複数はカンマ区切り) | 必須 |
| `CULLDROP_DATA` | データディレクトリ | `./data` |
| `CULLDROP_PUBLIC_URL` | 公開 URL(リンクプレビューに使用) | |
| `CULLDROP_LANG` | 言語指定のないリクエストの言語: `en`、`ja`、`zh-Hant` | `zh-Hant` |
| `CULLDROP_SITE_NAME` | ギャラリーのタイトル末尾 | 言語による |
| `CULLDROP_MIN_FREE_GB` | 空き容量がこの値を下回ると受信を停止 | `2` |
| `CULLDROP_MAX_INFLIGHT` | デバイスごとの同時アップロード数 | `4` |
| `CULLDROP_DEVICE_NETS` | デバイス API を許可するネットワーク | 制限なし |
| `CULLDROP_TRUSTED_PROXIES` | リバースプロキシの IP または CIDR | |
| `CULLDROP_NAS_DIR` | NAS バックアップ先 | |

## 利用者の責任

サーバーは利用者自身が運用するため、保存された写真と写っている人物については利用者が責任を負います。開発者はサーバーにアクセスできません。共有リンクを知っている人は誰でもそのアルバムを閲覧できるので、リンクは慎重に扱い、案件が終わったら共有を停止してください。

## サポート

csps40608a@gmail.com へメールするか、[Issues](https://github.com/normantaipei/CullDrop-server/issues) で報告してください。投稿前にトークンと非公開の URL を削除してください。

## ライセンス

[PolyForm Noncommercial 1.0.0](LICENSE)。個人利用や趣味などの非商用利用は無料です。このライセンスでは商用利用はできません。商用利用については csps40608a@gmail.com へご連絡ください。
