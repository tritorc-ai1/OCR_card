from __future__ import annotations

import json
import logging
import re
import hashlib

logger = logging.getLogger(__name__)


def _is_config_error(message: str) -> bool:
    """A 400 INVALID_ARGUMENT means the request itself is malformed (bad
    response_schema, unsupported field, oversized payload) — a code bug that
    will fail identically on every key and every card, NOT a transient quota
    issue. It must be logged loudly: silently swallowing it degrades the whole
    app to the regex fallback with no visible signal (exactly the
    additionalProperties bug that produced garbage extractions)."""
    return "400" in message and (
        "INVALID_ARGUMENT" in message or "additional_properties" in message.lower()
    )

from app.config import BUSINESS_CATEGORIES, GEMINI_API_KEY, GEMINI_API_KEYS, GEMINI_MODEL
from app.models import OCRSideResult
from app.extraction.field_resolver import (
    COUNTRY_HINTS,
    country_name_from_code,
    infer_country_and_code,
    national_phone_number,
    normalize_country_name,
    normalize_email,
    normalize_phone,
    normalize_website,
)
from app.llm.usage_monitor import estimate_tokens, record_usage, usage_snapshot

# Deliberately NOT imported at module level: `google.genai` alone takes ~1.5s
# to import (protobuf/grpc machinery), and this module is imported eagerly by
# app/main.py at process boot — paying that cost before uvicorn can even bind
# the port measurably slows every cold start (Render free tier especially).
# Loaded lazily on first real use instead; cached after that first call.
genai = None
types = None
_genai_load_attempted = False


def _load_genai():
    global genai, types, _genai_load_attempted
    if _genai_load_attempted:
        return genai, types
    _genai_load_attempted = True
    try:
        from google import genai as _genai
        from google.genai import types as _types
        genai = _genai
        types = _types
    except Exception:  # pragma: no cover
        pass
    return genai, types


FIELDS = [
    "front_text",
    "back_text",
    "all_visible_text",
    "name",
    "designation",
    "business",
    "address",
    "city",
    "state",
    "country",
    "zip_code",
    "website",
    "category",
    "social_media",
    "notes",
    "email1",
    "email2",
    "contact1",
    "contact2",
    "contact3",
    "company",
    "phone_primary",
    "phone_number",
    "mobile_number",
    "phone_extra",
    "fax_number",
    "country_code",
    "email",
    "field_evidence",
    "uncertain_fields",
]

try:
    from enum import Enum

    from pydantic import BaseModel

    _CategoryEnum = Enum("BusinessCategory", {value: value for value in BUSINESS_CATEGORIES})

    class FieldEvidenceItem(BaseModel):
        """One (field, source-line) citation. Modeled as an explicit object
        with fixed keys rather than a dict[str, str] because Gemini's
        `response_schema` REJECTS any schema containing `additionalProperties`
        (which an open-ended dict emits) with a 400 INVALID_ARGUMENT — that
        error silently killed every image-structuring call and forced the
        whole app onto the weak regex fallback. See _evidence_to_dict for the
        conversion back to the dict shape the rest of the app stores."""

        field: str | None = None
        evidence: str | None = None

    class GeminiCardExtraction(BaseModel):
        """Response-only schema for the multimodal Gemini call. Deliberately
        excludes app-managed record fields (record_id/date/time/etc — see
        BusinessCardRecord in app/models.py) since the model must never
        produce those; category is a real enum so Gemini cannot return free
        text like a job title instead of a taxonomy value.

        IMPORTANT: every field type here must map to a Gemini-compatible JSON
        schema — no bare `dict`/`Dict` fields (they emit `additionalProperties`,
        which Gemini rejects). Use a nested BaseModel list instead."""

        front_text: str | None = None
        back_text: str | None = None
        all_visible_text: str | None = None
        name: str | None = None
        designation: str | None = None
        business: str | None = None
        address: str | None = None
        city: str | None = None
        state: str | None = None
        country: str | None = None
        zip_code: str | None = None
        website: str | None = None
        category: _CategoryEnum | None = None
        social_media: str | None = None
        notes: str | None = None
        email1: str | None = None
        email2: str | None = None
        contact1: str | None = None
        contact2: str | None = None
        contact3: str | None = None
        country_code: str | None = None
        field_evidence: list[FieldEvidenceItem] | None = None
        uncertain_fields: list[str] | None = None
except Exception:  # pragma: no cover - pydantic/enum always available in practice
    GeminiCardExtraction = None
    FieldEvidenceItem = None


def _evidence_to_dict(value) -> dict | None:
    """Normalize field_evidence into the {field: evidence} dict the rest of
    the app stores/displays. Gemini now returns it as a list of {field,
    evidence} objects (the dict shape isn't expressible in its response
    schema), but the deterministic extractor and older records still use a
    plain dict — accept both."""
    if not value:
        return None
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items() if k}
    if isinstance(value, list):
        out: dict[str, str] = {}
        for item in value:
            if isinstance(item, dict):
                field = item.get("field")
                evidence = item.get("evidence")
            else:
                field = getattr(item, "field", None)
                evidence = getattr(item, "evidence", None)
            if field:
                out[str(field)] = evidence
        return out or None
    return None

BUSINESS_CARD_VISUAL_RULES = """
Business-card layout rules learned from local samples:
- First transcribe all readable printed text from each card side before choosing fields. Field values must come from that transcript.
- Treat the front side as the primary source for name, designation, company/business, email, website, and phone fields.
- The company/business name is usually the top-most stylized brand/logo text on the front side. It may be on the top-left or top-right, not necessarily in plain body text.
- Do not use branch names, office labels, building names, service-list headings, back-side product lists, slogans, or address landmarks as the company name.
- Back sides often contain branch addresses, support facilities, product photos, service outlines, target/safety slogans, and "we sell" lists. Use these for address/category support only unless a missing contact field is clearly printed there.
- Ignore QR codes, decorative icons, certification icons, product photos, separators, and handwritten notes unless the printed text beside them is part of a contact field.
- Person name is commonly the largest personal text block, often near the center/right, with designation directly below it.
- Never use a company/legal entity/brand as the person name. Legal suffixes and business words such as PT, Pvt, Ltd, LLC, Inc, Pte, Tbk, Co, Group, Engineering, Industrial, Services, Trading, Metal, Works, Radiator, Marine, Oil, Gas, or all-caps single-token brands are company clues, not person-name clues.
- If a line could be both a brand and a name, choose it as Business/company and return name as null unless there is a separate personal-name line near a designation.
- Designation is a role title such as Director, Procurement Manager, Plant Manager, Sales Engineer, Regional Sales Manager, or Quick Contact Specialist. Do not put company/service text into designation.
- Business is the company/legal entity/brand, such as PETROSEA, Babcock & Wilcox, TOKKI, PT Kezindo Sejahtera Abadi, PT. AIR SURYA RADIATOR, PT. Mechatechra Triasindo Indonesia, CESCO, or TIMAS SUPLINDO.
- Email may be preceded by E, Email, Mail, or an envelope icon. It must contain @ and a valid domain.
- Website may be preceded by W, Web, Website, a globe icon, or printed in the footer. It is a web/domain value, not the email address.
- Phone labels can be short: T/Telp/Tel/Phone for office, M/Mob/Mobile/HP/Cell for mobile, F/Fax for fax.
- Match the reference Excel format: Email1 is primary email, Email2 is secondary email, Contact1 is the main direct/mobile number, Contact2 is office/telephone, Contact3 is fax or another printed number.
- Contact1, Contact2, and Contact3 must be digits only with country calling code included when visible or inferable, for example 60133581918 or 912241266030. Do not include +, spaces, hyphens, or parentheses.
- For country_code, trust explicit phone prefixes like +62, +91, +971 before address guesses. Indonesia/Jakarta/Kalimantan/Papua/Sorong with +62 means country_code +62.
- If the card has multiple offices on the back, do not replace the front contact phone/mobile/email with back-side branch office numbers.
""".strip()

