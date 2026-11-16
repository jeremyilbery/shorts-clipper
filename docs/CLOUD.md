# Running shorts-clipper on a cloud server (24/7)

The `worker` service in `docker-compose.yml` watches the channels in
`outputs/watchdog.json`, clips new episodes and uploads them. Docker restarts it if it
crashes or the server reboots.

## 1. Server

Any Ubuntu 24.04 VPS with **4 vCPU, 8 GB RAM, 80 GB+ disk** (for example Hetzner
CPX31, DigitalOcean 8 GB, AWS Lightsail 8 GB). Transcription and rendering are
CPU-bound; full episodes are several GB each before they are cleaned up.

```bash
curl -fsSL https://get.docker.com | sh
git clone -b autoclipper-watchdog https://github.com/jeremyilbery/shorts-clipper.git
cd shorts-clipper
```

## 2. Copy your secrets and state from the PC

None of these are in git. From the project folder on Windows (replace `SERVER`):

```powershell
scp .env client_secret.json root@SERVER:shorts-clipper/
scp -r .cache root@SERVER:shorts-clipper/            # YouTube sign-in (token.pickle)
scp outputs/watchdog.json root@SERVER:shorts-clipper/outputs/
```

Copying `.cache/shorts-clipper/token.pickle` means you don't need to sign in to
YouTube again on the server (the browser sign-in can't run there).

## 3. YouTube's bot check (downloads)

YouTube often blocks downloads from data-centre IPs ("Sign in to confirm you're not
a bot"). Uploads are not affected. Pick one:

- **Cookies** — in a browser signed in to a *spare* Google account (not your channel
  account), export youtube.com cookies with a "Get cookies.txt" extension, then:
  ```bash
  mkdir -p secrets && mv cookies.txt secrets/
  echo "SHORTS_YTDLP_COOKIES=/app/secrets/cookies.txt" >> .env
  ```
  Cookies expire every few weeks/months; re-export when downloads start failing.
- **Residential proxy** — add `SHORTS_PROXY=http://user:pass@host:port` to `.env`
  (comma-separate several to rotate).

## 4. Start

```bash
docker compose up -d --build
docker compose logs -f worker        # watch it poll the channels
```

Update later with `git pull && docker compose up -d --build`.

## 5. Web console (optional)

It listens only on the server's localhost. From your PC:

```bash
ssh -L 8000:localhost:8000 root@SERVER
```

then open http://localhost:8000.

## 6. Stop the PC copy

Once the server is uploading, stop the worker on Windows and disable the
`ShortsClipper` scheduled task, or both machines will clip the same episodes:

```powershell
Disable-ScheduledTask -TaskName ShortsClipper
```
