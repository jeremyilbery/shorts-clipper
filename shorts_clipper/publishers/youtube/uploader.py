import logging
from collections.abc import Callable
from pathlib import Path

from .auth import get_youtube_service

log = logging.getLogger(__name__)


def upload_short(
    video_path: Path | str,
    title: str,
    description: str = "#Shorts",
    tags: list[str] | None = None,
    privacy_status: str = "private",
    progress_callback: Callable[[int], None] | None = None,
) -> str:
    """Upload a video to YouTube as a Short.

    Returns:
        The YouTube video ID of the uploaded video.
    """
    from googleapiclient.http import MediaFileUpload

    log.info("Uploading %s to YouTube...", video_path)

    youtube = get_youtube_service()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or ["Shorts", "Trending", "Viral"],
            "categoryId": "24",  # Entertainment
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(str(video_path), chunksize=2 * 1024 * 1024, resumable=True)
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = request.next_chunk(num_retries=5)
        if status:
            progress_pct = int(status.progress() * 100)
            log.info("Uploaded %d%%...", progress_pct)
            if progress_callback:
                try:
                    progress_callback(progress_pct)
                except Exception:
                    pass

    video_id = response.get("id")
    log.info("✅ Upload Complete! Video ID: %s", video_id)
    return video_id


def set_thumbnail(video_id: str, thumbnail_path: Path | str) -> bool:
    """Set a custom thumbnail on an uploaded video. Never raises.

    YouTube only allows custom thumbnails on channels that have been phone-verified;
    otherwise the API refuses and YouTube's auto-picked frame is kept.
    """
    from googleapiclient.http import MediaFileUpload

    try:
        youtube = get_youtube_service()
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
        ).execute()
        log.info("🖼  Thumbnail set for %s", video_id)
        return True
    except Exception as exc:
        log.warning(
            "Could not set thumbnail for %s (rate limit, quota, or channel not phone-verified): %s",
            video_id,
            exc,
        )
        return False
