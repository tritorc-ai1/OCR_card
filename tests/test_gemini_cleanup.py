from backend.llm.gemini_client import clean_structured_fields, structure_card_text_deterministic


def test_grid_ai_contacts_are_cleaned_to_excel_format() -> None:
    fields = clean_structured_fields(
        {
            "business": "East Coast O&G Metal Works",
            "country": "Malaysia",
            "email1": " kelvin@ecom.net.my ",
            "email2": "team@ecom.net.my",
            "website": "www.ecom.net.my",
            "contact1": "+60 13-358 1918",
            "contact2": "(+60) 9-859 1918",
            "contact3": "Fax +60 9-859 1919",
            "front_text": "\n".join(
                [
                    "East Coast O&G Metal Works",
                    "kelvin@ecom.net.my",
                    "team@ecom.net.my",
                    "www.ecom.net.my",
                    "M +60 13-358 1918",
                    "T (+60) 9-859 1918",
                    "Fax +60 9-859 1919",
                ]
            ),
        }
    )

    assert fields["business"] == "East Coast O&G Metal Works"
    assert fields["email1"] == "kelvin@ecom.net.my"
    assert fields["email2"] == "team@ecom.net.my"
    assert fields["email"] == "kelvin@ecom.net.my"
    assert fields["website"] == "https://www.ecom.net.my"
    assert fields["contact1"] == "60133581918"
    assert fields["contact2"] == "6098591918"
    assert fields["contact3"] == "6098591919"
    assert fields["phone_primary"] == "+60133581918"


def test_digit_contacts_with_country_code_are_not_prefixed_twice() -> None:
    fields = clean_structured_fields(
        {
            "country": "Malaysia",
            "country_code": "+60",
            "contact1": "60133581918",
            "contact2": "6098591918",
            "front_text": "M 60133581918\nT 6098591918",
        }
    )

    assert fields["contact1"] == "60133581918"
    assert fields["contact2"] == "6098591918"
    assert fields["phone_primary"] == "+60133581918"


def test_indonesia_contacts_are_not_prefixed_twice() -> None:
    fields = clean_structured_fields(
        {
            "country": "Indonesia",
            "country_code": "+62",
            "contact1": "6281236678953",
            "contact2": "622129770999",
            "phone_primary": "6281236678953",
            "front_text": "M 6281236678953\nT 622129770999",
        }
    )

    assert fields["contact1"] == "6281236678953"
    assert fields["contact2"] == "622129770999"
    assert fields["phone_primary"] == "+6281236678953"
    assert fields["mobile_number"] == "6281236678953"
    assert fields["country"] == "Indonesia"


def test_country_is_inferred_from_phone_code_when_not_printed() -> None:
    fields = clean_structured_fields(
        {
            "business": "Petrosea",
            "front_text": "Petrosea\nM +62 812 3667 8953\nE ahmad.ajitama@petrosea.com",
        }
    )

    assert fields["country_code"] == "+62"
    assert fields["country"] == "Indonesia"
    assert fields["contact1"] == "6281236678953"


def test_indonesia_local_mobile_uses_address_country_code() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "ASR PT. AIR SURYA RADIATOR",
                    "Mobile : 08212307 8763",
                    "Bekasi 17156",
                ]
            ),
        }
    )

    assert fields["country_code"] == "+62"
    assert fields["country"] == "Indonesia"
    assert fields["contact1"] == "6282123078763"


def test_model_business_not_supported_by_transcript_is_dropped() -> None:
    fields = clean_structured_fields(
        {
            "business": "Fake Hallucinated Company",
            "front_text": "ALPHA INDUSTRIAL\nRavi Kumar\nSales Manager\nravi@alpha.example",
        }
    )

    assert fields["business"] is None
    assert fields["company"] is None
    assert fields["email1"] == "ravi@alpha.example"


