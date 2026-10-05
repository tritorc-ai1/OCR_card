from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    name: str
    date: str
    location: str | None = None
    booth: str | None = None
    notes: str | None = None


class EventOut(EventIn):
    event_id: str


class OCRTextBlock(BaseModel):
    text: str
    confidence: float
    bbox: list[Any] = Field(default_factory=list)
    side: Literal["front", "back"]
    line_index: int
    engine: str | None = None
    variant: str | None = None
    normalized_text: str | None = None
    # Relative text height vs. the card's median line height. Large text is a
    # strong signal for a person's name or the company/brand name; used by the
    # candidate extractor and the Gemini prompt to disambiguate the two.
    size_tag: Literal["large", "normal", "small"] | None = None
    # Vertical position band within the card image (top/middle/bottom of bbox).
    position_band: Literal["top", "middle", "bottom"] | None = None


class OCRSideResult(BaseModel):
    side: Literal["front", "back"]
    raw_text: str
    average_confidence: float
    blocks: list[OCRTextBlock] = Field(default_factory=list)
    engine: str
    engine_version: str | None = None
    variant: str = "original"
    runtime_ms: int | None = None
    status: Literal["ok", "error", "skipped"] = "ok"
    error_message: str | None = None


class FieldCandidate(BaseModel):
    field: str
    value: str
    confidence: float
    source: str
    evidence: str | None = None


class BusinessCardRecord(BaseModel):
    record_id: str
    card_id: str
    event_id: str
    date: str
    time: str
    event_name: str
    name: str | None = None
    designation: str | None = None
    company: str | None = None
    business: str | None = None
    phone_primary: str | None = None
    phone_number: str | None = None
    mobile_number: str | None = None
    phone_extra: str | None = None
    fax_number: str | None = None
    country_code: str | None = None
    email: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    zip_code: str | None = None
    category: str | None = None
    social_media: str | None = None
    notes: str | None = None
    email1: str | None = None
    email2: str | None = None
    contact1: str | None = None
    contact2: str | None = None
    contact3: str | None = None
    confidence_score: Literal["High", "Medium", "Low"] = "Low"
    low_confidence_fields: list[str] = Field(default_factory=list)
    duplicate_flag: str = "No"
    front_image_filename: str | None = None
    back_image_filename: str | None = None
    reviewed_by_user: bool = False
    # Email of the user who scanned this card. None on records created before
    # per-user tracking existed; deliberately excluded from update_record's
    # PATCH allow-list so it can't be rewritten after creation.
    scanned_by: str | None = None


class ProcessingResult(BaseModel):
    card: BusinessCardRecord
    status: Literal["processed", "needs_review", "error"]
    error_message: str | None = None


class UploadResponse(BaseModel):
    total: int
    processed: int
    errors: int
    results: list[ProcessingResult]


class UpdateRecordIn(BaseModel):
    name: str | None = None
    designation: str | None = None
    company: str | None = None
    business: str | None = None
    phone_primary: str | None = None
    phone_number: str | None = None
    mobile_number: str | None = None
    phone_extra: str | None = None
    fax_number: str | None = None
    country_code: str | None = None
    email: str | None = None
    website: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    zip_code: str | None = None
    category: str | None = None
    social_media: str | None = None
    notes: str | None = None
    email1: str | None = None
    email2: str | None = None
    contact1: str | None = None
    contact2: str | None = None
    contact3: str | None = None
