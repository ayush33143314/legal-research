"""
Full-text indexer: extracts every PDF from every tar and indexes into Elasticsearch.
Runs in parallel using a process pool. Adds 'full_text' field to existing ES documents.
Resume-safe: skips already-indexed documents.
"""

import json
import glob
import io
import tarfile
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pymupdf
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from tqdm import tqdm

BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR  = BASE_DIR / "data"
ES_HOST   = "http://localhost:9200"
INDEX     = "judgments"
WORKERS   = 4


def extract_pdf_from_tar(tar_path: str, filename: str) -> str | None:
    """Extract a single PDF from a tar and return its text."""
    try:
        with tarfile.open(tar_path, "r") as tar:
            try:
                member = tar.getmember(filename)
            except KeyError:
                members = [m for m in tar.getmembers() if m.name.endswith(filename)]
                if not members:
                    return None
                member = members[0]

            pdf_bytes = tar.extractfile(member).read()

        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        pages = [page.get_text() for page in doc]
        doc.close()
        return "\n".join(pages).strip()
    except Exception:
        return None


def process_year(year: int) -> list[dict]:
    """Process all PDFs for a given year. Returns list of ES update actions."""
    index_path = DATA_DIR / f"year={year}" / "english" / "english.index.json"
    tar_path   = DATA_DIR / f"year={year}" / "english" / "english.tar"

    if not index_path.exists() or not tar_path.exists():
        return []

    with open(index_path) as f:
        data = json.load(f)

    actions = []
    for part in data.get("parts", []):
        for filename in part.get("files", []):
            # path = filename without _EN.pdf suffix
            path = filename.replace("_EN.pdf", "")
            text = extract_pdf_from_tar(str(tar_path), filename)
            if text:
                actions.append({
                    "_op_type": "update",
                    "_index": INDEX,
                    "_id": path,
                    "doc": {"full_text": text},
                    "doc_as_upsert": False,  # only update existing docs
                })
    return actions


def already_indexed_count(es: Elasticsearch) -> int:
    """Count docs that already have full_text indexed."""
    res = es.count(index=INDEX, body={"query": {"exists": {"field": "full_text"}}})
    return res["count"]


def add_fulltext_mapping(es: Elasticsearch):
    """Add full_text field to existing index mapping."""
    es.indices.put_mapping(
        index=INDEX,
        body={
            "properties": {
                "full_text": {
                    "type": "text",
                    "analyzer": "legal_analyzer"
                }
            }
        }
    )
    print("Added 'full_text' field to ES mapping")


def main():
    es = Elasticsearch(ES_HOST)
    if not es.ping():
        print("Cannot connect to Elasticsearch")
        sys.exit(1)

    add_fulltext_mapping(es)

    already = already_indexed_count(es)
    if already > 0:
        print(f"Resuming: {already:,} documents already have full_text indexed")

    years = list(range(1950, 2027))
    total_indexed = 0
    total_failed  = 0

    print(f"Processing {len(years)} years with {WORKERS} workers...\n")

    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(process_year, y): y for y in years}

        with tqdm(total=len(years), desc="Years processed") as pbar:
            for future in as_completed(futures):
                year = futures[future]
                try:
                    actions = future.result()
                    if actions:
                        ok, errors = bulk(es, actions, raise_on_error=False)
                        total_indexed += ok
                        total_failed  += len(errors) if errors else 0
                except Exception as e:
                    tqdm.write(f"Year {year} failed: {e}")
                finally:
                    pbar.update(1)
                    pbar.set_postfix(indexed=total_indexed, failed=total_failed)

    es.indices.refresh(index=INDEX)
    final_count = already_indexed_count(es)
    print(f"\nDone. Total with full_text: {final_count:,} | Failed: {total_failed:,}")


if __name__ == "__main__":
    main()
