"""
Legal research tools for the ADK agent.
Each function is a plain Python callable — ADK reads the type hints and
docstrings to generate the JSON schema sent to the model automatically.
"""

import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from elasticsearch import Elasticsearch

sys.path.append(str(Path(__file__).parent.parent))
from indexer.extractor import extract_section

ES_HOST = "http://localhost:9200"
INDEX_NAME = "judgments"
BASE_DIR = Path(__file__).parent.parent.parent

es = Elasticsearch(ES_HOST)


# ---------------------------------------------------------------------------
# Tool functions — docstrings are the schema descriptions sent to Gemini
# ---------------------------------------------------------------------------

def search_cases(
    query: str,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    disposal_nature: Optional[str] = None,
    judge: Optional[str] = None,
    court: Optional[str] = None,
    top_k: Optional[int] = None,
) -> dict:
    """Search Indian court judgments by legal concept, keyword, party name, or statute.

    Primary search tool. Results ranked by relevance + citation_count so landmark
    cases surface first.

    MANDATORY: For any query, call this at least 2 times with different phrasings.
    For long/multi-term queries: decompose into 3-5 key elements and try each combination.
    Do NOT give up after a single search — landmark cases may surface only on the 2nd or 3rd attempt.

    Args:
        query: Full-text search query. For long headnotes-style queries, use the most
            distinctive 3-4 terms (e.g. "Provisional Parliament First Amendment 31A").
            For named cases use SHORT party name only (e.g. "Shankari Prasad", not the
            full headnotes). Try alternate phrasings: article numbers, party names,
            doctrine names, statute names.
        year_from: Earliest judgment year (e.g. 2015). Use to find recent law or
            post-amendment positions.
        year_to: Latest judgment year (e.g. 2024).
        disposal_nature: Filter by outcome — "Dismissed", "Allowed", "Partly Allowed".
        judge: Filter by judge name (partial match — e.g. "Chandrachud").
        court: Partial court name — "Supreme", "Bombay", or "Karnataka".
        top_k: Results to return (default 3, use 5–10 for comprehensive research,
            max 20).

    Returns:
        dict with keys: total_found (int), returned (int), results (list of dicts).
        Each result has: path, title, court, citation, year,
        disposal_nature, citation_count, headnotes (truncated snippet).
        NOTE: Bench/judge info is NOT in search results. Call read_judgment()
        to get [FULL BENCH] with all judges. High citation_count (50+) = landmark.
    """
    top_k = min(top_k or 3, 20)

    must = [
        {
            "bool": {
                "should": [
                    # High-weight: title, party names, citation
                    {
                        "multi_match": {
                            "query": query,
                            "fields": ["title^5", "petitioner^3", "respondent^3", "judge^2", "citation_text^4"],
                            "type": "best_fields",
                            "fuzziness": "AUTO",
                        }
                    },
                    # Medium-weight: description headnotes (when available)
                    {
                        "match": {
                            "description": {
                                "query": query,
                                "boost": 2,
                            }
                        }
                    },
                    # Phrase match in full_text — high precision concept search
                    {
                        "match_phrase": {
                            "full_text": {
                                "query": query,
                                "boost": 3,
                                "slop": 2,
                            }
                        }
                    },
                    # Token match in full_text — recall fallback
                    {
                        "match": {
                            "full_text": {
                                "query": query,
                                "boost": 1,
                            }
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        }
    ]

    filters = []
    if year_from or year_to:
        year_range = {}
        if year_from:
            year_range["gte"] = year_from
        if year_to:
            year_range["lte"] = year_to
        filters.append({"range": {"year": year_range}})
    if disposal_nature:
        filters.append({"term": {"disposal_nature": disposal_nature}})
    if judge:
        filters.append({"match": {"judge": judge}})
    if court:
        filters.append({"wildcard": {"court": f"*{court}*"}})

    body = {
        "query": {
            "function_score": {
                "query": {"bool": {"must": must, "filter": filters}},
                "functions": [
                    {
                        "field_value_factor": {
                            "field": "citation_count",
                            "modifier": "log1p",
                            "factor": 0.3,
                            "missing": 0,
                        }
                    }
                ],
                "boost_mode": "sum",
            }
        },
        "size": top_k,
        "_source": [
            "title", "citation", "judge", "decision_date",
            "disposal_nature", "description", "path", "year",
            "court", "citation_count",
        ],
        "highlight": {
            "max_analyzed_offset": 999999,
            "fields": {
                "full_text": {
                    "fragment_size": 400,
                    "number_of_fragments": 2,
                    "pre_tags": [""],
                    "post_tags": [""],
                },
                "description": {
                    "fragment_size": 400,
                    "number_of_fragments": 1,
                    "pre_tags": [""],
                    "post_tags": [""],
                },
            },
            "require_field_match": False,
        },
    }

    res = es.search(index=INDEX_NAME, body=body)
    hits = res["hits"]["hits"]
    total = res["hits"]["total"]["value"]

    results = []
    for hit in hits:
        src = hit["_source"]
        desc = src.get("description", "")

        # Use ES highlight snippets when description is empty (98% of cases)
        if not desc.strip():
            hl = hit.get("highlight", {})
            snippets = hl.get("description", []) + hl.get("full_text", [])
            if snippets:
                desc = " … ".join(snippets)

        results.append({
            "path":            src.get("path"),
            "title":           src.get("title"),
            "court":           src.get("court") or "Supreme Court of India",
            "citation":        src.get("citation"),
            "year":            src.get("year"),
            "disposal_nature": src.get("disposal_nature"),
            "citation_count":  src.get("citation_count", 0),
            "headnotes":       (desc[:600] + "…") if len(desc) > 600 else desc,
        })

    return {
        "total_found": total,
        "returned": len(results),
        "results": results,
        "NEXT_STEP": "Call read_judgment(path, section='held') on the top 2-3 results. The response includes [FULL BENCH] with all judges and the actual ratio decidendi. Do NOT use these search snippets for bench composition or legal analysis.",
    }


def read_judgment(path: str, section: str = "all") -> dict:
    """Read the text of a judgment from the database by its file path.

    MANDATORY: Call this on the top 2-3 results after EVERY search_cases call.
    Search snippets are truncated 600-char excerpts — NEVER sufficient for
    legal analysis, ratio, or holdings. Use section="held" for the court's
    ruling; use section="all" for paragraph-level questions.

    Args:
        path: File path from a search_cases or get_cases_by_statute result
            (the 'path' field in each result).
        section: Which part of the judgment to return:
            "all" — full text (use for paragraph-specific questions),
            "headnotes" — summary/headnotes only,
            "facts" — background facts,
            "held" — ratio decidendi / the court's holding.

    Returns:
        dict with keys: path, section, truncated (bool), text (str).
        Returns an error dict if the file is unavailable.
    """
    # Fast path: read from pre-extracted corpus files (instant vs ~2s tar extraction)
    full_text = _read_from_corpus(path)

    if full_text is None:
        # Fallback: extract from tar (slower)
        raw = extract_section(path, "all")
        if not raw:
            return {
                "error": f"Could not extract judgment for path '{path}'. "
                         "File may not be available locally. Try grep_judgments to locate it."
            }
        full_text = _clean_text(raw)

    # Extract full bench from opening lines — handles OCR variants:
    #   [A AND B, JJ.]  [A AND B, JJ.)  [A and B,* JJ.]  [A, B & C, JJ.)
    bench_match = re.search(
        r'\[([A-Z][\w\s,.\'\*\-&;]+(?:AND|and|&)[\w\s,.\'\*\-&;]+JJ\.?)\s*[)\]]',
        full_text[:2000]
    )
    if not bench_match:
        # Single-judge fallback: [A, J.]  or  [A, J.)
        bench_match = re.search(
            r'\[([A-Z][\w\s,.\'\*\-]+J\.?)\s*[)\]]',
            full_text[:2000]
        )
    full_bench = bench_match.group(1).strip().rstrip(',') if bench_match else None
    # Clean OCR artifacts from bench string
    if full_bench:
        full_bench = re.sub(r'[*\']', '', full_bench).strip()
        full_bench = re.sub(r'\s+', ' ', full_bench)

    # Prepend bench info directly into text so the model can't miss it
    bench_header = ""
    if full_bench:
        bench_header = f"[FULL BENCH: {full_bench}]\n\n"

    # Extract requested section
    text = bench_header + _extract_section_from_text(full_text, section)

    # Character caps: generous for held (may be long), tighter for headnotes
    caps = {"headnotes": 4000, "held": 12000, "facts": 6000, "all": 10000}
    cap = caps.get(section, 10000)

    truncated = len(text) > cap
    if truncated:
        # Truncate at paragraph boundary
        cutoff = text.rfind("\n\n", 0, cap)
        text = text[:cutoff if cutoff > cap // 2 else cap] + "\n\n[... truncated — call read_judgment again with a more specific section or use grep_judgments to find a specific paragraph]"

    result = {"path": path, "section": section, "truncated": truncated, "text": text}
    if full_bench:
        result["full_bench"] = full_bench
        result["⚠️ NOTE"] = "Use 'full_bench' field for ALL judges — not the 'judge' field from search results which is only the authoring judge."
    return result


def find_citations(
    case_name: str,
    year_from: Optional[int] = None,
    top_k: Optional[int] = None,
) -> dict:
    """Find judgments that cite a specific case — use for good-law checks and evolution tracing.

    MANDATORY before relying on any case in an argument. A high total_citing_cases
    count confirms landmark status. Check the citing cases for any that overrule,
    distinguish, or limit the original judgment.

    Args:
        case_name: The case name to search for — e.g. "Vidya Drolia",
            "Maneka Gandhi", "Kesavananda Bharati", "Gurbaksh Singh Sibbia".
            Use the short name (first party); if 0 results, try the full name.
        year_from: Only return citing cases from this year onwards (e.g. 2020
            to check recent treatment).
        top_k: Maximum citing cases to return (default 10, max 20).

    Returns:
        dict with keys: case_searched, total_citing_cases (int),
        returned (int), citing_cases (list of dicts).
        Each citing case has: path, title, citation, year, decision_date,
        disposal_nature, judge.
        If total_citing_cases is 0: try alternate spellings of the case name.
    """
    top_k = min(top_k or 10, 20)

    filters = []
    if year_from:
        filters.append({"range": {"year": {"gte": year_from}}})

    body = {
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": case_name,
                            "fields": ["full_text^2", "description^1"],
                            "type": "phrase",
                        }
                    }
                ],
                "filter": filters,
            }
        },
        "size": top_k,
        "_source": [
            "title", "citation", "case_id", "judge",
            "decision_date", "disposal_nature", "path", "year",
        ],
    }

    res = es.search(index=INDEX_NAME, body=body)
    hits = res["hits"]["hits"]

    results = [
        {
            "path":            h["_source"].get("path"),
            "title":           h["_source"].get("title"),
            "citation":        h["_source"].get("citation"),
            "year":            h["_source"].get("year"),
            "decision_date":   h["_source"].get("decision_date"),
            "disposal_nature": h["_source"].get("disposal_nature"),
            "judge":           h["_source"].get("judge"),
        }
        for h in hits
    ]

    return {
        "case_searched":       case_name,
        "total_citing_cases":  res["hits"]["total"]["value"],
        "returned":            len(results),
        "citing_cases":        results,
    }


