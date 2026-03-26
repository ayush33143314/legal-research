"""
Backfill 'description' field for all ES documents that currently have an empty description.
Extracts the headnotes/opening section from each judgment's full_text and stores it.

This fixes the root cause of poor concept-search quality: 98% of cases have empty
description, so BM25 has no signal beyond raw title/full_text.

Strategy:
  1. Pull docs with full_text but empty description in batches (scroll API).
  2. Extract the first meaningful block: try headnotes markers first, then first 800 chars.
  3. Bulk-update description field.

Run once: python -m app.indexer.backfill_descriptions
Resume-safe: skips docs that already have a non-empty description.
"""

import re
import sys
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk, scan
from tqdm import tqdm

ES_HOST = "http://localhost:9200"
INDEX = "judgments"

HEADNOTE_MARKERS = [
    "HEAD NOTE",
    "HEADNOTE",
    "Headnote",
    "HEAD-NOTE",
    "HELD:",
    "Held:",
    "held:",
    "ORDER",
    "JUDGMENT",
    "J U D G M E N T",
    "SUMMARY",
]

STOP_MARKERS = [
    "JUDGMENT",
    "J U D G M E N T",
    "O R D E R",
    "ORDER",
    "Writ Petition",
    "Civil Appeal",
    "Criminal Appeal",
    "The facts",
    "FACTS",
]


def extract_opening(full_text: str) -> str:
    """Extract the most informative opening block from full_text."""
    if not full_text:
        return ""

    # Try to find a headnotes section
    for marker in HEADNOTE_MARKERS:
        idx = full_text.find(marker)
        if idx != -1 and idx < 3000:
            # Found a headnotes marker — extract up to 800 chars
            snippet = full_text[idx:idx + 800].strip()
            return snippet

    # No headnotes marker — skip the case header (party names, date, bench)
    # and return the first substantive paragraph
    lines = full_text.split("\n")
    content_lines = []
    skip_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Skip the first ~10 short header lines (party names, dates, citations)
        if skip_count < 10 and len(stripped) < 80:
            skip_count += 1
            continue
        content_lines.append(stripped)
        if sum(len(l) for l in content_lines) > 700:
            break

    return " ".join(content_lines)[:800].strip()


def main():
    es = Elasticsearch(ES_HOST)
    if not es.ping():
        print("Cannot connect to Elasticsearch")
        sys.exit(1)

    # Count how many need backfilling
    needs_fill = es.count(index=INDEX, body={
        "query": {
            "bool": {
                "must": [{"exists": {"field": "full_text"}}],
                "must_not": [{"wildcard": {"description": "*"}}],
            }
        }
    })["count"]

    print(f"Documents needing description backfill: {needs_fill:,}")
    if needs_fill == 0:
        print("Nothing to do.")
        return

    query = {
        "query": {
            "bool": {
                "must": [{"exists": {"field": "full_text"}}],
                "must_not": [{"wildcard": {"description": "*"}}],
            }
        },
        "_source": ["full_text", "path"],
    }

    actions = []
    processed = 0
    batch_size = 200

    with tqdm(total=needs_fill, desc="Backfilling descriptions") as pbar:
        for hit in scan(es, index=INDEX, query=query, scroll="5m", size=100):
            full_text = hit["_source"].get("full_text", "")
            desc = extract_opening(full_text)

            if desc:
                actions.append({
                    "_op_type": "update",
                    "_index": INDEX,
                    "_id": hit["_id"],
                    "doc": {"description": desc},
                })

            processed += 1
            pbar.update(1)

            if len(actions) >= batch_size:
                bulk(es, actions, raise_on_error=False)
                actions = []

        if actions:
            bulk(es, actions, raise_on_error=False)

    es.indices.refresh(index=INDEX)
    print(f"\nDone. Processed {processed:,} documents.")

    # Verify
    after = es.count(index=INDEX, body={"query": {"wildcard": {"description": "*"}}})["count"]
    print(f"Documents with non-empty description: {after:,}")


if __name__ == "__main__":
    main()
