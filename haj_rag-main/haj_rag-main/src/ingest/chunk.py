"""
Deterministic chunker: data/processed/pages.jsonl -> data/processed/chunks.jsonl

Turns the OCR'd Hajj book into retrieval-ready chunks for a Q&A assistant.
The book is mostly question->answer fatwas plus manasik guidance and a du'a
appendix, so the chunking rules are content-aware:

  * fatwa   - one self-contained chunk = topic heading + question + answer.
              The answer is the body of the following "## الجواب" section, or,
              when OCR merged it, the text after an inline "الجواب" marker.
  * guidance- heading-delimited manasik sections (parent "#" context kept).
  * dua     - short dhikr sections under "# ملحق الأدعية والأذكار".

Long units are split on paragraph boundaries with token overlap, and every
part is prefixed with its governing heading (+ question for fatwas) so each
chunk is self-contained. Footnotes are lifted into a citations[] list and kept
out of the normalized embedding text.

This is plain preprocessing (no LangGraph/LangChain) — run once, offline.

Run:  python -m src.ingest.chunk     (from the repo root)
"""

from __future__ import annotations

import json
import random
import re
import sys
from pathlib import Path

import tiktoken

from src.ingest.normalize import FOOTNOTE_RE, normalize_arabic

# --- paths -------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[2]
PROC_DIR = ROOT / "data" / "processed"
PAGES_JSONL_PATH = PROC_DIR / "pages.jsonl"
CHUNKS_JSONL_PATH = PROC_DIR / "chunks.jsonl"

# --- tunables ----------------------------------------------------------------
MAX_CHUNK_TOKENS = 900      # split units larger than this
OVERLAP_TOKENS = 120        # trailing context carried into the next part
TOKEN_ENCODING = "cl100k_base"  # matches OpenAI embedding tokenization
# Content ends with the du'a appendix on p123. Pages 124-130 are the table of
# contents (المحتويات) and back cover, which OCR appended with no heading so
# they fold into the last du'a section. A TOC is a retrieval "magnet" — it lists
# every section title, so it matches almost any query — with zero answer value;
# drop everything past this page.
CONTENT_LAST_PAGE = 123

# Question line opener (tested against *normalized* text, so no tashkeel here).
QUESTION_RE = re.compile(
    r"^(ما\s+حكم|ما\s+الحكم|ما\s+حكمه|هل\s+يجوز|هل\s+يصح|هل\s+هذا|هل\s+يجب|هل\s+تجب)"
)
# Pure number / bracketed-number headings are OCR'd margin marks -> dropped.
JUNK_HEADING_RE = re.compile(r"^\[?\s*[\d٠-٩۰-۹]+\s*\]?$")
# Standalone "الجواب" answer marker (heading text or inline token).
JAWAB = normalize_arabic("الجواب")
INLINE_JAWAB_RE = re.compile(r"الجواب")
# A section is a du'a if this normalized title sits in its ancestor path.
DUA_MARKER = normalize_arabic("ملحق الأدعية والأذكار")

_enc = None


def log(msg: str) -> None:
    print(f"[chunk] {msg}", flush=True)


# Console may be cp1252 on Windows; the logs print Arabic + a ✓ glyph.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def enc():
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding(TOKEN_ENCODING)
    return _enc


def n_tokens(text: str) -> int:
    return len(enc().encode(text))


# --- load: pages.jsonl -> ordered block stream -------------------------------
def load_blocks() -> list[dict]:
    """Flatten pages into an ordered list of paragraph/heading blocks.

    Each block: {text, page, is_heading, level, title}. Headings are detected by
    leading '#'; level is the count of '#'; title is the heading text without
    the markdown prefix.
    """
    if not PAGES_JSONL_PATH.exists():
        raise SystemExit(f"missing input: {PAGES_JSONL_PATH} (run azure_ocr first)")

    blocks: list[dict] = []
    with PAGES_JSONL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            page = rec["page_no"]
            if page > CONTENT_LAST_PAGE:
                continue  # table of contents / back cover — not retrievable content
            for raw in rec["markdown"].split("\n\n"):
                text = raw.strip()
                if not text:
                    continue
                m = re.match(r"^(#+)\s*(.*)$", text)
                if m:
                    blocks.append(
                        {
                            "text": text,
                            "page": page,
                            "is_heading": True,
                            "level": len(m.group(1)),
                            "title": m.group(2).strip(),
                        }
                    )
                else:
                    blocks.append(
                        {"text": text, "page": page, "is_heading": False, "level": 0, "title": None}
                    )
    return blocks


