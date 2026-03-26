"""
Step 2: On-demand PDF text extractor.
Handles both Supreme Court paths (e.g. "2026_1_1_23")
and High Court paths (e.g. "court/cnrorders/hcaurdb/orders/HCBM030088082020_1_2023-02-24.pdf").
"""

import io
import json
import re
import tarfile
from pathlib import Path
from functools import lru_cache

import pymupdf  # pip install pymupdf

BASE_DIR   = Path(__file__).parent.parent.parent
DATA_DIR   = BASE_DIR / "data"
HC_TAR_DIR = BASE_DIR / "hc_data" / "tar"

# Map bench name → court code for HC tar path resolution
BENCH_TO_COURT = {
    "hcaurdb":           "27_1",
    "hcbgoa":            "27_1",
    "kolhcdb":           "27_1",
    "newas":             "27_1",
    "newos":             "27_1",
    "newos_spl":         "27_1",
    "karhcdharwad":      "29_3",
    "karhckalaburagi":   "29_3",
    "karnataka_bng_old": "29_3",
}


@lru_cache(maxsize=128)
def load_index(year: int) -> dict:
    """Load and cache the index.json for a given year."""
    index_path = DATA_DIR / f"year={year}" / "english" / "english.index.json"
    if not index_path.exists():
        return {}
    with open(index_path) as f:
        data = json.load(f)

    # Build filename → part_name lookup
    file_map = {}
    for part in data.get("parts", []):
        for filename in part.get("files", []):
            file_map[filename] = part["name"]
    return file_map


def extract_text(path: str) -> str | None:
    """
    Extract text from a PDF given its path field (e.g. '2026_1_1_23').

    1. Derives year from path prefix
    2. Looks up which tar file contains it via index.json
    3. Extracts only that PDF from the tar (no full tar extraction)
    4. Returns text via pymupdf
    """
    # Some paths have a non-numeric court prefix (e.g. "S_1997_3_404_418").
    # Strip leading letter-only segments so the year can be parsed correctly.
    parts = path.split("_")
    numeric_parts = parts if parts[0].isdigit() else parts[1:]
    if not numeric_parts or not numeric_parts[0].isdigit():
        return None
    year = int(numeric_parts[0])
    # Rebuild a clean path (without the prefix) for the filename lookup
    clean_path = "_".join(numeric_parts)
    filename = f"{clean_path}_EN.pdf"

    file_map = load_index(year)
    if not file_map:
        return None

    if filename not in file_map:
        return None

    part_name = file_map[filename]
    tar_path = DATA_DIR / f"year={year}" / "english" / part_name

    if not tar_path.exists():
        return None

    try:
        with tarfile.open(tar_path, "r") as tar:
            try:
                member = tar.getmember(filename)
            except KeyError:
                # Try without directory prefix
                members = [m for m in tar.getmembers() if m.name.endswith(filename)]
                if not members:
                    return None
                member = members[0]

            pdf_bytes = tar.extractfile(member).read()

        # Parse PDF bytes with pymupdf
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()

        return "\n".join(pages).strip()

    except Exception as e:
        print(f"Error extracting {filename}: {e}")
        return None


@lru_cache(maxsize=256)
def load_hc_index(year: int, court: str, bench: str) -> dict:
    """Load and cache HC data.index.json, returning filename → part_name map."""
    index_path = HC_TAR_DIR / f"year={year}" / f"court={court}" / f"bench={bench}" / "data.index.json"
    if not index_path.exists():
        return {}
    with open(index_path) as f:
        data = json.load(f)
    file_map = {}
    for part in data.get("parts", []):
        for filename in part.get("files", []):
            file_map[filename] = part["name"]
    return file_map


def extract_hc_text(pdf_link: str) -> str | None:
    """
    Extract text from an HC judgment given its pdf_link.
    pdf_link format: 'court/cnrorders/{bench}/orders/{filename}.pdf'
    Year is derived from the date in the filename (e.g. HCBM..._1_2023-02-24.pdf → 2023).
    """
    parts = pdf_link.split("/")
    if len(parts) < 4:
        return None

    bench    = parts[2]
    filename = parts[-1]
    court    = BENCH_TO_COURT.get(bench)
    if not court:
        return None

    # Extract year from filename date suffix (e.g. HCBM030088082020_1_2023-02-24.pdf)
    m = re.search(r"_(\d{4})-\d{2}-\d{2}\.pdf$", filename)
    if not m:
        return None
    year = int(m.group(1))

    file_map = load_hc_index(year, court, bench)
    if not file_map:
        return None

    part_name = file_map.get(filename)
    if not part_name:
        return None

    tar_path = HC_TAR_DIR / f"year={year}" / f"court={court}" / f"bench={bench}" / part_name
    if not tar_path.exists():
        return None

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
    except Exception as e:
        print(f"Error extracting HC {filename}: {e}")
        return None


def extract_section(path: str, section: str = "all") -> str | None:
    """
    Extract a specific section from a judgment.
    Automatically routes to SC or HC extractor based on path format.
    section: 'all' | 'facts' | 'held' | 'headnotes'
    """
    # HC paths start with 'court/cnrorders/'
    if path.startswith("court/cnrorders/"):
        text = extract_hc_text(path)
    else:
        text = extract_text(path)
    if not text or section == "all":
        return text

    section_markers = {
        "headnotes": ["Headnotes", "HEADNOTES"],
        "facts":     ["BRIEF FACTS", "Brief Facts", "FACTS OF THE CASE", "Background"],
        "held":      ["Held:", "HELD:", "Result of the case", "ORDER", "JUDGMENT"],
    }

    markers = section_markers.get(section, [])
    if not markers:
        return text

    # Find the first matching section marker
    start_idx = -1
    for marker in markers:
        idx = text.find(marker)
        if idx != -1:
            start_idx = idx
            break

    if start_idx == -1:
        return text  # section not found, return full text

    # For headnotes: prepend the case header (title, date, judge names) which
    # lives before the HEADNOTES marker — this is where bench composition is.
    prefix = ""
    if section == "headnotes" and start_idx > 0:
        prefix = text[:start_idx].strip() + "\n\n"

    # Find the next major section after our target
    next_section_idx = len(text)
    all_markers = [m for markers in section_markers.values() for m in markers]
    for marker in all_markers:
        idx = text.find(marker, start_idx + len(markers[0]))
        if idx != -1 and idx < next_section_idx:
            next_section_idx = idx

    return (prefix + text[start_idx:next_section_idx]).strip()


if __name__ == "__main__":
    # Quick test
    test_path = "2026_1_1_23"
    print(f"Extracting: {test_path}")
    text = extract_text(test_path)
    if text:
        print(f"Extracted {len(text):,} characters")
        print("\nFirst 500 chars:")
        print(text[:500])
    else:
        print("Failed to extract")
