"""Generate the approved non-sensitive V1.11 benchmark corpus offline.

The script writes only repository-local synthetic fixtures and their manifest.
It never opens a network socket and never invokes a provider SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BENCHMARK = ROOT / "benchmarks" / "v111"
FIXTURES = BENCHMARK / "fixtures"
MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite"]


def _schema(properties: dict, *, nullable: tuple[str, ...] = ()) -> dict:
    declared = {}
    for name, kind in properties.items():
        if kind == "array":
            value = {"type": "array", "items": {"type": "string"}, "maxItems": 10}
        else:
            value = {"type": kind}
        if name in nullable:
            value["type"] = [kind, "null"]
        declared[name] = value
    return {
        "type": "object",
        "properties": declared,
        "required": list(properties),
        "additionalProperties": False,
    }


def _case(case_id, variant, request, oracle, providers, *, fixture=None):
    case = {
        "case_id": case_id,
        "variant": variant,
        "providers": providers,
        "models": [] if variant == "results" else MODELS,
        "request": request,
        "oracle": oracle,
        "expected_failure_code": None,
    }
    if fixture is not None:
        case["fixture"] = fixture
    return case


def build_cases() -> list[dict]:
    cases = []
    results = [
        ("RES-01", "site:docs.python.org asyncio TaskGroup", ["asyncio", "TaskGroup"]),
        ("RES-02", "Model Context Protocol Streamable HTTP transport specification", ["Model Context Protocol", "Streamable HTTP"]),
        ("RES-03", "x402 HTTP 402 PAYMENT-REQUIRED header", ["402", "PAYMENT-REQUIRED"]),
        ("RES-04", "Base mainnet chain ID 8453 official documentation", ["Base", "8453"]),
    ]
    for case_id, query, anchors in results:
        cases.append(_case(case_id, "results", {"query": query, "max_results": 5}, {"relevance_anchors": anchors}, ["exa"]))
    answers = [
        ("ANS-01", "Which PEP introduced Python's tomllib module, and in which Python version was it added?", ["PEP 680", "Python 3.11"]),
        ("ANS-02", "What are the two standard transports in the current Model Context Protocol specification?", ["stdio", "Streamable HTTP"]),
        ("ANS-03", "Which HTTP status and response header communicate an x402 v2 payment challenge?", ["402", "PAYMENT-REQUIRED"]),
        ("ANS-04", "What is the Base mainnet chain ID and CAIP-2 network identifier?", ["8453", "eip155:8453"]),
    ]
    for case_id, query, facts in answers:
        cases.append(_case(case_id, "answer", {"query": query, "max_results": 5}, {"required_facts": facts}, ["exa", "smartfetch", "gemini"]))
    structured = [
        ("SRS-01", "Which PEP introduced tomllib, and in which Python version?", {"pep": "integer", "module": "string", "python_version": "string"}, {"pep": 680, "module": "tomllib", "python_version": "3.11"}),
        ("SRS-02", "What are MCP's standard transports?", {"transports": "array"}, {"transports": ["stdio", "Streamable HTTP"]}),
        ("SRS-03", "What are the canonical x402 v2 HTTP status and headers?", {"status_code": "integer", "challenge_header": "string", "signature_header": "string", "settlement_header": "string"}, {"status_code": 402, "challenge_header": "PAYMENT-REQUIRED", "signature_header": "PAYMENT-SIGNATURE", "settlement_header": "PAYMENT-RESPONSE"}),
        ("SRS-04", "Identify the Base mainnet network.", {"network": "string", "chain_id": "integer", "caip2": "string"}, {"network": "Base mainnet", "chain_id": 8453, "caip2": "eip155:8453"}),
    ]
    for case_id, query, properties, expected in structured:
        cases.append(_case(case_id, "structured", {"query": query, "max_results": 5, "max_sources": 3, "json_schema": _schema(properties)}, {"expected": expected, "array_order_ignored": case_id == "SRS-02"}, ["exa", "smartfetch", "gemini"]))
    webpages = [
        ("WEB-01", "https://example.com/", {"title": "string", "domain": "string"}, {"title": "Example Domain", "domain": "example.com"}),
        ("WEB-02", "https://www.rfc-editor.org/rfc/rfc9110.html", {"rfc_number": "integer", "title": "string"}, {"rfc_number": 9110, "title": "HTTP Semantics"}),
        ("WEB-03", "https://docs.python.org/3/library/tomllib.html", {"module": "string", "purpose": "string"}, {"module": "tomllib", "purpose_contains": "TOML"}),
        ("WEB-04", "https://www.rfc-editor.org/rfc/rfc6901.html", {"rfc_number": "integer", "title": "string"}, {"rfc_number": 6901, "title": "JavaScript Object Notation (JSON) Pointer"}),
    ]
    for case_id, source_url, properties, expected in webpages:
        cases.append(_case(case_id, "webpage", {"source_url": source_url, "render_mode": "auto", "json_schema": _schema(properties)}, {"expected": expected}, ["smartfetch", "gemini"]))
    media = [
        ("IMG-01", "image", "png", {"order_id": "string", "total_usd": "string"}, {"order_id": "SF-1042", "total_usd": "42.75"}, ()),
        ("IMG-02", "image", "png", {"model": "string", "batch": "string", "mass_g": "integer"}, {"model": "ORBIT-7", "batch": "B-204", "mass_g": 350}, ()),
        ("IMG-03", "image", "png", {"event": "string", "date": "string", "venue": "string"}, {"event": "Aurora Demo", "date": "2026-10-14", "venue": "Harbor Hall"}, ()),
        ("IMG-04", "image", "png", {"north": "integer", "south": "integer", "west": "integer"}, {"north": 18, "south": 27, "west": 31}, ()),
        ("PDF-01", "pdf", "pdf", {"invoice_id": "string", "customer": "string", "total_usd": "string"}, {"invoice_id": "INV-7301", "customer": "Northwind Lab", "total_usd": "128.40"}, ()),
        ("PDF-02", "pdf", "pdf", {"project": "string", "milestone": "string", "completion_percent": "integer"}, {"project": "Cedar", "milestone": "Beta", "completion_percent": 72}, ()),
        ("PDF-03", "pdf", "pdf", {"policy_id": "string", "effective_date": "string", "limit_usd": "integer"}, {"policy_id": "POL-88", "effective_date": "2026-11-01", "limit_usd": 5000}, ()),
        ("PDF-04", "pdf", "pdf", {"shipment_id": "string", "crates": "integer", "destination": "string"}, {"shipment_id": "SHIP-204", "crates": 6, "destination": "Baltimore"}, ()),
        ("AUD-01", "audio", "wav", {"order_id": "string", "ship_date": "string"}, {"order_id": "SF-550", "ship_date": "2026-10-12"}, ()),
        ("AUD-02", "audio", "wav", {"red_score": "integer", "blue_score": "integer"}, {"red_score": 17, "blue_score": 23}, ()),
        ("AUD-03", "audio", "wav", {"meeting": "string", "time": "string", "room": "string"}, {"meeting": "Alpha", "time": "2:30 PM", "room": "Cedar"}, ()),
        ("AUD-04", "audio", "wav", {"ticket_id": "string", "assignee": "string"}, {"ticket_id": "TK-90", "assignee": None}, ("assignee",)),
        ("VID-01", "video", "webm", {"launch_code": "string", "launch_date": "string"}, {"launch_code": "LANTERN-4", "launch_date": "2026-12-03"}, ()),
        ("VID-02", "video", "webm", {"bolts": "integer", "gears": "integer", "springs": "integer"}, {"bolts": 14, "gears": 9, "springs": 22}, ()),
        ("VID-03", "video", "webm", {"steps": "array"}, {"steps": ["calibrate", "lock", "transmit"]}, ()),
        ("VID-04", "video", "webm", {"model": "string", "serial": "string", "warranty": "string"}, {"model": "NOVA-2", "serial": "SN-8841", "warranty": None}, ("warranty",)),
    ]
    for case_id, variant, suffix, properties, expected, nullable in media:
        cases.append(_case(case_id, variant, {"json_schema": _schema(properties, nullable=nullable)}, {"expected": expected}, ["gemini"], fixture=f"fixtures/{case_id}.{suffix}"))
    return cases


def _font(size: int):
    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _card(path: Path, heading: str, lines: list[str]):
    image = Image.new("RGB", (960, 540), "#f7f3ea")
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 28, 932, 512), outline="#253247", width=5)
    draw.text((64, 64), heading, fill="#16324f", font=_font(46))
    y = 150
    for line in lines:
        draw.text((64, y), line, fill="#1e293b", font=_font(34))
        y += 70
    image.save(path, "PNG", optimize=True)


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_pdf(path: Path, pages: list[list[str]]):
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    page_ids = []
    content_ids = []
    next_id = 4
    for _ in pages:
        page_ids.append(next_id)
        content_ids.append(next_id + 1)
        next_id += 2
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_id, content_id, lines in zip(page_ids, content_ids, pages):
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode())
        commands = ["BT", "/F1 18 Tf", "72 720 Td"]
        for index, line in enumerate(lines):
            if index:
                commands.append("0 -34 Td")
            commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(output)


def _speech(path: Path, phrase: str, *, executable: Path, data_root: Path):
    subprocess.run(
        [
            str(executable), f"--path={data_root}", "-v", "en-us", "-s", "145",
            "-w", str(path), phrase,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def _video(
    path: Path,
    heading: str,
    lines: list[str],
    narration: Path,
    *,
    executable: Path,
):
    with tempfile.TemporaryDirectory(prefix="smartfetch-benchmark-video-") as directory:
        card = Path(directory) / "card.png"
        _card(card, heading, lines)
        subprocess.run(
            [
                str(executable), "-hide_banner", "-loglevel", "error", "-y",
                "-loop", "1", "-framerate", "10", "-i", str(card),
                "-i", str(narration), "-c:v", "libvpx-vp9", "-deadline", "good",
                "-cpu-used", "6", "-crf", "42", "-b:v", "0", "-pix_fmt", "yuv420p",
                "-c:a", "libopus", "-b:a", "32k", "-shortest", str(path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )


def _validate_assets(*, ffprobe: Path) -> None:
    from smartfetch.media import inspect_image, inspect_pdf, inspect_timed_media

    async def portable_probe(path: Path) -> dict:
        completed = subprocess.run(
            [
                str(ffprobe), "-v", "error", "-show_entries",
                "format=format_name,duration:stream=codec_type,codec_name,duration",
                "-of", "json", str(path),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0 or completed.stderr or len(completed.stdout) > 65_536:
            raise RuntimeError("portable ffprobe validation failed")
        value = json.loads(completed.stdout.decode("utf-8"))
        if type(value) is not dict:
            raise RuntimeError("portable ffprobe returned invalid metadata")
        return value

    for path in sorted(FIXTURES.glob("IMG-*.png")):
        inspect_image(path, "image/png")
    for path in sorted(FIXTURES.glob("PDF-*.pdf")):
        inspect_pdf(path, "application/pdf")
    for path in sorted(FIXTURES.glob("AUD-*.wav")):
        asyncio.run(inspect_timed_media(path, "audio", "audio/wav", runner=portable_probe))
    for path in sorted(FIXTURES.glob("VID-*.webm")):
        asyncio.run(inspect_timed_media(path, "video", "video/webm", runner=portable_probe))


def generate(*, espeak: Path, espeak_data: Path, ffmpeg: Path, ffprobe: Path):
    FIXTURES.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    (BENCHMARK / "cases.json").write_text(json.dumps({
        "manifest_version": 1,
        "models": MODELS,
        "cases": cases,
    }, indent=2, ensure_ascii=False) + "\n", "utf-8")

    cards = {
        "IMG-01": ("Order Card", ["Order ID: SF-1042", "Total USD: 42.75"]),
        "IMG-02": ("Product Label", ["Model: ORBIT-7", "Batch: B-204", "Mass: 350 g"]),
        "IMG-03": ("Event Poster", ["Aurora Demo", "Date: 2026-10-14", "Venue: Harbor Hall"]),
        "IMG-04": ("Regional Table", ["North: 18", "South: 27", "West: 31"]),
    }
    for case_id, (heading, lines) in cards.items():
        target = FIXTURES / f"{case_id}.png"
        if not target.exists():
            _card(target, heading, lines)
    pdfs = {
        "PDF-01": [["Invoice INV-7301", "Customer: Northwind Lab", "Total USD: 128.40"]],
        "PDF-02": [["Project Cedar Report", "Milestone: Beta"], ["Completion percent: 72"]],
        "PDF-03": [["Policy POL-88", "Effective date: 2026-11-01"], ["Limit USD: 5000"]],
    }
    for case_id, pages in pdfs.items():
        target = FIXTURES / f"{case_id}.pdf"
        if not target.exists():
            _text_pdf(target, pages)
    shipment = FIXTURES / "PDF-04.pdf"
    if not shipment.exists():
        image = Image.new("RGB", (1240, 1754), "white")
        draw = ImageDraw.Draw(image)
        draw.text((100, 140), "Shipment SHIP-204", fill="black", font=_font(54))
        draw.text((100, 260), "Crates: 6", fill="black", font=_font(44))
        draw.text((100, 360), "Destination: Baltimore", fill="black", font=_font(44))
        image.save(shipment, "PDF", resolution=150.0)

    audio = {
        "AUD-01": "Order SF-550 ships on October 12, 2026.",
        "AUD-02": "The red team scored 17 points and the blue team scored 23 points.",
        "AUD-03": "Meeting Alpha begins at 2:30 PM in Room Cedar.",
        "AUD-04": "Ticket TK-90 was opened. No assignee was provided.",
    }
    for case_id, phrase in audio.items():
        target = FIXTURES / f"{case_id}.wav"
        if not target.exists():
            _speech(target, phrase, executable=espeak, data_root=espeak_data)

    video = {
        "VID-01": ("Launch", ["Code: LANTERN-4", "Date: 2026-12-03"], "Launch code LANTERN-4 is scheduled for December 3, 2026."),
        "VID-02": ("Inventory", ["Bolts: 14", "Gears: 9", "Springs: 22"], "Inventory contains 14 bolts, 9 gears, and 22 springs."),
        "VID-03": ("Procedure", ["1. calibrate", "2. lock", "3. transmit"], "Step one calibrate. Step two lock. Step three transmit."),
        "VID-04": ("Product Demo", ["Model: NOVA-2", "Serial: SN-8841", "Warranty: not stated"], "Model NOVA-2, serial SN-8841. No warranty is stated."),
    }
    for case_id, (heading, lines, phrase) in video.items():
        narration = FIXTURES / f".{case_id}-narration.wav"
        try:
            target = FIXTURES / f"{case_id}.webm"
            if target.exists():
                continue
            _speech(narration, phrase, executable=espeak, data_root=espeak_data)
            _video(target, heading, lines, narration, executable=ffmpeg)
        finally:
            narration.unlink(missing_ok=True)

    _validate_assets(ffprobe=ffprobe)

    fixture_entries = []
    mime_types = {"image": "image/png", "pdf": "application/pdf", "audio": "audio/wav", "video": "video/webm"}
    for case in cases[16:]:
        path = BENCHMARK / case["fixture"]
        payload = path.read_bytes()
        fixture_entries.append({
            "fixture_id": case["case_id"],
            "path": case["fixture"],
            "byte_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mime_type": mime_types[case["variant"]],
            "expected_semantic_content": case["oracle"]["expected"],
        })
    (BENCHMARK / "fixtures.json").write_text(json.dumps({
        "manifest_version": 1,
        "fixtures": fixture_entries,
    }, indent=2) + "\n", "utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--espeak", type=Path, required=True)
    parser.add_argument("--espeak-data", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    arguments = parser.parse_args()
    for tool in (arguments.espeak, arguments.ffmpeg, arguments.ffprobe):
        if not tool.is_file():
            parser.error("all portable tool paths must name files")
    if not arguments.espeak_data.is_dir():
        parser.error("the portable eSpeak data root must be a directory")
    generate(
        espeak=arguments.espeak,
        espeak_data=arguments.espeak_data,
        ffmpeg=arguments.ffmpeg,
        ffprobe=arguments.ffprobe,
    )