# --- group into flat sections (heading + body-until-next-heading) ------------
def build_sections(blocks: list[dict]) -> list[dict]:
    """One section per heading: {title, level, heading_block, body:[blocks]}.

    Junk-number headings are dropped and their body folded into the previous
    section. Blocks before the first heading become a leading title-less section.
    """
    sections: list[dict] = []
    cur = {"title": None, "level": 0, "heading_block": None, "body": []}

    for b in blocks:
        if b["is_heading"]:
            if JUNK_HEADING_RE.match(b["title"]):
                # Drop the stray-number heading; keep following text in place.
                continue
            if cur["heading_block"] is not None or cur["body"]:
                sections.append(cur)
            cur = {"title": b["title"], "level": b["level"], "heading_block": b, "body": []}
        else:
            cur["body"].append(b)
    if cur["heading_block"] is not None or cur["body"]:
        sections.append(cur)
    return sections


def section_pages(sec: dict, *extra_blocks: dict) -> tuple[int, int]:
    pages = [b["page"] for b in sec["body"]]
    if sec["heading_block"]:
        pages.append(sec["heading_block"]["page"])
    for b in extra_blocks:
        pages.append(b["page"])
    return (min(pages), max(pages)) if pages else (0, 0)


# --- fatwa detection ---------------------------------------------------------
def norm_title(sec: dict) -> str:
    return normalize_arabic(sec["title"]) if sec["title"] else ""


def is_jawab_heading(sec: dict) -> bool:
    return sec["heading_block"] is not None and norm_title(sec) == JAWAB


def find_question(body: list[dict]) -> dict | None:
    """First body block whose normalized text opens with a question pattern."""
    for b in body:
        if QUESTION_RE.match(normalize_arabic(b["text"])):
            return b
    return None


def inline_jawab_index(body: list[dict]) -> int | None:
    """Index of the body block that carries an inline 'الجواب' marker (the OCR
    case where the answer label was glued onto the question line)."""
    for i, b in enumerate(body):
        if INLINE_JAWAB_RE.search(b["text"]) and find_question(body[: i + 1]):
            return i
    return None


# --- citations / normalized text ---------------------------------------------
def extract_citations(display_text: str) -> list[str]:
    return [
        ln.strip()
        for ln in display_text.splitlines()
        if FOOTNOTE_RE.match(ln.strip())
    ]


def to_norm(display_text: str) -> str:
    """Normalized embedding text: drop footnote lines and heading markers."""
    kept = []
    for ln in display_text.splitlines():
        s = ln.strip()
        if not s or FOOTNOTE_RE.match(s):
            continue
        kept.append(re.sub(r"^#+\s*", "", s))
    return normalize_arabic(" ".join(kept))


