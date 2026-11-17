"""Decoupled task worker for processing Shorts Clipper jobs in a separate process.

Polls the SQLite job queue for pending tasks, runs them sequentially,
updates progress, supports job cancellation, and runs the Autopilot Channel Watchdog.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from shorts_clipper.core.logging import configure_logging
from shorts_clipper.core.queue import JobQueue, JobStatus
from shorts_clipper.core.settings import Settings
from shorts_clipper.core import watchdog
from shorts_clipper.core.watchdog import check_watchdog_channels
from shorts_clipper.pipeline.runner import run, run_autopilot
from shorts_clipper.providers import get_provider
from shorts_clipper.publishers import retry_queue

RETRY_CHECK_INTERVAL_S = 600

# Setup logging
configure_logging("INFO")
logger = logging.getLogger("shorts_clipper.worker")

# Worker state
active_job_id = None
_proc_lock = threading.Lock()
_active_processes: list[subprocess.Popen] = []

# Override Popen to track active subprocesses for cancellation
_original_popen_init = subprocess.Popen.__init__


def _tracked_popen_init(self, *args, **kwargs):
    _original_popen_init(self, *args, **kwargs)
    with _proc_lock:
        _active_processes.append(self)
    logger.info("Worker registered subprocess PID %s", self.pid)


subprocess.Popen.__init__ = _tracked_popen_init


def cancel_active_processes() -> None:
    """Forcefully terminate all subprocesses spawned by this worker."""
    with _proc_lock:
        procs = list(_active_processes)
        _active_processes.clear()

    logger.info("Cancelling worker tasks: terminating %d processes", len(procs))
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    time.sleep(0.5)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


def update_heartbeat() -> None:
    """Write current timestamp to worker_heartbeat.txt to signal health."""
    try:
        Path("outputs").mkdir(parents=True, exist_ok=True)
        Path("outputs/worker_heartbeat.txt").write_text(str(time.time()), encoding="utf-8")
    except Exception as e:
        logger.error("Failed to write worker heartbeat: %s", e)


class JobCancelledError(Exception):
    """Exception raised when a job is cancelled by the user mid-execution."""

    pass


def watchdog_thread_loop() -> None:
    """Thread running the channel watchdog check every 60 seconds."""
    logger.info("📡 Watchdog background thread started.")
    job_queue = JobQueue()
    while True:
        check_watchdog_channels(job_queue)
        time.sleep(60)


def heartbeat_thread_loop() -> None:
    """Thread running the heartbeat every 5 seconds."""
    while True:
        update_heartbeat()
        time.sleep(5.0)


def run_worker() -> None:
    global active_job_id
    logger.info("👷 Shorts Clipper background worker started.")

    job_queue = JobQueue()
    settings = Settings.from_env()

    # Launch watchdog thread
    watchdog_thread = threading.Thread(target=watchdog_thread_loop, daemon=True)
    watchdog_thread.start()

    # Launch heartbeat thread
    heartbeat_thread = threading.Thread(target=heartbeat_thread_loop, daemon=True)
    heartbeat_thread.start()

    def cancellation_monitor_thread_loop() -> None:
        """Continuously monitors the active job status and terminates subprocesses if cancelled."""
        while True:
            jid = active_job_id
            if jid:
                try:
                    current = job_queue.get(jid)
                    if current and current.status == JobStatus.CANCELLED:
                        logger.info(
                            "Cancellation monitor detected cancelled job: %s. Terminating processes...",
                            jid,
                        )
                        cancel_active_processes()
                except Exception:
                    pass
            time.sleep(1.0)

    # Launch cancellation monitor thread
    cancel_thread = threading.Thread(target=cancellation_monitor_thread_loop, daemon=True)
    cancel_thread.start()

    # Clean up any stuck running jobs left from a previous crash/termination
    for job in job_queue.list_by_status(JobStatus.RUNNING):
        logger.info("Resetting stuck running job to pending: %s", job.id)
        job_queue.update_status(job.id, JobStatus.PENDING, progress=0)

    last_retry_check = 0.0
    while True:
        time.sleep(1.0)

        # Check for and claim a pending job atomically
        job = job_queue.claim_job()
        if not job:
            # While idle, upload clips held back by yesterday's YouTube quota.
            if time.time() - last_retry_check >= RETRY_CHECK_INTERVAL_S:
                last_retry_check = time.time()
                target = watchdog.daily_upload_target()
                try:
                    stats = retry_queue.process_due(
                        limit=max(0, target - retry_queue.uploads_last_24h())
                    )
                    if any(stats.values()):
                        logger.info("🔁 Upload retry queue: %s", stats)
                except Exception:
                    logger.exception("Upload retry queue check failed")
                try:
                    # Failed episodes first, then older episodes to fill today's uploads.
                    if not watchdog.queue_due_retries(job_queue):
                        watchdog.queue_backfill(
                            job_queue,
                            uploads_today=retry_queue.uploads_last_24h(),
                            upload_blocked=bool(
                                retry_queue.blocked_until() or retry_queue.has_pending()
                            ),
                        )
                except Exception:
                    logger.exception("Watchdog retry/backfill check failed")
            continue

        active_job_id = job.id
        logger.info("📥 Acquired job: %s [%s]", job.id, job.kind)

        try:
            job_queue.update_status(job.id, JobStatus.RUNNING, progress=5)

            def check_cancelled(jid: str = job.id):
                current_job = job_queue.get(jid)
                if not current_job or current_job.status == JobStatus.CANCELLED:
                    raise JobCancelledError("Job cancelled by user.")

            def worker_progress(pct: int, jid: str = job.id) -> None:
                check_cancelled(jid)
                update_heartbeat()
                job_queue.update_progress(jid, pct)

            payload = job.payload

            if job.kind == "autopilot":
                logger.info(
                    f"JOB LOADED:\nniche={payload.get('niche')}\nkeyword={payload.get('keyword')}"
                )
                scout_duration = payload.get("scout_duration", "all")
                max_age_days = settings.scout_max_age_days
                if scout_duration == "today":
                    max_age_days = 1
                elif scout_duration == "week":
                    max_age_days = 7
                elif scout_duration == "month":
                    max_age_days = 30

                result = run_autopilot(
                    settings=settings,
                    channel=payload.get("channel"),
                    niche=payload.get("niche"),
                    keyword=payload.get("keyword"),
                    count=payload.get("count", 1),
                    upload=payload.get("upload", False),
                    privacy=payload.get("privacy", "private"),
                    progress_callback=worker_progress,
                    max_age_days=max_age_days,
                    job_id=job.id,
                )
                output_paths = []
                if result:
                    if isinstance(result, list):
                        output_paths = [str(p) for p in result]
                    else:
                        output_paths = [str(result)]

                check_cancelled()
                job_queue.update_status(
                    job.id,
                    JobStatus.DONE,
                    progress=100,
                    output_paths=output_paths,
                    result={"clip_count": len(output_paths)},
                )
                logger.info("✅ Job finished successfully: %s", job.id)

            elif job.kind == "clip":
                result = run(
                    payload["url"],
                    settings=settings,
                    count=payload.get("count", 1),
                    long_count=payload.get("long_count", 0),
                    upload=payload.get("upload", False),
                    privacy=payload.get("privacy", "private"),
                    progress_callback=worker_progress,
                    source_title=payload.get("source_title"),
                    source_channel=payload.get("source_channel"),
                )
                output_paths = []
                if isinstance(result, list):
                    output_paths = [str(p) for p in result]
                else:
                    output_paths = [str(result)]

                check_cancelled()
                job_queue.update_status(
                    job.id,
                    JobStatus.DONE,
                    progress=100,
                    output_paths=output_paths,
                    result={"clip_count": len(output_paths)},
                )
                logger.info("✅ Job finished successfully: %s", job.id)

            elif job.kind == "render":
                layout = payload.get("layout", "crop_center")
                from shorts_clipper.rendering.pipe import stream_render_pipeline

                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                current_output_path = settings.output_dir / f"custom_{ts}.mp4"
                settings.output_dir.mkdir(parents=True, exist_ok=True)

                with tempfile.TemporaryDirectory(prefix="worker_render_") as work_dir:
                    work_path = Path(work_dir)
                    audio_path = work_path / "audio.m4a"

                    logger.info("⬇ Downloading lightweight audio section for Whisper...")
                    from shorts_clipper.downloader.yt_dlp import download_audio
                    from shorts_clipper.transcription.whisper import transcribe_clip

                    download_audio(
                        payload["url"],
                        audio_path,
                        start_time=payload["start"],
                        end_time=payload["end"],
                    )
                    worker_progress(30)

                    logger.info("🎙 Transcribing audio segment for word-level captions...")
                    precision_segments = transcribe_clip(audio_path)
                    worker_progress(50)

                    logger.info(
                        "🚀 Launching Stream-Piped Rendering (Bypassing intermediate disk writes)..."
                    )
                    stream_render_pipeline(
                        url=payload["url"],
                        start_time=payload["start"],
                        end_time=payload["end"],
                        layout=layout,
                        segments=precision_segments,
                        output_path=current_output_path,
                        pacing=1.15,
                        video_codec=settings.video_codec,
                        preset=settings.video_preset,
                        subtitle_style=payload.get("subtitle_style", settings.subtitle_style),
                        enable_gpu=settings.enable_gpu,
                    )
                    worker_progress(90)

                try:
                    from shorts_clipper.render.thumbnailer import extract_thumbnail

                    extract_thumbnail(current_output_path)
                except Exception as thumb_err:
                    logger.warning("Thumbnail extraction failed: %s", thumb_err)

                meta = {
                    "title": None,
                    "description": None,
                    "tags": [],
                    "publish_status": "idle",
                    "publish_error": None,
                }
                try:
                    provider = get_provider(settings)
                    ai_meta = provider.generate_clip_metadata(precision_segments)
                    meta["title"] = ai_meta["title"]
                    meta["description"] = ai_meta["description"]
                    meta["tags"] = ai_meta["tags"]
                    logger.info("🧠 Generated metadata — Title: %s", meta["title"])
                except Exception as meta_err:
                    logger.error("❌ METADATA GENERATION FAILED for render job: %s", meta_err)
                    meta["title"] = None
                    meta["description"] = None
                    meta["publish_error"] = f"Metadata generation failed: {meta_err}"

                meta["segments"] = [
                    {"start": s.start, "end": s.end, "text": s.text} for s in precision_segments
                ]
                meta_path = current_output_path.with_suffix(".json")
                meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

                check_cancelled()
                job_queue.update_status(
                    job.id,
                    JobStatus.DONE,
                    progress=100,
                    output_paths=[str(current_output_path)],
                    result={"clip_count": 1},
                )
                logger.info("✅ Precise Render job finished successfully: %s", job.id)

            else:
                raise ValueError(f"Unknown job kind: {job.kind}")

        except JobCancelledError as exc:
            logger.warning("❌ Job %s cancelled: %s", job.id, exc)
            cancel_active_processes()
        except Exception as exc:
            logger.exception("❌ Job %s failed: %s", job.id, exc)
            cancel_active_processes()
            job_queue.update_status(job.id, JobStatus.FAILED, error=str(exc))
            if job.kind == "clip":
                try:
                    watchdog.record_failure(job.payload, str(exc))
                except Exception:
                    logger.exception("Could not schedule a retry for job %s", job.id)
        finally:
            active_job_id = None
            with _proc_lock:
                _active_processes.clear()


if __name__ == "__main__":
    try:
        run_worker()
    except KeyboardInterrupt:
        logger.info("Worker shutting down...")
        cancel_active_processes()
        sys.exit(0)