# The single most common extraction mistake is swapping/merging the person's
# name with the company/business name. This gets its own dedicated,
# step-by-step procedure (rather than folding it into the general rules
# above) because a short bullet list wasn't enough to stop the model from
# occasionally picking the brand line as the name or vice versa.
NAME_VS_COMPANY_PROCEDURE = """
Name vs. Business/Company — follow this procedure in order every time, because
mixing these two up is the most common mistake on this task:

Step 0. READ FIRST, CLASSIFY SECOND.
   Before assigning any field, read ALL lines on the card from top to bottom.
   Write out a mental list of every line and what it most likely represents
   (company brand, person name, job title, phone, email, address, etc.).
   Do NOT jump to conclusions from the first line — the person's name may
   appear below the company name, and a large word at the top is almost always
   a brand, not a person.

Step 1. List every distinct printed line on the front side (and back side only
if the front has no company line at all).

Step 2. Mark a line as a COMPANY candidate if ANY of the following is true:
   - It contains a legal-entity suffix or prefix: PT, CV, Pvt, Ltd, LLC, Inc,
     LLP, Pte, Tbk, Sdn, Bhd, Co, Corp, Corporation, GmbH, S.A., S.p.A., B.V.,
     N.V., FZE, FZC, FZCO, WLL, W.L.L., DMCC, PLC, PJSC, JSC, SARL, Pty, K.K.
   - It contains a generic business noun: Group, Engineering, Industrial,
     Industries, Services, Solutions, Technologies, Systems, Trading, Supply,
     Manufacturing, Marine, Contractors, Oil, Gas, Energy, Construction,
     Logistics, Metal, Works, Radiator, Enterprises, Consultancy, Ventures,
     International, Global, Holdings, Traders, Exports, Imports, Impex,
     Associates, Machinery, Equipment, Chemicals, Steel, Shipping, Freight,
     Cargo, Valves, Pipes, Hardware.
   - It is the top-most stylized/logo text on the front side (brands are
     almost always at the very top, even a single stylized word like
     "PETROSEA" or "TOKKI").
   - It is repeated on the back side as a header/letterhead — a company name
     is the one constant across both sides; a person's name is not.

Step 3. Mark a line as a PERSON-NAME candidate if ALL of the following are
true:
   - It is 1-5 words, shaped like a human name (optionally with an honorific
     like Mr./Mrs./Ms./Dr./Ir./Prof. or post-nominal initials).
   - It contains NO legal-entity suffix and NO generic business noun from
     Step 2.
   - It sits directly above or below a designation/job-title line (Director,
     Manager, Engineer, Officer, Sales, Procurement, Founder, CEO, etc.), OR
     it is visually distinct in position/size from the top brand line.
   - It is not identical (ignoring case/spacing) to whatever line you already
     marked as the company in Step 2.

Step 3b. Mark a line as a DESIGNATION candidate if it reads like a job role:
   - Director, Manager, Engineer, Officer, Executive, President, CEO, COO,
     CFO, CTO, Chairman, Managing Director, Founder, Owner, Proprietor,
     Partner, Principal, Consultant, Specialist, Coordinator, Supervisor,
     Superintendent, Analyst, Head, Advisor, Architect, Technician, Surveyor,
     Estimator, Secretary, Accountant, Auditor, Representative, In-charge,
     Vice President, Procurement, Purchasing, Sales, Marketing, Operations,
     Technical, Regional, Senior, Junior, Assistant, Deputy, General Manager,
     Plant Manager, Business Development, etc.
   - A designation may sit on the SAME line as the name ("John Doe, Sales
     Manager"), on the line directly BELOW the name (most common), or the
     line directly ABOVE it. It is usually printed smaller than the name.
   - A designation is NEVER a company name, NEVER a person name by itself.
   - If a line contains BOTH a person name and a designation separated by a
     comma, dash, or slash (e.g. "Ahmad Rizal, Sales Manager"), split it:
     the human-name portion is `name`, the role portion is `designation`.
   - If a designation line appears ABOVE a name line, it is still a
     designation — do not promote it to the name field.

Step 4. Resolve conflicts — a line CANNOT be both:
   - If a line satisfies both Step 2 and Step 3 (rare), Step 2 always wins:
     treat it as the company, not the name. A stylized all-caps single word
     at the top of the card is a brand even if it superficially resembles a
     name.
   - If the only candidate for "name" is the same text as the company line,
     the card has no separate person name — return name = null. Do NOT copy
     the company name into the name field just to fill it in.
   - A line combining both on one row with a separator (e.g. "John Doe,
     Sales Manager" or "John Doe — Regional Manager") must be split: the part
     before the separator that reads as a human name is `name`, the part
     after is `designation`. Never leave the combined, unsplit text in either
     field.
   - An honorific prefix (Mr./Mrs./Ms./Dr./Ir./Prof./Haji) or post-nominal
     letters (e.g. "B.Eng", "M.T.") stay attached to the name line and do not
     disqualify it from being a person name.
   - DESIGNATION vs NAME confusion: if you are unsure whether a short text
     is a name or a job title, check if it contains a role keyword from
     Step 3b. If it does, it is a designation, not a name.

Step 5. Before finalizing, sanity-check both fields against each other:
   - `name` must never equal `business`/`company` (case-insensitively).
   - `designation` must never equal `name` or `business`/`company`.
   - `business`/`company` must never be a personal name with no legal suffix
     and no business noun and no top-of-card logo positioning — if nothing on
     the card qualifies as a company under Step 2, return business = null
     rather than guessing a person's name is the company.
   - Re-read the OCR lines one final time and confirm that each assigned field
     value is actually printed on the card as-is and is not a mix-up.
""".strip()


# Zip/PIN extraction gets its own dedicated procedure for the same reason the
# name-vs-company confusion does: the model's most common zip mistake is
# copying a 5-6 digit chunk out of a phone/fax number, so the prompt walks
# through address-first location and per-country format checks explicitly.
ZIP_CODE_PROCEDURE = """
Zip/PIN/postal code (zip_code) — follow this procedure in order every time,
because the most common zip mistake is copying digits out of a phone number:

Step 1. Locate the printed postal ADDRESS block first (street / area / city /
   state / country lines). The postal code is ONLY ever part of that address.
   If the card has no printed address, zip_code MUST be null.

Step 2. Within the address, the postal code is a short code printed
   immediately before or after the city/state/country, for example:
   - "Navi Mumbai - 400703, Maharashtra, India" → zip_code = "400703"
   - "Bekasi 17530, Jawa Barat, Indonesia" → zip_code = "17530"
   - "Singapore 629733" → zip_code = "629733"
   - "Houston, TX 77002" → zip_code = "77002"
   It may also be labeled explicitly: PIN, PIN Code, Post Code, Postal Code,
   ZIP. A labeled value always wins over a positional guess.

Step 3. Check the value against the country's postal format before returning:
   - India: exactly 6 digits (PIN, e.g. 400703, 110001).
   - Indonesia, Malaysia, Thailand, Saudi Arabia, Turkey, Germany, France,
     Pakistan, Sri Lanka, South Korea, Kuwait: exactly 5 digits.
   - Singapore, China, Russia: exactly 6 digits.
   - USA: 5 digits (ZIP+4 like "77002-1234" is fine as printed).
   - Japan: 7 digits, usually written 123-4567.
   - Australia, Philippines, New Zealand, South Africa, Bangladesh: 4 digits.
   - United Kingdom: alphanumeric like "SW1A 1AA" — return exactly as printed.
   - UAE, Qatar, Hong Kong: NO postal codes exist — zip_code MUST be null.
     A number after "P.O. Box" is a post-office box number, NEVER a zip_code.
   If the digit run you picked does not fit the country's format, it is NOT
   the postal code — re-read the address block or return null.

Step 4. NEVER take zip_code from a telephone, mobile, fax, or WhatsApp
   number. Digit runs on lines labeled T / Tel / Telp / Phone / M / Mob / HP /
   Cell / F / Fax / WA are telephone digits, not postal codes — even when a
   5-6 digit chunk inside them happens to look like one (e.g. never return
   "82364" from "+62 21 8236 4551"). Also never use: P.O. Box numbers,
   registration or tax numbers (NPWP, GST, VAT, CIN, CR No.), certificate
   numbers (ISO 9001:2015), street/house/plot numbers, suite numbers, or
   years.

Step 5. If after Steps 1-4 no code clearly qualifies, return zip_code = null
   and add "zip_code" to uncertain_fields. Never guess.
""".strip()


THINK_BEFORE_CLASSIFY = """
Before writing any JSON output, work through these reasoning steps internally:

REASONING STEP A — List all lines:
  For each line visible on the card (front then back), write: LINE: "<text>" → LIKELY ROLE: <company|name|designation|phone|email|address|other>

REASONING STEP B — Identify the company:
  Which line is the top-most brand/logo text? Does it contain a legal entity suffix or business noun?
  → Company = <that line>

REASONING STEP C — Identify the person name:
  Is there a line of 1-5 human-name-shaped words that is:
    (a) NOT the company line,
    (b) near a designation/job-title line (above or below it),
    (c) free of legal entity suffixes and business nouns?
  → Name = <that line>, or null if none qualifies.

REASONING STEP D — Identify the designation:
  Is there a job-role line (Director / Manager / Engineer / Officer / CEO / Founder / etc.)?
  Is it directly adjacent to the name candidate?
  → Designation = <that line>
  If a single line reads "Ahmad Rizal, Sales Manager", split it:
    Name = "Ahmad Rizal", Designation = "Sales Manager"

REASONING STEP E — Identify the postal code:
  Which line(s) form the address block? Is there a short digit code adjacent
  to the city/state/country that matches that country's postal format
  (India 6, Indonesia 5, Singapore 6, US 5, ...)?
  → zip_code = <that code>, or null. NEVER a digit chunk from a line labeled
    T/Tel/M/Mob/HP/F/Fax/WA, and never a P.O. Box number.

REASONING STEP F — Final sanity check before output:
  1. Name ≠ Company (if they match, set name = null)
  2. Designation ≠ Name and Designation ≠ Company
  3. zip_code is from the address block and fits the country's format
  4. Every field value must be a verbatim substring of the OCR text (except category/country)
  5. If you are uncertain about a field, add that field to uncertain_fields

Only after completing steps A–F, write the JSON output.
""".strip()

def _key_label(api_key: str | None, index: int | None = None) -> str | None:
    if not api_key:
        return None
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
    prefix = f"key{index + 1}" if index is not None else "key"
    return f"{prefix}_{digest}"


def gemini_key_labels() -> list[str]:
    return [_key_label(key, index) or f"key{index + 1}" for index, key in enumerate(GEMINI_API_KEYS)]


def is_gemini_configured() -> bool:
    if not (GEMINI_API_KEYS or GEMINI_API_KEY):
        return False
    genai_mod, _ = _load_genai()
    return genai_mod is not None


# Keys that returned 429/RESOURCE_EXHAUSTED this process. A free-tier quota
# reset happens at most once a day, so once a key is exhausted it stays dead
# for the rest of the process — retrying it on every subsequent card wastes
# a network round trip and logs a duplicate error for no benefit.
_exhausted_key_labels: set[str] = set()


