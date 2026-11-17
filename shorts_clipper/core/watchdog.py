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
import threading
import time
from pathlib import Path

from shorts_clipper.core.queue import JobQueue

logger = logging.getLogger("shorts_clipper.watchdog")
WATCHDOG_PATH = Path("outputs") / "watchdog.json"
# watchdog.json is written by the poll thread and by the worker loop (retries, backfill).
_file_lock = threading.RLock()
# Defaults for every watched channel; each channel entry in watchdog.json can override them.
WATCHDOG_DEFAULTS = {
    "long": 2,  # long-form 16:9 clips per new upload
    "short": 1,  # vertical shorts per new upload
    "upload": True,
    "privacy": "private",  # uploads wait for you to review and publish them
    "min_duration_minutes": 20,  # ignore uploads shorter than this (trailers, shorts, promos)
}
_PROCESSED_HISTORY = 200  # how many video ids to remember per channel
_POLL_ENTRIES = 30  # newest uploads to inspect per poll (also the backfill pool)
# YouTube's default API quota fits 6 uploads a day (1,600 units each).
DAILY_UPLOAD_TARGET = 6
# A failed clip job (YouTube 429, network blip) is retried after these delays.
RETRY_DELAYS_S = (3600, 3 * 3600, 12 * 3600)


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
        if vid in done:
            # Everything below the newest handled upload is older: that's backfill's job,
            # not a new upload (matters when the poll window grows or history is trimmed).
            break
        if not vid:
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
    with _file_lock:
        _check_channels(job_queue, force)


def _check_channels(job_queue: JobQueue, force: bool) -> None:
    watchdog_path = Path(WATCHDOG_PATH)
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

            # Remember the channel's recent episodes: the pool backfill picks from.
            ch["recent"] = [
                {"id": e["id"], "title": e.get("title") or "", "duration": e.get("duration")}
                for e in entries
                if e.get("id") and e.get("live_status") not in ("is_live", "is_upcoming", "post_live")
            ]

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
                job_queue.create("clip", _clip_payload(entry, ch, opts, "watchdog"))
                _mark_queued(data, entry["id"])

            if newly:
                ch["processed_videos"] = (processed + newly)[-_PROCESSED_HISTORY:]
            # Save after each channel so a crash mid-poll can't re-queue the same upload.
            watchdog_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    except Exception as e:
        logger.error("Watchdog execution failed: %s", e)


# ---------------------------------------------------------------------------
# Retries and backfill (called from the worker loop while it is idle)
# ---------------------------------------------------------------------------


def _clip_payload(entry: dict, ch: dict, opts: dict, origin: str) -> dict:
    return {
        "url": f"https://www.youtube.com/watch?v={entry['id']}",
        "count": int(opts["short"]),
        "long_count": int(opts["long"]),
        "upload": bool(opts["upload"]),
        "privacy": opts["privacy"],
        "source_title": entry.get("title") or "",
        "source_channel": ch.get("name") or entry.get("channel") or "",
        "origin": origin,
    }


def _video_id(url: str) -> str:
    return url.split("v=", 1)[-1].split("&", 1)[0] if "v=" in url else url


def _mark_queued(data: dict, vid: str) -> None:
    queued = data.setdefault("queued_ids", [])
    if vid not in queued:
        queued.append(vid)
        del queued[: -_PROCESSED_HISTORY * 5]


def _clipped_before(outputs: Path) -> set[str]:
    """Source videos that already have run folders (for watchdog.json files predating queued_ids)."""
    ids = set()
    for trace in outputs.glob("run_*/decision_trace.json"):
        try:
            url = json.loads(trace.read_text(encoding="utf-8")).get("video_url") or ""
        except Exception:
            continue
        if "v=" in url:
            ids.add(_video_id(url))
    return ids


