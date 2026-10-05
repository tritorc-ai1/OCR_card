from __future__ import annotations

import re
from datetime import datetime

from backend.config import BUSINESS_CATEGORIES, DEFAULT_COUNTRY
from backend.models import BusinessCardRecord, FieldCandidate, OCRSideResult
from backend.storage.db import new_id


def _best(candidates: list[FieldCandidate], field: str) -> FieldCandidate | None:
    field_candidates = [candidate for candidate in candidates if candidate.field == field]
    if not field_candidates:
        return None
    return sorted(field_candidates, key=lambda candidate: candidate.confidence, reverse=True)[0]


def normalize_email(value: str | None) -> str | None:
    return value.strip().lower() if value else None


COUNTRY_HINTS = [
    # South Asia
    ("India", "IN", "+91", ["india", "bharat", "maharashtra", "gujarat", "mumbai", "pune", "delhi", "chennai", "kolkata", "bengaluru", "bangalore", "hyderabad", "ahmedabad"]),
    ("Pakistan", "PK", "+92", ["pakistan", "karachi", "lahore", "islamabad"]),
    ("Bangladesh", "BD", "+880", ["bangladesh", "dhaka", "chittagong"]),
    ("Sri Lanka", "LK", "+94", ["sri lanka", "colombo"]),
    ("Nepal", "NP", "+977", ["nepal", "kathmandu"]),
    # Southeast Asia
    ("Indonesia", "ID", "+62", ["indonesia", "jakarta", "balikpapan", "kalimantan", "papua", "sorong", "bekasi", "banten", "cilegon", "gresik", "cikarang"]),
    ("Singapore", "SG", "+65", ["singapore"]),
    ("Malaysia", "MY", "+60", ["malaysia", "kuala lumpur", "penang", "johor"]),
    ("Thailand", "TH", "+66", ["thailand", "bangkok"]),
    ("Vietnam", "VN", "+84", ["vietnam", "hanoi", "ho chi minh", "saigon"]),
    ("Philippines", "PH", "+63", ["philippines", "manila", "cebu"]),
    # East Asia
    ("China", "CN", "+86", ["china", "beijing", "shanghai", "shenzhen", "guangzhou"]),
    ("Hong Kong", "HK", "+852", ["hong kong"]),
    ("Taiwan", "TW", "+886", ["taiwan", "taipei"]),
    ("Japan", "JP", "+81", ["japan", "tokyo", "osaka", "yokohama"]),
    ("South Korea", "KR", "+82", ["korea", "seoul", "busan"]),
    # Middle East
    ("United Arab Emirates", "AE", "+971", ["uae", "united arab emirates", "dubai", "abu dhabi", "sharjah"]),
    ("Saudi Arabia", "SA", "+966", ["saudi", "ksa", "riyadh", "jeddah", "dammam"]),
    ("Qatar", "QA", "+974", ["qatar", "doha"]),
    ("Oman", "OM", "+968", ["oman", "muscat"]),
    ("Kuwait", "KW", "+965", ["kuwait"]),
    ("Bahrain", "BH", "+973", ["bahrain"]),
    ("Turkey", "TR", "+90", ["turkey", "istanbul", "ankara"]),
    ("Iran", "IR", "+98", ["iran", "tehran"]),
    ("Iraq", "IQ", "+964", ["iraq", "baghdad"]),
    ("Jordan", "JO", "+962", ["jordan", "amman"]),
    ("Lebanon", "LB", "+961", ["lebanon", "beirut"]),
    ("Israel", "IL", "+972", ["israel", "tel aviv", "jerusalem"]),
    # Africa
    ("Egypt", "EG", "+20", ["egypt", "cairo", "alexandria"]),
    ("South Africa", "ZA", "+27", ["south africa", "johannesburg", "cape town", "durban", "pretoria"]),
    ("Nigeria", "NG", "+234", ["nigeria", "lagos", "abuja"]),
    ("Kenya", "KE", "+254", ["kenya", "nairobi"]),
    # Europe
    ("United Kingdom", "GB", "+44", ["uk", "united kingdom", "england", "london", "scotland", "wales"]),
    ("Germany", "DE", "+49", ["germany", "berlin", "munich", "frankfurt", "hamburg"]),
    ("France", "FR", "+33", ["france", "paris", "lyon", "marseille"]),
    ("Italy", "IT", "+39", ["italy", "rome", "milan", "turin"]),
    ("Spain", "ES", "+34", ["spain", "madrid", "barcelona"]),
    ("Portugal", "PT", "+351", ["portugal", "lisbon", "porto"]),
    ("Netherlands", "NL", "+31", ["netherlands", "amsterdam", "rotterdam", "the hague"]),
    ("Belgium", "BE", "+32", ["belgium", "brussels", "antwerp"]),
    ("Switzerland", "CH", "+41", ["switzerland", "zurich", "geneva", "basel"]),
    ("Austria", "AT", "+43", ["austria", "vienna"]),
    ("Sweden", "SE", "+46", ["sweden", "stockholm", "gothenburg"]),
    ("Norway", "NO", "+47", ["norway", "oslo"]),
    ("Denmark", "DK", "+45", ["denmark", "copenhagen"]),
    ("Finland", "FI", "+358", ["finland", "helsinki"]),
    ("Poland", "PL", "+48", ["poland", "warsaw", "krakow"]),
    ("Ireland", "IE", "+353", ["ireland", "dublin"]),
    ("Greece", "GR", "+30", ["greece", "athens"]),
    ("Russia", "RU", "+7", ["russia", "moscow", "saint petersburg", "st petersburg"]),
    # Americas
    ("United States", "US", "+1", ["usa", "united states", "america", "california", "texas", "new york"]),
    # Canada also dials +1 (shared NANP with the US); this text-hint entry
    # only fires on an explicit Canadian city/name, so it never overrides a
    # phone-number-based +1 match, which stays ambiguous between the two.
    ("Canada", "CA", "+1", ["canada", "toronto", "vancouver", "montreal", "ontario"]),
    ("Mexico", "MX", "+52", ["mexico", "mexico city", "guadalajara"]),
    ("Brazil", "BR", "+55", ["brazil", "sao paulo", "rio de janeiro"]),
    ("Argentina", "AR", "+54", ["argentina", "buenos aires"]),
    ("Chile", "CL", "+56", ["chile", "santiago"]),
    ("Colombia", "CO", "+57", ["colombia", "bogota"]),
    ("Peru", "PE", "+51", ["peru", "lima"]),
    # Oceania
    ("Australia", "AU", "+61", ["australia", "sydney", "melbourne", "brisbane", "perth"]),
    ("New Zealand", "NZ", "+64", ["new zealand", "auckland", "wellington"]),
]