def _gemini_keys() -> list[tuple[str, str]]:
    keys = GEMINI_API_KEYS or ([GEMINI_API_KEY] if GEMINI_API_KEY else [])
    all_keys = [(key, _key_label(key, index) or f"key{index + 1}") for index, key in enumerate(keys)]
    live_keys = [(key, label) for key, label in all_keys if label not in _exhausted_key_labels]
    # If every key is marked exhausted, try them all again anyway — better to
    # pay for one more failed round trip than to permanently disable Gemini
    # until the process restarts.
    return live_keys or all_keys


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.I)
WEBSITE_RE = re.compile(r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*\.[a-z]{2,}(?:/[^\s,;]*)?", re.I)
PHONE_RE = re.compile(r"(?:\+|00)?\d[\d\s()./-]{5,}\d")
EMAIL_JOINER_RE = re.compile(r"\s*([@._+-])\s*")


def _clean_email(value: str | None) -> str | None:
    if not value:
        return None
    compact = EMAIL_JOINER_RE.sub(r"\1", value.replace("＠", "@"))
    match = EMAIL_RE.search(compact.replace(" ", ""))
    return normalize_email(match.group(0)) if match else normalize_email(value)


def _clean_contact(value: str | None, country_code: str | None = None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
    if country_code:
        code_digits = re.sub(r"\D", "", country_code)
        if code_digits and digits.startswith(code_digits):
            return digits
        if digits.startswith("00"):
            digits = digits[2:]
            if code_digits and digits.startswith(code_digits):
                return digits
        if code_digits:
            digits = f"{code_digits}{digits.lstrip('0')}"
    return digits or None


def _clean_website(value: str | None, email: str | None = None) -> str | None:
    if not value:
        return None
    compact = value.strip().replace(" ", "")
    match = WEBSITE_RE.search(compact)
    if not match:
        return None
    website = match.group(0).rstrip(".")
    if email and website.lower() in email.lower():
        return None
    return normalize_website(website)


DESIGNATION_RE = re.compile(
    r"\b(procurement|purchasing|marketing|sales|director|manager|engineer|officer|executive|"
    r"specialist|consultant|head|president|chairman|chairperson|founder|owner|proprietor|"
    r"partner|principal|supervisor|superintendent|coordinator|administrator|analyst|"
    r"advisor|adviser|architect|technician|surveyor|estimator|secretary|accountant|"
    r"auditor|representative|in-?charge|ceo|coo|cfo|cto|cmo|cio|vp|vice\s+president)\b",
    re.I,
)
# Credential/qualification lines ("B.Sc. Engg (Mechanical), F.I.E.", "MBBS,
# FCPS") commonly sit directly between a person's name and their designation
# on South/Southeast Asian cards — exactly the two neighboring lines the
# name/designation heuristics scan. Without excluding them, a qualification
# line gets mistaken for the name (it has no legal-entity/business-noun
# marker and often falls within the 1-5 word gate once split on
# spaces/punctuation).
QUALIFICATION_RE = re.compile(
    r"\b(b\.?\s?sc|m\.?\s?sc|b\.?\s?eng|m\.?\s?eng|b\.?\s?tech|m\.?\s?tech|b\.?\s?a|m\.?\s?a|"
    r"b\.?\s?com|m\.?\s?com|mba|mbbs|ph\.?\s?d|ll\.?\s?b|ll\.?\s?m|dip\.?|c\.?\s?eng|p\.?\s?eng|"
    r"f\.?\s?i\.?\s?e\.?|m\.?\s?i\.?\s?e\.?|f\.?\s?i\.?\s?mech|fcps|chartered|fellow)\b",
    re.I,
)
ADDRESS_RE = re.compile(
    r"\b(jl\.?|jalan|street|road|kav\.?|kel\.?|kelurahan|kec\.?|kecamatan|kabupaten|provinsi|kota|"
    r"bekasi|banten|cilegon|jakarta|ruko|wisma|building|floor|office|rt\.?|rw\.?|no\.?)\b",
    re.I,
)
NOISE_RE = re.compile(
    r"\b(asme|certified|iso|lrga|wqa|ykan|htri|ohsas|sk3|location|website|e-mail|email|mobile|telp|tel|fax)\b",
    re.I,
)
SERVICE_RE = re.compile(
    r"\b(design|fabrication|heavy duty|radiator|oil cooler|heat exchanger|shell|tube|pressure vessel|special order|engineering and fabrication)\b",
    re.I,
)
LEGAL_ENTITY_RE = re.compile(
    r"\b(pt|pvt|ltd|llc|inc|llp|pte|tbk|co\.?|corp|corporation|sdn|bhd|cv|fze|fzc|fzco|"
    r"wll|w\.l\.l|dmcc|plc|gmbh|sarl|s\.a\.r\.l|pty|jsc|pjsc|nv|bv)\b",
    re.I,
)
COMPANY_IDENTITY_RE = re.compile(
    r"\b(company|corporation|corp|group|industrial|industries|engineering|fabrication|solutions|"
    r"technologies|systems|services|enterprise|enterprises|trading|manufacturing|marine|contractors|"
    r"supply|supplies|metal|works|radiator|oil|gas|energy|construction|logistics|"
    r"international|global|holdings|traders|exports|imports|impex|associates|machinery|"
    r"equipment|chemicals|steel|shipping|freight|cargo|valves|pipes|hardware)\b",
    re.I,
)
FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
}


def _text_lines(text: str) -> list[str]:
    return [line.strip(" \t|,:;") for line in text.splitlines() if line.strip(" \t|,:;")]


def _has_contact_value(line: str) -> bool:
    return bool(EMAIL_RE.search(line) or PHONE_RE.search(line) or WEBSITE_RE.search(line))


HONORIFIC_RE = re.compile(r"^(mr|mrs|ms|mx|dr|prof|ir|eng|engr|drs|haji|hajjah)\.?$", re.I)

# Bare field-label words that appear on their own OCR line (the value sits on
# a different line/column). These must never become a name/designation/
# business value even though they are genuinely present in the OCR text —
# _value_supported_by_text can't tell "label" from "value" on its own.
FIELD_LABEL_BLOCKLIST = {
    "phone",
    "tel",
    "telp",
    "telephone",
    "mobile",
    "mob",
    "cell",
    "hp",
    "fax",
    "email",
    "e-mail",
    "mail",
    "web",
    "website",
    "address",
    "contact",
    "office",
    "direct",
    "whatsapp",
    "name",
    "designation",
    "company",
    "business",
}

# Countries/places that show up as standalone all-caps lines on a card (often
# the top of an address block) and must never be mistaken for a person name
# or a business/brand line.
_PLACE_BLOCKLIST = {
    hint for _country, _iso, _code, hints in COUNTRY_HINTS for hint in hints
} | {country.lower() for country, _iso, _code, _hints in COUNTRY_HINTS}


def _is_field_label(line: str) -> bool:
    normalized = re.sub(r"[^a-z-]", "", line.lower())
    return normalized in FIELD_LABEL_BLOCKLIST


def _is_place_name(line: str) -> bool:
    normalized = re.sub(r"[^a-z ]", "", line.lower()).strip()
    return normalized in _PLACE_BLOCKLIST


def _looks_like_person_name(line: str) -> bool:
    if _is_field_label(line) or _is_place_name(line):
        return False
    if _has_contact_value(line) or NOISE_RE.search(line) or ADDRESS_RE.search(line) or SERVICE_RE.search(line):
        return False
    if QUALIFICATION_RE.search(line):
        return False
    if _looks_like_company_identity(line):
        return False
    # \w with re.UNICODE (the default in Python 3) matches accented/non-Latin
    # letters too, so names like "Bùi Thị Hoa" or "Müller" aren't silently
    # excluded just because they contain non-ASCII characters.
    raw_words = [word for word in re.findall(r"[^\W\d_][\w.'-]*", line, re.UNICODE) if len(word) > 1]
    # Honorifics and initials (Dr., Ir., M.T.) commonly precede/follow a name;
    # don't let them count against the word-count gate.
    words = [word for word in raw_words if not HONORIFIC_RE.match(word.rstrip("."))]
    if not words:
        words = raw_words
    if not 1 <= len(words) <= 5:
        return False
    banned = {"office", "factory", "engineering", "fabrication", "radiator", "location", "website"}
    return not any(word.lower() in banned for word in words)


def _looks_like_company_identity(line: str | None) -> bool:
    if not line:
        return False
    words = re.findall(r"[A-Za-z0-9&.'-]+", line)
    if not words:
        return False
    if LEGAL_ENTITY_RE.search(line) or COMPANY_IDENTITY_RE.search(line):
        return True
    if any(char.isdigit() for char in line) or any(char in line for char in ("&", "/")):
        return True
    return len(words) == 1 and words[0].isupper() and len(words[0]) >= 3


def _looks_like_business(line: str) -> bool:
    if _is_field_label(line) or _is_place_name(line):
        return False
    if _has_contact_value(line) or NOISE_RE.search(line) or ADDRESS_RE.search(line):
        return False
    words = re.findall(r"[A-Za-z0-9&.'-]+", line)
    if not words:
        return False
    if LEGAL_ENTITY_RE.search(line):
        return True
    if DESIGNATION_RE.search(line):
        return False
    if SERVICE_RE.search(line):
        return False
    if line.isupper() and len(" ".join(words)) >= 3:
        return True
    return len(words) <= 4 and any(len(word) >= 4 for word in words)


# A candidate designation line that runs unusually long is almost always a
# Vision paragraph-merge (name + title + region collapsed into one OCR
# block) rather than a real title — accepting it whole would dump the
# merged blob into the designation column. Reject rather than guess at a
# split; the multimodal Gemini path (which reads the actual image layout)
# is what correctly separates these, not more OCR-text heuristics.
_MAX_DESIGNATION_WORDS = 6


def _infer_designation(lines: list[str]) -> tuple[str | None, int | None]:
    for index, line in enumerate(lines):
        # DESIGNATION_RE's "head" keyword also matches "Head Office"/"Head
        # Quarters" address lines, and ADDRESS_RE independently flags those
        # same office/building lines — reject them here so a building/branch
        # label is never mistaken for a person's job title.
        if (
            DESIGNATION_RE.search(line)
            and not SERVICE_RE.search(line)
            and not ADDRESS_RE.search(line)
            and not _has_contact_value(line)
            and len(line.split()) <= _MAX_DESIGNATION_WORDS
        ):
            return line, index
    return None, None


def _infer_name(lines: list[str], designation_index: int | None) -> str | None:
    if designation_index is not None:
        for index in range(designation_index - 1, max(-1, designation_index - 4), -1):
            if index >= 0 and _looks_like_person_name(lines[index]):
                return lines[index]
    for line in lines[:12]:
        if _looks_like_person_name(line):
            return line
    return None


def _infer_business(lines: list[str], name: str | None = None, front_line_count: int | None = None) -> str | None:
    # Company/business must come from the front side — the app's own rule is
    # that back-side branch/office/product lines are address/category
    # support only, never the company name. front_line_count lets the caller
    # tell us where the front side ends in the combined line list; without
    # it (e.g. existing single-side callers/tests) fall back to all lines.
    front_lines = lines if front_line_count is None else lines[:front_line_count]
    # A line carrying a legal-entity marker (PT/Ltd/Pte/...) is unambiguously
    # the company name, even if it isn't the first business-shaped line on
    # the card — a marketing tagline ("Heating and Cooling Solution") often
    # sits above it, and a multi-line wrapped address can push the legal-
    # entity line well past the first few lines, so scan the whole front
    # side rather than an arbitrary early cutoff.
    for line in front_lines:
        if line != name and LEGAL_ENTITY_RE.search(line) and _looks_like_business(line):
            return line
    # Scan the whole front side, not just the first 8 lines: some cards print
    # the brand/logo as a single word in the footer (e.g. "PETROSEA" after
    # the contact block) rather than at the top.
    for line in front_lines:
        if line == name:
            continue
        if _looks_like_business(line):
            return line
    return None


def _infer_address(lines: list[str]) -> str | None:
    address_lines = []
    for line in lines:
        lowered = line.lower()
        if _has_contact_value(line):
            continue
        if ADDRESS_RE.search(line) or "indonesia" in lowered or re.search(r"\b\d{5}\b", line):
            address_lines.append(line)
    return "\n".join(dict.fromkeys(address_lines)) or None


def _infer_zip(text: str) -> str | None:
    # Prefer a standalone digit run on an address-flavored line — scanning the
    # whole card for any 5-6 digit sequence risks grabbing an isolated segment
    # of a phone/fax number or an unrelated code instead of the postal code.
    for line in text.splitlines():
        if ADDRESS_RE.search(line) and not _has_contact_value(line):
            match = re.search(r"(?<!\d)\d{4,8}(?!\d)", line)
            if match:
                return match.group(0)
    # Fallback: standalone 5-6 digit runs, but never from a line that carries
    # a phone/email/website value or a phone label — a spaced phone number
    # ("+62 21 8236 4551") is full of zip-shaped digit chunks.
    candidates: list[str] = []
    for line in text.splitlines():
        if _has_contact_value(line) or _PHONE_LINE_LABEL_RE.search(line):
            continue
        candidates.extend(re.findall(r"\b\d{5,6}\b", line))
    return candidates[-1] if candidates else None


# Phone-label context that disqualifies a line as a zip/postal-code source:
# full label words, or a bare T/M/F letter immediately followed by ":"/".".
_PHONE_LINE_LABEL_RE = re.compile(
    r"\b(tel|telp|telephone|phone|ph|mob|mobile|hp|cell|fax|whatsapp|wa|direct|hotline|dial)\b"
    r"|(?:^|[\s|,;(])[tmf]\s*[.:]",
    re.I,
)
_ZIP_LABEL_RE = re.compile(r"\b(zip|postal\s*code|post\s*code|pin\s*code|pincode|pin)\b", re.I)
_PO_BOX_RE = re.compile(r"\bp\.?\s*o\.?\s*box\b", re.I)
_UK_POSTCODE_RE = re.compile(r"^[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}$")

# Postal-code digit counts by dial code. (0, 0) marks countries with no
# public postal-code system (UAE/Qatar/Hong Kong) — any digits Gemini
# returns for them are P.O. Box or phone fragments, never a zip.
_ZIP_DIGIT_RULES: dict[str, tuple[int, int]] = {
    "+1": (5, 5),    # USA/Canada (US ZIP)
    "+7": (6, 6),    # Russia
    "+27": (4, 4),   # South Africa
    "+33": (5, 5),   # France
    "+34": (5, 5),   # Spain
    "+39": (5, 5),   # Italy
    "+49": (5, 5),   # Germany
    "+55": (8, 8),   # Brazil CEP
    "+60": (5, 5),   # Malaysia
    "+61": (4, 4),   # Australia
    "+62": (5, 5),   # Indonesia
    "+63": (4, 4),   # Philippines
    "+64": (4, 4),   # New Zealand
    "+65": (6, 6),   # Singapore
    "+66": (5, 5),   # Thailand
    "+81": (7, 7),   # Japan (123-4567)
    "+82": (5, 5),   # South Korea
    "+84": (5, 6),   # Vietnam
    "+86": (6, 6),   # China
    "+90": (5, 5),   # Turkey
    "+91": (6, 6),   # India PIN
    "+92": (5, 5),   # Pakistan
    "+94": (5, 5),   # Sri Lanka
    "+880": (4, 4),  # Bangladesh
    "+965": (5, 5),  # Kuwait
    "+966": (5, 5),  # Saudi Arabia
    "+968": (3, 3),  # Oman
    "+973": (3, 4),  # Bahrain
    "+971": (0, 0),  # UAE — no postal codes
    "+974": (0, 0),  # Qatar — no postal codes
    "+852": (0, 0),  # Hong Kong — no postal codes
}


def _digit_run_around(line: str, start: int, end: int) -> str:
    """Digits of the contiguous number-ish run (digits plus common phone
    separators) surrounding line[start:end] — used to detect a 'zip' that is
    really a chunk of a longer phone/fax number."""
    allowed = set("0123456789 -.()/+")
    left, right = start, end
    while left > 0 and line[left - 1] in allowed:
        left -= 1
    while right < len(line) and line[right] in allowed:
        right += 1
    return re.sub(r"\D", "", line[left:right])


def _zip_printed_in_address_context(digits: str, text: str) -> bool:
    """True when the digit run appears somewhere in the transcript that reads
    like a postal address (or at least NOT like a phone/fax/P.O. Box line)."""
    pattern = re.compile(rf"(?<!\d){re.escape(digits)}(?!\d)")
    for line in text.splitlines():
        for match in pattern.finditer(line):
            if _PHONE_LINE_LABEL_RE.search(line):
                continue
            prefix = line[max(0, match.start() - 12): match.start()]
            if _PO_BOX_RE.search(prefix):
                continue
            # Embedded in a longer digit run (a spaced/hyphenated phone
            # number)? Allow a little slack for ZIP+4 / Japanese 123-4567.
            if len(_digit_run_around(line, match.start(), match.end())) > len(digits) + 4:
                continue
            lowered = line.lower()
            has_place_word = any(hint in lowered for hint in _PLACE_BLOCKLIST if len(hint) > 3)
            if (
                ADDRESS_RE.search(line)
                or _ZIP_LABEL_RE.search(line)
                or has_place_word
                or not PHONE_RE.search(line)
            ):
                return True
    return False


def _validate_zip_code(
    value: str | None,
    text: str,
    country_code: str | None,
    contact_numbers: list[str | None],
) -> str | None:
    """Accept a zip/PIN/postal code only when it fits the card's country
    format AND is printed in address context — never a digit chunk copied out
    of a phone/fax number or a P.O. Box."""
    if not value:
        return None
    raw = str(value).strip().strip(".,;:")
    if _UK_POSTCODE_RE.match(raw):
        return raw.upper() if not text or _value_supported_by_text(raw, text) else None
    if re.fullmatch(r"\d{5}-\d{4}", raw):  # US ZIP+4 → keep the 5-digit ZIP
        raw = raw[:5]
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    low, high = _ZIP_DIGIT_RULES.get(country_code or "", (4, 8))
    if low == 0 or not (low <= len(digits) <= high):
        return None
    if text:
        if not _zip_printed_in_address_context(digits, text):
            return None
    elif any(number and digits in re.sub(r"\D", "", str(number)) for number in contact_numbers):
        return None
    return digits


def _infer_city_state(text: str) -> tuple[str | None, str | None]:
    lowered = text.lower()
    rules = [
        ("Jakarta Timur", "Jakarta", ["jakarta timur"]),
        ("Jakarta Barat", "Jakarta", ["jakarta barat"]),
        ("Jakarta", "Jakarta", ["jakarta"]),
        ("Bekasi", "West Java", ["bekasi"]),
        ("Cilegon", "Banten", ["cilegon"]),
        ("Gresik", "East Java", ["gresik"]),
        ("Cikarang", "West Java", ["cikarang"]),
    ]
    for city, state, hints in rules:
        if any(hint in lowered for hint in hints):
            return city, state
    return None, None


def _domain_for_score(website: str) -> str:
    domain = website.lower()
    domain = re.sub(r"^https?://", "", domain)
    domain = domain.removeprefix("www.")
    return domain.split("/", 1)[0]


def _brand_tokens(*values: str | None) -> set[str]:
    ignored = {
        "pt",
        "pvt",
        "ltd",
        "llc",
        "inc",
        "tbk",
        "co",
        "company",
        "the",
        "and",
        "services",
        "service",
        "engineering",
        "industries",
        "industrial",
        "limited",
        "private",
        "head",
        "office",
    }
    tokens = set()
    for value in values:
        for token in re.findall(r"[a-z0-9]+", (value or "").lower()):
            if len(token) >= 3 and token not in ignored:
                tokens.add(token)
    return tokens


def _website_score(website: str, brand_tokens: set[str], line: str, email_domains: set[str]) -> tuple[int, int]:
    domain = _domain_for_score(website)
    root = domain.split(".")[0]
    line_lower = line.lower()
    score = 0
    if re.search(r"\b(w|web|website|www)\b", line_lower):
        score += 10
    if brand_tokens:
        for token in brand_tokens:
            if token in domain or domain in token:
                score += 8
            elif token[:5] in root or root[:5] in token:
                score += 4
    if domain in email_domains:
        score += 2
    if website.lower().startswith(("http://", "https://")) or domain.startswith("www."):
        score += 1
    return score, -len(domain)


_KNOWN_DIAL_CODES = {dial_code for _country, _iso, dial_code, _hints in COUNTRY_HINTS}


def _country_code_from_phone(*values: str | None) -> str | None:
    known_codes = _KNOWN_DIAL_CODES
    for value in values:
        if not value:
            continue
        explicit = re.match(r"\s*(?:\+\s*(\d{1,3})|00\s*(\d{1,3}))(?=[\s()./-]|$)", value)
        if explicit:
            return f"+{explicit.group(1) or explicit.group(2)}"
        digits = re.sub(r"[^\d+]", "", value)
        if digits.startswith("00"):
            digits = f"+{digits[2:]}"
        for code in sorted(known_codes, key=len, reverse=True):
            code_digits = code.replace("+", "")
            if digits.startswith(code) or re.sub(r"\D", "", digits).startswith(code_digits):
                return code
    return None


def _visible_text(fields: dict) -> str:
    parts = [
        str(fields.get("front_text") or ""),
        str(fields.get("back_text") or ""),
        str(fields.get("all_visible_text") or ""),
    ]
    return "\n".join(part for part in parts if part.strip())


def _normalize_match_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _value_supported_by_text(value: str | None, text: str) -> bool:
    if not value:
        return False
    normalized_value = _normalize_match_text(value)
    normalized_text = _normalize_match_text(text)
    if not normalized_value:
        return False
    # normalize_match_text already casefolds and strips punctuation/whitespace,
    # so this substring check is tolerant of case and spacing differences
    # between Gemini's answer and the raw OCR line (e.g. "John Doe" vs "JOHN
    # DOE" or extra spacing around a hyphenated surname).
    if normalized_value in normalized_text:
        return True
    # A name/value can be OCR'd split across two adjacent lines (e.g. a long
    # designation wrapping, or a name broken across a line break by the card
    # layout). Token-subset matching against the whole transcript still
    # requires every word to appear somewhere, so this doesn't accept
    # unrelated values — it only tolerates re-ordering/line-splitting of a
    # value whose words are genuinely all present in the OCR text.
    tokens = [token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 1]
    if not tokens:
        return False
    return all(token in normalized_text for token in tokens)


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _email_search_variants(text: str) -> list[str]:
    variants = [text]
    normalized_lines = []
    for line in text.splitlines():
        line = line.replace("＠", "@")
        line = re.sub(r"\s*[\[(]?\s*at\s*[\])]?\s*", "@", line, flags=re.I)
        line = re.sub(r"\s*[\[(]?\s*dot\s*[\])]?\s*", ".", line, flags=re.I)
        normalized_lines.append(EMAIL_JOINER_RE.sub(r"\1", line))
    variants.append("\n".join(normalized_lines))
    variants.append(EMAIL_JOINER_RE.sub(r"\1", text.replace("＠", "@")))
    return variants


def _emails_from_text(text: str) -> list[str]:
    emails = []
    for variant in _email_search_variants(text):
        emails.extend(normalize_email(match.group(0)) for match in EMAIL_RE.finditer(variant))
    return _unique(emails)


def _websites_from_text(text: str, emails: list[str], *brand_values: str | None) -> list[str]:
    email_domains = {email.split("@", 1)[1].lower() for email in emails if "@" in email}
    brand_tokens = _brand_tokens(*brand_values)
    scored: list[tuple[tuple[int, int], str]] = []
    for match in WEBSITE_RE.finditer(text):
        raw = match.group(0).strip().rstrip(".,;")
        if not raw or "@" in raw:
            continue
        if match.start() > 0 and text[match.start() - 1] == "@":
            continue
        if match.end() < len(text) and text[match.end()] == "@":
            continue
        normalized = normalize_website(raw)
        domain = normalized.replace("https://", "").replace("http://", "").split("/", 1)[0].lower()
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start: line_end if line_end != -1 else len(text)].lower()
        if domain in email_domains and not re.search(r"\b(w|web|website|www)\b", line):
            continue
        scored.append((_website_score(normalized, brand_tokens, line, email_domains), normalized))
    if not scored and email_domains and brand_tokens:
        for domain in email_domains:
            fallback = normalize_website(domain)
            score, _ = _website_score(fallback, brand_tokens, "", email_domains)
            if score >= 8:
                scored.append(((score - 1, -len(domain)), fallback))
    if not scored and email_domains:
        for domain in sorted(email_domains):
            if domain not in FREE_EMAIL_DOMAINS:
                scored.append(((1, -len(domain)), normalize_website(domain)))
    return _unique([website for _, website in sorted(scored, key=lambda item: item[0], reverse=True)])


