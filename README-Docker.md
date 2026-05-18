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
   - `LOGIN_WEBHOOK_URL` if you use login/account management
   - `LOGIN_PASSWORD_SECRET` if you use manual login/register/update user
   - `GOOGLE_CLIENT_ID` if you use Google sign-in
   - `MICROSOFT_CLIENT_ID` if you use Microsoft sign-in
   You can set them in `.env`.
   Docker Compose now loads `.env` directly into the container through `env_file`.
   If `.env` does not exist, Docker Compose and the app both fall back to `.env.example`.
3. Start the service:

```bash
docker compose up -d --build
```

On first start, the app will automatically create runtime files inside `./data`:

- `data/webhook-settings.json`
- `data/upload-history.json`

Important:
- When running in Docker, webhook/auth values from `.env` are passed into the container.
- Both `docker-compose.yml` and `docker-compose.server.yml` load `./.env` first and `./.env.example` as fallback.
- If both `.env` and `data/webhook-settings.json` contain values, the `.env` values take priority at runtime.
- For env-managed fields (`N8N_WEBHOOK_URL`, `EXPORT_DOC_WEBHOOK_URL`, `COMMAND_WEBHOOK_URL`, `LOGIN_WEBHOOK_URL`, `GOOGLE_CLIENT_ID`, `MICROSOFT_CLIENT_ID`, `MICROSOFT_AUTHORITY`), the app will read from environment first and will not let the persisted `data/webhook-settings.json` override them.
- When you save settings from the web UI, env-managed values are not persisted back into the runtime settings file. This avoids stale Docker data overriding Linux server environment variables later.
- This is useful for Linux servers where you want Docker deployment to always follow environment variables.

4. Open:

```text
http://localhost:3000
```

## Server deployment from GitHub image

Use this mode when you want the server to run from:

```text
https://github.com/natthawut-s-gif/Wed_Send_T0.git
```

and automatically update after new code is pushed to `main`.

### How it works

1. GitHub Actions builds and pushes:

```text
ghcr.io/natthawut-s-gif/wed_send_t0:latest
```

2. Your server runs `docker-compose.server.yml`
3. `watchtower` checks for a newer image and restarts the app automatically

### Server start

1. Create `.env` if needed:

```bash
cp .env.example .env
```

2. Start from the published image:

```bash
docker compose -f docker-compose.server.yml up -d
```

3. Open:

```text
http://localhost:3000
```

### Auto update behavior

- Push new code to `main`
- GitHub Actions publishes a new `latest` image to GHCR
- `watchtower` pulls the new image automatically
- The container restarts with the updated code

### Check server image deployment

```bash
docker compose -f docker-compose.server.yml ps
docker compose -f docker-compose.server.yml logs -f
```

### Stop server image deployment

```bash
docker compose -f docker-compose.server.yml down
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
