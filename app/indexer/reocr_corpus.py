"""
Re-OCR low-quality judgments using Tesseract 5 via PyMuPDF.

Why: The original PDFs have a poor-quality OCR text layer baked in from the
1990s-2000s. Tesseract 5 (LSTM) running on the embedded page images gives
dramatically better results: 'Coll&·titution' → 'Constitution', 'Jaw' → 'law',
'yrovisions' → 'provisions', 'MAOHU' → 'MADHU', etc.

Strategy:
  1. Score each corpus file for OCR errors (digit-in-letter, special-char substitutions).
  2. Re-OCR files above the error threshold using PyMuPDF's get_textpage_ocr().
  3. Write cleaned text back to corpus file.
  4. Optionally update Elasticsearch.
  5. Use multiprocessing for speed (~0.7s/page, 8 workers = ~3h for all pre-2000 cases).

Usage:
  # Dry run — show what would be processed
  python -m app.indexer.reocr_corpus --dry-run

  # Re-OCR only pre-2000 (worst quality, ~3h with 8 workers)
  python -m app.indexer.reocr_corpus --max-year 2000 --workers 8

  # Re-OCR everything that scores poorly
  python -m app.indexer.reocr_corpus --workers 8

  # Re-OCR a single case
  python -m app.indexer.reocr_corpus --path 1967_2_762_948

Prerequisites:
  brew install tesseract
  pip install pytesseract

Resume-safe: already-reocr'd files are tracked in data/text_corpus/.reocr_done
"""

import argparse
import json
import os
import re
import sys
import tarfile
import time
from multiprocessing import Pool, Manager
from pathlib import Path

import pymupdf

BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data"
CORPUS_DIR = DATA_DIR / "text_corpus"
DONE_FILE = CORPUS_DIR / ".reocr_done"
ES_HOST = "http://localhost:9200"
INDEX = "judgments"
TESSDATA = "/opt/homebrew/share/tessdata"

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def load_done() -> set:
    if DONE_FILE.exists():
        return set(DONE_FILE.read_text().splitlines())
    return set()


def mark_done(path: str):
    with open(DONE_FILE, "a") as f:
        f.write(path + "\n")


def ocr_error_count(text: str) -> int:
    """Count confirmed OCR errors in text. Returns error count."""
    sample = text[:8000]
    errors = 0
    errors += len(re.findall(r"[a-zA-Z][0-9][a-zA-Z]", sample))          # Co11stitution
    errors += len(re.findall(r"[a-z][>·&<][a-z]", sample))               # s>1bject
    errors += len(re.findall(r"\byrovisions\b|\bJaw\b|\bthc\b|\bllll\b", sample))  # known bad words
    return errors


def get_pdf_bytes(path: str) -> bytes | None:
    """Load PDF bytes from tar archive."""
    import json as _json
    parts = path.split("_")
    numeric = parts if parts[0].isdigit() else parts[1:]
    if not numeric or not numeric[0].isdigit():
        return None
    year = int(numeric[0])
    clean = "_".join(numeric)
    filename = f"{clean}_EN.pdf"

    index_path = DATA_DIR / f"year={year}" / "english" / "english.index.json"
    if not index_path.exists():
        return None

    with open(index_path) as f:
        data = _json.load(f)
    file_map = {fn: part["name"] for part in data.get("parts", []) for fn in part.get("files", [])}

    if filename not in file_map:
        return None

    tar_path = DATA_DIR / f"year={year}" / "english" / file_map[filename]
    if not tar_path.exists():
        return None

    try:
        with tarfile.open(tar_path) as tar:
            return tar.extractfile(tar.getmember(filename)).read()
    except Exception:
        return None


def reocr_pdf(pdf_bytes: bytes) -> str:
    """Run Tesseract 5 OCR on all pages of a PDF. Returns full text."""
    os.environ["TESSDATA_PREFIX"] = TESSDATA
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pages_text = []
    for page in doc:
        try:
            tp = page.get_textpage_ocr(language="eng", dpi=300, full=True)
            pages_text.append(page.get_text(textpage=tp))
        except Exception:
            # Fallback to existing text layer for this page
            pages_text.append(page.get_text())
    doc.close()
    return "\n".join(pages_text).strip()


