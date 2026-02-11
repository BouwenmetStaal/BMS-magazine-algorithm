# -*- coding: utf-8 -*-
"""
bms_run_extraction.py

Batch runner / orchestrator for Bouwen met Staal magazine extraction.

Role in the 3-script pipeline:
  1) bms_toc.py
     - Finds the TOC page and parses issue/article metadata into a Magazine object.
  2) bms_article_text.py
     - Extracts article content (intro/subheadings/body) and cleans it (headers + hyphens + reflow).
     - Enriches ArticleInfo with start/end page ranges.
  3) bms_run_extraction.py (this file)
     - Loops over PDFs, runs the extraction pipeline, and writes standardized outputs per article.
     - Produces compact per-issue logging: one DONE line + a warning overview (no per-article OK spam).

Current output (intermediate format):
  - One UTF-8 .txt file per article, stored in:
        C:\\BMS_C_Locatie\\AI_Project\\XML_test\\<issue>_articles_txt\\
  - Each file contains:
        (A) a metadata header (title, section, authors, page ranges)
        (B) the cleaned article text

End goal (planned next step):
  - Replace the TXT writer with an XML writer that emits BRIS-ready XML files.
  - The runner stays the integration point, while TOC and text logic remain reusable modules.
"""


from __future__ import annotations

from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from bms_toc import build_magazine_from_pdf, ArticleInfo, Magazine
from bms_article_text import extract_article_text_plain
import json
import pandas as pd
import re


# ---------------------------------------------------------------------------
# Helper: Clean illegal characters for Excel
# ---------------------------------------------------------------------------


