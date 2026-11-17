"""Deferred uploads for clips that hit YouTube's daily limits.

YouTube's API quota (10,000 units/day by default, ~6 uploads) and the per-channel
daily upload cap both reset at midnight Pacific time. When an upload fails for one of
those reasons, the clip stays on disk and is recorded in ``outputs/upload_retry.json``;
the worker retries it after the next reset instead of dropping it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ClipMetadata, PublishResult

log = logging.getLogger(__name__)

RETRY_FILE = Path("outputs") / "upload_retry.json"

# Substrings of Google API errors that mean "out of daily allowance, try tomorrow".
_DAILY_LIMIT_MARKERS = (
    "quotaexceeded",
    "dailylimitexceeded",
    "uploadlimitexceeded",
    "exceeded your quota",
    "exceeded the number of videos",
)

# Midnight Pacific is 08:00 UTC in winter and 07:00 UTC in summer; 08:05 UTC is
# safely after the reset all year round.
_RESET_HOUR_UTC = 8
_RESET_MINUTE_UTC = 5


def is_daily_limit_error(message: str | None) -> bool:
    """True if an upload error means the daily quota or upload cap is used up."""
    text = (message or "").lower()
    return any(marker in text for marker in _DAILY_LIMIT_MARKERS)


def next_quota_reset(now: float | None = None) -> float:
    """Unix time shortly after the next YouTube quota reset."""
    current = datetime.fromtimestamp(now if now is not None else time.time(), UTC)
    reset = current.replace(
        hour=_RESET_HOUR_UTC, minute=_RESET_MINUTE_UTC, second=0, microsecond=0
    )
    if reset <= current:
        reset += timedelta(days=1)
    return reset.timestamp()


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("pending", [])
            data.setdefault("thumbnails", [])
            data.setdefault("blocked_until", 0.0)
            return data
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.error("Could not read upload retry queue %s: %s", path, exc)
    return {"pending": [], "thumbnails": [], "blocked_until": 0.0}


THUMBNAIL_RETRY_S = 3600
MAX_THUMBNAIL_ATTEMPTS = 24


def enqueue_thumbnail(
    video_id: str,
    thumbnail_path: Path | str,
    *,
    reason: str | None = None,
    path: Path = RETRY_FILE,
    now: float | None = None,
) -> None:
    """Retry setting a custom thumbnail later (YouTube rate-limits thumbnail uploads)."""
    data = _load(path)
    current = now if now is not None else time.time()
    data["thumbnails"] = [t for t in data["thumbnails"] if t["video_id"] != video_id]
    data["thumbnails"].append(
        {
            "video_id": video_id,
            "path": str(Path(thumbnail_path).resolve()),
            "retry_at": current + THUMBNAIL_RETRY_S,
            "attempts": 0,
            "last_error": reason,
        }
    )
    _save(path, data)
    log.warning("⏸ Thumbnail for %s queued; retrying in an hour.", video_id)


def _process_thumbnails(
    data: dict, current: float, set_fn: Callable[[str, Path], bool] | None
) -> int:
    if set_fn is None:
        from .youtube.uploader import set_thumbnail as set_fn
    done = 0
    keep = []
    for t in data["thumbnails"]:
        if t.get("retry_at", 0) > current:
            keep.append(t)
            continue
        thumb = Path(t["path"])
        if thumb.exists() and set_fn(t["video_id"], thumb):
            done += 1
            continue
        t["attempts"] = t.get("attempts", 0) + 1
        if thumb.exists() and t["attempts"] < MAX_THUMBNAIL_ATTEMPTS:
            t["retry_at"] = current + THUMBNAIL_RETRY_S
            keep.append(t)
        else:
            log.error("Giving up on thumbnail for %s after %d tries.", t["video_id"], t["attempts"])
    data["thumbnails"] = keep
    return done


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def blocked_until(*, path: Path = RETRY_FILE, now: float | None = None) -> float | None:
    """If a daily limit was hit and has not reset yet, the time it resets; else None."""
    until = float(_load(path).get("blocked_until") or 0.0)
    current = now if now is not None else time.time()
    return until if until > current else None


def record_upload(*, path: Path = RETRY_FILE, now: float | None = None) -> None:
    """Log a successful YouTube upload, so backfill knows how much of today's quota is left."""
    current = now if now is not None else time.time()
    data = _load(path)
    data["uploads"] = [t for t in data.get("uploads", []) if t > current - 2 * 86400] + [current]
    _save(path, data)


