from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageEnhance, ImageOps


def _jpeg_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.convert("RGB").save(output, format="JPEG", quality=92)
    return output.getvalue()


def image_variants(base_image_bytes: bytes) -> dict[str, bytes]:
    with Image.open(BytesIO(base_image_bytes)) as raw:
        image = ImageOps.exif_transpose(raw).convert("RGB")
        variants = {"original_normalized": _jpeg_bytes(image)}

        contrast = ImageEnhance.Contrast(image).enhance(1.45)
        variants["contrast_enhanced"] = _jpeg_bytes(contrast)

        gray = ImageOps.grayscale(image)
        scale = 2 if max(image.size) < 1400 else 1
        if scale > 1:
            gray = gray.resize((gray.width * scale, gray.height * scale), Image.Resampling.LANCZOS)
        variants["grayscale_upscaled"] = _jpeg_bytes(gray.convert("RGB"))

        threshold = gray.point(lambda px: 255 if px > 165 else 0)
        variants["adaptive_threshold"] = _jpeg_bytes(threshold.convert("RGB"))

    return variants

