"""
Build citation graph from full_text of all SC judgments.

Steps:
1. Scroll all documents with full_text from ES
2. Extract SCR citation strings using regex
3. Count how many documents cite each citation → citation_count
4. Bulk-update every document with its own citation_count
5. Update search_cases to boost by citation_count

SCR formats found in the wild:
  [2002] 2 S.C.R. 712
  [2002] 2 SCR 712
  [1996] SUPP. 3 S.C.R. 671
  [2023] 7 S.C.R. 30
"""

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk, scan
from tqdm import tqdm

ES_HOST = "http://localhost:9200"
INDEX   = "judgments"

# Matches SCR citations in both dot and no-dot forms, with optional SUPP volume
# Captures: full matched string
SCR_PATTERN = re.compile(
    r'\[\d{4}\]\s+'           # [YYYY]
    r'(?:SUPP\.\s+\d+\s+)?'  # optional: SUPP. 3
    r'\d+\s+'                 # volume number
    r'S\.?\s*C\.?\s*R\.?\s+' # S.C.R. or SCR or S C R
    r'\d+',                   # page number
    re.IGNORECASE
)

# Also match SCC format — can't link to our docs but useful for future
SCC_PATTERN = re.compile(
    r'\(\d{4}\)\s+\d+\s+SCC\s+\d+',
    re.IGNORECASE
)

# AIR format
AIR_PATTERN = re.compile(
    r'AIR\s+\d{4}\s+SC\s+\d+',
    re.IGNORECASE
)


def normalize_scr(raw: str) -> str:
    """
    Normalise an SCR citation string to canonical form for matching.
    '[2002] 2 SCR  712' → '[2002] 2 SCR 712'
    '[2002] 2 S.C.R. 712' → '[2002] 2 SCR 712'
    '[1996] SUPP. 3 S.C.R. 671' → '[1996] SUPP 3 SCR 671'
    """
    s = raw.upper().strip()
    s = re.sub(r'S\.\s*C\.\s*R\.?', 'SCR', s)   # S.C.R. → SCR
    s = re.sub(r'SUPP\.', 'SUPP', s)              # SUPP. → SUPP
    s = re.sub(r'\s+', ' ', s)                    # collapse whitespace
    return s


def extract_scr_citations(text: str) -> list[str]:
    """Extract and normalise all SCR citations from a judgment text."""
    matches = SCR_PATTERN.findall(text)
    return [normalize_scr(m) for m in matches]


def build_citation_counts(es: Elasticsearch) -> tuple[Counter, dict]:
    """
    Scroll all documents, extract SCR citations from full_text.
    Returns:
      - citation_counts: Counter of normalized_citation → number of docs citing it
      - doc_citation_map: path → normalized citation of this doc (for lookup)
    """
    citation_counts  = Counter()   # normalized_citation → times cited
    doc_citation_map = {}          # doc_path → normalized citation of that doc

    query = {
        "query": {"exists": {"field": "full_text"}},
        "_source": ["path", "citation", "full_text"]
    }

    print("Scanning all documents to extract citations...")
    total = es.count(index=INDEX, body={"query": {"exists": {"field": "full_text"}}})["count"]

    for hit in tqdm(scan(es, index=INDEX, query=query, scroll="5m"), total=total):
        src  = hit["_source"]
        path = src.get("path", "")
        raw_citation = src.get("citation", "")
        full_text    = src.get("full_text", "")

        # Normalise this document's own citation for later lookup
        if raw_citation:
            doc_citation_map[path] = normalize_scr(raw_citation)

        # Extract all SCR citations mentioned in the judgment body
        cited = extract_scr_citations(full_text)
        for c in cited:
            citation_counts[c] += 1

    print(f"\nFound {len(citation_counts):,} unique SCR citations referenced across all judgments")
    print(f"Top 10 most cited:")
    for citation, count in citation_counts.most_common(10):
        print(f"  {count:>5}x  {citation}")

    return citation_counts, doc_citation_map


def build_update_actions(es: Elasticsearch,
                          citation_counts: Counter,
                          doc_citation_map: dict) -> list[dict]:
    """
    For each document, look up its citation in citation_counts.
    Build bulk update actions with citation_count field.
    """
    query = {
        "query": {"match_all": {}},
        "_source": ["path", "citation"]
    }

    actions = []
    zero_count = 0

    for hit in scan(es, index=INDEX, query=query, scroll="5m"):
        src  = hit["_source"]
        path = src.get("path", "")
        raw_citation = src.get("citation", "")

        count = 0
        if raw_citation:
            norm = normalize_scr(raw_citation)
            count = citation_counts.get(norm, 0)

        if count == 0:
            zero_count += 1

        actions.append({
            "_op_type": "update",
            "_index":   INDEX,
            "_id":      hit["_id"],
            "doc":      {"citation_count": count},
        })

    print(f"\nDocuments with citation_count > 0: {len(actions) - zero_count:,}")
    print(f"Documents with citation_count = 0: {zero_count:,}")
    return actions


def add_citation_count_mapping(es: Elasticsearch):
    es.indices.put_mapping(
        index=INDEX,
        body={"properties": {"citation_count": {"type": "integer"}}}
    )
    print("Added 'citation_count' field to ES mapping")


def bulk_update(es: Elasticsearch, actions: list[dict]):
    print(f"\nUpdating {len(actions):,} documents with citation_count...")
    success, failed = 0, 0
    batch_size = 500

    for i in tqdm(range(0, len(actions), batch_size)):
        batch = actions[i:i+batch_size]
        ok, errors = bulk(es, batch, raise_on_error=False)
        success += ok
        if errors:
            failed += len(errors)

    es.indices.refresh(index=INDEX)
    print(f"Done. Updated: {success:,} | Failed: {failed:,}")


def verify(es: Elasticsearch):
    # Find top cited cases
    res = es.search(index=INDEX, body={
        "query": {"range": {"citation_count": {"gt": 0}}},
        "sort": [{"citation_count": "desc"}],
        "size": 10,
        "_source": ["title", "citation", "citation_count", "year"]
    })
    print("\nTop 10 most cited SC judgments:")
    for hit in res["hits"]["hits"]:
        s = hit["_source"]
        print(f"  {s.get('citation_count'):>5}x  [{s.get('year')}] {s.get('title','')[:60]}")
        print(f"         {s.get('citation')}")


if __name__ == "__main__":
    es = Elasticsearch(ES_HOST)
    if not es.ping():
        print("Cannot connect to Elasticsearch")
        sys.exit(1)
    print("Connected to Elasticsearch\n")

    add_citation_count_mapping(es)
    citation_counts, doc_citation_map = build_citation_counts(es)
    actions = build_update_actions(es, citation_counts, doc_citation_map)
    bulk_update(es, actions)
    verify(es)