def uploads_today(*, path: Path = RETRY_FILE, now: float | None = None) -> int:
    """Successful YouTube uploads since the last daily quota reset."""
    current = now if now is not None else time.time()
    since = next_quota_reset(current) - 86400
    return sum(1 for t in _load(path).get("uploads", []) if t >= since)


def has_pending(*, path: Path = RETRY_FILE) -> bool:
    """True if clips are waiting for the quota reset."""
    return bool(_load(path)["pending"])


def mark_blocked(*, path: Path = RETRY_FILE, now: float | None = None) -> float:
    """Record that the daily limit is used up. Returns the retry time."""
    data = _load(path)
    until = next_quota_reset(now)
    data["blocked_until"] = until
    _save(path, data)
    return until


def enqueue(
    video_path: Path | str,
    metadata: ClipMetadata,
    *,
    platform: str = "youtube",
    sidecar_path: Path | str | None = None,
    reason: str | None = None,
    path: Path = RETRY_FILE,
    now: float | None = None,
) -> float:
    """Queue a clip for upload after the next quota reset. Returns the retry time."""
    data = _load(path)
    retry_at = max(float(data.get("blocked_until") or 0.0), next_quota_reset(now))
    data["blocked_until"] = retry_at
    video = str(Path(video_path).resolve())
    # Re-queuing the same clip replaces its entry instead of uploading it twice.
    data["pending"] = [
        e for e in data["pending"] if not (e["video_path"] == video and e["platform"] == platform)
    ]
    data["pending"].append(
        {
            "video_path": video,
            "platform": platform,
            "sidecar_path": str(Path(sidecar_path).resolve()) if sidecar_path else None,
            "metadata": {
                "title": metadata.title,
                "description": metadata.description,
                "tags": list(metadata.tags),
                "privacy_status": metadata.privacy_status,
                "language": metadata.language,
                "thumbnail_path": metadata.thumbnail_path,
            },
            "queued_at": now if now is not None else time.time(),
            "retry_at": retry_at,
            "attempts": 0,
            "last_error": reason,
        }
    )
    _save(path, data)
    log.warning(
        "⏸ YouTube daily limit reached; %s saved locally and queued for upload after %s.",
        Path(video).name,
        datetime.fromtimestamp(retry_at, UTC).strftime("%Y-%m-%d %H:%M UTC"),
    )
    return retry_at


def _update_sidecar(sidecar: str | None, platform: str, result: PublishResult | None, status: str):
    if not sidecar:
        return
    p = Path(sidecar)
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return
    meta["publish_status"] = status
    if result is not None:
        meta.setdefault("publish_results", {})[platform] = {
            "success": result.success,
            "url": result.url,
            "platform_id": result.platform_id,
            "error_message": result.error_message,
        }
        meta["publish_error"] = None if result.success else result.error_message
    if status != "queued_retry":
        meta.pop("retry_after", None)
    try:
        p.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not update %s: %s", p, exc)


def _default_publish(video_path: Path, metadata: ClipMetadata, platform: str) -> PublishResult:
    from .manager import PublishingEngine

    return PublishingEngine().publish(video_path, metadata, [platform])[platform]


MAX_OTHER_FAILURES = 3


