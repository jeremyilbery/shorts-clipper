"""Channel watchdog: notice new uploads on watched channels and queue clip jobs for them.

Configuration lives in outputs/watchdog.json:

    {
      "enabled": true,
      "defaults": {"long": 2, "short": 1, "privacy": "private", "min_duration_minutes": 20},
      "channels": [{"name": "PowerfulJRE", "url": "https://www.youtube.com/@joerogan"}]
    }

Any key from WATCHDOG_DEFAULTS can also be set on a single channel to override it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from shorts_clipper.core.queue import JobQueue

logger = logging.getLogger("shorts_clipper.watchdog")
# Defaults for every watched channel; each channel entry in watchdog.json can override them.
WATCHDOG_DEFAULTS = {
    "long": 2,  # long-form 16:9 clips per new upload
    "short": 1,  # vertical shorts per new upload
    "upload": True,
    "privacy": "private",  # uploads wait for you to review and publish them
    "min_duration_minutes": 20,  # ignore uploads shorter than this (trailers, shorts, promos)
}
_PROCESSED_HISTORY = 100  # how many video ids to remember per channel
_POLL_ENTRIES = 10  # newest uploads to inspect per poll


def find_new_uploads(
    entries: list[dict], processed: list[str], min_duration_s: float
) -> tuple[list[dict], list[str]]:
    """Decide which of a channel's newest uploads to clip.

    Args:
        entries:  Newest-first yt-dlp flat-playlist entries (id, title, duration, live_status).
        processed: Video ids already handled (clipped or permanently skipped).
        min_duration_s: Uploads known to be shorter than this are skipped.

    Returns:
        (uploads to clip, oldest first; ids to add to ``processed``).
        Live streams and upcoming premieres are left unprocessed so they are picked up
        once the finished video is available.
    """
    done = set(processed)
    to_clip: list[dict] = []
    newly_processed: list[str] = []
    for entry in entries:
        vid = entry.get("id")
        if not vid or vid in done:
            continue
        if entry.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            continue
        duration = entry.get("duration")
        if duration is not None and duration < min_duration_s:
            newly_processed.append(vid)
            continue
        to_clip.append(entry)
        newly_processed.append(vid)
    to_clip.reverse()
    return to_clip, newly_processed


def _fetch_channel_entries(url: str) -> list[dict]:
    from shorts_clipper.downloader.yt_dlp import get_base_yt_dlp_cmd

    cmd = get_base_yt_dlp_cmd()
    cmd.extend(
        [
            "--flat-playlist",
            "--playlist-end",
            str(_POLL_ENTRIES),
            "--dump-json",
            "--socket-timeout",
            "15",
            f"{url.rstrip('/')}/videos",
        ]
    )
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[-500:] or f"yt-dlp exit {res.returncode}")
    return [json.loads(line) for line in res.stdout.splitlines() if line.strip()]


def check_watchdog_channels(job_queue: JobQueue, *, force: bool = False) -> None:
    """Check all monitored channels in outputs/watchdog.json and queue clip jobs for new uploads.

    The first poll of a channel only records its current uploads, so adding a channel
    does not clip its whole back catalogue. Every upload after that is clipped.
    """
    watchdog_path = Path("outputs/watchdog.json")
    if not watchdog_path.exists():
        # Create empty setup if missing
        watchdog_path.parent.mkdir(parents=True, exist_ok=True)
        default_data = {"enabled": False, "channels": [], "last_poll_time": 0.0}
        watchdog_path.write_text(json.dumps(default_data, indent=2), encoding="utf-8")
        return

    try:
        data = json.loads(watchdog_path.read_text(encoding="utf-8"))
        if not data.get("enabled", False):
            return

        # Poll limit: once every 10 minutes
        last_poll = data.get("last_poll_time", 0.0)
        if not force and time.time() - last_poll < 600:
            return

        data["last_poll_time"] = time.time()
        watchdog_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        logger.info("📡 Watchdog: Polling monitored channels for new uploads...")
        defaults = {**WATCHDOG_DEFAULTS, **data.get("defaults", {})}

        for ch in data.get("channels", []):
            url = ch.get("url")
            if not url:
                continue
            opts = {**defaults, **{k: v for k, v in ch.items() if k in WATCHDOG_DEFAULTS}}
            name = ch.get("name") or url

            try:
                entries = _fetch_channel_entries(url)
            except Exception as fetch_err:
                logger.error("Watchdog could not list uploads for %s: %s", name, fetch_err)
                continue

            processed = list(ch.get("processed_videos") or [])
            if ch.get("last_seen_video") and ch["last_seen_video"] not in processed:
                processed.append(ch["last_seen_video"])  # migrate the old single-id format

            if not processed:
                # First poll: remember what's already there, clip only future uploads.
                ch["processed_videos"] = [e["id"] for e in entries if e.get("id")]
                logger.info(
                    "📡 Watchdog: now watching %s (%d existing uploads ignored)",
                    name,
                    len(ch["processed_videos"]),
                )
                watchdog_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                continue

            to_clip, newly = find_new_uploads(
                entries, processed, float(opts["min_duration_minutes"]) * 60
            )
            for entry in to_clip:
                video_url = f"https://www.youtube.com/watch?v={entry['id']}"
                logger.info("🔔 Watchdog: New upload on %s: %s", name, video_url)
                job_queue.create(
                    "clip",
                    {
                        "url": video_url,
                        "count": int(opts["short"]),
                        "long_count": int(opts["long"]),
                        "upload": bool(opts["upload"]),
                        "privacy": opts["privacy"],
                        "source_title": entry.get("title") or "",
                        "source_channel": ch.get("name") or entry.get("channel") or "",
                        "origin": "watchdog",
                    },
                )

            if newly:
                ch["processed_videos"] = (processed + newly)[-_PROCESSED_HISTORY:]
            # Save after each channel so a crash mid-poll can't re-queue the same upload.
            watchdog_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    except Exception as e:
        logger.error("Watchdog execution failed: %s", e)
