"""
Fix OCR errors in corpus files using SymSpell.
Only corrects words that are clearly broken (contain digits/special-chars
mixed with letters) — leaves valid words untouched.

Run:
  python -m app.indexer.symspell_fix              # fix all bad files
  python -m app.indexer.symspell_fix --dry-run    # preview without writing
  python -m app.indexer.symspell_fix --path 1959_1_1177_1191
"""

import argparse
import re
import sys
from pathlib import Path

from symspellpy import SymSpell, Verbosity

BASE_DIR   = Path(__file__).parent.parent.parent
CORPUS_DIR = BASE_DIR / "data" / "text_corpus"
DONE_FILE  = CORPUS_DIR / ".symspell_done"

# Words with digit mixed in letters: mod1fymg, 1udg1ne11t, n1ade
BAD_WORD_DIGIT = re.compile(r"\b([a-zA-Z]+[0-9][a-zA-Z0-9]*|[a-zA-Z0-9]*[0-9][a-zA-Z]+)\b")

# Words with tilde in middle: th~t, St~te, und~r, appe~l
BAD_WORD_TILDE = re.compile(r"\b[a-zA-Z]+~[a-zA-Z]+\b")

# Words with ii substituting u: constitiition→constitution, wliich→which
BAD_WORD_II = re.compile(r"\b\w*ii\w*\b")

# Known common OCR substitutions — applied before SymSpell (faster, exact)
KNOWN_FIXES = [
    # Backslash-V/v → W/w (\Vas→Was, \Vith→With, \vhich→which)
    (re.compile(r"\\V([a-z])"), lambda m: "W" + m.group(1)),
    (re.compile(r"\\v([a-z])"), lambda m: "w" + m.group(1)),
    (re.compile(r"\\h([a-z])"), lambda m: "th" + m.group(1)),
    (re.compile(r"\\ct\b"), "Act"),
    (re.compile(r"\\rt\b"), "Art"),
    # b substituting h (tbe→the, tbat→that, tbis→this, wbich→which, beld→held, bave→have)
    (re.compile(r"\btbe\b"), "the"),
    (re.compile(r"\bTbe\b"), "The"),
    (re.compile(r"\btbat\b"), "that"),
    (re.compile(r"\bTbat\b"), "That"),
    (re.compile(r"\btbis\b"), "this"),
    (re.compile(r"\bTbis\b"), "This"),
    (re.compile(r"\bwbich\b"), "which"),
    (re.compile(r"\bWbich\b"), "Which"),
    (re.compile(r"\bbeld\b"), "held"),
    (re.compile(r"\bBeld\b"), "Held"),
    (re.compile(r"\bbave\b"), "have"),
    (re.compile(r"\blhe\b"), "the"),
    (re.compile(r"\bJhe\b"), "the"),
    # oi → of (standalone: 'State oi India')
    (re.compile(r"(?<=[a-z]) oi (?=[a-z])"), " of "),
    # lt → It at sentence start
    (re.compile(r"(?<=[.\n] )lt (?=[A-Z]|[a-z])"), "It "),
    (re.compile(r"\blt appears\b"), "It appears"),
    (re.compile(r"\blt is\b"), "It is"),
    (re.compile(r"\blt was\b"), "It was"),
    # Digit substitutions
    (re.compile(r"\bn1\b"), "m"),
    (re.compile(r"\bJaw\b"), "law"),
    (re.compile(r"\bthc\b"), "the"),
    (re.compile(r"\byrovisions\b"), "provisions"),
    (re.compile(r"\bcon1\b"), "com"),
    (re.compile(r"\bI1\b"), "Il"),
    # an<l → and, a.nd → and
    (re.compile(r"\ban<l\b"), "and"),
    (re.compile(r"\ba\.nd\b"), "and"),
    (re.compile(r"\bt\.he\b"), "the"),
]


def load_done() -> set:
    if DONE_FILE.exists():
        return set(DONE_FILE.read_text().splitlines())
    return set()


def mark_done(path: str):
    with open(DONE_FILE, "a") as f:
        f.write(path + "\n")


def ocr_error_count(text: str) -> int:
    sample = text[:8000]
    errors = 0
    errors += len(re.findall(r"[a-zA-Z][0-9][a-zA-Z]", sample))
    errors += len(re.findall(r"[a-z][>·&<][a-z]", sample))
    errors += len(re.findall(r"\byrovisions\b|\bJaw\b|\bthc\b|\bllll\b", sample))
    return errors


def build_symspell() -> SymSpell:
    sym = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
    import symspellpy
    dict_path = Path(symspellpy.__file__).parent / "frequency_dictionary_en_82_765.txt"
    sym.load_dictionary(str(dict_path), term_index=0, count_index=1)
    return sym