def clean_text(text: str) -> str:
    """Post-clean OCR output: remove page markers, join hyphens."""
    text = re.sub(r"^[A-H~*']\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"\[\d{1,4}[A-Z]?[- ][A-H]\]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ──────────────────────────────────────────────────────────────
# Worker function (runs in subprocess)
# ──────────────────────────────────────────────────────────────

def process_one(args) -> dict:
    path, dry_run = args
    corpus_file = CORPUS_DIR / f"{path}.txt"

    if not corpus_file.exists():
        return {"path": path, "status": "missing_corpus"}

    # Read existing text
    raw = corpus_file.read_text(encoding="utf-8")
    header_end = raw.find("\n\n")
    body = raw[header_end:] if header_end != -1 else raw
    header = raw[:header_end + 2] if header_end != -1 else ""

    # Check if re-OCR needed
    errors = ocr_error_count(body)
    if errors < 3:
        return {"path": path, "status": "skip_clean", "errors": errors}

    if dry_run:
        return {"path": path, "status": "would_reocr", "errors": errors}

    # Get PDF and re-OCR
    pdf_bytes = get_pdf_bytes(path)
    if pdf_bytes is None:
        return {"path": path, "status": "no_pdf"}

    try:
        new_text = reocr_pdf(pdf_bytes)
        cleaned = clean_text(new_text)
        corpus_file.write_text(header + cleaned, encoding="utf-8")
        mark_done(path)
        return {"path": path, "status": "reocr_done", "chars": len(cleaned), "errors_was": errors}
    except Exception as e:
        return {"path": path, "status": "error", "msg": str(e)[:100]}


# ──────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Re-OCR low-quality corpus files")
    parser.add_argument("--max-year", type=int, default=None, help="Only process cases up to this year")
    parser.add_argument("--min-year", type=int, default=None, help="Only process cases from this year")
    parser.add_argument("--workers", type=int, default=4, help="Parallel workers (default 4)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--path", type=str, default=None, help="Re-OCR a single case by path")
    args = parser.parse_args()

    CORPUS_DIR.mkdir(exist_ok=True)
    done = load_done()

    if args.path:
        result = process_one((args.path, args.dry_run))
        print(result)
        return

    # Collect candidates
    all_files = sorted(CORPUS_DIR.glob("*.txt"))
    candidates = []
    for f in all_files:
        if f.name.startswith("."): continue
        year_str = f.stem.split("_")[0]
        if not year_str.isdigit(): continue
        year = int(year_str)
        if args.max_year and year > args.max_year: continue
        if args.min_year and year < args.min_year: continue
        if f.stem in done: continue
        candidates.append(f.stem)

    print(f"Candidates: {len(candidates):,} | Workers: {args.workers} | Dry run: {args.dry_run}")
    if args.dry_run:
        # Quick quality scan
        bad = sum(1 for p in candidates[:500]
                  if ocr_error_count((CORPUS_DIR / f"{p}.txt").read_text()[200:]) >= 3)
        print(f"Sample: {bad}/{min(500, len(candidates))} need re-OCR ({bad*100//min(500,len(candidates))}%)")
        avg_pages = 11  # estimated
        est_hours = len(candidates) * avg_pages * 0.7 / args.workers / 3600
        print(f"Estimated time: ~{est_hours:.1f} hours")
        return

    # Process
    t0 = time.time()
    results = {"reocr_done": 0, "skip_clean": 0, "no_pdf": 0, "error": 0}
    task_args = [(p, False) for p in candidates]

    with Pool(processes=args.workers) as pool:
        for i, result in enumerate(pool.imap_unordered(process_one, task_args, chunksize=4)):
            status = result.get("status", "error")
            results[status] = results.get(status, 0) + 1

            if (i + 1) % 50 == 0:
                elapsed = time.time() - t0
                rate = (i + 1) / elapsed
                eta = (len(candidates) - i - 1) / rate / 3600
                print(f"[{i+1}/{len(candidates)}] re-OCR'd={results['reocr_done']} "
                      f"skipped={results['skip_clean']} errors={results.get('error',0)} "
                      f"ETA={eta:.1f}h")

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed/3600:.1f}h")
    print(f"Re-OCR'd: {results['reocr_done']:,} | Skipped (clean): {results['skip_clean']:,} | "
          f"No PDF: {results.get('no_pdf',0):,} | Errors: {results.get('error',0):,}")


if __name__ == "__main__":
    main()
