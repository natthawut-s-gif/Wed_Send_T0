# Docker Deployment

This project can run on Linux or Windows servers with Docker.

## Requirements

- Docker Engine 24+ or Docker Desktop
- Docker Compose v2

## First start

1. Create these local runtime files:

```bash
cp .env.example .env
cp webhook-settings.example.json webhook-settings.json
cp upload-history.example.json upload-history.json
```

If you use a named Cloudflare tunnel:

```bash
cp cloudflared-settings.example.json cloudflared-settings.json
```

2. Set at least:
   - `N8N_WEBHOOK_URL`
   - `EXPORT_DOC_WEBHOOK_URL` if you use document actions
   - `COMMAND_WEBHOOK_URL` if you use command chat
3. Start the service:

```bash
docker compose up -d --build
```

4. Open:

```text
http://localhost:3000
```

## Update code

If this project is deployed from a git clone:

```bash
git pull
docker compose up -d --build
```

If you only need to restart the current code:

```bash
docker compose restart
```

## Check status

```bash
docker compose ps
docker compose logs -f
```

## Stop

```bash
docker compose down
```

## Persisted files

These files stay on the host and are mounted into the container:

- `webhook-settings.json`
- `upload-history.json`

That means webhook settings and upload history remain after container restarts.