# --- long-unit splitting -----------------------------------------------------
def split_into_parts(prefix_blocks: list[str], body_blocks: list[str]) -> list[str]:
    """Split a unit into <= MAX_CHUNK_TOKENS parts on paragraph boundaries.

    `prefix_blocks` (heading, question, الجواب label) lead every part. Each new
    part also carries OVERLAP_TOKENS of trailing context from the previous part
    so a paragraph spanning a boundary stays retrievable from either side.
    Returns the full display text of each part.
    """
    prefix = "\n\n".join(p for p in prefix_blocks if p)
    whole = prefix + ("\n\n" if prefix and body_blocks else "") + "\n\n".join(body_blocks)
    if n_tokens(whole) <= MAX_CHUNK_TOKENS:
        return [whole]

    parts: list[str] = []
    cur: list[str] = []
    overlap = ""

    def assemble(body_list: list[str], lead_overlap: str) -> str:
        pieces = [prefix] if prefix else []
        if lead_overlap:
            pieces.append(lead_overlap)
        pieces.extend(body_list)
        return "\n\n".join(p for p in pieces if p)

    for para in body_blocks:
        trial = assemble(cur + [para], overlap)
        if cur and n_tokens(trial) > MAX_CHUNK_TOKENS:
            parts.append(assemble(cur, overlap))
            tail_ids = enc().encode("\n\n".join(cur))[-OVERLAP_TOKENS:]
            overlap = enc().decode(tail_ids)
            # The token slice can land mid-word, so the decoded overlap may begin
            # with a dangling fragment (e.g. "ِ عَلَى"). Snap to the first
            # whitespace so the carried-over context starts on a word boundary.
            head_tail = re.split(r"\s+", overlap, maxsplit=1)
            if len(head_tail) == 2:
                overlap = head_tail[1]
            cur = [para]
        else:
            cur.append(para)
    if cur:
        parts.append(assemble(cur, overlap))
    return parts


# --- unit -> chunk records ---------------------------------------------------
def emit_unit(
    section_type: str,
    heading_path: list[str],
    topic_title: str,
    question: str,
    prefix_blocks: list[str],
    body_blocks: list[str],
    page_start: int,
    page_end: int,
    counter: dict[str, int],
) -> list[dict]:
    parts = split_into_parts(prefix_blocks, body_blocks)
    part_total = len(parts)
    counter[section_type] = counter.get(section_type, 0) + 1
    base = f"{section_type}_{counter[section_type]:04d}"

    records = []
    for i, display in enumerate(parts, start=1):
        chunk_id = base if part_total == 1 else f"{base}_p{i}"
        records.append(
            {
                "chunk_id": chunk_id,
                "section_type": section_type,
                "heading_path": heading_path,
                "topic_title": topic_title,
                "question": question,
                "text": display,
                "text_norm": to_norm(display),
                "citations": extract_citations(display),
                "page_start": page_start,
                "page_end": page_end,
                "part_index": i,
                "part_total": part_total,
            }
        )
    return records


# --- main assembly -----------------------------------------------------------
def build_chunks(sections: list[dict]) -> list[dict]:
    # First pass: flag fatwa-topic sections (followed by الجواب, or inline).
    for i, sec in enumerate(sections):
        sec["is_fatwa_topic"] = False
        if sec["heading_block"] is None or is_jawab_heading(sec):
            continue
        nxt = sections[i + 1] if i + 1 < len(sections) else None
        if (nxt and is_jawab_heading(nxt)) or inline_jawab_index(sec["body"]) is not None:
            sec["is_fatwa_topic"] = True

    chunks: list[dict] = []
    counter: dict[str, int] = {}
    ancestors: list[tuple[int, str]] = []  # (level, title) of non-fatwa headings
    i = 0
    while i < len(sections):
        sec = sections[i]

        if sec["heading_block"] is None:
            i += 1  # leading front matter (title page) — skip
            continue

        anc_titles = [t for _, t in ancestors]

        if sec["is_fatwa_topic"]:
            # Fatwa topics are conceptually one level below the current section,
            # so they neither pop nor join the ancestor stack — they just hang
            # off it (e.g. ["فتاوى مختارة", topic]).
            heading_path = anc_titles + [sec["title"]]
            q_block = find_question(sec["body"])
            nxt = sections[i + 1] if i + 1 < len(sections) else None

            if nxt and is_jawab_heading(nxt):
                topic_body = sec["body"]
                answer_blocks = nxt["body"]
                consumed = 2
                page_start, page_end = section_pages(sec, *nxt["body"], nxt["heading_block"])
            else:
                # Inline-الجواب case: split the question block at the marker.
                j = inline_jawab_index(sec["body"])
                before, _, after = sec["body"][j]["text"].partition("الجواب")
                topic_body = sec["body"][:j] + (
                    [{"text": before.strip(), "page": sec["body"][j]["page"]}]
                    if before.strip()
                    else []
                )
                answer_blocks = (
                    [{"text": after.strip(), "page": sec["body"][j]["page"]}]
                    if after.strip()
                    else []
                ) + sec["body"][j + 1 :]
                consumed = 1
                page_start, page_end = section_pages(sec)

            question = (q_block["text"].strip() if q_block else
                        (topic_body[0]["text"].strip() if topic_body else ""))
            question = question.partition("الجواب")[0].strip() or question

            prefix_blocks = [sec["heading_block"]["text"], question, "## الجواب"]
            chunks.extend(
                emit_unit(
                    "fatwa", heading_path, sec["title"], question,
                    prefix_blocks, [b["text"] for b in answer_blocks],
                    page_start, page_end, counter,
                )
            )
            i += consumed
            continue

        if is_jawab_heading(sec):
            i += 1  # orphan answer with no preceding topic — already consumed
            continue

        # Guidance / du'a section: normal ancestor stack behaviour.
        while ancestors and ancestors[-1][0] >= sec["level"]:
            ancestors.pop()
        anc_titles = [t for _, t in ancestors]
        heading_path = anc_titles + [sec["title"]]
        section_type = "dua" if any(DUA_MARKER in normalize_arabic(t) for t in heading_path) else "guidance"

        prefix_blocks = [sec["heading_block"]["text"]]
        page_start, page_end = section_pages(sec)
        chunks.extend(
            emit_unit(
                section_type, heading_path, sec["title"], "",
                prefix_blocks, [b["text"] for b in sec["body"]],
                page_start, page_end, counter,
            )
        )
        ancestors.append((sec["level"], sec["title"]))
        i += 1

    return chunks