def structure_card_text_deterministic(
    front_text: str,
    back_text: str | None = None,
    candidate_hints: list[dict] | None = None,
) -> dict:
    fields = {field: None for field in FIELDS}
    fields["front_text"] = front_text
    fields["back_text"] = back_text
    fields["all_visible_text"] = "\n".join(part for part in [front_text, back_text or ""] if part.strip()).strip()
    lines = _text_lines(fields["all_visible_text"])
    front_line_count = len(_text_lines(front_text))

    candidate_hints = candidate_hints or []
    by_field: dict[str, list[dict]] = {}
    for candidate in candidate_hints:
        by_field.setdefault(str(candidate.get("field") or ""), []).append(candidate)
    for values in by_field.values():
        values.sort(key=lambda item: float(item.get("confidence") or 0), reverse=True)

    field_map = {
        "name": "name",
        "designation": "designation",
        "company": "business",
        "address": "address",
        "website": "website",
        "mobile": "contact1",
        "phone": "contact2",
        "fax": "contact3",
        "zip": "zip_code",
    }
    for source, target in field_map.items():
        if by_field.get(source):
            fields[target] = by_field[source][0].get("value")

    # A "designation" hint comes from TITLE_KEYWORDS (extract_candidates),
    # a broader vocabulary than DESIGNATION_RE (it also catches CEO/Founder/
    # Partner/VP, which DESIGNATION_RE doesn't) — so trust the hint unless
    # it's demonstrably wrong: an office/building line ("Head Office" matches
    # DESIGNATION_RE's "head"), a service/tagline match, a line carrying a
    # contact value, or an over-long Vision paragraph-merge blob. A hint that
    # merely fails DESIGNATION_RE's narrower keyword match is not evidence
    # it's wrong.
    designation_hint = fields.get("designation")
    if designation_hint and not (
        SERVICE_RE.search(str(designation_hint))
        or ADDRESS_RE.search(str(designation_hint))
        or _has_contact_value(str(designation_hint))
        or len(str(designation_hint).split()) > _MAX_DESIGNATION_WORDS
    ):
        fields["designation"] = designation_hint
        designation_index = next((i for i, line in enumerate(lines) if line == designation_hint), None)
    else:
        fields["designation"], designation_index = _infer_designation(lines)

    # Unlike designation/business, every "name" candidate hint comes from
    # exactly one source (extract_candidates' top_front_line heuristic: the
    # first short front-side line, with no positional/designation context).
    # It is not independently trustworthy, so the designation-anchored
    # _infer_name is always authoritative for name.
    fields["name"] = _infer_name(lines, designation_index)

    # Like name, every "business"/"company" hint comes from position-blind
    # sources (top_front_brand/top_front_company/company_keyword — the
    # highest-Vision-confidence match, not the correct one), so a one-word
    # marketing tagline ("RENEWABLE", "ENVIRONMENTAL") can outrank the real
    # brand. _infer_business's legal-entity-first, front-side-scoped scan is
    # the authoritative source; the hint only fills in when inference finds
    # nothing at all.
    inferred_business = _infer_business(lines, fields.get("name"), front_line_count=front_line_count)
    business_hint = fields.get("business")
    if inferred_business:
        fields["business"] = inferred_business
    elif (
        business_hint
        and not _is_field_label(str(business_hint))
        and not _is_place_name(str(business_hint))
        and not DESIGNATION_RE.search(str(business_hint))
    ):
        fields["business"] = business_hint
    else:
        fields["business"] = None
    if not fields.get("address"):
        fields["address"] = _infer_address(lines)
    if not fields.get("zip_code"):
        fields["zip_code"] = _infer_zip(fields["all_visible_text"])
    city, state = _infer_city_state(fields["all_visible_text"])
    fields["city"] = fields.get("city") or city
    fields["state"] = fields.get("state") or state

    emails = [candidate.get("value") for candidate in by_field.get("email", []) if candidate.get("value")]
    if emails:
        fields["email1"] = emails[0]
    if len(emails) > 1:
        fields["email2"] = emails[1]

    fields["company"] = fields.get("business")
    fields["field_evidence"] = {
        target: by_field[source][0].get("evidence") or by_field[source][0].get("value")
        for source, target in field_map.items()
        if by_field.get(source)
    }
    fields["uncertain_fields"] = ["llm_sorting_skipped_or_failed"]
    return clean_structured_fields(fields)


