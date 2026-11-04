# Deploying Soni2 on EC2

## Why the previous attempt failed

Four separate problems, in the order they bite:

1. **The code was not on `main`.** The work lives on
   `codex/once-pipeline-upgrade-20261015`; `main` stops at an older commit. So
   `git pull origin main` fetched a tree without the current modules, and the
   containers failed on import.
2. **The frontend image ran the Vite development server.** Vite refuses
   requests whose `Host` header it does not recognise, so reaching the site by
   the instance's public address answered **"Blocked request. This host is not
   allowed."** The image now builds the bundle and serves it with nginx, which
   has no such notion.
3. **No `.env` on the server.** It is git-ignored, by design. Without it every
   provider key is empty and narration cannot start.
4. **The backend image was overridden by a bind mount** (`./backend/app`), so
   the container ran whatever the checkout happened to contain rather than the
   code that was built and tested. That mount is now only in the development
   overlay.

nginx also proxies `/api` to the backend, so the browser talks to a single
origin: no CORS configuration, and the API is not exposed to the internet.

## One-time setup

Instance size: **t3.small or larger.** A `t2.micro`/`t3.micro` (1 GB) runs out
of memory during `npm ci` and the Vite build. If you must use 1 GB, add swap:

```bash
sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Docker and Compose:

```bash
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
sudo mkdir -p /usr/local/lib/docker/cli-plugins
sudo curl -SL https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64 \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
exit   # log in again so the docker group applies
```

**Security group:** allow inbound TCP **80** (and 22 for SSH). Port 5173 also
reaches the site, so an existing rule keeps working.

## Create `.env` on the server

```bash
cd ~/Soni2
cp .env.example .env
nano .env
```

Fill in at least:

```
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=...
ELEVENLABS_VOICE_ID=...
ASR_PROVIDER=deepgram
DEEPGRAM_API_KEY=...
JOB_STORAGE_DIR=/data
```

`.env` is read by Compose for `${VAR}` substitution. Keep it out of git — it
already is.

## Deploy

```bash
ssh -i "D:\Downloads\Soni2.pem" ec2-user@16.171.35.105
cd ~/Soni2
git fetch origin
git checkout codex/once-pipeline-upgrade-20261015
git pull origin codex/once-pipeline-upgrade-20261015
docker compose up -d --build
```

Once the branch is merged into `main`, the last three lines become
`git checkout main && git pull origin main && docker compose up -d --build`.

Then open `http://16.171.35.105/`.

## Checking it

```bash
docker compose ps                      # both services "healthy"
docker compose logs -f backend         # the API
curl -s localhost/api/health           # {"status":"ok"} through nginx
curl -s localhost/api/capabilities     # provider, voice, and what is configured
```

`capabilities` is the quickest diagnosis: `tts_configured: false` means the key
or the voice is missing from `.env`; `voice_is_peninsular: false` means the
selected voice is not Spain Spanish and production will refuse it.

## Updating

```bash
cd ~/Soni2 && git pull && docker compose up -d --build
```

The `soni2-data` volume survives, so finished jobs **and the narration cache**
are kept — unchanged text is not paid for again.

## The current server: too small for a full build

The server is a t3.micro with an 8 GB disk that it shares with another project, so
a normal `docker compose up -d --build` does not fit. Deploy like this instead:

```bash
# On your own machine: the frontend is built here.
cd frontend && npm run build
tar -czf dist.tgz -C dist .
ssh -i "D:\Downloads\Soni2.pem" ec2-user@16.171.35.105 \
  'cd ~/soni2-frontend-prebuilt && rm -rf dist.previous && mv dist dist.previous && mkdir dist && tar -xzf - -C dist' < dist.tgz

# On the server: the code comes from GitHub, never the other way round.
cd ~/Soni2
git fetch origin <branch> && git checkout -B <branch> FETCH_HEAD
cp frontend/nginx.conf ~/soni2-frontend-prebuilt/nginx.conf
~/soni2-deploy.sh
```

`soni2-deploy.sh` lays the new `backend/app` over the image tagged
`soni2-backend:base`, and refuses to run if `backend/Dockerfile` or
`backend/requirements.txt` have changed, because then a full build is needed.

* **Do not run `docker system prune`.** It removes `soni2-backend:base`, which the
  script needs, and it once removed the other project's image. Remove images by name.
* **To roll back:** `git checkout <previous commit>`, restore `dist.previous` as
  `dist`, and run `~/soni2-deploy.sh`.
* **The narration pauses are read from `.env`:** `AUDIO_PARAGRAPH_GAP_MS` and
  `AUDIO_CHAPTER_GAP_MS` there override the defaults.

**Never empty `/data/audio-cache` to free disk.** It is what stops you paying twice.
If it does get emptied, this rebuilds it from the job folders, at no cost:

```bash
docker cp backend/tools/reseed_cache.py soni2-backend-1:/tmp/reseed.py
docker exec -w /app -e PYTHONPATH=/app soni2-backend-1 python /tmp/reseed.py --dry-run
docker exec -w /app -e PYTHONPATH=/app soni2-backend-1 python /tmp/reseed.py
```

See [cloud-architecture.md](cloud-architecture.md) for where to go from here.

## Common failures

| Symptom | Cause | Fix |
|---|---|---|
| "Blocked request. This host is not allowed." | Running the development overlay on a public address | Use the plain `docker compose up -d --build` |
| Site loads, every API call fails | `VITE_API_URL` set to something unreachable | Leave it empty; nginx proxies `/api` |
| `tts_configured: false` | `.env` missing or key empty | Create `.env`, then `docker compose up -d` |
| Build killed, exit 137 | Out of memory | Larger instance, or add swap as above |
| Upload fails on a big book | Body limit | Raise `client_max_body_size` in `frontend/nginx.conf` and `MAX_UPLOAD_SIZE_MB` |
| Job fails with `No space left on device` | The disk is full: assembling a 2-hour book needs about 500 MB | Enlarge the EBS volume. The audio already narrated is kept, and `POST /api/jobs/{id}/resume` continues from it |
| Paragraphs fail with `credit` | ElevenLabs balance or key quota is used up | Raise it, then resume the job: nothing narrated is paid for again |
| Download stops part way | A proxy timeout in front of nginx | nginx itself allows an hour; check any load balancer |

## Local development

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Vite with hot reload on `http://localhost:5173`, the backend reloading from
`backend/app`.
