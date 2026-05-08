# Cloudflare Tunnel Setup

This folder contains a local-managed Cloudflare Tunnel configuration for publishing the app through a real public hostname such as `doc.yourdomain.com`.

Files:

- `config.yml` - generated tunnel config used by `cloudflared`
- `../cloudflared-settings.json` - editable settings consumed by `manage_web_ui.py`

Required steps:

1. Install `cloudflared`
2. Authenticate:

```bash
cloudflared login
```

3. Create a named tunnel:

```bash
cloudflared tunnel create doc-extraction-tunnel
```

4. Replace these values in `cloudflared-settings.json`:

- `tunnel_id`
- `hostname`
- `credentials_file`
- `executable_path` if `cloudflared` is not in `PATH`

5. Create the DNS route:

```bash
cloudflared tunnel route dns YOUR-TUNNEL-UUID doc.yourdomain.com
```

6. Run the tunnel with this config:

```bash
cloudflared tunnel --config "cloudflared/config.yml" --loglevel info --metrics 127.0.0.1:20241 run YOUR-TUNNEL-UUID
```

7. Or use the Python controller:

```bash
python manage_web.py tunnel-start
python manage_web.py tunnel-status
python manage_web.py tunnel-stop
```

Cloudflare references:

- https://developers.cloudflare.com/tunnel/advanced/local-management/configuration-file/
- https://developers.cloudflare.com/tunnel/configuration/
- https://developers.cloudflare.com/tunnel/setup/