def _label_for_phone_line(line: str) -> str | None:
    lowered = line.lower()
    if re.search(r"\b(f|fax)\b", lowered):
        return "contact3"
    if re.search(r"\b(m|mob|mobile|hp|cell|direct)\b", lowered):
        return "contact1"
    if re.search(r"\b(t|tel|telp|phone|office|landline)\b", lowered):
        return "contact2"
    return None


def _contacts_from_text(text: str, country_code: str | None) -> dict[str, str | None]:
    contacts: dict[str, str | None] = {"contact1": None, "contact2": None, "contact3": None}
    unlabeled: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if "@" in line or re.search(r"\b(iso|asme|certified|ohsas|sk3|lrga|wqa|ykan|htri)\b", lowered):
            continue
        line_numbers = []
        for match in PHONE_RE.finditer(line):
            raw = match.group(0)
            raw_digits = re.sub(r"\D", "", raw)
            if len(raw_digits) < 8 or len(raw_digits) > 15:
                continue
            cleaned = _clean_contact(raw, country_code)
            if not cleaned or len(cleaned) < 7:
                continue
            line_numbers.append(cleaned)

        if not line_numbers:
            continue
        if re.search(r"\b(f|fax)\b", lowered) and re.search(r"\b(t|tel|telp|phone|office|landline)\b", lowered) and len(line_numbers) >= 2:
            if not contacts["contact2"]:
                contacts["contact2"] = line_numbers[0]
            if not contacts["contact3"]:
                contacts["contact3"] = line_numbers[-1]
            continue

        for cleaned in line_numbers:
            label = _label_for_phone_line(line)
            if label and not contacts[label]:
                contacts[label] = cleaned
            elif not label:
                unlabeled.append(cleaned)

    for key in ("contact1", "contact2", "contact3"):
        if not contacts[key] and unlabeled:
            contacts[key] = unlabeled.pop(0)
    return contacts