def get_cases_by_statute(
    act: str,
    section: Optional[str] = None,
    year_from: Optional[int] = None,
    top_k: Optional[int] = None,
) -> dict:
    """Find judgments that interpret a specific Act and (optionally) a section.

    Always call this before search_cases when the query mentions an Act or
    section number — it searches headnotes specifically for statutory references.

    Args:
        act: The full or partial Act name — e.g. "Arbitration and Conciliation
            Act", "Constitution of India", "Indian Penal Code".
        section: Section number to filter on — e.g. "11", "34", "21".
            Omit to search all sections of the Act.
        year_from: Only return cases from this year onwards.
        top_k: Maximum results to return (default 10, max 20).

    Returns:
        dict with keys: act, section, total_found (int), returned (int),
        results (list of dicts).
        Each result has: path, title, citation, year, decision_date,
        disposal_nature, judge, headnotes.
    """
    top_k = min(top_k or 10, 20)

    query_str = f"section {section} {act}" if section else act

    filters = []
    if year_from:
        filters.append({"range": {"year": {"gte": year_from}}})

    body = {
        "query": {
            "bool": {
                "must": [
                    {
                        "bool": {
                            "should": [
                                {"match": {"description": {"query": query_str, "boost": 3}}},
                                {"match_phrase": {"full_text": {"query": query_str, "boost": 2, "slop": 2}}},
                                {"match": {"full_text": {"query": query_str, "boost": 1}}},
                            ],
                            "minimum_should_match": 1,
                        }
                    }
                ],
                "filter": filters,
            }
        },
        "size": top_k,
        "_source": [
            "title", "citation", "case_id", "judge",
            "decision_date", "disposal_nature", "path", "year", "description",
        ],
        "highlight": {
            "fields": {
                "full_text": {"fragment_size": 500, "number_of_fragments": 2, "pre_tags": [""], "post_tags": [""]},
                "description": {"fragment_size": 500, "number_of_fragments": 1, "pre_tags": [""], "post_tags": [""]},
            },
            "require_field_match": False,
        },
    }

    res = es.search(index=INDEX_NAME, body=body)
    hits = res["hits"]["hits"]

    results = []
    for h in hits:
        src = h["_source"]
        desc = src.get("description", "")
        if not desc.strip():
            hl = h.get("highlight", {})
            snippets = hl.get("description", []) + hl.get("full_text", [])
            if snippets:
                desc = " … ".join(snippets)
        results.append({
            "path":            src.get("path"),
            "title":           src.get("title"),
            "citation":        src.get("citation"),
            "year":            src.get("year"),
            "decision_date":   src.get("decision_date"),
            "disposal_nature": src.get("disposal_nature"),
            "judge":           src.get("judge"),
            "headnotes":       desc[:600] + "…" if len(desc) > 600 else desc,
        })

    return {
        "act":         act,
        "section":     section,
        "total_found": res["hits"]["total"]["value"],
        "returned":    len(results),
        "results":     results,
    }


