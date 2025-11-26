# -*- coding: utf-8 -*-
"""
bms_run_extraction.py

Test runner:
- open a BmS magazine PDF
- build Magazine (TOC)
- extract the SECOND article
- write result to a .txt file
"""

# bms_run_extraction.py

# bms_run_extraction.py

from __future__ import annotations
from pathlib import Path
import fitz

from bms_toc import build_magazine_from_pdf
from bms_article_text import extract_article_text_plain


def main() -> None:
    pdf_path = Path(r"C:\BMS_C_Locatie\AI_Project\305_BmS_magazine_lr_compleet.pdf")
    output_path = pdf_path.with_name("305_second_article.txt")

    # 1. Open PDF
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    doc = fitz.open(str(pdf_path))

    # 2. Build magazine structure (TOC)
    magazine = build_magazine_from_pdf(doc)

    # 3. Select the article to extract (second article)
    article = magazine.articles[1]

    # 4. Compute PDF start page
    printed_page = article.page
    pdf_index = printed_page + magazine.pdf_index_offset
    pdf_page_display = pdf_index + 1   # human-readable

    # 5. Extract the article text
    article_text = extract_article_text_plain(doc, magazine, article)

    # 6. Build metadata section
    metadata_lines = []
    metadata_lines.append("========== ARTICLE METADATA ==========")
    metadata_lines.append(f"Title          : {article.title}")

    if article.subtitle:
        metadata_lines.append(f"Subtitle       : {article.subtitle}")

    metadata_lines.append(f"Section        : {article.section}")

    if article.authors:
        metadata_lines.append("Authors        : " + ", ".join(article.authors))
    elif article.author:
        metadata_lines.append("Author         : " + article.author)

    metadata_lines.append(f"Edition        : Bouwen met Staal {magazine.issue_number}")
    metadata_lines.append(f"Release        : {magazine.release_month}-{magazine.release_year}")
    metadata_lines.append(f"Printed page   : {printed_page}")
    metadata_lines.append(f"PDF page index : {pdf_index} (PDF page {pdf_page_display})")
    metadata_lines.append("======================================")
    metadata_lines.append("")  # blank line before article text

    metadata_text = "\n".join(metadata_lines)

    # 7. Combine metadata + article text
    full_output = metadata_text + article_text

    # 8. Write to TXT
    output_path.write_text(full_output, encoding="utf-8")

    print(f"Saved output to:\n  {output_path}")


if __name__ == "__main__":
    main()