def _drop_unsupported_fields(cleaned: dict, text: str) -> None:
    for field in [
        "name",
        "designation",
        "business",
        "company",
        "address",
        "city",
        "state",
        "zip_code",
        "social_media",
        "notes",
    ]:
        value = cleaned.get(field)
        if value and not _value_supported_by_text(str(value), text):
            cleaned[field] = None


def _same_identity(left: str | None, right: str | None) -> bool:
    normalized_left = _normalize_match_text(left)
    normalized_right = _normalize_match_text(right)
    return bool(normalized_left and normalized_left == normalized_right)


# "John Doe, Sales Manager" / "John Doe — Sales Manager" merged into one
# value. The separator must be a comma or a SPACED dash/pipe/slash so that
# hyphenated names ("Jean-Pierre") are never split.
_NAME_TITLE_SPLIT_RE = re.compile(r"^(?P<name>.{2,}?)(?:\s*,\s*|\s+[|/–—-]\s+)(?P<title>.{2,})$")


def _split_name_designation(cleaned: dict) -> None:
    name = cleaned.get("name")
    if not name:
        return
    match = _NAME_TITLE_SPLIT_RE.match(str(name).strip())
    if not match:
        return
    name_part = match.group("name").strip()
    title_part = match.group("title").strip()
    # Only split when the tail reads like a job title and the head reads like
    # a bare person name — "Ir. Bambang Suryanto, M.T." must stay intact.
    if (
        DESIGNATION_RE.search(title_part)
        and not DESIGNATION_RE.search(name_part)
        and _looks_like_person_name(name_part)
    ):
        cleaned["name"] = name_part
        if not cleaned.get("designation"):
            cleaned["designation"] = title_part


def _repair_designation(cleaned: dict) -> None:
    """Designation must be a job title. Drop it when it is really the name,
    the company, a field label, an address/service line, or a brand."""
    designation = cleaned.get("designation")
    if not designation:
        return
    value = str(designation).strip()
    if (
        _same_identity(designation, cleaned.get("name"))
        or _same_identity(designation, cleaned.get("business"))
        or _same_identity(designation, cleaned.get("company"))
        or _has_contact_value(value)
        or _is_field_label(value)
        or _is_place_name(value)
        or len(value.split()) > _MAX_DESIGNATION_WORDS
    ):
        cleaned["designation"] = None
        return
    # A recognised role keyword ("Design Engineer", "Marketing") is kept even
    # when a service/address word also appears in it.
    if DESIGNATION_RE.search(value):
        return
    if (
        ADDRESS_RE.search(value)
        or SERVICE_RE.search(value)
        or LEGAL_ENTITY_RE.search(value)
        or _looks_like_company_identity(value)
    ):
        cleaned["designation"] = None


def _repair_name_company_conflict(cleaned: dict, visible_text: str) -> None:
    name = cleaned.get("name")
    business = cleaned.get("business")
    company = cleaned.get("company")
    if not name:
        return

    name_is_company = (
        _same_identity(name, business)
        or _same_identity(name, company)
        or _looks_like_company_identity(str(name))
    )
    if not name_is_company:
        return

    if not business and _looks_like_company_identity(str(name)):
        cleaned["business"] = name
        cleaned["company"] = cleaned.get("company") or name

    lines = _text_lines(visible_text)
    _designation, designation_index = _infer_designation(lines)
    repaired = _infer_name(lines, designation_index)
    if repaired and not _same_identity(repaired, cleaned.get("business")) and not _same_identity(repaired, cleaned.get("company")):
        cleaned["name"] = repaired
    else:
        cleaned["name"] = None


def clean_structured_fields(fields: dict) -> dict:
    cleaned = {field: fields.get(field) for field in FIELDS}
    # Gemini now returns field_evidence as a list of {field, evidence} objects
    # (a dict isn't expressible in its response schema); collapse back to the
    # {field: evidence} dict the app stores and displays.
    cleaned["field_evidence"] = _evidence_to_dict(cleaned.get("field_evidence"))
    visible_text = _visible_text(cleaned)
    if not cleaned.get("all_visible_text"):
        cleaned["all_visible_text"] = visible_text or None

    transcript_emails = _emails_from_text(visible_text)
    cleaned["email1"] = transcript_emails[0] if transcript_emails else _clean_email(cleaned.get("email1") or cleaned.get("email"))
    cleaned["email2"] = transcript_emails[1] if len(transcript_emails) > 1 else _clean_email(cleaned.get("email2"))
    cleaned["email"] = cleaned["email1"]
    transcript_websites = _websites_from_text(
        visible_text,
        transcript_emails,
        cleaned.get("business"),
        cleaned.get("company"),
        cleaned.get("name"),
    )
    cleaned["website"] = transcript_websites[0] if transcript_websites else _clean_website(cleaned.get("website"), cleaned.get("email1"))

    inferred_country, inferred_code = infer_country_and_code(
        cleaned.get("address"),
        visible_text,
        cleaned.get("city"),
        cleaned.get("state"),
        cleaned.get("country"),
        cleaned.get("phone_number"),
        cleaned.get("mobile_number"),
        cleaned.get("fax_number"),
    )
    phone_code = _country_code_from_phone(
        cleaned.get("contact1"),
        cleaned.get("contact2"),
        cleaned.get("contact3"),
        cleaned.get("mobile_number"),
        cleaned.get("phone_number"),
        cleaned.get("fax_number"),
        visible_text,
    )
    cleaned["country_code"] = phone_code or inferred_code or cleaned.get("country_code")
    phone_country = country_name_from_code(cleaned.get("country_code"))
    cleaned["country"] = phone_country or normalize_country_name(cleaned.get("country")) or inferred_country
    inferred_city, inferred_state = _infer_city_state("\n".join([visible_text, str(cleaned.get("address") or "")]))
    cleaned["city"] = cleaned.get("city") or inferred_city
    cleaned["state"] = cleaned.get("state") or inferred_state

    transcript_contacts = _contacts_from_text(visible_text, cleaned.get("country_code"))
    cleaned["contact1"] = transcript_contacts.get("contact1") or _clean_contact(cleaned.get("contact1") or cleaned.get("mobile_number"), cleaned.get("country_code"))
    cleaned["contact2"] = transcript_contacts.get("contact2") or _clean_contact(cleaned.get("contact2") or cleaned.get("phone_number"), cleaned.get("country_code"))
    cleaned["contact3"] = transcript_contacts.get("contact3") or _clean_contact(cleaned.get("contact3") or cleaned.get("fax_number"), cleaned.get("country_code"))
    if cleaned.get("contact2") == cleaned.get("contact1"):
        cleaned["contact2"] = None
    if cleaned.get("contact3") in {cleaned.get("contact1"), cleaned.get("contact2")}:
        cleaned["contact3"] = None
    contact_values = [cleaned.get("contact1"), cleaned.get("contact2"), cleaned.get("contact3")]
    cleaned["zip_code"] = (
        _validate_zip_code(cleaned.get("zip_code"), visible_text, cleaned.get("country_code"), contact_values)
        or _validate_zip_code(_infer_zip(visible_text), visible_text, cleaned.get("country_code"), contact_values)
    )
    normalized_phone = normalize_phone(cleaned.get("phone_number"), cleaned.get("country_code"))
    normalized_mobile = normalize_phone(cleaned.get("mobile_number"), cleaned.get("country_code"))
    normalized_fax = normalize_phone(cleaned.get("fax_number"), cleaned.get("country_code"))
    cleaned["phone_number"] = cleaned.get("contact2") or national_phone_number(normalized_phone, cleaned.get("country_code"))
    cleaned["mobile_number"] = cleaned.get("contact1") or national_phone_number(normalized_mobile, cleaned.get("country_code"))
    cleaned["fax_number"] = cleaned.get("contact3") or national_phone_number(normalized_fax, cleaned.get("country_code"))
    cleaned["phone_primary"] = (
        f"+{cleaned['contact1']}" if cleaned.get("contact1") else None
    ) or normalize_phone(cleaned.get("phone_primary"), cleaned.get("country_code")) or normalized_mobile or normalized_phone
    if not cleaned.get("company"):
        cleaned["company"] = cleaned.get("business")
    _split_name_designation(cleaned)
    _drop_unsupported_fields(cleaned, visible_text)
    _repair_name_company_conflict(cleaned, visible_text)
    _repair_designation(cleaned)
    if cleaned.get("business") and not cleaned.get("company"):
        cleaned["company"] = cleaned["business"]
    if cleaned.get("company") and not cleaned.get("business"):
        cleaned["business"] = cleaned["company"]
    return cleaned