def test_spaced_ocr_email_is_captured() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "E info @ tritorc . com\nsales (at) tritorc (dot) com",
        }
    )

    assert fields["email1"] == "info@tritorc.com"
    assert fields["email2"] == "sales@tritorc.com"


def test_website_prefers_company_matching_domain() -> None:
    fields = clean_structured_fields(
        {
            "business": "Petrosea",
            "front_text": "\n".join(
                [
                    "Petrosea",
                    "E contact@randomvendor.com",
                    "W www.petrosea.com",
                    "www.unrelated-example.com",
                ]
            ),
        }
    )

    assert fields["website"] == "https://www.petrosea.com"


def test_email_domain_can_fill_website_when_it_matches_company() -> None:
    fields = clean_structured_fields(
        {
            "business": "Tritorc",
            "front_text": "Tritorc\nE info@tritorc.com",
        }
    )

    assert fields["website"] == "https://tritorc.com"


def test_email_local_part_is_not_used_as_website() -> None:
    fields = clean_structured_fields(
        {
            "business": "Petrosea",
            "front_text": "Petrosea\nE Ahmad.Ajitama@petrosea.com",
        }
    )

    assert fields["website"] == "https://petrosea.com"


def test_deterministic_text_structuring_fills_fields_without_llm() -> None:
    fields = structure_card_text_deterministic(
        front_text="\n".join(
            [
                "TOKKI",
                "FITRI ALFIANA",
                "Procurement Officer",
                "+62 877-7190-3337",
                "fia@tef.co.id",
                "Indonesia 42443",
            ]
        ),
        candidate_hints=[
            {"field": "name", "value": "FITRI ALFIANA", "confidence": 0.8, "source": "front"},
            {"field": "designation", "value": "Procurement Officer", "confidence": 0.8, "source": "front"},
            {"field": "company", "value": "TOKKI", "confidence": 0.75, "source": "front"},
            {"field": "email", "value": "fia@tef.co.id", "confidence": 0.95, "source": "rule"},
            {"field": "mobile", "value": "+62 877-7190-3337", "confidence": 0.9, "source": "rule"},
        ],
    )

    assert fields["name"] == "FITRI ALFIANA"
    assert fields["designation"] == "Procurement Officer"
    assert fields["business"] == "TOKKI"
    assert fields["email1"] == "fia@tef.co.id"
    assert fields["contact1"] == "6287771903337"
    assert fields["country"] == "Indonesia"


def test_deterministic_text_structuring_infers_top_brand_and_address() -> None:
    fields = structure_card_text_deterministic(
        front_text="\n".join(
            [
                "ASR PT. AIR SURYA RADIATOR",
                "DESIGN & FABRICATION",
                "BUDI SANTOSO",
                "Marketing",
                "Mobile : 08212307 8763",
                "E-mail : bsantoso69.ptasr@gmail.com",
                "Jl. Mawar No. 88 Rt. 04/02 Padurenan, Mustika Jaya - Bekasi 17156",
                "Website: http://www.ptasr.co.id; e-mail: marketing@ptasr.co.id",
            ]
        ),
        candidate_hints=[
            {"field": "email", "value": "bsantoso69.ptasr@gmail.com", "confidence": 0.95},
            {"field": "email", "value": "marketing@ptasr.co.id", "confidence": 0.95},
            {"field": "mobile", "value": "08212307 8763", "confidence": 0.9},
            {"field": "website", "value": "http://www.ptasr.co.id", "confidence": 0.8},
        ],
    )

    assert fields["business"] == "ASR PT. AIR SURYA RADIATOR"
    assert fields["name"] == "BUDI SANTOSO"
    assert fields["designation"] == "Marketing"
    assert "Bekasi" in fields["address"]
    assert fields["zip_code"] == "17156"
    assert fields["website"] == "http://www.ptasr.co.id"