CORPUS_DIR = BASE_DIR / "data" / "text_corpus"

# ---------------------------------------------------------------------------
# Text cleaning helpers
# ---------------------------------------------------------------------------

# Regex for SCR pinpoint citation refs like "[507 D-E]", "[1234 A]"
_SCR_REF = re.compile(r"\[\d{1,4}[A-Z]?[- ][A-Z]\]|\[\d{1,4} [A-H]-[A-H]\]")

# Standalone single capital letters A-H on their own line (SCR margin markers)
# Handles trailing spaces and tildes (~) sometimes added by OCR
_PAGE_MARKER = re.compile(r"^[A-H~]\s*$", re.MULTILINE)

# Hyphenated line-break: word ending with - followed by newline + continuation
_HYPHEN_BREAK = re.compile(r"(\w)-\n(\w)")


def _clean_text(text: str) -> str:
    """Remove SCR formatting noise from extracted PDF text."""
    # Remove standalone A-H page margin markers
    text = _PAGE_MARKER.sub("", text)
    # Remove SCR pinpoint refs
    text = _SCR_REF.sub("", text)
    # Join hyphenated line breaks
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    # Collapse runs of blank lines to at most one blank line
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Section markers ordered by priority — modern SCR format first
_SECTION_MARKERS = {
    "issue": [
        "Issue for Consideration",
        "Issues for Consideration",
        "ISSUE FOR CONSIDERATION",
        "Questions for consideration",
        "Question for Consideration",
    ],
    "headnotes": [
        "Issue for Consideration",   # modern SCR — issue+held block IS the headnote
        "HEAD NOTE",
        "HEADNOTE",
        "Headnote",
        "HEADNOTES",
        "Head Note",
    ],
    "held": [
        "Held (per majority)",
        "Held (per",
        "HELD (per",
        "Held:",
        "HELD:",
        "held:",
        "Held that,",
    ],
    "facts": [
        "Brief Facts",
        "BRIEF FACTS",
        "FACTS OF THE CASE",
        "FACTUAL BACKGROUND",
        "Background",
        "BACKGROUND",
        "Facts",
    ],
}

