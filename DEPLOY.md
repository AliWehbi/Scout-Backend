# Deploying the backend

Pushing a version tag builds the Docker image, pushes it to GitHub Container Registry and
deploys it to the production server (`mydroplet`).

```bash
git tag v1.0.0
git push origin v1.0.0
```

```
tag v*  ->  GitHub Actions
              1. test     ruff + pytest on Postgres (a failure stops here)
              2. build    image -> ghcr.io/aliwehbi/scout-backend:<tag> and :latest
              3. deploy   copy docker-compose.yml + nginx/ to ~/scout-backend on the server,
                          docker compose pull + up -d, wait for /api/health/

server:  host nginx (TLS, api domain) -> 127.0.0.1:8001 -> scout_nginx -> scout_backend (gunicorn)
                                                           /static, /media     |
                                                                           scout_db (Postgres 16)
```

Every start of the backend container runs `migrate` and `collectstatic` before gunicorn.

The server holds only `~/scout-backend/` with `docker-compose.yml`, `nginx/nginx.conf`
(both copied in by each deploy) and `.env` (yours, created once). No git clone is needed.

---

## One-time setup

### 1. GitHub secrets

Repo **Scout-Backend** → Settings → Secrets and variables → Actions → New repository secret.

| Secret | Value |
|---|---|
| `SSH_HOST` | `167.71.97.155` |
| `SSH_USER` | `root` (check with `whoami` on the server) |
| `SSH_PORT` | `22` (check with `ss -tlnp \| grep sshd` on the server) |
| `SSH_PRIVATE_KEY` | the deploy key below |

Create the deploy key on the server (`ssh mydroplet`):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/scout_deploy -C "scout github actions" -N ""
cat ~/.ssh/scout_deploy.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
cat ~/.ssh/scout_deploy      # copy everything, including the BEGIN/END lines -> SSH_PRIVATE_KEY
rm ~/.ssh/scout_deploy       # GitHub has it now; the server keeps only the .pub half
```

Pulling the image uses the workflow's own `GITHUB_TOKEN`; no extra token is needed.

### 2. The server folder and `.env`

```bash
mkdir -p ~/scout-backend
nano ~/scout-backend/.env
```

Fill it from the **production** section of `.env.example`. Generate secrets with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Use such a value for `POSTGRES_PASSWORD` too: it goes into a URL, so stick to letters,
digits, `-` and `_`. `DJANGO_ALLOWED_HOSTS` is the API domain;
`CORS_ALLOWED_ORIGINS` and `FRONTEND_URL` are the frontend's address.

### 3. Switch over from the old backend

The old backend (`/home/scout_backend`, repo `scout_backend`) also listens on `127.0.0.1:8001`.

1. **Stop the old auto-deploy**: GitHub → `scout_backend` → Actions → "Deploy to production" →
   ⋯ → Disable workflow. Otherwise a push there would bring the old app back on port 8001.
2. **Stop the old stack**, keeping its data as a backup (no `-v`):
   ```bash
   cd /home/scout_backend && docker compose down
   ```
3. **Host nginx**: in the API site (e.g. `/etc/nginx/sites-available/scout-api`) remove the
   `location /static/` and `location /media/` blocks (they point at `/home/scout_backend/data/`; the new
   stack serves both itself), keep the `proxy_pass http://127.0.0.1:8001;` block and set
   `client_max_body_size 25M;`. The certbot TLS lines stay as they are.
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```
4. **Release**: push the first tag from your machine (`git tag v1.0.0 && git push origin v1.0.0`)
   and follow it in the repo's Actions tab.
5. Optional, for the Django admin at `/admin/`:
   ```bash
   cd ~/scout-backend && docker compose exec backend python manage.py createsuperuser
   ```
   Organizations themselves are created through the app's sign-up page.

The new backend starts with an empty Postgres database; the old MySQL data stays untouched
in the old stack's volume. Release the new frontend at the same time, since the old frontend
does not speak the new API.

---

## Day to day

```bash
cd ~/scout-backend
cat .release                          # deployed version
docker compose ps                     # status
docker compose logs -f backend        # app logs
docker compose exec backend python manage.py shell
```

### Roll back

- From GitHub: Actions → **Build and Deploy** → Run workflow → enter the older tag. It deploys
  exactly that version (code, compose file and nginx config).
- Quick, on the server (the 2 previous images are kept):
  ```bash
  cd ~/scout-backend && IMAGE_TAG=v1.0.0 docker compose up -d
  ```

Migrations are not reversed by a rollback; check before rolling back past a release that
changed the database.

### Back up and restore the database

```bash
cd ~/scout-backend
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > scout-$(date +%F).sql.gz

# restore into an empty database
gunzip -c scout-YYYY-MM-DD.sql.gz | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

Uploaded files (logos, photos) live in the `scout_media_volume` Docker volume. Uploaded Word templates live
in `scout_private_volume`, which only the backend mounts (nginx must never serve it); back up both.
