"""Channel watchdog: which uploads get clipped, and the jobs it queues."""

import json
from unittest import mock

from shorts_clipper.core import watchdog
from shorts_clipper.core.watchdog import find_new_uploads

HOUR = 3600


def _e(vid, duration=2 * HOUR, live_status="not_live", title=None):
    return {"id": vid, "duration": duration, "live_status": live_status, "title": title or vid}


def test_new_uploads_are_returned_oldest_first():
    entries = [_e("new2"), _e("new1"), _e("old")]
    to_clip, newly = find_new_uploads(entries, ["old"], 20 * 60)
    assert [e["id"] for e in to_clip] == ["new1", "new2"]
    assert set(newly) == {"new1", "new2"}


def test_short_uploads_are_skipped_but_remembered():
    to_clip, newly = find_new_uploads([_e("trailer", duration=90)], ["old"], 20 * 60)
    assert to_clip == []
    assert newly == ["trailer"]


def test_live_and_upcoming_are_left_for_later():
    entries = [_e("live", live_status="is_live"), _e("soon", live_status="is_upcoming", duration=None)]
    to_clip, newly = find_new_uploads(entries, ["old"], 20 * 60)
    assert to_clip == [] and newly == []


def test_unknown_duration_is_clipped():
    to_clip, _ = find_new_uploads([_e("x", duration=None)], ["old"], 20 * 60)
    assert [e["id"] for e in to_clip] == ["x"]


def _run_check(tmp_path, monkeypatch, config, entries):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "watchdog.json").write_text(json.dumps(config))
    queue = mock.Mock()
    with mock.patch.object(watchdog, "_fetch_channel_entries", return_value=entries):
        watchdog.check_watchdog_channels(queue, force=True)
    saved = json.loads((tmp_path / "outputs" / "watchdog.json").read_text())
    return queue, saved


def test_first_poll_records_existing_uploads_without_clipping(tmp_path, monkeypatch):
    config = {"enabled": True, "channels": [{"name": "Pod", "url": "https://youtube.com/@pod"}]}
    queue, saved = _run_check(tmp_path, monkeypatch, config, [_e("a"), _e("b")])
    queue.create.assert_not_called()
    assert saved["channels"][0]["processed_videos"] == ["a", "b"]


def test_new_upload_queues_clip_job_with_long_and_short_counts(tmp_path, monkeypatch):
    config = {
        "enabled": True,
        "channels": [
            {"name": "Pod", "url": "https://youtube.com/@pod", "processed_videos": ["a"], "short": 3}
        ],
    }
    queue, saved = _run_check(tmp_path, monkeypatch, config, [_e("new", title="Ep 5"), _e("a")])

    queue.create.assert_called_once()
    kind, payload = queue.create.call_args.args
    assert kind == "clip"
    assert payload["url"] == "https://www.youtube.com/watch?v=new"
    assert (payload["long_count"], payload["count"]) == (2, 3)  # default long, per-channel short
    assert payload["upload"] is True and payload["privacy"] == "private"
    assert payload["source_title"] == "Ep 5" and payload["source_channel"] == "Pod"
    assert saved["channels"][0]["processed_videos"] == ["a", "new"]


def test_already_processed_upload_is_not_queued_again(tmp_path, monkeypatch):
    config = {
        "enabled": True,
        "channels": [{"name": "Pod", "url": "u", "processed_videos": ["new", "a"]}],
    }
    queue, _ = _run_check(tmp_path, monkeypatch, config, [_e("new"), _e("a")])
    queue.create.assert_not_called()


def _backfill_config(**extra):
    return {
        "enabled": True,
        "queued_ids": ["a1"],
        "channels": [
            {"name": "A", "url": "ua", "recent": [_e("a1"), _e("a2"), _e("a3")]},
            {"name": "B", "url": "ub", "recent": [_e("b1"), _e("short", duration=600)]},
        ],
        **extra,
    }


def _write(tmp_path, config):
    p = tmp_path / "watchdog.json"
    p.write_text(json.dumps(config))
    return p


def test_backfill_takes_newest_unclipped_episode_channel_by_channel(tmp_path):
    p = _write(tmp_path, _backfill_config())
    queue = mock.Mock()
    picked = []
    for _ in range(4):
        payload = watchdog.queue_backfill(queue, uploads_today=0, upload_blocked=False, path=p)
        picked.append(payload and payload["url"].split("=")[-1])
    # a1 was already clipped; "short" is under the minimum length.
    assert picked == ["b1", "a2", "a3", None]
    assert queue.create.call_args.args[1]["origin"] == "backfill"


def test_backfill_respects_daily_upload_target(tmp_path):
    p = _write(tmp_path, _backfill_config())
    queue = mock.Mock()
    # 2 long + 1 Short per episode: 3 uploads done leaves room for one more episode.
    assert watchdog.queue_backfill(queue, uploads_today=3, upload_blocked=False, path=p)
    assert watchdog.queue_backfill(queue, uploads_today=4, upload_blocked=False, path=p) is None
    assert watchdog.queue_backfill(queue, uploads_today=0, upload_blocked=True, path=p) is None
    assert queue.create.call_count == 1


def test_failed_job_is_retried_then_given_up(tmp_path):
    p = _write(tmp_path, {"enabled": True, "channels": []})
    payload = {"url": "https://www.youtube.com/watch?v=x", "origin": "watchdog"}
    queue = mock.Mock()
    now = 1_000_000.0
    assert watchdog.record_failure(payload, "Rate limited by YouTube", path=p, now=now)
    assert watchdog.queue_due_retries(queue, path=p, now=now + 60) == 0
    assert watchdog.queue_due_retries(queue, path=p, now=now + 3601) == 1
    retried = queue.create.call_args.args[1]
    assert retried["attempt"] == 1 and retried["url"] == payload["url"]
    # Third retry fails too -> no more attempts.
    assert watchdog.record_failure({**retried, "attempt": 3}, "boom", path=p, now=now)is False
    assert json.loads(p.read_text())["retry_jobs"] == []


def test_manual_clip_failure_is_not_retried(tmp_path):
    p = _write(tmp_path, {"enabled": True, "channels": []})
    assert not watchdog.record_failure({"url": "u"}, "err", path=p)


def test_disabled_watchdog_does_nothing(tmp_path, monkeypatch):
    config = {"enabled": False, "channels": [{"name": "Pod", "url": "u", "processed_videos": ["a"]}]}
    queue, _ = _run_check(tmp_path, monkeypatch, config, [_e("new")])
    queue.create.assert_not_called()
