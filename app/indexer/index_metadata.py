"""
Step 1: Index all 43,046 judgments from metadata parquets into Elasticsearch.
Reads all 77 parquet files, one per year, and bulk-indexes into ES.
No PDF parsing needed — metadata already has titles, headnotes, judges, citations.
"""

import glob
import json
import sys
from pathlib import Path

import pandas as pd
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk, BulkIndexError
from tqdm import tqdm

BASE_DIR = Path(__file__).parent.parent.parent
METADATA_DIR = BASE_DIR / "metadata"
ES_HOST = "http://localhost:9200"
INDEX_NAME = "judgments"

# ---------------------------------------------------------------------------
# Index mapping — defines field types for search and filtering
# ---------------------------------------------------------------------------
INDEX_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "analysis": {
            "analyzer": {
                "legal_analyzer": {
                    "type": "standard",
                    "stopwords": "_english_"
                }
            }
        }
    },
    "mappings": {
        "properties": {
            # Full-text search fields
            "title":            {"type": "text", "analyzer": "legal_analyzer"},
            "petitioner":       {"type": "text", "analyzer": "legal_analyzer"},
            "respondent":       {"type": "text", "analyzer": "legal_analyzer"},
            "description":      {"type": "text", "analyzer": "legal_analyzer"},
            "judge":            {"type": "text", "analyzer": "legal_analyzer",
                                 "fields": {"keyword": {"type": "keyword"}}},

            # Exact match / filter fields
            "citation":         {"type": "keyword"},
            "case_id":          {"type": "keyword"},
            "cnr":              {"type": "keyword"},
            "court":            {"type": "keyword"},
            "disposal_nature":  {"type": "keyword"},
            "year":             {"type": "integer"},
            "path":             {"type": "keyword"},
            "available_languages": {"type": "keyword"},

            # Date
            "decision_date":    {"type": "date", "format": "dd-MM-yyyy||yyyy-MM-dd||strict_date_optional_time"},
        }
    }
}


def create_index(es: Elasticsearch):
    if es.indices.exists(index=INDEX_NAME):
        print(f"Index '{INDEX_NAME}' already exists. Deleting and recreating...")
        es.indices.delete(index=INDEX_NAME)
    es.indices.create(index=INDEX_NAME, body=INDEX_MAPPING)
    print(f"Created index '{INDEX_NAME}'")


def load_all_parquets() -> pd.DataFrame:
    files = sorted(glob.glob(str(METADATA_DIR / "year=*" / "metadata.parquet")))
    if not files:
        print(f"No parquet files found in {METADATA_DIR}")
        sys.exit(1)

    dfs = []
    for f in files:
        df = pd.read_parquet(f)
        dfs.append(df)

    all_df = pd.concat(dfs, ignore_index=True)
    print(f"Loaded {len(all_df):,} rows from {len(files)} parquet files")

    # Deduplicate by path — same judgment scraped multiple times,
    # only raw_html and scraped_at differ. Keep the latest scrape.
    before = len(all_df)
    all_df = all_df.sort_values("scraped_at", ascending=False).drop_duplicates(
        subset=["path"], keep="first"
    ).reset_index(drop=True)
    print(f"After deduplication: {len(all_df):,} unique judgments (removed {before - len(all_df):,} duplicates)")
    return all_df


def row_to_doc(row: dict) -> dict:
    """Convert a parquet row to an ES document."""
    # Clean up date — some are empty strings
    decision_date = row.get("decision_date", "")
    if not decision_date or str(decision_date).strip() == "":
        decision_date = None

    # Convert year to int safely
    try:
        year = int(row.get("year", 0))
    except (ValueError, TypeError):
        year = None

    return {
        "_index": INDEX_NAME,
        "_id": row.get("path"),  # path is unique after deduplication
        "_source": {
            "title":               str(row.get("title", "") or ""),
            "petitioner":          str(row.get("petitioner", "") or ""),
            "respondent":          str(row.get("respondent", "") or ""),
            "description":         str(row.get("description", "") or ""),
            "judge":               str(row.get("judge", "") or ""),
            "author_judge":        str(row.get("author_judge", "") or ""),
            "citation":            str(row.get("citation", "") or ""),
            "case_id":             str(row.get("case_id", "") or ""),
            "cnr":                 str(row.get("cnr", "") or ""),
            "court":               str(row.get("court", "") or ""),
            "disposal_nature":     str(row.get("disposal_nature", "") or ""),
            "available_languages": str(row.get("available_languages", "") or ""),
            "path":                str(row.get("path", "") or ""),
            "decision_date":       decision_date,
            "year":                year,
        }
    }


def index_all(es: Elasticsearch, df: pd.DataFrame):
    records = df.to_dict(orient="records")
    actions = [row_to_doc(r) for r in records]

    print(f"Indexing {len(actions):,} documents...")
    success, failed = 0, 0

    # Bulk index in batches of 500
    batch_size = 500
    batches = [actions[i:i+batch_size] for i in range(0, len(actions), batch_size)]

    for batch in tqdm(batches, desc="Indexing"):
        try:
            ok, errors = bulk(es, batch, raise_on_error=False)
            success += ok
            if errors:
                failed += len(errors)
                for err in errors[:2]:
                    print(f"  Error: {err}")
        except BulkIndexError as e:
            failed += len(e.errors)

    print(f"\nDone. Indexed: {success:,} | Failed: {failed:,}")


def verify_index(es: Elasticsearch):
    count = es.count(index=INDEX_NAME)["count"]
    print(f"\nVerification — Total documents in ES: {count:,}")

    # Test a sample query
    res = es.search(index=INDEX_NAME, body={
        "query": {"match": {"description": "arbitration agreement"}},
        "size": 3,
        "_source": ["title", "year", "citation", "disposal_nature"]
    })
    print(f"Sample query 'arbitration agreement' → {res['hits']['total']['value']} hits")
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        print(f"  [{src['year']}] {src['title'][:60]}... | {src['disposal_nature']}")


if __name__ == "__main__":
    es = Elasticsearch(ES_HOST)
    if not es.ping():
        print("Cannot connect to Elasticsearch at", ES_HOST)
        sys.exit(1)
    print(f"Connected to Elasticsearch")

    create_index(es)
    df = load_all_parquets()
    index_all(es, df)
    verify_index(es)
