from __future__ import annotations

import base64
import binascii
import json
import time
from pathlib import Path
from typing import Any

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from backend.config import (
    GOOGLE_APPLICATION_CREDENTIALS,
    GOOGLE_CREDENTIALS_JSON,
    GOOGLE_VISION_LANGUAGE_HINTS,
    GOOGLE_VISION_MODEL,
    GOOGLE_VISION_TIMEOUT_SECONDS,
)
from backend.llm.usage_monitor import record_usage
from backend.models import OCRSideResult, OCRTextBlock

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
VISION_SCOPES = ["https://www.googleapis.com/auth/cloud-vision"]


def is_google_vision_configured() -> bool:
    if GOOGLE_CREDENTIALS_JSON:
        return True
    return bool(GOOGLE_APPLICATION_CREDENTIALS) and Path(GOOGLE_APPLICATION_CREDENTIALS).exists()


def _parse_credentials_json(raw: str) -> dict[str, Any]:
    """Accept the service-account JSON either as raw JSON or base64-encoded JSON."""
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        decoded = base64.b64decode(text).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not valid JSON or base64-encoded JSON") from exc
    return json.loads(decoded)


def _load_credentials() -> service_account.Credentials:
    if GOOGLE_CREDENTIALS_JSON:
        info = _parse_credentials_json(GOOGLE_CREDENTIALS_JSON)
        return service_account.Credentials.from_service_account_info(info, scopes=VISION_SCOPES)
    return service_account.Credentials.from_service_account_file(
        GOOGLE_APPLICATION_CREDENTIALS,
        scopes=VISION_SCOPES,
    )


def _access_token() -> str:
    credentials = _load_credentials()
    credentials.refresh(Request())
    return credentials.token


def _bbox(vertices: list[dict[str, Any]] | None) -> list[dict[str, int]]:
    return [
        {"x": int(vertex.get("x", 0)), "y": int(vertex.get("y", 0))}
        for vertex in vertices or []
    ]


def _average_confidence(annotation: dict[str, Any]) -> float:
    values: list[float] = []
    for page in annotation.get("pages", []):
        if "confidence" in page:
            values.append(float(page["confidence"]))
        for block in page.get("blocks", []):
            if "confidence" in block:
                values.append(float(block["confidence"]))
            for paragraph in block.get("paragraphs", []):
                if "confidence" in paragraph:
                    values.append(float(paragraph["confidence"]))
                for word in paragraph.get("words", []):
                    if "confidence" in word:
                        values.append(float(word["confidence"]))
    return round(sum(values) / len(values), 4) if values else 0.0


def _bbox_height(bbox: list[dict[str, int]]) -> float:
    ys = [point["y"] for point in bbox if "y" in point]
    return float(max(ys) - min(ys)) if len(ys) >= 2 else 0.0


def _position_band(bbox: list[dict[str, int]], page_height: float) -> str | None:
    if not bbox or not page_height:
        return None
    ys = [point["y"] for point in bbox if "y" in point]
    if not ys:
        return None
    center = sum(ys) / len(ys)
    ratio = center / page_height
    if ratio < 0.33:
        return "top"
    if ratio > 0.66:
        return "bottom"
    return "middle"