def test_cleanup_replaces_company_name_misclassified_as_person_name() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "TOKKI",
                    "FITRI ALFIANA",
                    "Procurement Officer",
                    "+62 877-7190-3337",
                    "fia@tef.co.id",
                ]
            ),
            "name": "TOKKI",
            "business": "TOKKI",
            "designation": "Procurement Officer",
            "contact1": "+62 877-7190-3337",
            "email1": "fia@tef.co.id",
        }
    )

    assert fields["business"] == "TOKKI"
    assert fields["company"] == "TOKKI"
    assert fields["name"] == "FITRI ALFIANA"


def test_cleanup_rejects_legal_entity_as_person_name() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "ASR PT. AIR SURYA RADIATOR",
                    "DESIGN & FABRICATION",
                    "BUDI SANTOSO",
                    "Marketing",
                    "Mobile : 08212307 8763",
                ]
            ),
            "name": "ASR PT. AIR SURYA RADIATOR",
            "business": "ASR PT. AIR SURYA RADIATOR",
            "designation": "Marketing",
            "contact1": "08212307 8763",
        }
    )

    assert fields["business"] == "ASR PT. AIR SURYA RADIATOR"
    assert fields["name"] == "BUDI SANTOSO"


def test_deterministic_fallback_finds_name_below_top_brand_logo() -> None:
    # Regression: AALBORG card — a real card where OCR merged the top brand
    # across several lines and the person's name/designation sit well below
    # it. The old top-front-line heuristic picked the brand as the "name".
    fields = structure_card_text_deterministic(
        front_text="\n".join(
            [
                "AALBORG INDUSTRI",
                "INDONESIA",
                "NS Group",
                "Budhi Kristyo Wibowo",
                "Technical Director",
                "Email: budikris@aalborgindo.com",
                "Mobile: +62 811-2140-234",
                "PT. AALBORG INDUSTRI INDONESIA",
                "Jl. Rawa Sumur II, Blok III, Kav. CC 6-7",
                "Jakarta Timur, Indonesia 13930",
            ]
        ),
        candidate_hints=[
            {"field": "name", "value": "AALBORG INDUSTRI", "confidence": 0.9, "evidence": "top_front_line"},
            {"field": "company", "value": "INDONESIA", "confidence": 0.9, "evidence": "top_front_brand"},
            {"field": "designation", "value": "Technical Director", "confidence": 0.72, "evidence": "title_keyword"},
        ],
    )

    assert fields["name"] == "Budhi Kristyo Wibowo"
    assert fields["business"] == "PT. AALBORG INDUSTRI INDONESIA"
    assert fields["designation"] == "Technical Director"


def test_deterministic_fallback_rejects_field_label_words_as_name() -> None:
    # Regression: bare OCR label words ("Phone", "Email", "Address") sitting
    # on their own line, with the actual value on a different line, must
    # never become the extracted name.
    fields = structure_card_text_deterministic(
        front_text="\n".join(
            [
                "Office",
                "Phone",
                "M. Phone",
                "Email",
                "PT. ENERTECH ENGINEERING",
                "RICHARD ADOLF VHW",
                "Director",
                ": 021 2105 1245",
                ": richard.mytwins@gmail.com",
            ]
        ),
        candidate_hints=[
            {"field": "name", "value": "Email", "confidence": 0.9, "evidence": "top_front_line"},
            {"field": "company", "value": "Phone", "confidence": 0.72, "evidence": "company_keyword"},
            {"field": "designation", "value": "Director", "confidence": 0.72, "evidence": "title_keyword"},
        ],
    )

    assert fields["name"] == "RICHARD ADOLF VHW"
    assert fields["business"] == "PT. ENERTECH ENGINEERING"
    assert fields["name"] not in {"Phone", "Email", "Address", "Office"}


