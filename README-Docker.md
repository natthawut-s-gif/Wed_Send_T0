# Docker Deployment

This project can run on Linux or Windows servers with Docker.

## Requirements

- Docker Engine 24+ or Docker Desktop
- Docker Compose v2

## First start

1. Optional: create `.env` from the example if you want to override defaults:

```bash
cp .env.example .env
```

2. Set at least:
   - `N8N_WEBHOOK_URL`
   - `EXPORT_DOC_WEBHOOK_URL` if you use document actions
   - `COMMAND_WEBHOOK_URL` if you use command chat
   You can set them in `.env`, or pass them from the shell when running Docker Compose.
3. Start the service:

```bash
docker compose up -d --build
```

On first start, the app will automatically create runtime files inside `./data`:

- `data/webhook-settings.json`
- `data/upload-history.json`

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

These files stay on the host inside `./data` and are mounted into the container:

- `data/webhook-settings.json`
- `data/upload-history.json`

That means webhook settings and upload history remain after container restarts.
