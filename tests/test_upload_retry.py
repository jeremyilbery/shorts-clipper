import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from shorts_clipper.publishers import ClipMetadata, PublishingEngine, PublishResult, retry_queue

QUOTA_MSG = (
    '<HttpError 403 when requesting https://youtube.googleapis.com/upload/youtube/v3/videos '
    'returned "The request cannot be completed because you have exceeded your quota.". '
    "Details: \"[{'domain': 'youtube.quota', 'reason': 'quotaExceeded'}]\">"
)


def _ts(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


def _meta():
    return ClipMetadata(title="T", description="D", tags=["a"], privacy_status="private")


def test_detects_daily_limit_errors():
    assert retry_queue.is_daily_limit_error(QUOTA_MSG)
    assert retry_queue.is_daily_limit_error("reason: uploadLimitExceeded")
    assert not retry_queue.is_daily_limit_error("HttpError 500 backendError")
    assert not retry_queue.is_daily_limit_error(None)


def test_next_reset_is_after_midnight_pacific():
    # 03:00 UTC -> same day 08:05 UTC; 09:00 UTC -> next day 08:05 UTC.
    assert retry_queue.next_quota_reset(_ts(2026, 11, 14, 3)) == _ts(2026, 11, 14, 8, 5)
    assert retry_queue.next_quota_reset(_ts(2026, 11, 14, 9)) == _ts(2026, 11, 15, 8, 5)


def test_queued_clip_waits_then_uploads(tmp_path):
    q = tmp_path / "retry.json"
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    sidecar = tmp_path / "clip.json"
    sidecar.write_text(json.dumps({"publish_status": "queued_retry"}), encoding="utf-8")
    now = _ts(2026, 11, 14, 20)

    retry_queue.enqueue(video, _meta(), sidecar_path=sidecar, reason=QUOTA_MSG, path=q, now=now)
    assert retry_queue.blocked_until(path=q, now=now) == _ts(2026, 11, 15, 8, 5)

    publish = MagicMock(
        return_value=PublishResult(
            platform="youtube", success=True, url="https://youtu.be/abc", platform_id="abc"
        )
    )
    # Before the reset nothing is attempted.
    assert retry_queue.process_due(path=q, now=now + 3600, publish_fn=publish)["uploaded"] == 0
    publish.assert_not_called()

    stats = retry_queue.process_due(path=q, now=_ts(2026, 11, 15, 9), publish_fn=publish)
    assert stats == {"uploaded": 1, "requeued": 0, "dropped": 0, "thumbnails": 0}
    assert json.loads(q.read_text())["pending"] == []
    side = json.loads(sidecar.read_text())
    assert side["publish_status"] == "success"
    assert side["publish_results"]["youtube"]["url"] == "https://youtu.be/abc"


def test_still_over_quota_requeues_everything_for_next_day(tmp_path):
    q = tmp_path / "retry.json"
    now = _ts(2026, 11, 14, 20)
    for name in ("a.mp4", "b.mp4"):
        (tmp_path / name).write_bytes(b"x")
        retry_queue.enqueue(tmp_path / name, _meta(), path=q, now=now)

    publish = MagicMock(
        return_value=PublishResult(platform="youtube", success=False, error_message=QUOTA_MSG)
    )
    later = _ts(2026, 11, 15, 9)
    stats = retry_queue.process_due(path=q, now=later, publish_fn=publish)

    # The first failure stops the round; the second clip isn't wasted on a doomed call.
    assert publish.call_count == 1
    assert stats["requeued"] == 1
    data = json.loads(q.read_text())
    assert len(data["pending"]) == 2
    assert all(e["retry_at"] == _ts(2026, 11, 16, 8, 5) for e in data["pending"])
    assert data["blocked_until"] == _ts(2026, 11, 16, 8, 5)


def test_requeuing_same_clip_does_not_duplicate(tmp_path):
    q = tmp_path / "retry.json"
    video = tmp_path / "a.mp4"
    video.write_bytes(b"x")
    retry_queue.enqueue(video, _meta(), path=q, now=_ts(2026, 11, 14, 20))
    retry_queue.enqueue(video, _meta(), path=q, now=_ts(2026, 11, 14, 21))
    assert len(json.loads(q.read_text())["pending"]) == 1


def test_missing_file_is_dropped(tmp_path):
    q = tmp_path / "retry.json"
    video = tmp_path / "gone.mp4"
    video.write_bytes(b"x")
    retry_queue.enqueue(video, _meta(), path=q, now=_ts(2026, 11, 14, 20))
    video.unlink()
    publish = MagicMock()
    stats = retry_queue.process_due(path=q, now=_ts(2026, 11, 15, 9), publish_fn=publish)
    assert stats["dropped"] == 1
    publish.assert_not_called()


def test_failed_thumbnail_retries_hourly(tmp_path):
    q = tmp_path / "retry.json"
    thumb = tmp_path / "t.jpg"
    thumb.write_bytes(b"x")
    now = _ts(2026, 11, 15, 10)
    retry_queue.enqueue_thumbnail("vid1", thumb, path=q, now=now)

    set_fn = MagicMock(side_effect=[False, True])
    # Not due yet.
    retry_queue.process_due(path=q, now=now + 60, thumbnail_fn=set_fn)
    set_fn.assert_not_called()
    # First retry still rate-limited -> pushed back an hour.
    assert retry_queue.process_due(path=q, now=now + 3601, thumbnail_fn=set_fn)["thumbnails"] == 0
    assert len(json.loads(q.read_text())["thumbnails"]) == 1
    # Second retry succeeds -> removed.
    assert retry_queue.process_due(path=q, now=now + 7300, thumbnail_fn=set_fn)["thumbnails"] == 1
    assert json.loads(q.read_text())["thumbnails"] == []


def test_upload_tally_resets_daily(tmp_path):
    q = tmp_path / "retry.json"
    retry_queue.record_upload(path=q, now=_ts(2026, 11, 15, 7))  # before the 08:05 UTC reset
    retry_queue.record_upload(path=q, now=_ts(2026, 11, 15, 9))
    retry_queue.record_upload(path=q, now=_ts(2026, 11, 15, 20))
    assert retry_queue.uploads_today(path=q, now=_ts(2026, 11, 15, 21)) == 2
    assert retry_queue.uploads_today(path=q, now=_ts(2026, 11, 16, 9)) == 0


def test_youtube_only_publish_does_not_need_r2(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    yt = MagicMock()
    yt.publish.return_value = PublishResult(
        platform="youtube", success=True, url="https://youtu.be/x", platform_id="x"
    )
    yt.verify.return_value = True
    with (
        patch("shorts_clipper.publishers.manager.PublisherRegistry.get_publisher", return_value=yt),
        patch("shorts_clipper.publishers.manager.R2Storage") as r2,
    ):
        results = PublishingEngine().publish(video, _meta(), ["youtube"])
    r2.assert_not_called()
    assert results["youtube"].success