# Section boundaries — when reading section X, stop at these
_SECTION_END_MARKERS = [
    "Held (per majority)", "Held (per", "HELD (per",
    "Held:", "HELD:", "held:",
    "ORDER", "JUDGMENT", "J U D G M E N T",
    "DISSENTING OPINION", "CONCURRING OPINION",
    "SEPARATE OPINION",
]


def _extract_section_from_text(text: str, section: str) -> str:
    """Smart section extraction from cleaned text."""
    markers = _SECTION_MARKERS.get(section, [])
    if not markers or section == "all":
        return text

    # Find first marker
    start_idx = -1
    for m in markers:
        idx = text.find(m)
        if idx != -1:
            start_idx = idx
            break

    if start_idx == -1:
        # Section not found — return opening block (first 3000 chars of non-header content)
        # Skip the file header we wrote (PATH/TITLE/CITATION/YEAR lines)
        body_start = text.find("\n\n")
        if body_start == -1:
            return text[:3000]
        return text[body_start:body_start + 3000].strip()

    # For headnotes: prepend the case header (title, date, judge names) which
    # lives before the first marker — bench composition is in the header block.
    if section == "headnotes":
        # Prefix = everything before the headnotes marker (the CASE DETAILS block)
        prefix = text[:start_idx].strip()
        end_markers = ["JUDGMENT", "J U D G M E N T", "ORDER\n", "Per ", "1. "]
        end_idx = len(text)
        for em in end_markers:
            idx = text.find(em, start_idx + 100)
            if idx != -1 and idx < end_idx:
                end_idx = idx
        body = text[start_idx:end_idx].strip()
        return (prefix + "\n\n" + body).strip() if prefix else body

    # For held/facts: return from start marker to next major section
    all_stops = _SECTION_END_MARKERS[:]
    if section == "held":
        all_stops = ["JUDGMENT", "J U D G M E N T", "DISSENTING", "CONCURRING", "SEPARATE OPINION"]

    end_idx = min(start_idx + 15000, len(text))  # generous cap within section
    for em in all_stops:
        idx = text.find(em, start_idx + 200)
        if idx != -1 and idx < end_idx:
            end_idx = idx

    return text[start_idx:end_idx].strip()


