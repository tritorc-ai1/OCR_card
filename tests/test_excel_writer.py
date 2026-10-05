from io import BytesIO

from PIL import Image as PillowImage

from backend.storage.excel_writer import _add_card_image
from openpyxl import Workbook


def _fake_card_bytes(width: int = 1600, height: int = 900) -> bytes:
    buffer = BytesIO()
    PillowImage.new("RGB", (width, height), (200, 100, 50)).save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


def test_embedded_card_thumbnail_is_far_smaller_than_a_full_resolution_png() -> None:
    # Regression: _add_card_image used to decode the already-compressed source
    # JPEG and re-encode it as a lossless PNG at full resolution, even though
    # the sheet only ever displays it at a 140x84pt thumbnail — inflating a
    # ~150-250KB stored image back up to 1-1.5MB per card in the export.
    source = _fake_card_bytes()
    wb = Workbook()
    ws = wb.active
    _add_card_image(ws, source, 2, "card")

    output = BytesIO()
    wb.save(output)
    workbook_bytes = output.getvalue()

    # A workbook with one embedded thumbnail should stay in the tens of KB,
    # not balloon to the size of the (already fairly small) source image.
    assert len(workbook_bytes) < len(source)
    assert len(workbook_bytes) < 100_000


def test_add_card_image_ignores_missing_bytes() -> None:
    wb = Workbook()
    ws = wb.active
    _add_card_image(ws, None, 2, "card")  # must not raise
    assert not ws._images
