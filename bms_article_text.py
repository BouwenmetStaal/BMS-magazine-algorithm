# -*- coding: utf-8 -*-
"""
bms_article_text.py

Article text extraction for Bouwen met Staal magazines.

This module assumes:
- TOC parsing + Magazine/ArticleInfo definitions live in bms_toc.py
- ArticleInfo.page is the printed start page of the article (int)
- Magazine.pdf_index_offset maps printed pages to PDF indices:
      pdf_index = printed_page + pdf_index_offset

Goal (current version):
- For a given article, read from its start page onward,
  using font-based rules to select intro/body/subheadings,
  and stop when we encounter the '. •' end marker.
- Return the article text as a plain string, preserving reading order.

We keep things line-based for now; paragraph reconstruction and
XML export can be added later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

import fitz  # PyMuPDF

from bms_toc import Magazine, ArticleInfo, build_magazine_from_pdf


# --- basic helpers / constants ------------------------------------------------

RELEVANT_SIZE_MIN = 8.5   # main text ~9 pt
RELEVANT_SIZE_MAX = 9.5
COLUMN_GAP_THRESHOLD = 60.0  # distance in points to separate columns (tweak if needed)


def _norm_space(text: str) -> str:
    """Collapse internal whitespace, keep basic line structure."""
    return " ".join(text.replace("\r", " ").split())


# --- data structures ----------------------------------------------------------


@dataclass
class ArticlePageLine:
    page_index: int
    text: str
    bbox: Tuple[float, float, float, float]
    x_center: float
    y_top: float
    max_font_size: float
    has_univers: bool
    has_minion: bool
    is_bold_like: bool
    spans: List[dict]
    column_index: Optional[int] = None


@dataclass
class ArticleBlock:
    """
    Logical unit in article flow.

    kind:
      - "intro"       : Univers bold 9 before main body
      - "subheading"  : Univers bold 9 after body has started
      - "paragraph"   : Minion 9 lines (for now still line-level)
    """
    kind: str
    text: str
    page: int
    column: int
    order_index: int


# --- mapping: TOC -> PDF page -------------------------------------------------


def compute_pdf_index_for_article(
    magazine: Magazine,
    article: ArticleInfo,
) -> int:
    """
    Compute zero-based PDF index where an article starts.

    pdf_index = article.page + magazine.pdf_index_offset
    """
    if article.page is None:
        raise ValueError(f"Article '{article.title}' has no page number from TOC.")
    if magazine.pdf_index_offset is None:
        raise ValueError("Magazine.pdf_index_offset is None.")

    pdf_index = article.page + magazine.pdf_index_offset
    if pdf_index < 0:
        raise ValueError(
            f"Computed negative pdf_index={pdf_index} "
            f"(page={article.page}, offset={magazine.pdf_index_offset})"
        )
    return pdf_index


def get_article_start_page(
    doc: fitz.Document,
    magazine: Magazine,
    article: ArticleInfo,
) -> fitz.Page:
    """Return the fitz.Page where this article starts."""
    idx = compute_pdf_index_for_article(magazine, article)
    if idx >= len(doc):
        raise ValueError(
            f"pdf_index={idx} out of range for document with {len(doc)} pages."
        )
    return doc[idx]


# --- per-page line extraction -------------------------------------------------


def collect_page_lines(page: fitz.Page, page_index: int) -> List[ArticlePageLine]:
    """
    Extract line-level structures from a single PDF page using get_text('dict').

    We do NOT filter yet; this just turns the PDF structure into Python objects.
    """
    data = page.get_text("dict")
    lines: List[ArticlePageLine] = []

    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:  # text blocks only
            continue

        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue

            raw_text = "".join(s.get("text", "") for s in spans)
            text = _norm_space(raw_text)
            if not text:
                continue

            sizes = [float(s.get("size", 0.0)) for s in spans]
            max_size = max(sizes) if sizes else 0.0

            font_names = [(s.get("font") or "").lower() for s in spans]
            has_univers = any("univers" in f for f in font_names)
            has_minion = any("minion" in f for f in font_names)
            is_bold_like = any(
                ("bold" in f)
                or ("black" in f)
                or ("heavy" in f)
                or ("semibold" in f)
                or ("demi" in f)
                for f in font_names
            )

            xs, ys, xe, ye = [], [], [], []
            for s in spans:
                x0, y0, x1, y1 = s.get("bbox", (0, 0, 0, 0))
                xs.append(x0)
                ys.append(y0)
                xe.append(x1)
                ye.append(y1)
            x0 = min(xs or [0])
            y0 = min(ys or [0])
            x1 = max(xe or [0])
            y1 = max(ye or [0])

            x_center = 0.5 * (x0 + x1)
            y_top = y0

            lines.append(
                ArticlePageLine(
                    page_index=page_index,
                    text=text,
                    bbox=(x0, y0, x1, y1),
                    x_center=x_center,
                    y_top=y_top,
                    max_font_size=max_size,
                    has_univers=has_univers,
                    has_minion=has_minion,
                    is_bold_like=is_bold_like,
                    spans=spans,
                )
            )

    # Stable order: top-to-bottom, left-to-right
    lines.sort(key=lambda ln: (round(ln.y_top, 1), round(ln.x_center, 1)))
    return lines


# --- relevance & column logic -------------------------------------------------


def is_relevant_main_text(line: ArticlePageLine) -> bool:
    """
    Filter lines to those likely part of the article main text.

    Rules (current version):
      - Font size around 9 pt
      - Font family is Minion or Univers
      - Discard everything smaller or larger (titles, subtitles, footnotes, etc.)
    """
    if line.max_font_size < RELEVANT_SIZE_MIN or line.max_font_size > RELEVANT_SIZE_MAX:
        return False
    if not (line.has_univers or line.has_minion):
        return False
    return True


def assign_columns(lines: List[ArticlePageLine]) -> None:
    """
    Group lines into up to 3 columns based on x_center.

    Updated logic:
      - Use only "anchor" lines (reasonably wide) to determine column centers.
      - This prevents short last-lines of a paragraph (like 'oppervlak van 50x50 m.')
        from being mis-detected as a separate column.
    """
    if not lines:
        return

    # 1) Compute line widths
    widths = [ln.bbox[2] - ln.bbox[0] for ln in lines]
    widths_sorted = sorted(widths)
    median_width = widths_sorted[len(widths_sorted) // 2] if widths_sorted else 0.0

    # 2) Choose anchor lines: reasonably wide compared to the median
    #    (e.g. at least 60% of median width)
    width_threshold = 0.6 * median_width
    anchors = [ln for ln in lines if (ln.bbox[2] - ln.bbox[0]) >= width_threshold]

    # Fallback: if somehow no anchors, use all lines
    if not anchors:
        anchors = list(lines)

    # 3) Determine column centers from anchor lines only
    anchors_sorted = sorted(anchors, key=lambda ln: ln.x_center)
    centers: List[float] = []

    for ln in anchors_sorted:
        x = ln.x_center
        if not centers:
            centers.append(x)
        else:
            # new column only if far from all existing centers
            if all(abs(x - c) > COLUMN_GAP_THRESHOLD for c in centers):
                centers.append(x)

    # Safety: cap at 3 columns and ensure at least one center
    if not centers:
        centers = [anchors_sorted[0].x_center]
    centers = centers[:3]

    # 4) Assign each line to the nearest center
    for ln in lines:
        distances = [abs(ln.x_center - c) for c in centers]
        ln.column_index = distances.index(min(distances))



# --- classification & end marker ----------------------------------------------


def classify_line_kind(
    line: ArticlePageLine,
    body_seen: bool,
) -> Optional[str]:
    """
    Classify a relevant line as 'intro', 'subheading', or 'body'.

    Rules (aligned with your latest description):

      - Only called for lines where is_relevant_main_text(line) == True.
      - Main body text  : Minion Pro 9 pt          -> 'body'
      - Intro + headers : Univers LT Std bold 9 pt:
            * FIRST such line before any body     -> 'intro'
            * Any later such line after body_seen -> 'subheading'
      - Everything else in size 9                 -> 'body'

    Notes:
      - We do NOT look at punctuation or word count anymore.
      - We do NOT look at column index here. Column changes do NOT imply new
        paragraphs or headers; they only affect reading order.
    """
    if not is_relevant_main_text(line):
        return None

    # Pure Minion 9 -> body text
    if line.has_minion and not line.has_univers:
        return "body"

    # Univers 9 (bold or not) without Minion mixed in
    if line.has_univers and not line.has_minion:
        if line.is_bold_like:
            # Bold Univers 9: intro before body, subheading after body
            if not body_seen:
                return "intro"
            return "subheading"
        else:
            # Non-bold Univers 9 -> treat as body text
            return "body"

    # Mixed fonts or anything else in 9 pt -> body
    return "body"




def check_end_marker(line: ArticlePageLine) -> Tuple[bool, str]:
    """
    Detect end-of-article pattern '. •' on the same line.

    Conditions:
      - line.text contains a '•'
      - there is at least one '.' before the '•'
      - line does NOT start with '•' (to avoid bullet lists)
    """
    t = line.text
    if "•" not in t:
        return False, t

    if t.lstrip().startswith("•"):
        # likely a bullet list, not end-of-article marker
        return False, t

    bullet_idx = t.rfind("•")
    dot_idx = t.rfind(".")

    if dot_idx == -1 or dot_idx > bullet_idx:
        return False, t

    # keep everything up to the last '.' before the bullet
    trimmed = t[: dot_idx + 1].rstrip()
    return True, trimmed


# --- main extraction logic ----------------------------------------------------


def extract_article_blocks(
    doc: fitz.Document,
    magazine: Magazine,
    article: ArticleInfo,
) -> List[ArticleBlock]:
    """
    Core extractor: walk pages from article start until '. •' marker.

    Returns a list of ArticleBlock instances in reading order.
    """
    start_index = compute_pdf_index_for_article(magazine, article)

    blocks: List[ArticleBlock] = []
    order_index = 0
    body_seen = False
    end_reached = False

    for page_index in range(start_index, len(doc)):
        page = doc[page_index]
        all_lines = collect_page_lines(page, page_index)

        # keep only lines that are potentially part of main text
        candidate_lines = [ln for ln in all_lines if is_relevant_main_text(ln)]
        if not candidate_lines:
            # page without main text; skip but continue
            continue

        assign_columns(candidate_lines)

        # reading order: column 0 top->down, then 1, then 2
        candidate_lines.sort(key=lambda ln: (ln.column_index or 0, ln.y_top))

        for ln in candidate_lines:
            kind = classify_line_kind(ln, body_seen)
            if kind is None:
                continue

            if kind == "body":
                body_seen = True

            is_end, cleaned_text = check_end_marker(ln)

            # map 'body' to 'paragraph' for block kind
            block_kind = "paragraph"
            if kind == "intro":
                block_kind = "intro"
            elif kind == "subheading":
                block_kind = "subheading"

            blocks.append(
                ArticleBlock(
                    kind=block_kind,
                    text=cleaned_text,
                    page=page_index,
                    column=ln.column_index or 0,
                    order_index=order_index,
                )
            )
            order_index += 1

            if is_end:
                end_reached = True
                break

        if end_reached:
            break

    return blocks


# --- rendering to plain text --------------------------------------------------


def render_article_to_text(
    article: ArticleInfo,
    blocks: List[ArticleBlock],
) -> str:
    """
    Render ONLY the article content (intro, subheadings, body text),
    without repeating metadata like title/section/authors.

    The file-level metadata (title, section, edition, etc.) is now handled
    in bms_run_extraction.py, so this function returns just the actual text.

    Layout:
      - intro lines where they belong + a blank line after the intro paragraph
      - subheadings as standalone lines with blank lines around them
      - body paragraphs as line-based text (column line breaks kept for now)
    """

    # Ensure blocks are in reading order
    blocks = sorted(blocks, key=lambda b: b.order_index)

    lines: List[str] = []

    intro_done = False
    intro_present = any(b.kind == "intro" for b in blocks)

    for b in blocks:
        txt = b.text.strip()
        if not txt:
            continue

        if b.kind == "intro":
            # Intro is printed where it appears
            lines.append(txt)
            continue

        # When we see the first non-intro block after intro(s),
        # insert one blank line once.
        if intro_present and not intro_done and b.kind != "intro":
            intro_done = True
            if lines and lines[-1] != "":
                lines.append("")

        if b.kind == "subheading":
            # Blank line before subheading
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(txt)
            # Blank line after subheading
            lines.append("")
        else:
            # Normal paragraph/body line
            lines.append(txt)

    # Remove trailing blank lines
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines).rstrip() + "\n"



def extract_article_text_plain(
    doc: fitz.Document,
    magazine: Magazine,
    article: ArticleInfo,
) -> str:
    """
    High-level helper: extract one article as plain text.
    """
    blocks = extract_article_blocks(doc, magazine, article)
    return render_article_to_text(article, blocks)


# --- convenience for standalone testing --------------------------------------


def main() -> None:
    """
    Debug entrypoint for manual testing:

    python -m bms_article_text path/to/magazine.pdf
    (extracts first article and prints it)
    """
    import sys
    from pathlib import Path

    if len(sys.argv) != 2:
        print("Usage: python -m bms_article_text path/to/magazine.pdf")
        raise SystemExit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.is_file():
        print(f"File not found: {pdf_path}")
        raise SystemExit(1)

    doc = fitz.open(str(pdf_path))
    magazine = build_magazine_from_pdf(doc)

    if not magazine.articles:
        print("Magazine contains no articles.")
        raise SystemExit(1)

    first_article = magazine.articles[0]
    text = extract_article_text_plain(doc, magazine, first_article)

    print(text)


if __name__ == "__main__":
    main()