def test_deterministic_fallback_keeps_designation_hint_outside_narrow_keyword_list() -> None:
    # Regression: DESIGNATION_RE's keyword list is narrower than the
    # TITLE_KEYWORDS vocabulary that produces designation hints (e.g. CEO,
    # Founder, Partner aren't in DESIGNATION_RE). A hint that merely fails
    # that narrower keyword match must not be discarded.
    fields = structure_card_text_deterministic(
        front_text="ACME CORP\nJohn Doe\nCEO\nEmail: john@acme.com",
        candidate_hints=[
            {"field": "name", "value": "John Doe", "confidence": 0.8, "evidence": "top_front_line"},
            {"field": "designation", "value": "CEO", "confidence": 0.72, "evidence": "title_keyword"},
            {"field": "company", "value": "ACME CORP", "confidence": 0.82, "evidence": "top_front_company"},
        ],
    )

    assert fields["designation"] == "CEO"


def test_deterministic_fallback_does_not_use_back_side_branch_name_as_business() -> None:
    # Regression: _infer_business's whole-card scan must stay front-side
    # only — a back-side branch/office line must never win over (or replace
    # with nothing) a legitimate front-side company name.
    fields = structure_card_text_deterministic(
        front_text="xyz\nJohn Doe\nSales Manager",
        back_text="XYZ SERVICE CENTER BRANCH OFFICE",
    )

    assert fields["business"] != "XYZ SERVICE CENTER BRANCH OFFICE"


def test_deterministic_fallback_finds_legal_entity_past_wrapped_address() -> None:
    # Regression: a multi-line wrapped address pushed the real "PT. ..."
    # company line past the old first-8-lines cutoff in _infer_business.
    fields = structure_card_text_deterministic(
        front_text="\n".join(
            [
                "Sahat Siahaan",
                "Director",
                "0859-2532-5999",
                "info@mechaeltra.com",
                "www.mechaelta.com",
                "Jl. Pinang Raya Blok F16 No. 21,",
                "Kawasan Delta Silicon 3, Lippo",
                "Cikarang, Kabupaten Bekasi.",
                "Jawa Barat 17530",
                "PT. Mechatronic Transtec Indonesia",
            ]
        ),
    )

    assert fields["business"] == "PT. Mechatronic Transtec Indonesia"
    assert fields["name"] == "Sahat Siahaan"

def test_zip_code_copied_from_phone_number_is_rejected() -> None:
    # Regression: Gemini sometimes returns a 5-digit chunk of a phone/fax
    # number as the zip. The transcript has no address, so zip must be null.
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "PT. SARANA TEKNIK",
                    "Dewi Lestari",
                    "Sales Engineer",
                    "T: +62 21 8236 4551",
                    "F: +62 21 8236 4552",
                ]
            ),
            "name": "Dewi Lestari",
            "business": "PT. SARANA TEKNIK",
            "zip_code": "82364",
            "contact2": "+62 21 8236 4551",
        }
    )

    assert fields["zip_code"] is None


def test_zip_code_from_address_line_is_kept() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "PT. SARANA TEKNIK",
                    "Dewi Lestari",
                    "Sales Engineer",
                    "Jl. Raya Narogong Km 12, Bekasi 17310, Indonesia",
                    "T: +62 21 8236 4551",
                ]
            ),
            "name": "Dewi Lestari",
            "business": "PT. SARANA TEKNIK",
            "zip_code": "17310",
            "contact2": "+62 21 8236 4551",
        }
    )

    assert fields["zip_code"] == "17310"


def test_zip_code_wrong_length_for_country_is_rejected() -> None:
    # India (+91) PIN codes are exactly 6 digits — a 5-digit run cannot be
    # an Indian postal code even when it sits on an address-looking line.
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "ACME ENGINEERING PVT LTD",
                    "Rakesh Patel",
                    "Director",
                    "Plot No. 12345, MIDC Road, Navi Mumbai, India",
                    "M: +91 98765 43210",
                ]
            ),
            "name": "Rakesh Patel",
            "business": "ACME ENGINEERING PVT LTD",
            "zip_code": "12345",
            "contact1": "+91 98765 43210",
        }
    )

    assert fields["zip_code"] is None


