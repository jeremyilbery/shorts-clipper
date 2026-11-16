"""Designed YouTube thumbnails for long-form clips.

A raw video frame looks like YouTube's own auto-pick. This builds a proper 1280x720
thumbnail instead:

1. Samples frames across the clip and keeps the sharpest, well-lit one.
2. Zooms in from the top so faces are bigger and burned-in captions (bottom of frame)
   are cropped away.
3. Boosts contrast/colour, darkens the bottom, and sets a 2-4 word headline in heavy
   type with a thick outline.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from shorts_clipper.utils.video import get_video_metadata

log = logging.getLogger(__name__)

WIDTH, HEIGHT = 1280, 720
# Burned-in captions sit in the bottom ~20% of the clip; thumbnails never show that band.
CAPTION_FREE = 0.80
ZOOM = 1 / CAPTION_FREE
SAMPLES = 12
ACCENT = (255, 212, 0)
WHITE = (255, 255, 255)

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\impact.ttf",
    r"C:\Windows\Fonts\ariblk.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
]


def _font_path() -> str | None:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def headline_from_title(title: str, max_words: int = 4) -> str:
    """Fallback headline: the punchiest short phrase we can get from the title."""
    text = re.sub(r"#\w+", "", title or "").strip()
    # Prefer the part after "X on ..." / before a colon or dash, whichever is shorter.
    parts = re.split(r"\s*[:|–—-]\s*", text)
    text = min((p for p in parts if p), key=len, default=text)
    text = re.sub(r"^(joe rogan|rogan|theo von|lex fridman)\s+(on|about)\s+", "", text, flags=re.I)
    text = re.sub(r"^(the|a|an)\s+", "", text, flags=re.I)
    words = re.sub(r"[^\w'’!?$% ]", "", text).split()[:max_words]
    while len(words) > 1 and words[-1].lower() in _TRAILING_FILLER:
        words.pop()
    return " ".join(words).upper()


_TRAILING_FILLER = {"and", "or", "of", "the", "a", "an", "to", "in", "on", "for", "with", "is", "his", "her"}


def _sample_frames(video: Path, work: Path) -> list[Path]:
    try:
        duration = get_video_metadata(str(video)).duration
    except Exception:
        duration = 0.0
    if duration <= 0:
        duration = 10.0
    frames = []
    for i in range(SAMPLES):
        t = duration * (0.08 + 0.84 * i / max(1, SAMPLES - 1))
        out = work / f"f{i:02d}.jpg"
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)],
            capture_output=True,
        )
        if r.returncode == 0 and out.exists():
            frames.append(out)
    return frames


_face_cascade = None


def _detect_face(img) -> tuple[int, int, int, int] | None:
    """Largest front-facing face as (x, y, w, h) in image pixels, or None."""
    global _face_cascade
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    if _face_cascade is None:
        _face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    w, h = img.size
    scale = 640 / w
    gray = np.asarray(img.convert("L").resize((640, int(h * scale))))
    faces = _face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(36, 36))
    # Ignore hits in the bottom strip, where burned-in captions live.
    faces = [f for f in faces if (f[1] + f[3]) < gray.shape[0] * CAPTION_FREE]
    if not faces:
        return None
    x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
    return int(x / scale), int(y / scale), int(fw / scale), int(fh / scale)


def _frame_score(img) -> tuple[float, tuple[int, int, int, int] | None]:
    """Frames with a big, sharp face win; otherwise sharp, well-lit frames."""
    from PIL import ImageFilter, ImageStat

    face = _detect_face(img)
    small = img.convert("L").resize((320, 180)).crop((0, 0, 320, int(180 / ZOOM)))
    sharpness = ImageStat.Stat(small.filter(ImageFilter.FIND_EDGES)).mean[0]
    stat = ImageStat.Stat(small)
    light_penalty = min(abs(stat.mean[0] - 115) / 115, 1.0)
    base = sharpness * (1.0 - 0.6 * light_penalty) + 0.15 * stat.stddev[0]
    if face is None:
        return base, None
    face_share = face[3] / img.size[1]
    return 100.0 + 300.0 * face_share + 0.2 * base, face


def _crop_zoom(img, face: tuple[int, int, int, int] | None = None):
    from PIL import Image

    src_w, src_h = img.size
    cover = max(WIDTH / src_w, HEIGHT / src_h)
    if face is None:
        scale = cover * ZOOM
        fx, fy = src_w / 2, 0.0
    else:
        x, y, fw, fh = face
        # Zoom until the face is ~34% of the thumbnail height (but never zoom out).
        scale = min(max(cover * ZOOM, 0.34 * HEIGHT / fh), cover * 2.6)
        fx, fy = x + fw / 2, y
    img = img.resize((int(src_w * scale), int(src_h * scale)), Image.LANCZOS)
    w, h = img.size
    if face is None:
        left, top = (w - WIDTH) // 2, 0
    else:
        # Face centred horizontally, its top ~9% from the top edge; text goes below.
        left = int(fx * scale - WIDTH / 2)
        top = int(fy * scale - HEIGHT * 0.09)
    left = max(0, min(left, w - WIDTH))
    top = max(0, min(top, int(h * CAPTION_FREE) - HEIGHT))
    out = img.crop((left, top, left + WIDTH, top + HEIGHT))
    if face is None:
        return out, None
    x, y, fw, fh = face
    # Face box in thumbnail coordinates, with room for the chin/mouth below it.
    fy0 = int(y * scale) - top
    fy1 = int((y + fh * 1.25) * scale) - top
    return out, (int(x * scale) - left, fy0, int(fw * scale), fy1 - fy0)


def _wrap(words: list[str], draw, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for word in words:
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= max_width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


def compose(frame, headline: str, face: tuple[int, int, int, int] | None = None):
    """Build the finished thumbnail image from a frame and a headline."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFont

    img, face_box = _crop_zoom(frame.convert("RGB"), face)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    img = ImageEnhance.Color(img).enhance(1.2)
    img = ImageEnhance.Sharpness(img).enhance(1.3)

    headline = (headline or "").strip().upper()
    margin_top, margin_bottom = 30, 40

    # Put the headline in whichever band (above or below the face) has more room.
    at_top = False
    max_h = int(HEIGHT * 0.42)
    if face_box is not None:
        free_top = face_box[1] - margin_top
        free_bottom = HEIGHT - margin_bottom - (face_box[1] + face_box[3])
        at_top = free_top > free_bottom
        free = max(free_top, free_bottom)
        max_h = int(min(max(free, HEIGHT * 0.24), HEIGHT * 0.42))

    # Gradient on the text side so the text pops on any background.
    shade = Image.new("L", (1, HEIGHT))
    span = int(HEIGHT * 0.58)
    for y in range(HEIGHT):
        d = y if at_top else HEIGHT - 1 - y  # distance from the text edge
        shade.putpixel((0, y), int(215 * (1 - d / span) ** 1.3) if d < span else 0)
    black = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    img = Image.composite(black, img, shade.resize((WIDTH, HEIGHT)))

    if not headline:
        return img

    draw = ImageDraw.Draw(img)
    font_file = _font_path()
    words = headline.split()
    max_w = WIDTH - 90

    # Largest size where the headline fits in at most 2 lines.
    size, lines, font = 190, [headline], None
    while size >= 48:
        font = ImageFont.truetype(font_file, size) if font_file else ImageFont.load_default()
        lines = _wrap(words, draw, font, max_w)
        line_h = size * 1.02
        if len(lines) <= 2 and line_h * len(lines) <= max_h:
            break
        size -= 6
    if font is None:
        return img

    stroke = max(4, size // 14)
    line_h = int(size * 1.02)
    y = margin_top if at_top else HEIGHT - margin_bottom - line_h * len(lines)
    def _put(x: int, text: str, colour) -> None:
        draw.text((x + 6, y + 7), text, font=font, fill=(0, 0, 0), stroke_width=stroke, stroke_fill=(0, 0, 0))
        draw.text((x, y), text, font=font, fill=colour, stroke_width=stroke, stroke_fill=(0, 0, 0))

    for i, line in enumerate(lines):
        x = 45
        if len(lines) > 1:
            # Last line in the accent colour, like most podcast clip channels.
            _put(x, line, ACCENT if i == len(lines) - 1 else WHITE)
        elif len(words) > 1:
            # One line: accent the final word.
            head, last = line.rsplit(" ", 1)
            _put(x, head, WHITE)
            _put(x + int(draw.textlength(head + " ", font=font)), last, ACCENT)
        else:
            _put(x, line, ACCENT)
        y += line_h
    return img


def design_thumbnail(video_path: str | Path, output_path: str | Path, headline: str) -> Path:
    """Write a designed 1280x720 JPEG thumbnail for ``video_path`` to ``output_path``."""
    from PIL import Image

    video, out = Path(video_path), Path(output_path)
    with tempfile.TemporaryDirectory(prefix="thumb_") as tmp:
        frames = _sample_frames(video, Path(tmp))
        if not frames:
            raise RuntimeError(f"Could not extract any frames from {video}")
        scored = []
        for f in frames:
            with Image.open(f) as im:
                im.load()
                score, face = _frame_score(im)
                scored.append((score, str(f), face))
        _, best, face = max(scored, key=lambda s: s[0])
        with Image.open(best) as frame:
            frame.load()
            img = compose(frame, headline, face)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=90, optimize=True)
    log.info("🎨 Designed thumbnail (%r) → %s", headline, out)
    return out
