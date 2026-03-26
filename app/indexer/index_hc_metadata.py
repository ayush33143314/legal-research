"""
Index High Court metadata (Bombay + Karnataka) into the existing 'judgments' ES index.
Reads all metadata.parquet files under hc_data/metadata/ for court=27_1 and court=29_3.
Adds court_type='hc', court_name, bench fields alongside existing SC documents.
"""

import glob
import json
import re
import sys
from pathlib import Path

import pandas as pd
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk, BulkIndexError
from tqdm import tqdm

BASE_DIR = Path(__file__).parent.parent.parent
HC_METADATA_DIR = BASE_DIR / "hc_data" / "metadata"
ES_HOST = "http://localhost:9200"
INDEX_NAME = "judgments"

# Only index these courts
TARGET_COURTS = {"27_1", "29_3"}

COURT_NAMES = {
    "27_1": "Bombay High Court",
    "29_3": "Karnataka High Court",
}

# Map bench name → court code (for extractor routing)
BENCH_TO_COURT = {
    "hcaurdb":            "27_1",
    "hcbgoa":             "27_1",
    "kolhcdb":            "27_1",
    "newas":              "27_1",
    "newos":              "27_1",
    "newos_spl":          "27_1",
    "karhcdharwad":       "29_3",
    "karhckalaburagi":    "29_3",
    "karnataka_bng_old":  "29_3",
}


def add_hc_mapping(es: Elasticsearch):
    """Add HC-specific fields to the existing index mapping (non-destructive)."""
    es.indices.put_mapping(
        index=INDEX_NAME,
        body={
            "properties": {
                "court_type":  {"type": "keyword"},
                "bench":       {"type": "keyword"},
                "pdf_link":    {"type": "keyword"},
                "pdf_exists":  {"type": "boolean"},
            }
        }
    )


def extract_bench_from_pdf_link(pdf_link: str) -> str:
    """
    Extract bench name from pdf_link.
    e.g. 'court/cnrorders/hcaurdb/orders/HCBM...pdf' → 'hcaurdb'
    """
    parts = pdf_link.split("/")
    if len(parts) >= 3:
        return parts[2]
    return ""


def extract_year_from_decision_date(decision_date) -> int | None:
    if not decision_date:
        return None
    s = str(decision_date).strip()
    # Try YYYY-MM-DD
    m = re.match(r"(\d{4})-\d{2}-\d{2}", s)
    if m:
        return int(m.group(1))
    # Try DD-MM-YYYY
    m = re.match(r"\d{2}-\d{2}-(\d{4})", s)
    if m:
        return int(m.group(1))
    return None


def load_hc_parquets() -> pd.DataFrame:
    pattern = str(HC_METADATA_DIR / "year=*" / "court=*" / "bench=*" / "metadata.parquet")
    files = sorted(glob.glob(pattern))

    # Filter to target courts only
    filtered = [
        f for f in files
        if any(f"court={c}/" in f for c in TARGET_COURTS)
    ]

    if not filtered:
        print(f"No parquet files found under {HC_METADATA_DIR}")
        print("Run download_hc.sh first to fetch the data.")
        sys.exit(1)

    dfs = []
    for f in tqdm(filtered, desc="Loading parquets"):
        df = pd.read_parquet(f)
        # Embed court and bench from the path
        parts = Path(f).parts
        year_part  = next((p for p in parts if p.startswith("year=")),  "year=0")
        court_part = next((p for p in parts if p.startswith("court=")), "court=unknown")
        bench_part = next((p for p in parts if p.startswith("bench=")), "bench=unknown")
        df["_year_dir"]  = year_part.split("=")[1]
        df["_court_dir"] = court_part.split("=")[1]
        df["_bench_dir"] = bench_part.split("=")[1]
        dfs.append(df)

    all_df = pd.concat(dfs, ignore_index=True)
    print(f"\nLoaded {len(all_df):,} rows from {len(filtered)} parquet files")

    # Deduplicate by cnr — keep latest decision_date
    before = len(all_df)
    all_df = all_df.drop_duplicates(subset=["cnr"], keep="first").reset_index(drop=True)
    print(f"After deduplication: {len(all_df):,} unique judgments (removed {before - len(all_df):,})")

    return all_df


def row_to_doc(row: dict) -> dict:
    cnr        = str(row.get("cnr", "") or "").strip()
    pdf_link   = str(row.get("pdf_link", "") or "").strip()
    court_code = str(row.get("_court_dir", "") or "").strip()
    bench      = str(row.get("_bench_dir", "") or "").strip()

    decision_date = row.get("decision_date", "")
    if not decision_date or str(decision_date).strip() == "":
        decision_date = None

    year = extract_year_from_decision_date(decision_date)

    # Normalise date to YYYY-MM-DD if it's DD-MM-YYYY
    if decision_date and re.match(r"\d{2}-\d{2}-\d{4}", str(decision_date)):
        parts = str(decision_date).split("-")
        decision_date = f"{parts[2]}-{parts[1]}-{parts[0]}"

    court_name = COURT_NAMES.get(court_code, str(row.get("court", "") or ""))

    return {
        "_index": INDEX_NAME,
        "_id":    cnr,           # CNR is unique across HC
        "_source": {
            "title":           str(row.get("title", "") or ""),
            "description":     str(row.get("description", "") or ""),
            "judge":           str(row.get("judge", "") or ""),
            "cnr":             cnr,
            "path":            pdf_link,   # used by read_judgment tool
            "pdf_link":        pdf_link,
            "pdf_exists":      bool(row.get("pdf_exists", False)),
            "disposal_nature": str(row.get("disposal_nature", "") or ""),
            "court":           court_name,
            "court_type":      "hc",
            "bench":           bench,
            "decision_date":   decision_date,
            "year":            year,
            # SC-only fields left absent (null) for HC docs
            "petitioner":  "",
            "respondent":  "",
            "citation":    "",
            "case_id":     "",
        }
    }


def index_all(es: Elasticsearch, df: pd.DataFrame):
    records = df.to_dict(orient="records")
    actions = [row_to_doc(r) for r in records]

    print(f"Indexing {len(actions):,} HC documents...")
    success, failed = 0, 0

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


def verify(es: Elasticsearch):
    es.indices.refresh(index=INDEX_NAME)
    total = es.count(index=INDEX_NAME)["count"]
    hc = es.count(index=INDEX_NAME, body={"query": {"term": {"court_type": "hc"}}})["count"]
    print(f"\nTotal documents in ES: {total:,}  (HC: {hc:,})")

    # Sample query
    res = es.search(index=INDEX_NAME, body={
        "query": {
            "bool": {
                "must":   [{"match": {"description": "arbitration"}}],
                "filter": [{"term": {"court_type": "hc"}}]
            }
        },
        "size": 3,
        "_source": ["title", "court", "year", "disposal_nature"]
    })
    print(f"Sample HC query 'arbitration' → {res['hits']['total']['value']} hits")
    for hit in res["hits"]["hits"]:
        s = hit["_source"]
        print(f"  [{s.get('year')}] {s.get('title', '')[:70]} | {s.get('court')}")


if __name__ == "__main__":
    es = Elasticsearch(ES_HOST)
    if not es.ping():
        print("Cannot connect to Elasticsearch at", ES_HOST)
        sys.exit(1)
    print("Connected to Elasticsearch")

    add_hc_mapping(es)
    df = load_hc_parquets()
    index_all(es, df)
    verify(es)