def sanitize_for_excel(value):
    """
    Remove illegal characters that Excel cannot handle in worksheet cells.
    Openpyxl raises IllegalCharacterError for control characters (0x00-0x1F except tab, newline, carriage return).
    """
    if value is None:
        return value
    if isinstance(value, str):
        # Remove control characters except tab (0x09), newline (0x0A), and carriage return (0x0D)
        return re.sub(r"[\x00-\x08\x0B-\x0C\x0E-\x1F]", "", value)
    if isinstance(value, list):
        return [sanitize_for_excel(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Helper: build the metadata header (same content as the old single-article
# runner, just factored into a reusable function for the batch loop).
# ---------------------------------------------------------------------------


def build_article_metadata(magazine: Magazine, article: ArticleInfo) -> str:
    """
    Build the human-readable metadata header for one article.

    This reproduces the behaviour/content of the previous runner:
    - Title / Subtitle / Section / Author(s)
    - Edition and Release
    - Printed page range
    - PDF page range (index and 1-based display)


    Inputs:
      magazine: issue-level metadata and printed->PDF mapping
      article: article-level metadata + extracted page range fields (if available)

    Output:
      A multi-line UTF-8 string containing human-readable metadata.

    """
    # Printed start page (from TOC)
    printed_start = article.page

    # Start PDF index: from the extractor (fallback to offset if needed)
    if article.start_page_pdf is not None:
        pdf_start_index = article.start_page_pdf
    else:
        if printed_start is None or magazine.pdf_index_offset is None:
            raise ValueError(
                f"BMS-{magazine.issue_number}, Cannot determine PDF start index for article."
            )
        pdf_start_index = printed_start + magazine.pdf_index_offset

    # End PDF index: from extractor, fallback to start if missing
    if article.end_page_pdf is not None:
        pdf_end_index = article.end_page_pdf
    else:
        pdf_end_index = pdf_start_index

    # Printed end page: from extractor, fallback to start/offset if missing
    if article.end_page is not None:
        printed_end = article.end_page
    else:
        if magazine.pdf_index_offset is not None and printed_start is not None:
            printed_end = pdf_end_index - magazine.pdf_index_offset
        else:
            printed_end = printed_start

    # Human-readable (1-based) PDF page numbers
    pdf_start_display = pdf_start_index + 1
    pdf_end_display = pdf_end_index + 1

    # Build metadata lines (same structure as before)
    metadata_lines = []
    metadata_lines.append("========== ARTICLE METADATA ==========")
    metadata_lines.append(f"Title          : {article.chapot}")

    if article.title:
        metadata_lines.append(f"Subtitle       : {article.title}")

    metadata_lines.append(f"Section        : {article.section}")

    if getattr(article, "authors", None):
        metadata_lines.append("Authors        : " + ", ".join(article.authors))
    elif getattr(article, "author", ""):
        metadata_lines.append("Author         : " + article.author)

    metadata_lines.append(f"Edition        : Bouwen met Staal {magazine.issue_number}")
    metadata_lines.append(
        f"Release        : {magazine.release_month}-{magazine.release_year}"
    )

    # Printed page range
    if printed_start is not None:
        if printed_end is not None and printed_end != printed_start:
            metadata_lines.append(f"Printed pages  : {printed_start}–{printed_end}")
        else:
            metadata_lines.append(f"Printed pages  : {printed_start}")
    else:
        metadata_lines.append("Printed pages  : (unknown)")

    # PDF page range (both index and 1-based display)
    if pdf_start_index is not None:
        if pdf_end_index != pdf_start_index:
            metadata_lines.append(
                f"PDF pages      : index {pdf_start_index}–{pdf_end_index} "
                f"(PDF {pdf_start_display}–{pdf_end_display})"
            )
        else:
            metadata_lines.append(
                f"PDF pages      : index {pdf_start_index} "
                f"(PDF {pdf_start_display})"
            )
    else:
        metadata_lines.append("PDF pages      : (unknown)")

    metadata_lines.append("======================================")
    metadata_lines.append("")  # blank line before article text

    return "\n".join(metadata_lines)


# ---------------------------------------------------------------------------
# Helper: determine output folder name for a magazine
# ---------------------------------------------------------------------------


def get_issue_folder_name(magazine: Magazine, pdf_path: Path) -> str:
    """
    Determine the output subfolder name for one issue.

    Preferred:
      '<xxx>_articles_txt' where xxx is the 3-digit issue number (e.g. 305_articles_txt)

    Fallback:
      '<pdf_stem>_articles_txt' when the issue number could not be parsed.

    Inputs:
      magazine: used for issue_number
      pdf_path: used for fallback naming

    Output:
      Folder name (not a full path).
    """

    issue_number: Optional[int] = magazine.issue_number

    if issue_number is not None:
        issue_str = f"{issue_number:03d}"
        return f"{issue_str}_articles_txt"
    else:
        # Fallback: use the filename stem and notify via print in caller.
        return f"{pdf_path.stem}_articles_txt"


# ---------------------------------------------------------------------------
# Helper: Check low hyphenation, indicating errors in text
# ---------------------------------------------------------------------------


def check_low_hyphenation_signal(
    text: str,
    min_chars: int = 2000,
    min_hyphens_per_1000: float = 1.0,
) -> tuple[bool, int, int, float]:
    """
    Quality signal used by the runner to flag suspicious extractions.

    Why:
      - Articles normally contain a baseline amount of hyphen characters.
      - Unusually low hyphen density can indicate extraction issues
        (missed hyphen joins, column order problems, skipped content).

    Inputs:
      text: final extracted article text
      thresholds: only evaluate sufficiently long texts

    Output:
      (is_low, hyphen_count, total_chars, hyphens_per_1000)
      Used to build a compact per-issue warning overview.
    """
    if not text:
        return False, 0, 0, 0.0

    total_chars = len(text)
    if total_chars < min_chars:
        return False, 0, total_chars, 0.0

    hyphen_chars = [
        "-",  # hyphen-minus
        "\u2011",  # non-breaking hyphen
        "\u00ad",  # soft hyphen
        "–",  # en dash (sometimes used as hyphen)
    ]

    hyphen_count = sum(text.count(h) for h in hyphen_chars)
    hyphens_per_1000 = (hyphen_count / total_chars) * 1000.0

    is_low = hyphens_per_1000 < min_hyphens_per_1000
    return is_low, hyphen_count, total_chars, hyphens_per_1000


# ---------------------------------------------------------------------------
# Process a single magazine PDF
# ---------------------------------------------------------------------------


def process_magazine_pdf(pdf_path: Path, base_output_dir: Path) -> pd.DataFrame:
    """
    Open one magazine PDF, parse its TOC and articles, and write one TXT
    file per article into a dedicated issue subfolder.

    Output behaviour:
      - No per-article '[OK]' prints.
      - One summary line per PDF.
      - A compact warning overview for articles with suspiciously low hyphenation.
    """
    print(f"[INFO] Processing PDF: {pdf_path.name}")

    status_dataframe = []
    if not pdf_path.is_file():
        print(f"[ERROR] File not found, skipping: {pdf_path}")
        return

    # Open PDF
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"[ERROR] Could not open PDF '{pdf_path.name}': {e}")
        return

    # Build magazine structure (TOC + article list)
    try:
        magazine = build_magazine_from_pdf(doc)
    except ValueError as e:
        print(f"[SKIP] {pdf_path.name}: {e}")
        return

    if not magazine.articles:
        print(f"[WARN] No articles detected in '{pdf_path.name}'. Nothing to export.")
        return

    # Determine issue folder name (and warn if we had to fall back)
    issue_folder_name = get_issue_folder_name(magazine, pdf_path)
    if magazine.issue_number is None:
        print(
            f"[WARN] Issue number not detected for '{pdf_path.name}'. "
            f"Using folder name '{issue_folder_name}'."
        )

    # Create issue-specific output directory
    issue_output_dir = base_output_dir / issue_folder_name
    issue_output_dir.mkdir(parents=True, exist_ok=True)

    exported_count = 0
    low_hyphenation_hits: list[dict] = []

    # Build magazine-level JSON metadata
    magazine_json = {
        "issue_number": magazine.issue_number,
        "release_month": magazine.release_month,
        "release_year": magazine.release_year,
        "pdf_index_offset": magazine.pdf_index_offset,
        "total_articles": len(magazine.articles),
        "articles": [],
    }

    # Loop over all articles and export each to a TXT file
    for idx, article in enumerate(magazine.articles, start=1):
        # Extract article text (this also fills start/end PDF page info)
        article_text = extract_article_text_plain(doc, magazine, article)

        # Runner-level warning signal (so we can summarize per issue)
        is_low, hy_count, total_chars, hy_per_1000 = check_low_hyphenation_signal(
            article_text,
            min_chars=2000,
            min_hyphens_per_1000=1.0,
        )
        if is_low:
            low_hyphenation_hits.append(
                {
                    "idx": idx,
                    "chapot": article.chapot,
                    "title": article.title,
                    "hy_count": hy_count,
                    "total_chars": total_chars,
                    "hy_per_1000": hy_per_1000,
                }
            )

        # Build metadata header
        metadata_text = build_article_metadata(magazine, article)
        full_output = metadata_text + article_text

        # File name pattern: <issue>_article_<nn>.txt and .json
        issue_number = magazine.issue_number
        if issue_number is not None:
            issue_str = f"{issue_number:03d}"
        else:
            issue_str = pdf_path.stem

        article_filename = f"{issue_str}_article_{idx:02d}.txt"
        output_path = issue_output_dir / article_filename

        # Write TXT output
        output_path.write_text(full_output, encoding="utf-8")

        # Write JSON output

        json_data = {
            "metadata": {
                "chapot": article.chapot,
                "title": article.title,
                "section": article.section,
                "authors": getattr(article, "authors", None)
                or (
                    [getattr(article, "author", "")]
                    if getattr(article, "author", "")
                    else []
                ),
                "edition": f"Bouwen met Staal {magazine.issue_number}",
                "release_month": magazine.release_month,
                "release_year": magazine.release_year,
                "printed_page_start": article.page,
                "printed_page_end": article.end_page,
                "pdf_page_start_index": article.start_page_pdf,
                "pdf_page_end_index": article.end_page_pdf,
            },
            "text": {
                "intro": article.article_text.intro_text,
                "first_paragraph": article.article_text.first_paragraph,
                "paragraphs": [
                    {
                        "header": pt.header,
                        "text": pt.text,
                    }
                    for pt in article.article_text.paragraph_texts
                ],
            },
        }

        # Add article to magazine JSON
        magazine_json["articles"].append(json_data)

        exported_count += 1

        status_dataframe.append(
            {
                "edition": f"Bouwen met Staal {magazine.issue_number}",
                "chapot": (
                    article.chapot.replace("\x07", "")
                    if article.chapot
                    else article.chapot
                ),
                "title": (
                    article.title.replace("\x07", "")
                    if article.title
                    else article.title
                ),
                "section": (
                    article.section.replace("\x07", "")
                    if article.section
                    else article.section
                ),
                "authors": [
                    author.replace("\x07", "")
                    for author in (
                        getattr(article, "authors", None)
                        or (
                            [getattr(article, "author", "")]
                            if getattr(article, "author", "")
                            else []
                        )
                    )
                ],
                "hyphens_per_1000": hy_per_1000,
            }
        )

    # Save magazine-level JSON after processing all articles
    magazine_json_filename = f"{issue_str}_magazine.json"
    magazine_json_path = issue_output_dir / magazine_json_filename
    magazine_json_path.write_text(
        json.dumps(magazine_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Summary per PDF
    issue_str = (
        f"{magazine.issue_number}"
        if magazine.issue_number is not None
        else pdf_path.stem
    )
    warn_count = len(low_hyphenation_hits)

    if warn_count == 0:
        print(
            f"[DONE] Issue {issue_str}: exported {exported_count} article(s). No hyphenation warnings."
        )
    else:
        print(
            f"[DONE] Issue {issue_str}: exported {exported_count} article(s). "
            f"Hyphenation warnings: {warn_count} article(s)."
        )
        for hit in low_hyphenation_hits:
            print(
                f"       - Article {hit['idx']:02d}: {hit['title']} "
                f"(Warning: {hit['hy_per_1000']:.2f} hyphens per 1000 chars.)"
            )

    return pd.DataFrame(status_dataframe)


# ---------------------------------------------------------------------------
# Main entry point: batch over all PDFs in the magazine directory
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Batch runner:

    - Looks for all PDF files in
        C:\\BMS_C_Locatie\\AI_Project\\Magazine_test_location
    - For each PDF, calls process_magazine_pdf().
    - Outputs TXT files under:
        C:\\BMS_C_Locatie\\AI_Project\\XML_test\\...
    """

    # ------------------ Locatie aanpassen --------------------------

    magazine_dir = Path(
        r"C:\Users\AJOR\Bouwen met Staal\ChatBmS - General\Archief_BMS_magazines [Erik]\Magazine_compleet_archief\2025 (303-308)"
    )  # locatie Aanpassen naar eigen voorkeur
    # Get all subdirectories in the archive folder
    archive_root = Path(
        r"C:\Users\AJOR\Bouwen met Staal\ChatBmS - General\Archief_BMS_magazines [Erik]\Magazine_compleet_archief"
    )
    year_folders = sorted(
        [d for d in archive_root.iterdir() if d.is_dir()], reverse=True
    )

    # Create status tracking dataframe
    status_data_all = pd.DataFrame()

    # For testing, process only the latest year folder
    year_folders = year_folders[:3]

    # Process each year folder
    for magazine_dir in year_folders:
        print(f"\n[INFO] Processing folder: {magazine_dir.name}")
        magazine_dir = archive_root / magazine_dir.name
        base_output_dir = Path(r"C:\Users\AJOR\Documents\BMS_algortime_testfolder")

        # ---------------------------------------------------------------

        # Ensure base output directory exists
        base_output_dir.mkdir(parents=True, exist_ok=True)

        if not magazine_dir.is_dir():
            raise FileNotFoundError(f"Magazine directory not found: {magazine_dir}")

        pdf_files = sorted(magazine_dir.glob("*.pdf"))

        pdf_files = sorted(magazine_dir.glob("*.pdf"), reverse=True)
        if not pdf_files:
            print(f"[INFO] No PDF files found in: {magazine_dir}")
            return

        print(f"[INFO] Found {len(pdf_files)} PDF file(s) to process.\n")

        for pdf_path in pdf_files:
            status_data_magazine = process_magazine_pdf(pdf_path, base_output_dir)
            status_data_all = pd.concat(
                [status_data_all, status_data_magazine], ignore_index=True
            )

        print("\n[INFO] Batch processing finished.")

    # Save status data to Excel
    status_df = pd.DataFrame(status_data_all)

    # Sanitize all string columns to remove illegal characters
    for col in status_df.columns:
        status_df[col] = status_df[col].apply(sanitize_for_excel)

    excel_output_path = base_output_dir / "extraction_status.xlsx"
    status_df.to_excel(excel_output_path, index=False)
    print(f"\n[INFO] Status data saved to: {excel_output_path}")


if __name__ == "__main__":
    main()
