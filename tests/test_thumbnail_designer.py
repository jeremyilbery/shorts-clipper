from PIL import Image

from shorts_clipper.providers.gemini import clean_thumbnail_text
from shorts_clipper.rendering import thumbnail_designer as td


def test_headline_from_title_is_short_and_upper():
    h = td.headline_from_title("Joe Rogan on the History of Eugenics and Forced Sterilization")
    assert h == h.upper()
    assert 1 <= len(h.split()) <= 4
    assert not h.startswith("JOE ROGAN ON")
    assert h == "HISTORY OF EUGENICS"
    assert td.headline_from_title("Why Your Doctor Lies: The Real Story #jre") == "REAL STORY"


def test_clean_thumbnail_text():
    assert clean_thumbnail_text('"They lied to us" #jre') == "THEY LIED TO US"
    assert clean_thumbnail_text("one two three four five six") == "ONE TWO THREE FOUR FIVE"


def test_compose_outputs_1280x720_and_skips_caption_band():
    # Bright red caption band at the bottom 15% of a 1920x1080 frame.
    frame = Image.new("RGB", (1920, 1080), (40, 90, 140))
    frame.paste((255, 0, 0), (0, int(1080 * 0.85), 1920, 1080))
    for face in (None, (860, 700, 200, 200)):  # a face low in frame pushes the crop down
        img = td.compose(frame, "", face)
        assert img.size == (td.WIDTH, td.HEIGHT)
        r, g, b = img.getpixel((td.WIDTH // 2, td.HEIGHT - 1))
        assert not (r > 150 and g < 60), "caption band leaked into the thumbnail"


def test_compose_draws_headline():
    frame = Image.new("RGB", (1920, 1080), (40, 90, 140))
    plain = td.compose(frame, "")
    text = td.compose(frame, "THEY LIED TO US")
    assert plain.tobytes() != text.tobytes()