def _read_from_corpus(path: str) -> Optional[str]:
    """Read and clean text from pre-extracted corpus file. Fast path — avoids tar."""
    corpus_file = CORPUS_DIR / f"{path}.txt"
    if not corpus_file.exists():
        return None
    raw = corpus_file.read_text(encoding="utf-8")
    # Strip the 4-line header we added during dump
    header_end = raw.find("\n\n")
    body = raw[header_end:] if header_end != -1 else raw
    return _clean_text(body)


def get_context_around(
    path: str,
    phrase: str,
    window_chars: Optional[int] = None,
) -> dict:
    """Read a large window of text around a specific phrase in a judgment.

    Use this after grep_judgments finds the right path, or after read_judgment
    shows a truncated result at a critical point. Gives you the full paragraph
    and surrounding reasoning around the exact phrase.

    Args:
        path: The judgment path (e.g. "2009_6_152_159").
        phrase: An exact phrase (5+ words) to locate within the judgment.
            The text around this phrase will be returned.
        window_chars: Characters to return on each side of the match
            (default 2000 — roughly 2-3 paragraphs either side).

    Returns:
        dict with keys: path, phrase, found (bool), context (str).
        If phrase not found, returns the first 3000 chars of the judgment instead.
    """
    window = window_chars or 2000
    full_text = _read_from_corpus(path)
    if full_text is None:
        return {"error": f"Path '{path}' not in corpus. Run dump_text_corpus first."}

    idx = full_text.lower().find(phrase.lower())
    if idx == -1:
        normalized = re.sub(r"\s+", " ", full_text.lower())
        norm_phrase = re.sub(r"\s+", " ", phrase.lower())
        idx = normalized.find(norm_phrase)
        if idx != -1:
            idx = min(idx, len(full_text) - 1)

    if idx == -1:
        return {
            "path": path,
            "phrase": phrase,
            "found": False,
            "context": f"Phrase not found. First 3000 chars:\n\n{full_text[:3000]}",
        }

    start = max(0, idx - window)
    end = min(len(full_text), idx + len(phrase) + window)

    para_start = full_text.rfind("\n\n", 0, start)
    if para_start != -1 and start - para_start < 200:
        start = para_start
    para_end = full_text.find("\n\n", end)
    if para_end != -1 and para_end - end < 200:
        end = para_end

    context = full_text[start:end].strip()
    return {"path": path, "phrase": phrase, "found": True, "context": context}