def _structured_lines(annotation: dict[str, Any], side: str, average_confidence: float) -> list[OCRTextBlock]:
    """Extract lines with real bbox/height from Vision's paragraph structure.

    This is the layout-aware path: it preserves position and relative text
    size so downstream name/company disambiguation isn't limited to reading
    order alone (a card's largest text is usually the person's name or the
    company/brand, and that signal is otherwise lost if we only split on
    newlines).
    """
    raw_lines: list[tuple[str, float, list[dict[str, int]], float]] = []  # text, confidence, bbox, height
    page_height = 0.0
    for page in annotation.get("pages", []):
        page_height = max(page_height, float(page.get("height", 0) or 0))
        for block in page.get("blocks", []):
            for paragraph in block.get("paragraphs", []):
                words = []
                confidences = []
                bbox: list[dict[str, int]] = []
                for word in paragraph.get("words", []):
                    text = "".join(symbol.get("text", "") for symbol in word.get("symbols", []))
                    if text:
                        words.append(text)
                    if "confidence" in word:
                        confidences.append(float(word["confidence"]))
                    word_bbox = _bbox((word.get("boundingBox") or {}).get("vertices"))
                    if word_bbox:
                        bbox = bbox + word_bbox if bbox else word_bbox
                line = " ".join(words).strip()
                if not line:
                    continue
                confidence = round(sum(confidences) / len(confidences), 4) if confidences else average_confidence
                raw_lines.append((line, confidence, bbox, _bbox_height(bbox)))

    if not raw_lines:
        return []

    heights = sorted(height for *_rest, height in raw_lines if height > 0)
    median_height = heights[len(heights) // 2] if heights else 0.0

    blocks: list[OCRTextBlock] = []
    for line_index, (text, confidence, bbox, height) in enumerate(raw_lines):
        size_tag = None
        if median_height > 0 and height > 0:
            ratio = height / median_height
            size_tag = "large" if ratio >= 1.4 else "small" if ratio <= 0.7 else "normal"
        blocks.append(
            OCRTextBlock(
                text=text,
                confidence=confidence,
                bbox=bbox,
                side=side,
                line_index=line_index,
                engine="google_vision",
                variant="document_text_detection",
                normalized_text=" ".join(text.lower().split()),
                size_tag=size_tag,
                position_band=_position_band(bbox, page_height),
            )
        )
    return blocks


def _line_blocks(annotation: dict[str, Any], raw_text: str, side: str, average_confidence: float) -> list[OCRTextBlock]:
    structured = _structured_lines(annotation, side, average_confidence)
    if structured:
        return structured

    # Fallback for responses without paragraph/word structure: split on
    # newlines with no bbox/size signal (still better than nothing).
    blocks: list[OCRTextBlock] = []
    line_index = 0
    for line in raw_text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        blocks.append(
            OCRTextBlock(
                text=clean,
                confidence=average_confidence,
                side=side,
                line_index=line_index,
                engine="google_vision",
                variant="document_text_detection",
                normalized_text=" ".join(clean.lower().split()),
            )
        )
        line_index += 1
    return blocks


def _annotate_image(image_bytes: bytes) -> dict[str, Any]:
    """POST one image to Google Vision and return its response entry.

    One image = one billable Vision unit. Callers own usage recording so a
    single logical operation (e.g. a stitched front+back composite) is charged
    exactly once. Raises on transport errors or a Vision-level error payload.
    """
    token = _access_token()
    request: dict[str, Any] = {
        "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
        "features": [
            {
                "type": "DOCUMENT_TEXT_DETECTION",
                "model": GOOGLE_VISION_MODEL,
            }
        ],
    }
    if GOOGLE_VISION_LANGUAGE_HINTS:
        request["imageContext"] = {"languageHints": GOOGLE_VISION_LANGUAGE_HINTS}
    payload = {"requests": [request]}
    response = requests.post(
        VISION_ENDPOINT,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=GOOGLE_VISION_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    item = (body.get("responses") or [{}])[0]
    if item.get("error"):
        raise RuntimeError(item["error"].get("message", "Google Vision OCR error"))
    return item


def extract_text(image_bytes: bytes, side: str, event_id: str | None = None) -> OCRSideResult:
    started = time.perf_counter()
    try:
        item = _annotate_image(image_bytes)
        annotation = item.get("fullTextAnnotation") or {}
        raw_text = (annotation.get("text") or "").strip()
        average_confidence = _average_confidence(annotation)
        result = OCRSideResult(
            side=side,
            raw_text=raw_text,
            average_confidence=average_confidence,
            blocks=_line_blocks(annotation, raw_text, side, average_confidence),
            engine="google_vision",
            engine_version="v1",
            variant="document_text_detection",
            runtime_ms=int((time.perf_counter() - started) * 1000),
            status="ok" if raw_text else "skipped",
            error_message=None if raw_text else "No text detected",
        )
        if event_id:
            record_usage(
                event_id,
                provider="google_vision",
                model=GOOGLE_VISION_MODEL,
                purpose=f"document_text_detection_{side}",
                prompt_tokens=0,
                unit_count=1,
                status=result.status,
                error_message=result.error_message,
            )
        return result
    except Exception as exc:
        result = OCRSideResult(
            side=side,
            raw_text="",
            average_confidence=0.0,
            blocks=[],
            engine="google_vision",
            engine_version="v1",
            variant="document_text_detection",
            runtime_ms=int((time.perf_counter() - started) * 1000),
            status="error",
            error_message=str(exc)[:500],
        )
        if event_id:
            record_usage(
                event_id,
                provider="google_vision",
                model=GOOGLE_VISION_MODEL,
                purpose=f"document_text_detection_{side}",
                prompt_tokens=0,
                unit_count=1,
                status="error",
                error_message=result.error_message,
            )
        return result


def _block_center_y(block: OCRTextBlock) -> float:
    ys = [point["y"] for point in (block.bbox or []) if isinstance(point, dict) and "y" in point]
    return sum(ys) / len(ys) if ys else 0.0


def _rebuild_side(
    blocks: list[OCRTextBlock],
    side: str,
    average_confidence: float,
    runtime_ms: int,
    y_offset: int,
    region_height: float,
) -> OCRSideResult:
    """Re-key a slice of composite blocks into a single-side result.

    bboxes are shifted into side-local coordinates and ``position_band`` is
    recomputed against that side's own height so the front/back layout cues
    match what a per-side OCR would have produced.
    """
    out_blocks: list[OCRTextBlock] = []
    texts: list[str] = []
    for line_index, block in enumerate(blocks):
        local_bbox = [
            {"x": point.get("x", 0), "y": point.get("y", 0) - y_offset}
            for point in (block.bbox or [])
            if isinstance(point, dict)
        ]
        band = _position_band(local_bbox, region_height) if local_bbox and region_height else block.position_band
        out_blocks.append(
            OCRTextBlock(
                text=block.text,
                confidence=block.confidence,
                bbox=local_bbox or block.bbox,
                side=side,
                line_index=line_index,
                engine="google_vision",
                variant="document_text_detection",
                normalized_text=block.normalized_text,
                size_tag=block.size_tag,
                position_band=band,
            )
        )
        texts.append(block.text)
    raw_text = "\n".join(texts).strip()
    return OCRSideResult(
        side=side,
        raw_text=raw_text,
        average_confidence=average_confidence,
        blocks=out_blocks,
        engine="google_vision",
        engine_version="v1",
        variant="document_text_detection",
        runtime_ms=runtime_ms,
        status="ok" if raw_text else "skipped",
        error_message=None if raw_text else "No text detected",
    )


def extract_text_combined(
    front_bytes: bytes,
    back_bytes: bytes | None,
    event_id: str | None = None,
) -> list[OCRSideResult]:
    """OCR front (+ optional back) using a SINGLE Vision call when a back exists.

    Front and back are stitched into one composite image so Google bills one
    unit for a two-sided card. The combined annotation is then split back into
    separate front/back results by the seam y-coordinate, preserving the
    per-side contract the structuring step expects. Falls back to two separate
    calls only if stitching is unavailable (e.g. Pillow missing).
    """
    if not back_bytes:
        return [extract_text(front_bytes, "front", event_id)]

    try:
        from backend.imaging.preprocess import stitch_vertical

        composite_bytes, seam_y = stitch_vertical(front_bytes, back_bytes)
    except Exception:
        return [
            extract_text(front_bytes, "front", event_id),
            extract_text(back_bytes, "back", event_id),
        ]

    started = time.perf_counter()
    try:
        item = _annotate_image(composite_bytes)
        annotation = item.get("fullTextAnnotation") or {}
        average_confidence = _average_confidence(annotation)
        raw_text = (annotation.get("text") or "").strip()
        blocks = _line_blocks(annotation, raw_text, "front", average_confidence)
        runtime_ms = int((time.perf_counter() - started) * 1000)

        front_blocks = [block for block in blocks if _block_center_y(block) < seam_y]
        back_blocks = [block for block in blocks if _block_center_y(block) >= seam_y]
        back_ys = [_block_center_y(block) for block in back_blocks]
        back_height = (max(back_ys) - seam_y) if back_ys else 0.0

        front_result = _rebuild_side(front_blocks, "front", average_confidence, runtime_ms, 0, float(seam_y))
        back_result = _rebuild_side(back_blocks, "back", average_confidence, runtime_ms, seam_y, back_height)

        if event_id:
            record_usage(
                event_id,
                provider="google_vision",
                model=GOOGLE_VISION_MODEL,
                purpose="document_text_detection_combined",
                prompt_tokens=0,
                unit_count=1,
                status=front_result.status,
                error_message=front_result.error_message,
            )
        return [front_result, back_result]
    except Exception as exc:
        runtime_ms = int((time.perf_counter() - started) * 1000)
        if event_id:
            record_usage(
                event_id,
                provider="google_vision",
                model=GOOGLE_VISION_MODEL,
                purpose="document_text_detection_combined",
                prompt_tokens=0,
                unit_count=1,
                status="error",
                error_message=str(exc)[:500],
            )
        return [
            OCRSideResult(
                side=side,
                raw_text="",
                average_confidence=0.0,
                blocks=[],
                engine="google_vision",
                engine_version="v1",
                variant="document_text_detection",
                runtime_ms=runtime_ms,
                status="error",
                error_message=str(exc)[:500],
            )
            for side in ("front", "back")
        ]
