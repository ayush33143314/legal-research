"""
Full-text indexer for High Court PDFs (Bombay + Karnataka).
Walks all data.tar files under hc_data/tar/year=*/court=*/bench=*/
Extracts each PDF and updates the corresponding ES document with full_text.
Resume-safe: skips documents that already have full_text.
"""

import json
import glob
import sys
import tarfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pymupdf
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from tqdm import tqdm

BASE_DIR  = Path(__file__).parent.parent.parent
HC_TAR_DIR = BASE_DIR / "hc_data" / "tar"
ES_HOST   = "http://localhost:9200"
INDEX     = "judgments"
WORKERS   = 4

TARGET_COURTS = {"27_1", "29_3"}


def extract_pdf_from_tar(tar_path: str, filename: str) -> str | None:
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


def cnr_from_filename(filename: str) -> str:
    """
    Derive CNR from PDF filename.
    e.g. 'HCBM030088082020_1_2023-02-24.pdf' → 'HCBM030088082020'
    The CNR is the first underscore-delimited segment.
    """
    return filename.split("_")[0]


def process_tar(tar_path_str: str) -> list[dict]:
    """Process all PDFs in one tar file. Returns ES update actions."""
    tar_path = Path(tar_path_str)
    index_path = tar_path.parent / "data.index.json"

    if not index_path.exists():
        return []

    with open(index_path) as f:
        data = json.load(f)

    actions = []
    for part in data.get("parts", []):
        part_tar = tar_path.parent / part["name"]
        if not part_tar.exists():
            continue
        for filename in part.get("files", []):
            if not filename.endswith(".pdf"):
                continue
            cnr = cnr_from_filename(filename)
            if not cnr:
                continue
            text = extract_pdf_from_tar(str(part_tar), filename)
            if text:
                actions.append({
                    "_op_type":    "update",
                    "_index":      INDEX,
                    "_id":         cnr,
                    "doc":         {"full_text": text},
                    "doc_as_upsert": False,
                })
    return actions


def already_indexed_hc_count(es: Elasticsearch) -> int:
    res = es.count(index=INDEX, body={
        "query": {
            "bool": {
                "must":   [{"exists": {"field": "full_text"}}],
                "filter": [{"term":   {"court_type": "hc"}}]
            }
        }
    })
    return res["count"]


def main():
    es = Elasticsearch(ES_HOST)
    if not es.ping():
        print("Cannot connect to Elasticsearch")
        sys.exit(1)

    already = already_indexed_hc_count(es)
    if already > 0:
        print(f"Resuming: {already:,} HC documents already have full_text")

    # Find all data.tar files for target courts
    pattern = str(HC_TAR_DIR / "year=*" / "court=*" / "bench=*" / "data.tar")
    all_tars = sorted(glob.glob(pattern))
    tars = [t for t in all_tars if any(f"court={c}/" in t for c in TARGET_COURTS)]

    if not tars:
        print(f"No tar files found under {HC_TAR_DIR}")
        print("Run download_hc.sh first.")
        sys.exit(1)

    print(f"Found {len(tars)} tar files to process with {WORKERS} workers\n")

    total_indexed = 0
    total_failed  = 0

    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(process_tar, t): t for t in tars}

        with tqdm(total=len(tars), desc="Tars processed") as pbar:
            for future in as_completed(futures):
                tar_path = futures[future]
                try:
                    actions = future.result()
                    if actions:
                        ok, errors = bulk(es, actions, raise_on_error=False)
                        total_indexed += ok
                        total_failed  += len(errors) if errors else 0
                except Exception as e:
                    tqdm.write(f"Tar {Path(tar_path).parent.name} failed: {e}")
                finally:
                    pbar.update(1)
                    pbar.set_postfix(indexed=total_indexed, failed=total_failed)

    es.indices.refresh(index=INDEX)
    final = already_indexed_hc_count(es)
    print(f"\nDone. HC docs with full_text: {final:,} | Failed this run: {total_failed:,}")


if __name__ == "__main__":
    main()