# --- output + verification ---------------------------------------------------
def write_chunks(chunks: list[dict]) -> None:
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    with CHUNKS_JSONL_PATH.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    log(f"wrote {CHUNKS_JSONL_PATH} ({len(chunks)} chunks)")


def verify(chunks: list[dict]) -> None:
    fatwas = [c for c in chunks if c["section_type"] == "fatwa"]
    guidance = [c for c in chunks if c["section_type"] == "guidance"]
    dua = [c for c in chunks if c["section_type"] == "dua"]

    # Automated assertions.
    for c in chunks:
        assert not JUNK_HEADING_RE.match((c["topic_title"] or "").strip()), \
            f"bare-number title leaked: {c['chunk_id']}"
        assert normalize_arabic(c["topic_title"] or "") != JAWAB, \
            f"standalone الجواب chunk: {c['chunk_id']}"
        for cit in c["citations"]:
            assert normalize_arabic(cit) not in c["text_norm"], \
                f"footnote leaked into text_norm: {c['chunk_id']}"
        if c["part_total"] > 1:
            head = c["text"].splitlines()[0].strip()
            assert head.startswith("#"), \
                f"multi-part chunk missing governing heading: {c['chunk_id']}"
    for c in fatwas:
        assert c["question"].strip(), f"fatwa without question: {c['chunk_id']}"
        assert normalize_arabic(c["topic_title"]) != JAWAB

    log("assertions passed ✓")
    log(f"chunks: {len(chunks)} | fatwa: {len(fatwas)} | guidance: {len(guidance)} | dua: {len(dua)}")
    multipart = sum(1 for c in chunks if c["part_total"] > 1)
    log(f"multi-part chunks: {multipart}")

    rng = random.Random(7)

    def show(label: str, pool: list[dict]) -> None:
        log(f"--- {label} samples ---")
        for c in rng.sample(pool, min(5, len(pool))):
            head = " > ".join(c["heading_path"])
            log(f"[{c['chunk_id']}] p{c['page_start']}-{c['page_end']}  {head}")
            if c["question"]:
                log(f"    Q: {c['question'][:90]}")
            preview = c["text_norm"][:120].replace("\n", " ")
            log(f"    …{preview}")

    if fatwas:
        show("fatwa", fatwas)
    if guidance:
        show("guidance", guidance)


def main() -> int:
    blocks = load_blocks()
    log(f"loaded {len(blocks)} blocks from {PAGES_JSONL_PATH.name}")
    sections = build_sections(blocks)
    log(f"built {len(sections)} flat sections")
    chunks = build_chunks(sections)
    write_chunks(chunks)
    verify(chunks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
