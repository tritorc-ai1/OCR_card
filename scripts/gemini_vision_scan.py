from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.llm.gemini_client import is_gemini_configured, structure_card_image


def read_image(path: Path) -> tuple[bytes, str]:
    if not path.exists():
        raise FileNotFoundError(path)
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return path.read_bytes(), mime_type


def main() -> int:
    parser = argparse.ArgumentParser(description="Send business card images directly to Gemini Vision.")
    parser.add_argument("front", type=Path, help="Front-side business card image")
    parser.add_argument("--back", type=Path, help="Optional back-side business card image")
    parser.add_argument("--event-id", default="test_uploads", help="Event id used for local usage logging")
    args = parser.parse_args()

    if not is_gemini_configured():
        print("Gemini is not configured. Check GEMINI_API_KEY and google-genai installation.", file=sys.stderr)
        return 2

    front_bytes, front_mime = read_image(args.front)
    back_bytes = None
    back_mime = None
    if args.back:
        back_bytes, back_mime = read_image(args.back)

    result = structure_card_image(
        event_id=args.event_id,
        front_image=front_bytes,
        front_mime_type=front_mime,
        back_image=back_bytes,
        back_mime_type=back_mime,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