def grep_judgments(
    pattern: str,
    paths: Optional[list] = None,
    context_lines: Optional[int] = None,
    top_k: Optional[int] = None,
    multiline: Optional[bool] = None,
    normalize_whitespace: Optional[bool] = None,
) -> dict:
    """Search judgment text using exact phrase or regex — optionally within a candidate set.

    RECOMMENDED WORKFLOW (especially for large corpora):
      1. Call search_cases or get_cases_by_statute first to get the top-N candidate paths.
      2. Pass those paths here as the `paths` argument.
      3. grep then runs only on those N files — fast and precise regardless of corpus size.

    Use grep_judgments without `paths` only when: the user pastes a verbatim excerpt and
    you have no candidate set yet (rg on 38K files still returns in ~5s).

    HANDLING OCR / PASTED TEXT (very important):
    - DO NOT paste the entire excerpt as the pattern. Extract a SHORT (5-8 word) distinctive
      phrase from it instead.
    - Set normalize_whitespace=True (default) so spaces in the pattern automatically become
      \\s+ — this matches text that is split across lines or has extra whitespace in the corpus.
    - Set multiline=True when the phrase spans multiple lines (e.g. OCR text with hyphens at
      line-ends like "Legis-\\nlature").
    - For OCR hyphens at line-end, use regex: "Legis-?\\s*lature" or simply pick a phrase
      that avoids the hyphen.

    Args:
        pattern: Exact phrase or regex. Use a distinctive 5-10 word substring for verbatim
            text. Standard ripgrep syntax for regex.
            Examples:
              "vital part of it had been removed"
              "rarest of rare cases"
              "Constitution.*First Amendment.*1951"
              "Removal of Difficulties.*Order"
        paths: Optional list of case path IDs (e.g. ["2009_6_152_159", "2021_7_153_165"])
            from search_cases or get_cases_by_statute results. When provided, grep runs
            only on these files — recommended for large corpora to avoid full-scan overhead.
        context_lines: Lines of context around each match (default 2).
        top_k: Maximum matching files to return (default 5, max 20).
        multiline: If True, enables rg --multiline so patterns can span line boundaries.
            Use when matching phrases that may be split across lines in OCR text.
        normalize_whitespace: If True (default), replaces spaces in the pattern with \\s+
            so the match is insensitive to newlines and extra spaces in the corpus.
            Set to False only when using a hand-crafted regex that already handles whitespace.

    Returns:
        dict with keys: pattern, matches_found (int), results (list).
        Each result has: path, title, citation, year, snippets (list of matched context strings).
    """
    if not CORPUS_DIR.exists() or not any(CORPUS_DIR.glob("*.txt")):
        return {
            "error": "Text corpus not yet built. Run: python -m app.indexer.dump_text_corpus",
            "note": "Falling back to ES phrase search is recommended in the meantime.",
        }

    top_k = min(top_k or 5, 20)
    context_lines = context_lines or 2

    # Normalize whitespace by default: replace literal spaces with \s+ so the
    # pattern matches text split across lines (OCR hyphenation, extra spaces, etc.)
    do_normalize = (normalize_whitespace is None) or normalize_whitespace
    if do_normalize:
        # Escape the pattern for regex, then replace escaped-spaces with \s+
        # Only normalize if the pattern doesn't already look like a hand-crafted regex
        # (heuristic: no regex metacharacters other than * ? [ ])
        looks_like_regex = bool(re.search(r'[\\()+{}|^$]', pattern))
        if not looks_like_regex:
            safe_pattern = re.sub(r' +', r'\\s+', re.escape(pattern))
        else:
            safe_pattern = pattern
    else:
        safe_pattern = pattern

    # Build the list of files/directory to search
    if paths:
        # Target only the specified files — fast even for 2-crore corpora
        target_files = []
        for p in paths:
            f = CORPUS_DIR / f"{p}.txt"
            if f.exists():
                target_files.append(str(f))
        if not target_files:
            return {"pattern": pattern, "matches_found": 0, "results": [],
                    "note": "None of the provided paths exist in the corpus."}
        rg_targets = target_files
    else:
        # Full corpus scan — fine for 38K files, slow for larger corpora
        rg_targets = [str(CORPUS_DIR)]

    try:
        rg_cmd = [
            "rg",
            "--ignore-case",
            "--context", str(context_lines),
            "--max-count", "3",
            "--json",
        ]
        if multiline:
            rg_cmd += ["--multiline", "--multiline-dotall"]
        rg_cmd += [safe_pattern, *rg_targets]

        result = subprocess.run(
            rg_cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return {"error": "ripgrep (rg) not found. Install with: brew install ripgrep"}
    except subprocess.TimeoutExpired:
        return {"error": "Search timed out. Use a more specific pattern."}

    if result.returncode not in (0, 1):
        return {"error": f"ripgrep error: {result.stderr[:200]}"}

    # Parse JSON output — collect file paths and snippets
    import json as _json
    file_snippets: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        try:
            obj = _json.loads(line)
        except Exception:
            continue
        if obj.get("type") == "match":
            fpath = obj["data"]["path"]["text"]
            text = obj["data"]["lines"]["text"].strip()
            file_snippets.setdefault(fpath, []).append(text)
        elif obj.get("type") == "context":
            fpath = obj["data"]["path"]["text"]
            text = obj["data"]["lines"]["text"].strip()
            if fpath in file_snippets and text:
                file_snippets[fpath].append("  " + text)

    # Cap to top_k files
    file_snippets = dict(list(file_snippets.items())[:top_k])

    # Look up metadata from ES for the matching paths
    paths = [Path(fp).stem for fp in file_snippets]
    results = []
    if paths:
        meta_res = es.mget(index=INDEX_NAME, ids=paths, _source=["title", "citation", "year"])
        meta = {d["_id"]: d.get("_source", {}) for d in meta_res["docs"] if d.get("found")}

        for fp, snippets in file_snippets.items():
            path_id = Path(fp).stem
            m = meta.get(path_id, {})
            results.append({
                "path": path_id,
                "title": m.get("title", path_id),
                "citation": m.get("citation", ""),
                "year": m.get("year", ""),
                "snippets": snippets[:6],
            })

    return {
        "pattern": pattern,
        "matches_found": len(results),
        "results": results,
    }


if __name__ == "__main__":
    print("=== search_cases ===")
    res = search_cases("arbitration clause non-arbitrable", year_from=2020, top_k=3)
    print(f"Found: {res['total_found']}")
    for r in res["results"]:
        print(f"  [{r['year']}] {r['title'][:60]} | {r['disposal_nature']}")

    print("\n=== find_citations ===")
    res = find_citations("Vidya Drolia", year_from=2021, top_k=3)
    print(f"Cases citing Vidya Drolia: {res['total_citing_cases']}")
    for r in res["citing_cases"]:
        print(f"  [{r['year']}] {r['title'][:60]}")

    print("\n=== get_cases_by_statute ===")
    res = get_cases_by_statute("Arbitration and Conciliation Act", section="11", year_from=2020, top_k=3)
    print(f"Cases on Section 11: {res['total_found']}")
    for r in res["results"]:
        print(f"  [{r['year']}] {r['title'][:60]}")
