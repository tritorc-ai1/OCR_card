from __future__ import annotations

from io import BytesIO

from backend.config import IMAGE_STORE_MAX_EDGE, IMAGE_STORE_QUALITY

try:
    from PIL import Image, ImageEnhance, ImageOps
except Exception:  # pragma: no cover - dependency checked at runtime
    Image = None
    ImageEnhance = None
    ImageOps = None


def compress_for_storage(
    file_bytes: bytes,
    max_edge: int = IMAGE_STORE_MAX_EDGE,
    quality: int = IMAGE_STORE_QUALITY,
) -> bytes:
    """Return a smaller JPEG of the upload for durable storage in GridFS.

    Bakes in EXIF rotation (then drops metadata), downscales so the longest edge
    is at most ``max_edge`` px, and re-encodes JPEG at ``quality`` with optimize
    on. The result is typically several times smaller than a phone-camera
    original. Used only for the STORED/displayed copy — OCR runs separately on
    ``preprocess_image`` output, so this never affects extraction accuracy.

    Falls back to the original bytes if Pillow is unavailable or the image can't
    be decoded, so a bad input never loses the capture.
    """
    if Image is None:
        return file_bytes
    try:
        with Image.open(BytesIO(file_bytes)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            width, height = img.size
            longest = max(width, height)
            if longest > max_edge:
                scale = max_edge / longest
                img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))))
            output = BytesIO()
            img.save(output, format="JPEG", quality=quality, optimize=True)
        compressed = output.getvalue()
        # Never store something bigger than the original (e.g. tiny PNGs).
        return compressed if len(compressed) < len(file_bytes) else file_bytes
    except Exception:  # pragma: no cover - defensive; keep the capture regardless
        return file_bytes


def preprocess_image(file_bytes: bytes) -> tuple[bytes, int | None, int | None, str, list[str]]:
    if Image is None:
        return file_bytes, None, None, "Unknown", ["Pillow is not installed; image preprocessing skipped"]

    warnings: list[str] = []
    with Image.open(BytesIO(file_bytes)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        width, height = img.size
        if width < 600 or height < 300:
            warnings.append("low_resolution")
        if width > 2000 or height > 2000:
            img.thumbnail((2000, 2000))
            width, height = img.size
        gray = ImageOps.grayscale(img)
        brightness = sum(gray.histogram()[i] * i for i in range(256)) / max(1, width * height)
        if brightness < 55:
            warnings.append("dark_image")
        if brightness > 220:
            warnings.append("overexposed_image")
        img = ImageEnhance.Contrast(img).enhance(1.25)
        output = BytesIO()
        img.save(output, format="JPEG", quality=92)

    quality = "Low" if warnings else "High"
    return output.getvalue(), width, height, quality, warnings


def stitch_vertical(front_bytes: bytes, back_bytes: bytes, separator: int = 8) -> tuple[bytes, int]:
    """Stack front (top) and back (bottom) into one JPEG for a single OCR call.

    Returns ``(composite_jpeg_bytes, seam_y)`` where ``seam_y`` is the y-pixel
    boundary between the front region (above) and the back region (below). The
    back is resized to the front's width so x-coordinates stay comparable across
    sides, and a small white ``separator`` band keeps the two sides from bleeding
    into one OCR line. OCRing this composite bills a single Google Vision unit
    for a two-sided card.
    """
    if Image is None:
        raise RuntimeError("Pillow is not installed; cannot stitch images")

    with Image.open(BytesIO(front_bytes)) as front_img, Image.open(BytesIO(back_bytes)) as back_img:
        front = ImageOps.exif_transpose(front_img).convert("RGB")
        back = ImageOps.exif_transpose(back_img).convert("RGB")
        width = front.width
        if back.width != width and back.width > 0:
            new_height = max(1, round(back.height * (width / back.width)))
            back = back.resize((width, new_height))
        seam_y = front.height
        total_height = front.height + separator + back.height
        composite = Image.new("RGB", (width, total_height), (255, 255, 255))
        composite.paste(front, (0, 0))
        composite.paste(back, (0, front.height + separator))
        output = BytesIO()
        composite.save(output, format="JPEG", quality=92)
        return output.getvalue(), seam_y

