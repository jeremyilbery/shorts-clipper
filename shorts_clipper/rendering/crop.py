"""Video crop + vertical layout processor using FFmpeg.

Single-pass scale+crop to 1080×1920.  No Python frame loop,
no MoviePy, no zoompan (which forces 30 fps and tanks CPU perf).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from shorts_clipper.cropping.geometry import compute_center_crop
from shorts_clipper.utils.video import get_video_metadata

log = logging.getLogger(__name__)

_TARGET_W = 1080
_TARGET_H = 1920

# "landscape" layout: keep the full 16:9 picture for long-form uploads.
_LANDSCAPE_W = 1920
_LANDSCAPE_H = 1080


def _target_size(layout: str) -> tuple[int, int]:
    if layout == "landscape":
        return _LANDSCAPE_W, _LANDSCAPE_H
    return _TARGET_W, _TARGET_H


def _build_crop_filter(src_w: int, src_h: int, layout: str) -> str:
    """Return an FFmpeg -vf / -filter_complex string for the requested layout."""
    src_ratio = src_w / src_h

    if layout == "landscape":
        # No crop: fit the whole frame into 1920x1080, letterboxing if the source isn't 16:9.
        return (
            f"scale={_LANDSCAPE_W}:{_LANDSCAPE_H}:force_original_aspect_ratio=decrease,"
            f"pad={_LANDSCAPE_W}:{_LANDSCAPE_H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )

    if layout == "crop_left":
        scale_h = _TARGET_H
        scale_w = max(_TARGET_W, round(_TARGET_H * src_ratio) // 2 * 2)
        return f"scale={scale_w}:{scale_h},crop={_TARGET_W}:{_TARGET_H}:0:0,setsar=1"

    if layout == "crop_right":
        scale_h = _TARGET_H
        scale_w = max(_TARGET_W, round(_TARGET_H * src_ratio) // 2 * 2)
        x_offset = scale_w - _TARGET_W
        return f"scale={scale_w}:{scale_h},crop={_TARGET_W}:{_TARGET_H}:{x_offset}:0,setsar=1"

    # Default: crop_center — compute precise center crop box
    crop = compute_center_crop(
        width=src_w,
        height=src_h,
        target_width=_TARGET_W,
        target_height=_TARGET_H,
    )
    # Scale so the crop region exactly fills the target frame
    scale = max(_TARGET_W / crop.width, _TARGET_H / crop.height)
    scaled_w = round(src_w * scale) // 2 * 2
    scaled_h = round(src_h * scale) // 2 * 2
    x = (scaled_w - _TARGET_W) // 2
    y = (scaled_h - _TARGET_H) // 2
    return f"scale={scaled_w}:{scaled_h},crop={_TARGET_W}:{_TARGET_H}:{x}:{y},setsar=1"


def process_to_vertical(
    input_path: str | Path,
    output_path: str | Path,
    *,
    layout: str = "crop_center",
    crf: int = 28,
    preset: str = "ultrafast",
    video_codec: str = "libx264",
    start_time: float | None = None,
    duration: float | None = None,
) -> Path:
    """
    Crop and scale a video to 1080×1920 vertical using pure FFmpeg.

    Args:
        input_path: Source video file.
        output_path: Destination file (will be overwritten if it exists).
        layout: crop_center | crop_left | crop_right.
        crf: Constant rate factor (18 = near-lossless, 23 = default).
        preset: FFmpeg x264 preset (fast / medium / slow).
        video_codec: FFmpeg video encoder codec to use.

    Returns:
        Path to the output file.

    Raises:
        RuntimeError: If FFmpeg exits with a non-zero status.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    meta = get_video_metadata(str(input_path))
    vf = _build_crop_filter(meta.width, meta.height, layout)

    out_w, out_h = _target_size(layout)
    log.info(
        "\n--- %s [%s] %dx%d → %dx%d ---",
        "LANDSCAPE SCALE" if layout == "landscape" else "VERTICAL CROP",
        layout,
        meta.width,
        meta.height,
        out_w,
        out_h,
    )

    cmd = [
        "ffmpeg",
        "-y",
    ]
    if start_time is not None:
        cmd.extend(["-ss", str(start_time)])
    if duration is not None:
        cmd.extend(["-t", str(duration)])

    cmd.extend(
        [
            "-i",
            str(input_path),
            "-vf",
            vf,
            "-c:v",
            video_codec,
        ]
    )

    if video_codec == "libx264":
        cmd.extend(["-crf", str(crf), "-preset", preset])
    elif video_codec == "h264_nvenc":
        cmd.extend(["-rc:v", "vbr", "-cq", str(crf), "-preset", preset])
    else:
        cmd.extend(["-preset", preset])

    cmd.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-use_editlist",
            "0",
            str(output_path),
        ]
    )

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("FFmpeg crop stderr:\n%s", result.stderr[-3000:])
        raise RuntimeError(f"FFmpeg crop failed (exit {result.returncode})")

    log.info("✅ Vertical crop done → %s", output_path)
    return output_path
