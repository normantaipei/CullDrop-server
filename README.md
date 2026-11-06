# Culldrop Server

Self-hosted server and client gallery for the [Culldrop](https://normantaipei.github.io/CullDrop-server/) iOS app.

**English** · [繁體中文](README.zh-Hant.md) · [日本語](README.ja.md)

One server holds many albums. The app manages them with a device token, and your clients view and download photos from a share link, with no account or token. The app works without a server; you only need one for gallery links and off-device storage.

## Install

Debian or Ubuntu, as root. It prints the server URL and device token at the end.

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install.sh | bash
```

Enter both in the app under **Settings → Server**. Include `http://` or `https://` in the URL.

Running it again updates the program. Your tokens and photos are not touched.

### Proxmox

On the Proxmox host, this creates an LXC and installs into it. The default disk is 200 GB, and photos are stored there.

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install-lxc.sh | CT_DISK_GB=200 bash
```

For a fixed IP, add `CT_NET=name=eth0,bridge=vmbr0,ip=192.168.1.50/24,gw=192.168.1.1`.

### Docker

```bash
git clone https://github.com/normantaipei/CullDrop-server.git && cd CullDrop-server
CULLDROP_DEVICE_TOKENS="iphone:$(openssl rand -hex 24)" docker compose -f deploy/docker-compose.yml up -d
```

### Manual

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CULLDROP_DEVICE_TOKENS="iphone:$(openssl rand -hex 24)" CULLDROP_DATA=./data \
  .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Access from outside your network

Share links need a public address, and iOS only allows `https://` outside your local network. Put a reverse proxy such as Caddy or Nginx in front of port 8000, then install with:

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/install.sh | CULLDROP_PUBLIC_URL=https://photos.example.com CULLDROP_TRUSTED_PROXIES=192.168.1.10 bash
```

`CULLDROP_TRUSTED_PROXIES` is your proxy's IP, so login blocking sees the real source address.

## NAS backup

Received files are copied to `<NAS>/<album>/<original filename>`, with ratings and tags written to the JPG and to a `.xmp` file beside each RAW. The app deletes its local copy only after the NAS has the file. Deleting in the app does not touch the NAS.

On a Proxmox host, this mounts SMB on the host and binds it into the LXC. It asks for the NAS password.

```bash
curl -fsSL https://raw.githubusercontent.com/normantaipei/CullDrop-server/main/deploy/setup-nas.sh | NAS_SHARE=//192.168.1.10/share/photos NAS_USER=photos bash
```

On other systems, mount the share yourself, create an empty `.culldrop-nas` file in it, and set `CULLDROP_NAS_DIR`.

## Language

The gallery follows the visitor's browser language (English, 繁體中文, 日本語), and `?lang=ja` forces one. Installer messages follow `CULLDROP_LANG` or your system language, and the installer saves it as the server default for link previews.

## Settings

Set these in `/etc/culldrop.env`, then run `systemctl restart culldrop`.

| Variable | Purpose | Default |
|---|---|---|
| `CULLDROP_DEVICE_TOKENS` | `name:token` pairs, comma separated | Required |
| `CULLDROP_DATA` | Data directory | `./data` |
| `CULLDROP_PUBLIC_URL` | Public URL, used for link previews | |
| `CULLDROP_LANG` | Language when a request has none: `en`, `ja`, `zh-Hant` | `zh-Hant` |
| `CULLDROP_SITE_NAME` | Gallery title suffix | Depends on language |
| `CULLDROP_MIN_FREE_GB` | Stop accepting uploads below this free space | `2` |
| `CULLDROP_MAX_INFLIGHT` | Simultaneous uploads per device | `4` |
| `CULLDROP_DEVICE_NETS` | Networks allowed to use device APIs | Any |
| `CULLDROP_TRUSTED_PROXIES` | Reverse proxy IPs or CIDRs | |
| `CULLDROP_NAS_DIR` | NAS backup directory | |

## Your responsibility

You run this server, so you are responsible for the photos on it, including the people in them. The developer has no access to your server. Anyone with a share link can view that album, so keep the links private and stop sharing when a job is done.

## Help

Report problems in [Issues](https://github.com/normantaipei/CullDrop-server/issues). Remove tokens and private URLs from anything you post.

## License

[MIT](LICENSE)