def structure_card_image(
    event_id: str,
    front_image: bytes,
    front_mime_type: str,
    back_image: bytes | None = None,
    back_mime_type: str | None = None,
    front_text: str | None = None,
    back_text: str | None = None,
    candidate_hints: list[dict] | None = None,
    ocr_results: list[OCRSideResult] | None = None,
) -> dict:
    """Send the card image(s) to Gemini so layout (logo size/position, a
    person-name line vs. a brand line) is judged from the actual picture
    instead of flattened OCR text. The Vision OCR transcript is still passed
    as grounding/hint text — the image is authoritative, OCR fills gaps and
    lets field_evidence cite exact printed lines.
    """
    _load_genai()
    annotated_transcript = _annotated_transcript(ocr_results)
    grounding_section = ""
    if front_text or back_text or annotated_transcript:
        grounding_section = f"""
Google Vision OCR already read this card as grounding context (it can contain
mistakes — the image is the source of truth for anything OCR mis-read):

OCR front:
{front_text or ""}

OCR back:
{back_text or ""}

Annotated OCR lines (position/size cues — large text near the top is usually the
company/brand; a large personal-name-looking line elsewhere is usually the person):
{annotated_transcript}

Rule-based candidate hints:
{json.dumps(candidate_hints or [], ensure_ascii=False, indent=2)}
"""

    prompt = f"""
Look at the attached business card image(s) and extract structured contact fields
matching the response schema. Transcribe the printed text you can see, then sort
it into the schema's fields following your field procedures (name vs. company,
designation, zip_code). Use the image as the source of truth for layout
(which line is the logo/brand, which line is a person's name, relative text size
and position); use the OCR grounding text below only to fill in exact wording and
catch what OCR read correctly.

Task-specific rules:
- Use only values visible on the card image. Do not invent missing values; prefer null over a guessed value.
- Every non-null field except category/country_code must be supported by a line in front_text/back_text.
- Never use service bullet text, marketing slogans, product names, or "Business Outline" headings as a person name or business name.
- front_text/back_text must contain the full transcription of that side, lines separated by "\\n"; back_text is null if no back image was provided.
- all_visible_text must combine front_text and back_text.
- field_evidence must be an array of objects, each {{"field": "<field name>", "evidence": "<exact transcript line(s) used for that field>"}}, one entry per non-null field you filled.
- uncertain_fields must be an array of field names you are not confident about.
- Contact1 is main direct/mobile, Contact2 is office/telephone, Contact3 is fax or another printed number — all digits only, with country calling code when visible or inferable.
- Country must be the full country name, not an ISO code (e.g. Indonesia, not ID).
- Category must be the single closest match to the company's business; never copy a job title or address into category.
- zip_code must come from the address block per your zip procedure — never from a phone/fax number or P.O. Box.
{grounding_section}
""".strip()

    snapshot = usage_snapshot()
    prompt_tokens = estimate_tokens(GEMINI_SYSTEM_INSTRUCTION + "\n" + prompt)
    if not snapshot.allowed or snapshot.daily_tokens + prompt_tokens > snapshot.daily_token_limit:
        record_usage(
            event_id,
            model=GEMINI_MODEL,
            purpose="business_card_image_structuring",
            prompt_tokens=prompt_tokens,
            status="blocked_local_limit",
            error_message="Local Gemini usage limit reached",
        )
        return {}

    if not is_gemini_configured():
        record_usage(
            event_id,
            model=GEMINI_MODEL,
            purpose="business_card_image_structuring",
            prompt_tokens=prompt_tokens,
            status="skipped_not_configured",
            error_message="Gemini SDK or API key is missing",
        )
        return {}

    all_visible_text = "\n".join(part for part in [front_text or "", back_text or ""] if part.strip()).strip()
    generation_config = (
        types.GenerateContentConfig(
            temperature=0,
            system_instruction=GEMINI_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=GeminiCardExtraction,
        )
        if types is not None and GeminiCardExtraction is not None
        else (
            types.GenerateContentConfig(
                temperature=0,
                system_instruction=GEMINI_SYSTEM_INSTRUCTION,
                response_mime_type="application/json",
            )
            if types is not None
            else None
        )
    )
    contents = [prompt, types.Part.from_bytes(data=front_image, mime_type=front_mime_type or "image/jpeg")]
    if back_image:
        contents.append(types.Part.from_bytes(data=back_image, mime_type=back_mime_type or "image/jpeg"))

    try:
        last_error: Exception | None = None
        for api_key, key_label in _gemini_keys():
            try:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=generation_config,
                )
                text = (getattr(response, "text", None) or "").strip()
                completion_tokens = estimate_tokens(text)
                # response.parsed is a schema-validated Pydantic instance when
                # response_schema was honored; fall back to hand-parsing the
                # raw JSON text if the SDK/model didn't populate it.
                parsed_model = getattr(response, "parsed", None)
                if parsed_model is not None and hasattr(parsed_model, "model_dump"):
                    parsed = parsed_model.model_dump(mode="json")
                else:
                    parsed = _parse_json(text)
                record_usage(
                    event_id,
                    provider="gemini",
                    model=GEMINI_MODEL,
                    purpose="business_card_image_structuring",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    key_label=key_label,
                )
                if not parsed:
                    return {}
                parsed["front_text"] = front_text or parsed.get("front_text")
                parsed["back_text"] = back_text if back_text is not None else parsed.get("back_text")
                parsed["all_visible_text"] = all_visible_text or parsed.get("all_visible_text")
                return clean_structured_fields(parsed)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                record_usage(
                    event_id,
                    provider="gemini",
                    model=GEMINI_MODEL,
                    purpose="business_card_image_structuring",
                    prompt_tokens=prompt_tokens,
                    status="error",
                    error_message=message[:500],
                    key_label=key_label,
                )
                if "429" in message or "RESOURCE_EXHAUSTED" in message:
                    _exhausted_key_labels.add(key_label)
                    continue
                if _is_config_error(message):
                    # The request itself is malformed — every key will reject
                    # it identically, so trying the rest would just waste
                    # round trips and quota. Stop here.
                    logger.error(
                        "Gemini image structuring rejected the request (config/schema bug, "
                        "NOT a quota issue) — every card will silently fall back to the regex "
                        "extractor until this is fixed: %s",
                        message[:400],
                    )
                    break
                # Any other error (auth failure, transient network/server
                # error, one bad/revoked key) is specific to THIS key or THIS
                # attempt, not proof every key is broken — try the next one
                # instead of abandoning the whole card. A prior version broke
                # out of the loop unconditionally here: a single key with an
                # invalid credential (401 UNAUTHENTICATED) silently degraded
                # every card to the weak regex fallback for the rest of the
                # process, even though the other configured keys were never
                # attempted.
                logger.warning(
                    "Gemini image structuring failed on %s, trying next key: %s", key_label, message[:300]
                )
                continue
        if last_error:
            raise last_error
        return {}
    except Exception:
        return {}


FEW_SHOT_EXAMPLE = """
Example — how to pick name vs. company from an annotated transcript:
Annotated OCR lines:
[front L0 | top | large] ACME INDUSTRIAL SUPPLY PTE LTD
[front L1 | top | small] Precision Engineering & Fabrication
[front L2 | middle | large] Tan Wei Ming
[front L3 | middle | normal] Regional Sales Manager
[front L4 | bottom | normal] M: +65 9123 4567  E: wei.ming@acme-industrial.com
Correct extraction: business="ACME INDUSTRIAL SUPPLY PTE LTD" (top brand line), name="Tan Wei Ming"
(large personal-name line, not the top brand, immediately above the designation),
designation="Regional Sales Manager". Do not swap these: the company is the legal-entity/brand
line even if it isn't the largest text, and the name is a personal-looking line distinct from it.

Negative example — company text must not become the name:
Annotated OCR lines:
[front L0 | top | large] TOKKI
[front L1 | middle | large] FITRI ALFIANA
[front L2 | middle | normal] Procurement Officer
Correct extraction: business="TOKKI", name="FITRI ALFIANA", designation="Procurement Officer".
Wrong extraction: name="TOKKI". A single all-caps brand token is a company/brand unless it is
clearly printed as a person's personal-name line.

Example — a combined name+title line must be split, not left merged or dropped:
Annotated OCR lines:
[front L0 | top | large] Babcock & Wilcox
[front L1 | middle | normal] John Doe, Regional Manager
[front L2 | bottom | normal] T: +91 22 4126 6030  E: john.doe@babcock.com
Correct extraction: business="Babcock & Wilcox", name="John Doe", designation="Regional Manager".
Wrong extraction: name="John Doe, Regional Manager" (unsplit) or name=null (dropped). Split on the
comma/dash separator: the human-name part is name, the role part is designation.

Example — honorific and post-nominal letters stay attached to the name:
Annotated OCR lines:
[front L0 | top | large] CESCO
[front L1 | middle | large] Ir. Bambang Suryanto, M.T.
[front L2 | middle | normal] Plant Manager
Correct extraction: business="CESCO", name="Ir. Bambang Suryanto, M.T.", designation="Plant Manager".
An honorific (Ir./Dr./Mr./Mrs./Prof./Haji) or post-nominal (M.T., B.Eng) does not disqualify a line
from being the name — do not strip it and do not mistake it for a business/legal suffix.

Example — an individual card with no company at all:
Annotated OCR lines:
[front L0 | middle | large] Priya Sharma
[front L1 | middle | normal] Independent Consultant
[front L2 | bottom | normal] M: +91 98765 43210  E: priya.sharma@gmail.com
Correct extraction: name="Priya Sharma", designation="Independent Consultant", business=null.
There is no legal-entity line or business noun anywhere on the card, so business must be null —
never promote the name or a generic title into the business/company field just to fill it in.

Example — the postal code comes from the address block, never from a phone number:
Annotated OCR lines:
[front L0 | top | large] PT. SARANA TEKNIK MANDIRI
[front L1 | middle | large] Dewi Lestari
[front L2 | middle | normal] Sales Engineer
[front L3 | bottom | normal] Jl. Raya Narogong Km 12, Bantargebang, Bekasi 17310, Indonesia
[front L4 | bottom | normal] T: +62 21 8236 4551  F: +62 21 8236 4552
Correct extraction: zip_code="17310" — the 5-digit code printed after the city inside the
address line (Indonesia uses 5-digit postal codes).
Wrong extraction: zip_code="82364" or "4551" (digit chunks inside the phone/fax numbers are
never postal codes, even when they are 5 digits long).

Negative example — P.O. Box numbers and no-postal-code countries:
Annotated OCR lines:
[front L0 | top | large] GULF MARINE SERVICES FZE
[front L1 | middle | large] Ahmed Al Mansoori
[front L2 | middle | normal] Operations Manager
[front L3 | bottom | normal] P.O. Box 61242, Jebel Ali Free Zone, Dubai, UAE
Correct extraction: zip_code=null. The UAE has no postal codes; "61242" is a P.O. Box
number, which is part of the address text but never a zip_code.
""".strip()