def infer_country_and_code(*values: str | None) -> tuple[str, str]:
    text = " ".join(value or "" for value in values).lower()
    for country, _, dial_code, hints in COUNTRY_HINTS:
        if any(hint in text for hint in hints):
            return country, dial_code
    default = DEFAULT_COUNTRY.upper()
    for country, iso_code, dial_code, _ in COUNTRY_HINTS:
        if iso_code == default or country.upper() == default:
            return country, dial_code
    return "India", "+91"


def country_name_from_code(country_code: str | None) -> str | None:
    if not country_code:
        return None
    code_digits = re.sub(r"\D", "", country_code)
    if not code_digits:
        return None
    for country, _, dial_code, _ in COUNTRY_HINTS:
        if code_digits == re.sub(r"\D", "", dial_code):
            return country
    return None


def normalize_country_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    normalized_key = normalized.lower()
    for country, iso_code, _, hints in COUNTRY_HINTS:
        if normalized_key in {country.lower(), iso_code.lower()} or normalized_key in hints:
            return country
    return normalized


def normalize_phone(value: str | None, country_code: str | None = None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d+]", "", value)
    if cleaned.count("+") > 1:
        cleaned = cleaned.replace("+", "")
    if cleaned.startswith("00"):
        cleaned = f"+{cleaned[2:]}"
    if country_code and cleaned and not cleaned.startswith("+"):
        digits = re.sub(r"\D", "", cleaned)
        if digits.startswith("0"):
            digits = digits.lstrip("0")
        cleaned = f"{country_code}{digits}"
    return cleaned or None