def test_po_box_number_is_not_a_zip_code() -> None:
    # UAE has no postal codes; the P.O. Box number must never fill zip_code.
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "GULF MARINE SERVICES FZE",
                    "Ahmed Al Mansoori",
                    "Operations Manager",
                    "P.O. Box 61242, Jebel Ali Free Zone, Dubai, UAE",
                    "T: +971 4 883 5555",
                ]
            ),
            "name": "Ahmed Al Mansoori",
            "business": "GULF MARINE SERVICES FZE",
            "zip_code": "61242",
            "contact2": "+971 4 883 5555",
        }
    )

    assert fields["zip_code"] is None


def test_merged_name_and_title_are_split() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "Babcock & Wilcox",
                    "John Doe, Regional Manager",
                    "T: +91 22 4126 6030",
                ]
            ),
            "name": "John Doe, Regional Manager",
            "business": "Babcock & Wilcox",
        }
    )

    assert fields["name"] == "John Doe"
    assert fields["designation"] == "Regional Manager"


def test_hyphenated_name_is_not_split() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "ACME GROUP",
                    "Jean-Pierre Dubois",
                    "Sales Director",
                ]
            ),
            "name": "Jean-Pierre Dubois",
            "designation": "Sales Director",
            "business": "ACME GROUP",
        }
    )

    assert fields["name"] == "Jean-Pierre Dubois"
    assert fields["designation"] == "Sales Director"


def test_company_text_in_designation_is_dropped() -> None:
    # Gemini occasionally copies the brand or a service tagline into
    # designation — both must be nulled, not left to corrupt the record.
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "TOKKI",
                    "FITRI ALFIANA",
                    "Procurement Officer",
                ]
            ),
            "name": "FITRI ALFIANA",
            "business": "TOKKI",
            "designation": "TOKKI",
        }
    )

    assert fields["designation"] is None
    assert fields["business"] == "TOKKI"
    assert fields["name"] == "FITRI ALFIANA"


def test_ceo_and_founder_designations_are_kept() -> None:
    fields = clean_structured_fields(
        {
            "front_text": "\n".join(
                [
                    "NUSANTARA TECH",
                    "Andi Wijaya",
                    "Founder & CEO",
                ]
            ),
            "name": "Andi Wijaya",
            "business": "NUSANTARA TECH",
            "designation": "Founder & CEO",
        }
    )

    assert fields["designation"] == "Founder & CEO"

def test_gemini_response_schema_has_no_additional_properties() -> None:
    # Regression: a dict[str, str] field emits `additionalProperties` in the
    # JSON schema, which Gemini's response_schema REJECTS with a 400
    # INVALID_ARGUMENT. That error silently forced every card onto the weak
    # regex fallback. The response schema must stay free of it forever.
    from backend.llm.gemini_client import GeminiCardExtraction

    schema = GeminiCardExtraction.model_json_schema()
    hits = []

    def walk(node, path="root"):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "additionalProperties":
                    hits.append(path)
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(schema)
    assert not hits, f"response schema must not contain additionalProperties: {hits}"


def test_field_evidence_list_shape_is_normalized_to_dict() -> None:
    # Gemini returns field_evidence as a list of {field, evidence} objects;
    # the app stores it as a {field: evidence} dict.
    fields = clean_structured_fields(
        {
            "front_text": "ACME LTD\nJohn Doe\nSales Manager",
            "name": "John Doe",
            "business": "ACME LTD",
            "designation": "Sales Manager",
            "field_evidence": [
                {"field": "name", "evidence": "John Doe"},
                {"field": "business", "evidence": "ACME LTD"},
            ],
        }
    )

    assert fields["field_evidence"] == {"name": "John Doe", "business": "ACME LTD"}