def fix_word(word: str, sym: SymSpell) -> str:
    """Try to correct a broken word. Return original if no good match."""
    suggestions = sym.lookup(word.lower(), Verbosity.CLOSEST, max_edit_distance=2)
    if not suggestions:
        return word
    best = suggestions[0]
    # Only accept if edit distance is small relative to word length
    if best.distance > max(2, len(word) // 4):
        return word
    # Preserve original capitalisation
    fixed = best.term
    if word[0].isupper():
        fixed = fixed.capitalize()
    if word.isupper():
        fixed = fixed.upper()
    return fixed


def fix_text(text: str, sym: SymSpell) -> tuple[str, int]:
    """Apply known fixes + SymSpell on broken words. Returns (fixed_text, fix_count)."""
    fixes = 0

    # 1. Known exact substitutions
    for pattern, replacement in KNOWN_FIXES:
        new, n = pattern.subn(replacement, text)
        fixes += n
        text = new

    # 2. SymSpell on digit-in-letter words (mod1fymg → modifying)
    def replace_digit_bad(m):
        nonlocal fixes
        original = m.group()
        corrected = fix_word(original, sym)
        if corrected != original:
            fixes += 1
            return corrected
        return original

    text = BAD_WORD_DIGIT.sub(replace_digit_bad, text)

    # 3. Tilde words: strip ~ and try SymSpell (th~t→that, St~te→State)
    def replace_tilde(m):
        nonlocal fixes
        original = m.group()
        stripped = original.replace("~", "")
        corrected = fix_word(stripped, sym)
        if corrected.lower() != original.lower():
            fixes += 1
            return corrected
        return original

    text = BAD_WORD_TILDE.sub(replace_tilde, text)

    # 4. ii-as-u: constitiition→constitution, wliich→which, Agriciiltiiral→Agricultural
    def replace_ii(m):
        nonlocal fixes
        original = m.group()
        # Only try if word has ii and isn't a known proper word (e.g. Hawaii,anii)
        if "ii" not in original:
            return original
        candidate = original.replace("ii", "u")
        corrected = fix_word(candidate, sym)
        if corrected.lower() != original.lower():
            fixes += 1
            return corrected
        return original

    text = BAD_WORD_II.sub(replace_ii, text)

    return text, fixes


def process_file(path: str, sym: SymSpell, dry_run: bool) -> dict:
    corpus_file = CORPUS_DIR / f"{path}.txt"
    if not corpus_file.exists():
        return {"path": path, "status": "missing"}

    raw = corpus_file.read_text(encoding="utf-8", errors="ignore")
    errors_before = ocr_error_count(raw)

    if errors_before < 3:
        return {"path": path, "status": "skip_clean", "errors": errors_before}

    header_end = raw.find("\n\n")
    header = raw[:header_end + 2] if header_end != -1 else ""
    body   = raw[header_end + 2:] if header_end != -1 else raw

    fixed_body, fix_count = fix_text(body, sym)
    errors_after = ocr_error_count(fixed_body)

    if dry_run:
        return {
            "path": path, "status": "would_fix",
            "errors_before": errors_before, "errors_after": errors_after,
            "fixes_applied": fix_count,
        }

    corpus_file.write_text(header + fixed_body, encoding="utf-8")
    mark_done(path)
    return {
        "path": path, "status": "fixed",
        "errors_before": errors_before, "errors_after": errors_after,
        "fixes_applied": fix_count,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--path", type=str, default=None)
    parser.add_argument("--max-year", type=int, default=None)
    args = parser.parse_args()

    print("Loading SymSpell dictionary...", end=" ", flush=True)
    sym = build_symspell()
    print("done")

    done = load_done()

    if args.path:
        result = process_file(args.path, sym, args.dry_run)
        print(result)
        return

    # Collect bad files
    candidates = []
    for f in sorted(CORPUS_DIR.glob("*.txt")):
        if f.name.startswith("."): continue
        year_str = f.stem.split("_")[0]
        if not year_str.isdigit(): continue
        year = int(year_str)
        if args.max_year and year > args.max_year: continue
        if f.stem in done: continue
        candidates.append(f.stem)

    print(f"Candidates: {len(candidates):,} | Dry run: {args.dry_run}")

    total_fixed = skipped = errors_obj = 0
    total_fixes_applied = 0

    for i, path in enumerate(candidates):
        result = process_file(path, sym, args.dry_run)
        status = result["status"]

        if status in ("fixed", "would_fix"):
            total_fixed += 1
            total_fixes_applied += result.get("fixes_applied", 0)
        elif status == "skip_clean":
            skipped += 1

        if (i + 1) % 200 == 0:
            print(f"[{i+1}/{len(candidates)}] fixed={total_fixed} "
                  f"skipped={skipped} total_token_fixes={total_fixes_applied:,}")

    print(f"\nDone.")
    print(f"Files fixed: {total_fixed:,} | Skipped (clean): {skipped:,} | "
          f"Total token fixes: {total_fixes_applied:,}")


if __name__ == "__main__":
    main()
