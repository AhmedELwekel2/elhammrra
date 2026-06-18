"""
Azure Document Intelligence OCR -> clean Markdown for the Hajj book.

Converts data/raw/hajj-book.pdf (Arabic, RTL) into reading-order-correct
Markdown using the *prebuilt-layout* model in markdown mode. Layout (unlike
prebuilt-read) emits markdown plus heading roles, which we map to # / ##.

Outputs:
  - data/processed/azure_layout.json  full raw response (local cache; see guard)
  - data/processed/book.md            full concatenated markdown
  - data/processed/pages.jsonl        one JSON record per page (chunker contract)

Cache guard: if azure_layout.json already exists it is loaded and the (billed)
API call is skipped entirely.

Run:  python -m src.ingest.azure_ocr     (from the repo root)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# --- paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = ROOT / "data" / "raw" / "hajj-book.pdf"
PROC_DIR = ROOT / "data" / "processed"
CACHE_PATH = PROC_DIR / "azure_layout.json"
BOOK_MD_PATH = PROC_DIR / "book.md"
PAGES_JSONL_PATH = PROC_DIR / "pages.jsonl"

API_VERSION = "2024-11-30"
MODEL_ID = "prebuilt-layout"

# Roles that are page chrome / stray numbers -> dropped from the clean output.
DROP_ROLES = {"pageHeader", "pageFooter", "pageNumber"}
# Heading roles -> markdown prefixes.
HEADING_PREFIX = {"title": "# ", "sectionHeading": "## "}


def log(msg: str) -> None:
    print(f"[azure_ocr] {msg}", flush=True)


# --- Azure call (only when no cache) -----------------------------------------
def analyze_pdf(pdf_bytes: bytes) -> dict:
    """Call prebuilt-layout in markdown mode, with retry on throttling/503."""
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.core.credentials import AzureKeyCredential
    from azure.core.exceptions import HttpResponseError, ServiceResponseError

    endpoint = os.getenv("AZURE_AI_ENDPOINT")
    key = os.getenv("AZURE_AI_KEY")
    # Fail fast with a clear message; never print the secret value itself.
    missing = [n for n, v in (("AZURE_AI_ENDPOINT", endpoint), ("AZURE_AI_KEY", key)) if not v]
    if missing:
        raise SystemExit(
            f"Missing required env var(s): {', '.join(missing)}. "
            f"Set them in a .env file (see .env.example)."
        )

    client = DocumentIntelligenceClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key),
        api_version=API_VERSION,
    )

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            log(f"submitting {MODEL_ID} (markdown, api {API_VERSION}); attempt {attempt}/{max_attempts}")
            t0 = time.monotonic()
            poller = client.begin_analyze_document(
                MODEL_ID,
                body=pdf_bytes,
                output_content_format="markdown",
            )
            result = poller.result()  # poll long-running op to completion
            elapsed = time.monotonic() - t0
            log(f"analysis complete in {elapsed:.1f}s")
            return result.as_dict()
        except (HttpResponseError, ServiceResponseError) as exc:
            status = getattr(exc, "status_code", None)
            throttled = status in (429, 503)
            if throttled and attempt < max_attempts:
                backoff = 2 ** attempt  # 2s, 4s
                log(f"throttled/unavailable (status={status}); retrying in {backoff}s")
                time.sleep(backoff)
                continue
            raise


def get_layout_dict(pdf_bytes: bytes) -> dict:
    """Cache guard: load azure_layout.json if present, else call Azure and save it."""
    if CACHE_PATH.exists():
        log(f"cache hit: loading {CACHE_PATH.name} (SKIPPING Azure call — no re-bill)")
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)

    log("cache miss: calling Azure Document Intelligence")
    layout = analyze_pdf(pdf_bytes)
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False)
    log(f"saved raw response -> {CACHE_PATH}")
    return layout


# --- transform: layout dict -> per-page markdown -----------------------------
def _page_of(paragraph: dict) -> int | None:
    regions = paragraph.get("boundingRegions") or paragraph.get("bounding_regions") or []
    if not regions:
        return None
    reg = regions[0]
    return reg.get("pageNumber") or reg.get("page_number")


def build_pages(layout: dict) -> list[dict]:
    """Return [{page_no, markdown, roles}] for every page in the document."""
    num_pages = len(layout.get("pages") or [])
    if not num_pages:
        # Fall back to max page referenced by any paragraph.
        num_pages = max(
            (_page_of(p) or 0 for p in (layout.get("paragraphs") or [])), default=0
        )

    # Accumulate per-page content lines and roles in document order.
    lines_by_page: dict[int, list[str]] = {n: [] for n in range(1, num_pages + 1)}
    roles_by_page: dict[int, list[str]] = {n: [] for n in range(1, num_pages + 1)}

    for para in layout.get("paragraphs") or []:
        role = para.get("role")
        if role in DROP_ROLES:
            continue
        page_no = _page_of(para)
        if page_no is None:
            continue
        content = (para.get("content") or "").strip()
        if not content:
            continue

        lines_by_page.setdefault(page_no, [])
        roles_by_page.setdefault(page_no, [])

        prefix = HEADING_PREFIX.get(role, "")
        lines_by_page[page_no].append(f"{prefix}{content}" if prefix else content)
        if role:
            roles_by_page[page_no].append(role)

    pages = []
    for page_no in sorted(lines_by_page):
        markdown = "\n\n".join(lines_by_page[page_no]).strip()
        pages.append(
            {
                "page_no": page_no,
                "markdown": markdown,
                "roles": roles_by_page.get(page_no, []),
            }
        )
    return pages


# --- outputs -----------------------------------------------------------------
def write_outputs(pages: list[dict]) -> None:
    PROC_DIR.mkdir(parents=True, exist_ok=True)

    parts = []
    for p in pages:
        if p["markdown"]:
            parts.append(p["markdown"])
    book_md = "\n\n".join(parts) + "\n"
    BOOK_MD_PATH.write_text(book_md, encoding="utf-8")
    log(f"wrote {BOOK_MD_PATH} ({len(book_md):,} chars)")

    with PAGES_JSONL_PATH.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    log(f"wrote {PAGES_JSONL_PATH} ({len(pages)} records)")


def main() -> int:
    load_dotenv(ROOT / ".env")

    if not PDF_PATH.exists():
        raise SystemExit(f"PDF not found: {PDF_PATH}")

    pdf_bytes = PDF_PATH.read_bytes() if not CACHE_PATH.exists() else b""
    layout = get_layout_dict(pdf_bytes)

    pages = build_pages(layout)
    write_outputs(pages)

    # --- summary / sanity logging --------------------------------------------
    heading_count = sum(
        1 for p in pages for r in p["roles"] if r in ("title", "sectionHeading")
    )
    empty_pages = [p["page_no"] for p in pages if not p["markdown"]]
    log(f"pages: {len(pages)}  | headings: {heading_count}  | empty pages: {len(empty_pages)}")
    if empty_pages:
        log(f"empty page numbers: {empty_pages}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
