"""
Dump all full_text from ES to individual .txt files for ripgrep-based exact search.

Output: data/text_corpus/{path}.txt  (e.g. 2009_6_152_159.txt)

Resume-safe: skips files that already exist.
Run: python -m app.indexer.dump_text_corpus

After completion, use ripgrep:
  rg "vital part of it had been removed" data/text_corpus/ -l
  rg -l "PWs1 and 3" data/text_corpus/ | head -5
"""

import sys
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch.helpers import scan
from tqdm import tqdm

ES_HOST = "http://localhost:9200"
INDEX = "judgments"
BASE_DIR = Path(__file__).parent.parent.parent
CORPUS_DIR = BASE_DIR / "data" / "text_corpus"


def main():
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)

    es = Elasticsearch(ES_HOST)
    if not es.ping():
        print("Cannot connect to Elasticsearch")
        sys.exit(1)

    # Count total
    total = es.count(index=INDEX, body={"query": {"exists": {"field": "full_text"}}})["count"]
    existing = len(list(CORPUS_DIR.glob("*.txt")))
    print(f"Total in ES: {total:,} | Already dumped: {existing:,} | Remaining: {total - existing:,}")

    query = {
        "query": {"exists": {"field": "full_text"}},
        "_source": ["path", "full_text", "title", "citation", "year"],
    }

    written = 0
    skipped = 0

    with tqdm(total=total, desc="Dumping text corpus", initial=existing) as pbar:
        for hit in scan(es, index=INDEX, query=query, scroll="5m", size=200):
            src = hit["_source"]
            path = src.get("path", hit["_id"])
            out_file = CORPUS_DIR / f"{path}.txt"

            if out_file.exists():
                skipped += 1
                # don't update pbar for already-existing — count was set in initial
                continue

            full_text = src.get("full_text", "")
            if not full_text:
                continue

            # Write header line + full text
            header = f"PATH: {path}\nTITLE: {src.get('title','')}\nCITATION: {src.get('citation','')}\nYEAR: {src.get('year','')}\n\n"
            out_file.write_text(header + full_text, encoding="utf-8")
            written += 1
            pbar.update(1)

    print(f"\nDone. Written: {written:,} | Skipped (already existed): {skipped:,}")
    print(f"Corpus at: {CORPUS_DIR}")
    print(f"\nTest with: rg 'vital part of it had been removed' {CORPUS_DIR} -l")


if __name__ == "__main__":
    main()
