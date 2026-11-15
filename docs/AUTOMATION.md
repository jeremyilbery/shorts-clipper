# Automatic clipping of watched channels

The background worker checks the channels you choose every 10 minutes. When one
posts a new full-length video, it makes **2 long-form clips (8–12 min, 16:9)** and
**1 Short (20–60 s, vertical)**, writes their titles, descriptions, tags and chapters,
and uploads them to your YouTube channel as **private** videos for you to review.

## One-time setup

### 1. Create YouTube API credentials (about 10 minutes, in your browser)

1. Go to <https://console.cloud.google.com/> and create a project (any name).
2. **APIs & Services → Library**: enable **YouTube Data API v3**.
3. **APIs & Services → OAuth consent screen**: choose *External*, fill in the app
   name and your email, and add your own Google account under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs: add both
     `http://127.0.0.1:8000/api/youtube/callback` and
     `http://localhost:8000/api/youtube/callback`
5. Download the JSON and save it in the project folder as **`client_secret.json`**
   (it is git-ignored).

### 2. Connect your channel

```powershell
.\.venv\Scripts\shorts-clipper.exe web
```

Open <http://127.0.0.1:8000>, click **Connect YouTube** in the sidebar, and sign in
with the account that owns the channel you upload to. The login is saved in
`.cache/shorts-clipper/token.pickle`, so this is only needed once.

For custom thumbnails, the channel must be phone-verified
(<https://www.youtube.com/verify>); without it, uploads still work and YouTube picks
the thumbnail.

### 3. Choose the channels to watch

Edit `outputs/watchdog.json` (or use the Watchdog panel in the web console):

```json
{
  "enabled": true,
  "defaults": { "long": 2, "short": 1, "privacy": "private", "min_duration_minutes": 20 },
  "channels": [
    { "name": "PowerfulJRE", "url": "https://www.youtube.com/@joerogan" }
  ]
}
```

- `long` / `short`: clips per new upload. Any of these keys can also be set on a
  single channel to override the default for it.
- `privacy`: `private` (review before publishing), `unlisted` or `public`.
- `min_duration_minutes`: uploads shorter than this (trailers, promos, Shorts) are skipped.

The first check of a new channel only records its existing videos, so it clips
**new uploads from then on**, not the whole back catalogue.

### 4. Start the worker

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-autoclipper.ps1
```

To have it start automatically when you log in to Windows:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PWD\scripts\start-autoclipper.ps1`""
Register-ScheduledTask -TaskName "ShortsClipper" -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0)
```

Remove it again with `Unregister-ScheduledTask -TaskName ShortsClipper`.

## Day to day

- The computer has to be on (and not asleep) for uploads to be noticed and processed.
  One 3-hour podcast takes about 25 minutes to clip.
- New clips land in `outputs/run_*` and appear as private videos in YouTube Studio.
  Check the clip, title and description there, then publish.
- YouTube's default API quota allows about 6 uploads a day. When it (or the channel's
  daily upload cap) runs out, the clip stays in `outputs/run_*`, is listed in
  `outputs/upload_retry.json`, and is uploaded automatically after the quota resets
  (midnight Pacific time). Its `final_metadata_N.json` shows `"publish_status": "queued_retry"`
  until then.
- Logs: `outputs/worker.err.log`.
- The free Gemini tier allows about 250k input tokens per minute; one full podcast
  transcript is a large share of that, so runs pause and retry when the limit is hit.
  Several channels posting at once will be processed one after another.