def _load_watchdog(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _save_watchdog(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def record_failure(
    payload: dict, error: str, *, path: Path | None = None, now: float | None = None
) -> bool:
    """A watchdog/backfill clip job failed: try it again later. True if a retry is scheduled."""
    if payload.get("origin") not in ("watchdog", "backfill") or not payload.get("url"):
        return False
    path = Path(path or WATCHDOG_PATH)
    current = now if now is not None else time.time()
    attempt = int(payload.get("attempt", 0)) + 1
    with _file_lock:
        data = _load_watchdog(path)
        if data is None:
            return False
        retries = [r for r in data.get("retry_jobs", []) if r["payload"]["url"] != payload["url"]]
        if attempt > len(RETRY_DELAYS_S):
            logger.error("Giving up on %s after %d attempts: %s", payload["url"], attempt, error)
            data["retry_jobs"] = retries
            _save_watchdog(path, data)
            return False
        delay = RETRY_DELAYS_S[attempt - 1]
        retries.append(
            {
                "payload": {**payload, "attempt": attempt},
                "retry_at": current + delay,
                "last_error": (error or "")[:300],
            }
        )
        data["retry_jobs"] = retries
        _save_watchdog(path, data)
    logger.warning(
        "🔁 %s failed (%s); retrying in %d min.", payload["url"], (error or "")[:80], delay // 60
    )
    return True


def queue_due_retries(job_queue, *, path: Path | None = None, now: float | None = None) -> int:
    """Re-queue failed clip jobs whose retry time has come."""
    path = Path(path or WATCHDOG_PATH)
    current = now if now is not None else time.time()
    with _file_lock:
        data = _load_watchdog(path)
        if not data or not data.get("retry_jobs"):
            return 0
        due = [r for r in data["retry_jobs"] if r.get("retry_at", 0) <= current]
        if not due:
            return 0
        data["retry_jobs"] = [r for r in data["retry_jobs"] if r not in due]
        for r in due:
            logger.info("🔁 Retrying clip job for %s (attempt %d)", r["payload"]["url"], r["payload"]["attempt"] + 1)
            job_queue.create("clip", r["payload"])
        _save_watchdog(path, data)
    return len(due)


def pick_backfill(data: dict, exclude: set[str]) -> tuple[dict, dict, dict] | None:
    """Most recent not-yet-clipped episode across channels. Returns (entry, channel, opts).

    "Recent" is the position in each channel's upload list, so every channel's latest
    episode comes before anyone's second-latest.
    """
    defaults = {**WATCHDOG_DEFAULTS, **data.get("defaults", {})}
    best = None
    for ch_i, ch in enumerate(data.get("channels", [])):
        opts = {**defaults, **{k: v for k, v in ch.items() if k in WATCHDOG_DEFAULTS}}
        min_s = float(opts["min_duration_minutes"]) * 60
        for pos, e in enumerate(ch.get("recent") or []):
            if e["id"] in exclude or (e.get("duration") is not None and e["duration"] < min_s):
                continue
            if best is None or (pos, ch_i) < best[0]:
                best = ((pos, ch_i), e, ch, opts)
            break
    return best[1:] if best else None


def queue_backfill(
    job_queue,
    *,
    uploads_today: int,
    upload_blocked: bool,
    path: Path | None = None,
) -> dict | None:
    """When today's uploads leave room, clip an older episode from a watched channel.

    New uploads always come first (they are queued by the poll). This only fills the
    unused part of the daily upload allowance, one episode at a time, newest first.
    """
    path = Path(path or WATCHDOG_PATH)
    with _file_lock:
        data = _load_watchdog(path)
        if not data or not data.get("enabled") or not data.get("backfill", True):
            return None
        if upload_blocked:
            return None
        target = int(data.get("daily_upload_target", DAILY_UPLOAD_TARGET))
        if "queued_ids" not in data:
            data["queued_ids"] = sorted(_clipped_before(path.parent))
        exclude = set(data["queued_ids"])
        picked = pick_backfill(data, exclude)
        if picked is None:
            return None
        entry, ch, opts = picked
        per_episode = int(opts["long"]) + int(opts["short"]) if opts["upload"] else 0
        if per_episode and uploads_today + per_episode > target:
            return None
        payload = _clip_payload(entry, ch, opts, "backfill")
        logger.info(
            "📚 Backfill (%d/%d uploads today): %s — %s",
            uploads_today,
            target,
            ch.get("name") or ch.get("url"),
            entry.get("title"),
        )
        job_queue.create("clip", payload)
        _mark_queued(data, entry["id"])
        _save_watchdog(path, data)
        return payload