def process_due(
    *,
    path: Path = RETRY_FILE,
    now: float | None = None,
    publish_fn: Callable[[Path, ClipMetadata, str], PublishResult] | None = None,
    thumbnail_fn: Callable[[str, Path], bool] | None = None,
) -> dict[str, int]:
    """Upload queued clips (and thumbnails) whose retry time has passed. Safe to call often."""
    current = now if now is not None else time.time()
    data = _load(path)

    thumbs_done = 0
    if any(t.get("retry_at", 0) <= current for t in data["thumbnails"]):
        thumbs_done = _process_thumbnails(data, current, thumbnail_fn)
        _save(path, data)

    pending = data["pending"]
    if not pending or float(data.get("blocked_until") or 0.0) > current:
        return {"uploaded": 0, "requeued": 0, "dropped": 0, "thumbnails": thumbs_done}

    publish = publish_fn or _default_publish
    stats = {"uploaded": 0, "requeued": 0, "dropped": 0, "thumbnails": thumbs_done}
    remaining: list[dict] = []
    blocked = False

    for entry in sorted(pending, key=lambda e: e.get("queued_at", 0)):
        if blocked or entry.get("retry_at", 0) > current:
            remaining.append(entry)
            continue

        video = Path(entry["video_path"])
        if not video.exists():
            log.error("Dropping queued upload, file is gone: %s", video)
            _update_sidecar(entry.get("sidecar_path"), entry["platform"], None, "failed")
            stats["dropped"] += 1
            continue

        m = entry["metadata"]
        thumb = m.get("thumbnail_path")
        metadata = ClipMetadata(
            title=m["title"],
            description=m["description"],
            tags=m.get("tags") or [],
            privacy_status=m.get("privacy_status", "private"),
            language=m.get("language", "en"),
            thumbnail_path=thumb if thumb and Path(thumb).exists() else None,
        )
        log.info("🔁 Retrying queued upload: %s", video.name)
        try:
            result = publish(video, metadata, entry["platform"])
        except Exception as exc:
            result = PublishResult(platform=entry["platform"], success=False, error_message=str(exc))

        entry["attempts"] = entry.get("attempts", 0) + 1
        entry["last_error"] = result.error_message

        if result.success:
            log.info("✅ Queued upload done: %s → %s", video.name, result.url)
            _update_sidecar(entry.get("sidecar_path"), entry["platform"], result, "success")
            stats["uploaded"] += 1
        elif is_daily_limit_error(result.error_message):
            # Still out of quota: everything left waits for the next reset.
            blocked = True
            entry["retry_at"] = next_quota_reset(current)
            remaining.append(entry)
            stats["requeued"] += 1
        elif entry["attempts"] < MAX_OTHER_FAILURES:
            # Some other error (network, expired login): try again in an hour.
            entry["retry_at"] = current + 3600
            remaining.append(entry)
            stats["requeued"] += 1
        else:
            log.error(
                "Giving up on queued upload %s after %d attempts: %s",
                video.name,
                entry["attempts"],
                result.error_message,
            )
            _update_sidecar(entry.get("sidecar_path"), entry["platform"], result, "failed")
            stats["dropped"] += 1

    if blocked:
        reset = next_quota_reset(current)
        for entry in remaining:
            entry["retry_at"] = max(entry.get("retry_at", 0), reset)
        data["blocked_until"] = reset
    # Keep anything another process queued while these uploads were running.
    seen = {(e["video_path"], e["platform"]) for e in pending}
    latest = _load(path)
    remaining += [e for e in latest["pending"] if (e["video_path"], e["platform"]) not in seen]
    known_thumbs = {t["video_id"] for t in data["thumbnails"]}
    data["thumbnails"] += [t for t in latest["thumbnails"] if t["video_id"] not in known_thumbs]
    data["blocked_until"] = max(
        float(data.get("blocked_until") or 0.0), float(latest.get("blocked_until") or 0.0)
    )
    data["pending"] = remaining
    # Uploads done above were tallied straight into the file; keep that tally.
    data["uploads"] = latest.get("uploads", [])
    _save(path, data)
    return stats
