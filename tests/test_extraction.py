from backend.extraction.candidate_extractors import extract_candidates
from backend.extraction.field_resolver import format_phone_for_display, resolve_record
from backend.models import OCRSideResult, OCRTextBlock


def make_result(text: str) -> OCRSideResult:
    blocks = [
        OCRTextBlock(text=line, confidence=0.92, side="front", line_index=index)
        for index, line in enumerate(text.splitlines())
    ]
    return OCRSideResult(side="front", raw_text=text, average_confidence=0.92, blocks=blocks, engine="test")


def test_email_domain_is_not_promoted_to_website() -> None:
    candidates = extract_candidates([make_result("John Smith\njohn@example.com")])
    assert [candidate.value for candidate in candidates if candidate.field == "email"] == ["john@example.com"]
    assert [candidate.value for candidate in candidates if candidate.field == "website"] == []


def test_email_local_part_is_not_promoted_to_website_candidate() -> None:
    candidates = extract_candidates([make_result("E Ahmad.Ajitama@petrosea.com")])
    assert [candidate.value for candidate in candidates if candidate.field == "email"] == ["Ahmad.Ajitama@petrosea.com"]
    assert [candidate.value for candidate in candidates if candidate.field == "website"] == []


def test_spaced_ocr_email_is_extracted() -> None:
    candidates = extract_candidates([make_result("E info @ tritorc . com")])
    assert [candidate.value for candidate in candidates if candidate.field == "email"] == ["info@tritorc.com"]


def test_resolves_core_business_card_fields() -> None:
    result = make_result("John Smith\nCEO Example Pvt Ltd\njohn@example.com +91 9876543210\nwww.example.com")
    candidates = extract_candidates([result])
    record = resolve_record(
        event_id="test_uploads",
        event_name="Test Uploads",
        card_id="card_test",
        front_image_filename="front.jpg",
        back_image_filename=None,
        ocr_results=[result],
        candidates=candidates,
    )
    assert record.name == "John Smith"
    assert record.company == "CEO Example Pvt Ltd"
    assert record.business == "CEO Example Pvt Ltd"
    assert record.email == "john@example.com"
    assert record.phone_primary == "+919876543210"
    assert record.country_code == "+91"
    assert record.phone_number == "9876543210"
    assert record.website == "https://www.example.com"
    assert record.category in {"Engineering", "Other"}
    assert record.confidence_score == "High"


def test_industry_category_rules() -> None:
    result = make_result("Ocean Marine Contractors\nOffshore oil and gas maintenance services\nwww.oceanmarine.com")
    candidates = extract_candidates([result])
    record = resolve_record(
        event_id="test_uploads",
        event_name="Test Uploads",
        card_id="card_test",
        front_image_filename="front.jpg",
        back_image_filename=None,
        ocr_results=[result],
        candidates=candidates,
    )
    assert record.category == "Oil & Gas"


def test_address_country_hint_sets_phone_country_code() -> None:
    result = make_result("Jane Doe\nIndustrial Services\nDubai UAE\n501234567")
    candidates = extract_candidates([result])
    record = resolve_record(
        event_id="test_uploads",
        event_name="Test Uploads",
        card_id="card_test",
        front_image_filename="front.jpg",
        back_image_filename=None,
        ocr_results=[result],
        candidates=candidates,
    )
    assert record.country == "United Arab Emirates"
    assert record.country_code == "+971"
    assert record.phone_primary == "+971501234567"
    assert record.phone_number == "501234567"


def test_labeled_phone_mobile_and_fax_are_separated() -> None:
    result = make_result(
        "\n".join(
            [
                "Ahmad Sofryan Ajitama",
                "Procurement Engineer",
                "PT Petrosea Tbk - Head Office",
                "Indy Bintaro Office Park, Jakarta 12330, Indonesia",
                "T +62 21 2977 0999",
                "F +62 21 2977 0988",
                "M +62 812 3667 8953",
                "E Ahmad.Ajitama@petrosea.com",
                "W www.petrosea.com",
            ]
        )
    )
    candidates = extract_candidates([result])
    record = resolve_record(
        event_id="test_uploads",
        event_name="Test Uploads",
        card_id="card_test",
        front_image_filename="front.jpg",
        back_image_filename=None,
        ocr_results=[result],
        candidates=candidates,
    )
    assert record.country == "Indonesia"
    assert record.country_code == "+62"
    assert record.phone_primary == "+6281236678953"
    assert record.phone_number == "2129770999"
    assert record.mobile_number == "81236678953"
    assert record.fax_number == "2129770988"


def test_top_all_caps_brand_is_not_name_candidate() -> None:
    candidates = extract_candidates(
        [
            make_result(
                "\n".join(
                    [
                        "TOKKI",
                        "FITRI ALFIANA",
                        "Procurement Officer",
                        "+62 877-7190-3337",
                        "fia@tef.co.id",
                    ]
                )
            )
        ]
    )

    name_values = [candidate.value for candidate in candidates if candidate.field == "name"]
    company_values = [candidate.value for candidate in candidates if candidate.field == "company"]
    assert "TOKKI" not in name_values
    assert "FITRI ALFIANA" in name_values
    assert "TOKKI" in company_values


def test_phone_display_format_wraps_country_code() -> None:
    assert format_phone_for_display("6281236678953", "+62") == "(+62) 81236678953"
    assert format_phone_for_display("81236678953", "+62") == "(+62) 81236678953"
    assert format_phone_for_display("+91 98765 43210", None) == "(+91) 9876543210"