# Everything Gemini must know about HOW to classify — persona, layout rules,
# per-field procedures, internal reasoning steps, and worked examples — is
# packed into a single system instruction shared by the image and text paths.
# The per-call user prompt then only carries the task statement and the
# card-specific data (image/OCR transcript/hints), which keeps the rules in
# the highest-authority slot of the request instead of buried mid-prompt.
GEMINI_SYSTEM_INSTRUCTION = f"""
You are a meticulous business-card data-entry specialist. Your only job is to
transcribe business cards and sort the printed text into structured contact
fields without ever putting a value into the wrong field. You follow the
procedures below exactly, every time, for every card. When a field is not
clearly printed on the card you return null instead of guessing — a null is
always better than a wrong value, because a wrong value silently corrupts a
CRM record while a null gets reviewed by a human.

The four fields that are most often confused are: name (the PERSON), business/
company (the ORGANIZATION), designation (the JOB TITLE), and zip_code (the
POSTAL CODE). Each has a dedicated procedure below. Never skip them.

{BUSINESS_CARD_VISUAL_RULES}

{NAME_VS_COMPANY_PROCEDURE}

{ZIP_CODE_PROCEDURE}

{THINK_BEFORE_CLASSIFY}

{FEW_SHOT_EXAMPLE}
""".strip()


def _annotated_lines(side: str, blocks: list) -> list[str]:
    lines = []
    for block in blocks:
        size_tag = getattr(block, "size_tag", None) or "normal"
        position_band = getattr(block, "position_band", None) or "unknown"
        lines.append(f"[{side} L{block.line_index} | {position_band} | {size_tag}] {block.text}")
    return lines


def _annotated_transcript(ocr_results: list[OCRSideResult] | None) -> str:
    """Render OCR lines with position/size annotations so Gemini gets the same
    layout cues a human reads a card with (top brand vs. a large personal-name
    line), instead of only a flat, order-less block of text.
    """
    if not ocr_results:
        return ""
    lines: list[str] = []
    for result in ocr_results:
        if result.blocks:
            lines.extend(_annotated_lines(result.side, result.blocks))
    return "\n".join(lines)


def structure_card_text(
    event_id: str,
    front_text: str,
    back_text: str | None = None,
    candidate_hints: list[dict] | None = None,
    ocr_results: list[OCRSideResult] | None = None,
) -> dict:
    _load_genai()
    all_visible_text = "\n".join(part for part in [front_text, back_text or ""] if part.strip()).strip()
    annotated_transcript = _annotated_transcript(ocr_results)
    annotated_section = (
        f"\nAnnotated OCR lines (position/size cues — large text near the top is usually the\n"
        f"company/brand; a large personal-name-looking line elsewhere is usually the person):\n"
        f"{annotated_transcript}\n"
        if annotated_transcript
        else ""
    )
    prompt = f"""
Sort the OCR text below from a two-sided business card into Excel contact fields,
following your field procedures (name vs. company, designation, zip_code).

Return only valid JSON with these keys:
{", ".join(FIELDS)}

Task-specific rules:
- The OCR text below is the only source of truth. Do not use outside knowledge.
- Do not invent missing values. Prefer null over a guessed value.
- Every non-null field except category/country_code must be supported by an exact OCR line.
- Preserve front_text and back_text exactly as supplied.
- Set all_visible_text to the combined OCR text.
- field_evidence must be an array of objects, each {{"field": "<field name>", "evidence": "<exact OCR line(s) used>"}}, citing the source line for each non-null field.
- uncertain_fields must list fields where OCR is ambiguous or incomplete.
- Email fields must contain @.
- Website must be a printed website/domain value, not just the domain part of an email unless separately printed.
- If multiple domains are visible, choose the website whose domain is most similar to Business/company.
- If no separate website is printed, use the email domain as Website only when it clearly matches Business/company.
- Country must be the full country name, not ISO code. For example use Indonesia, not ID.
- Contact1 is main direct/mobile. Contact2 is office/telephone. Contact3 is fax or another printed number.
- Contact1, Contact2, and Contact3 must be digits only and include country calling code when visible or inferable.
- Business/company should be the top brand/company line from the front OCR text. If OCR missed the logo text, return null rather than guessing.
- Name is a PERSON's name, distinct from the business/company line, even when both appear near the top. A line tagged "large" near the top-middle or middle of the card that reads like a person's name (not a legal entity, not containing Pte/Ltd/PT/Inc/Co) is very likely the name. Do not put the company name into the name field, and do not put a person's name into business/company.
- A candidate is NOT a person name if it is the same text as Business/company, is a single all-caps brand/acronym, contains legal suffixes (PT/Pvt/Ltd/LLC/Inc/Pte/Tbk/Co), or contains business nouns such as Engineering, Industrial, Services, Trading, Metal, Works, Radiator, Marine, Oil, Gas, Group, Corporation. Put those lines in Business/company or return name=null.
- If no separate personal-name line is visible near a designation, return name=null. Never copy Business/company into name just to fill the field.
- zip_code must come from the address block per your zip procedure — never from a phone/mobile/fax number or P.O. Box.
- Category must be exactly one of:
{", ".join(BUSINESS_CATEGORIES)}
{annotated_section}
OCR front:
{front_text or ""}

OCR back:
{back_text or ""}

Rule-based candidate hints:
{json.dumps(candidate_hints or [], ensure_ascii=False, indent=2)}
""".strip()

    snapshot = usage_snapshot()
    prompt_tokens = estimate_tokens(GEMINI_SYSTEM_INSTRUCTION + "\n" + prompt)
    if not snapshot.allowed or snapshot.daily_tokens + prompt_tokens > snapshot.daily_token_limit:
        record_usage(
            event_id,
            model=GEMINI_MODEL,
            purpose="business_card_text_structuring",
            prompt_tokens=prompt_tokens,
            status="blocked_local_limit",
            error_message="Local Gemini usage limit reached",
        )
        return {}

    if not is_gemini_configured():
        record_usage(
            event_id,
            model=GEMINI_MODEL,
            purpose="business_card_text_structuring",
            prompt_tokens=prompt_tokens,
            status="skipped_not_configured",
            error_message="Gemini SDK or API key is missing",
        )
        return {}

    generation_config = (
        types.GenerateContentConfig(
            temperature=0,
            system_instruction=GEMINI_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
        )
        if types is not None
        else None
    )

    try:
        last_error: Exception | None = None
        for api_key, key_label in _gemini_keys():
            try:
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                    config=generation_config,
                )
                text = (getattr(response, "text", None) or "").strip()
                completion_tokens = estimate_tokens(text)
                parsed = _parse_json(text)
                if not parsed and text:
                    # JSON mode should prevent this, but if the model still
                    # wrapped the JSON in prose, retry once with a corrective
                    # reprompt rather than silently proceeding with an
                    # almost-empty (but truthy) record.
                    retry_prompt = (
                        f"{prompt}\n\nYour previous reply was not valid JSON. "
                        "Reply with ONLY the JSON object, no prose, no markdown fences."
                    )
                    retry_response = client.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=retry_prompt,
                        config=generation_config,
                    )
                    retry_text = (getattr(retry_response, "text", None) or "").strip()
                    completion_tokens += estimate_tokens(retry_text)
                    parsed = _parse_json(retry_text)
                record_usage(
                    event_id,
                    provider="gemini",
                    model=GEMINI_MODEL,
                    purpose="business_card_text_structuring",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    key_label=key_label,
                )
                if not parsed:
                    # Still nothing usable after the retry — let the caller's
                    # deterministic fallback run instead of returning a
                    # near-empty record.
                    return {}
                parsed["front_text"] = front_text or parsed.get("front_text")
                parsed["back_text"] = back_text if back_text is not None else parsed.get("back_text")
                parsed["all_visible_text"] = all_visible_text or parsed.get("all_visible_text")
                return clean_structured_fields(parsed)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                record_usage(
                    event_id,
                    provider="gemini",
                    model=GEMINI_MODEL,
                    purpose="business_card_text_structuring",
                    prompt_tokens=prompt_tokens,
                    status="error",
                    error_message=message[:500],
                    key_label=key_label,
                )
                if "429" in message or "RESOURCE_EXHAUSTED" in message:
                    _exhausted_key_labels.add(key_label)
                    continue
                if _is_config_error(message):
                    logger.error(
                        "Gemini text structuring rejected the request (config/schema bug, "
                        "NOT a quota issue) — every card will silently fall back to the regex "
                        "extractor until this is fixed: %s",
                        message[:400],
                    )
                    break
                # See structure_card_image's identical comment: a non-quota
                # error is specific to this key/attempt, not every key — keep
                # trying the rest instead of falling back to the weak regex
                # extractor after just one bad key.
                logger.warning(
                    "Gemini text structuring failed on %s, trying next key: %s", key_label, message[:300]
                )
                continue
        if last_error:
            raise last_error
        return {}
    except Exception:
        return {}