def national_phone_number(value: str | None, country_code: str | None = None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if country_code:
        code_digits = re.sub(r"\D", "", country_code)
        if digits.startswith(code_digits):
            digits = digits[len(code_digits):]
    return digits.lstrip("0") or None


def format_phone_for_display(value: str | None, country_code: str | None = None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return value.strip() or None
    code_digits = re.sub(r"\D", "", country_code or "")
    if not code_digits and str(value).strip().startswith("+"):
        for _, _, dial_code, _ in COUNTRY_HINTS:
            candidate_code = re.sub(r"\D", "", dial_code)
            if digits.startswith(candidate_code):
                code_digits = candidate_code
                break
    national = digits
    if code_digits and national.startswith(code_digits):
        national = national[len(code_digits):]
    national = national.lstrip("0") or digits
    if code_digits:
        return f"(+{code_digits}) {national}"
    return national


def normalize_website(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if value.lower().startswith(("http://", "https://")):
        return value
    return f"https://{value}"


def category_from_business_text(*values: str | None) -> str:
    text = " ".join(value or "" for value in values).lower()
    rules = [
        ("Oil & Gas", ["oil", "gas", "petroleum", "petrochemical", "offshore", "refinery"]),
        ("Marine Contractors", ["marine", "ship", "vessel", "dock", "port", "harbour", "harbor"]),
        ("Certification", ["certification", "inspection", "audit", "iso", "testing", "quality"]),
        ("Supply Chain Management", ["supply chain", "logistics", "warehouse", "procurement", "freight"]),
        ("Industrial Services", ["industrial service", "maintenance", "fabrication", "shutdown", "repair"]),
        ("Engineering", ["engineering", "engineers", "automation", "design", "technical", "consulting"]),
        ("Manufacturing", ["manufacturing", "manufacturer", "factory", "production"]),
        ("Construction", ["construction", "contractor", "infrastructure", "civil"]),
        ("Trading", ["trading", "traders", "exports", "imports", "distributor", "supplier"]),
        ("Logistics", ["logistics", "transport", "cargo", "shipping"]),
    ]
    for category, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return category
    return "Other"


def resolve_record(
    *,
    event_id: str,
    event_name: str,
    card_id: str,
    front_image_filename: str,
    back_image_filename: str | None,
    ocr_results: list[OCRSideResult],
    candidates: list[FieldCandidate],
    duplicate_flag: str = "No",
) -> BusinessCardRecord:
    now = datetime.now()
    phone_candidates = sorted([candidate for candidate in candidates if candidate.field == "phone"], key=lambda item: item.confidence, reverse=True)
    mobile_candidates = sorted([candidate for candidate in candidates if candidate.field == "mobile"], key=lambda item: item.confidence, reverse=True)
    fax_candidates = sorted([candidate for candidate in candidates if candidate.field == "fax"], key=lambda item: item.confidence, reverse=True)
    address_lines = [candidate.value for candidate in candidates if candidate.field == "address"]
    avg_ocr = sum(result.average_confidence for result in ocr_results) / max(1, len(ocr_results))
    raw_text = " ".join(result.raw_text for result in ocr_results)

    name = _best(candidates, "name")
    designation = _best(candidates, "designation")
    company = _best(candidates, "company")
    email = _best(candidates, "email")
    website = _best(candidates, "website")
    address = ", ".join(dict.fromkeys(address_lines)) or None
    country, country_code = infer_country_and_code(address, raw_text)

    normalized_phone = normalize_phone(phone_candidates[0].value, country_code) if phone_candidates else None
    normalized_mobile = normalize_phone(mobile_candidates[0].value, country_code) if mobile_candidates else None
    normalized_primary = normalized_mobile or normalized_phone
    resolved = {
        "name": name.value if name else None,
        "designation": designation.value if designation else None,
        "company": company.value if company else None,
        "business": company.value if company else None,
        "phone_primary": normalized_primary,
        "phone_number": national_phone_number(normalized_phone, country_code),
        "mobile_number": national_phone_number(normalized_mobile, country_code),
        "phone_extra": normalize_phone(phone_candidates[1].value, country_code) if len(phone_candidates) > 1 else None,
        "fax_number": national_phone_number(normalize_phone(fax_candidates[0].value, country_code), country_code) if fax_candidates else None,
        "country_code": country_code,
        "email": normalize_email(email.value if email else None),
        "website": normalize_website(website.value if website else None),
        "address": address,
        "country": country,
    }
    resolved["category"] = category_from_business_text(
        resolved["business"],
        resolved["designation"],
        resolved["website"],
        resolved["address"],
        raw_text,
    )

    low_fields = [
        field
        for field in ["name", "company", "phone_primary", "email"]
        if not resolved.get(field)
    ]
    if avg_ocr < 0.65:
        low_fields.append("ocr")
    if resolved["email"] or resolved["phone_primary"]:
        confidence = "High" if not low_fields[:2] and avg_ocr >= 0.75 else "Medium"
    else:
        confidence = "Low"

    return BusinessCardRecord(
        record_id=new_id("record"),
        card_id=card_id,
        event_id=event_id,
        date=now.strftime("%Y-%m-%d"),
        time=now.strftime("%H:%M:%S"),
        event_name=event_name,
        confidence_score=confidence,
        low_confidence_fields=sorted(set(low_fields)),
        duplicate_flag=duplicate_flag,
        front_image_filename=front_image_filename,
        back_image_filename=back_image_filename,
        **resolved,
    )
