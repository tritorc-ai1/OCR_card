"""Tests for the stitched front+back single-call OCR (one Vision unit per card)."""

import backend.imaging.preprocess as pp
import backend.ocr.google_vision as gv


def _word(text: str, y: int) -> dict:
    return {
        "symbols": [{"text": ch} for ch in text],
        "confidence": 0.9,
        "boundingBox": {
            "vertices": [
                {"x": 10, "y": y},
                {"x": 100, "y": y},
                {"x": 100, "y": y + 20},
                {"x": 10, "y": y + 20},
            ]
        },
    }


def _fake_item() -> dict:
    """A composite annotation: one line above the seam, one below (seam=300)."""
    return {
        "fullTextAnnotation": {
            "text": "FRONTLINE\nBACKLINE",
            "pages": [
                {
                    "height": 620,
                    "blocks": [
                        {"paragraphs": [{"words": [_word("FRONTLINE", 50)]}]},
                        {"paragraphs": [{"words": [_word("BACKLINE", 400)]}]},
                    ],
                }
            ],
        }
    }


def test_combined_two_sided_records_single_vision_unit(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(pp, "stitch_vertical", lambda front, back, separator=8: (b"composite", 300))
    monkeypatch.setattr(gv, "_annotate_image", lambda image_bytes: _fake_item())
    monkeypatch.setattr(gv, "record_usage", lambda *args, **kwargs: calls.append(kwargs))

    results = gv.extract_text_combined(b"front", b"back", event_id="evt1")

    assert len(results) == 2
    assert results[0].side == "front" and "FRONTLINE" in results[0].raw_text
    assert results[1].side == "back" and "BACKLINE" in results[1].raw_text
    # A two-sided card must bill exactly one Google Vision unit.
    vision_units = [call for call in calls if call.get("provider") == "google_vision"]
    assert len(vision_units) == 1
    assert vision_units[0].get("unit_count") == 1


def test_combined_front_only_is_single_call(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(gv, "_annotate_image", lambda image_bytes: _fake_item())
    monkeypatch.setattr(gv, "record_usage", lambda *args, **kwargs: calls.append(kwargs))

    results = gv.extract_text_combined(b"front", None, event_id="evt1")

    assert len(results) == 1
    assert results[0].side == "front"
    assert len([call for call in calls if call.get("provider") == "google_vision"]) == 1


def test_stitch_vertical_reports_seam_and_matches_width() -> None:
    from io import BytesIO

    from PIL import Image

    def make(width: int, height: int) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (width, height), (200, 200, 200)).save(buffer, format="JPEG")
        return buffer.getvalue()

    composite_bytes, seam_y = pp.stitch_vertical(make(600, 300), make(500, 250))
    composite = Image.open(BytesIO(composite_bytes))
    assert seam_y == 300
    assert composite.width == 600  # back resized to front width
