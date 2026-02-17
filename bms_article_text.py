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

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple
from enums import TextType
from core.article_text import ArticleText, ParagraphText

import re
import textwrap
import fitz  # PyMuPDF


from bms_toc import Magazine, Article, build_magazine_from_pdf


# --- basic helpers / constants ------------------------------------------------

RELEVANT_SIZE_MIN = 8.5  # main text ~9 pt
RELEVANT_SIZE_MAX = 9.5
COLUMN_GAP_THRESHOLD = 60.0  # distance in points to separate columns (tweak if needed)


def _norm_space(text: str) -> str:
    """Collapse internal whitespace, keep basic line structure."""
    return " ".join(text.replace("\r", " ").split())


# --- data structures ----------------------------------------------------------


@dataclass
class ArticlePageLine:
    """
    One visual line extracted from a PDF page during article extraction.

    Purpose:
      - Acts as the low-level unit for column detection and text classification.

    Key idea:
      - Stores geometry (bbox/x/y) + font signals (Univers/Minion/bold) so later
        logic can decide intro/subheading/body and preserve reading order.
    """

    page_index: int
    text: str
    bbox: Tuple[float, float, float, float]
    x_center: float
    x_left: float
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
    Logical unit in the extracted article flow.

    Attributes:
        texttype: Classification of this block (INTRO, SUBHEADING, or PARAGRAPH)
        text: The actual text content of this block
        page: PDF page index where this block was found
        column: Column index (0-2) within the page
        order_index: Sequential position in the article's reading flow

    Purpose:
        Represents a classified segment of article text with metadata needed
        for stable reading order and downstream post-processing (header merging,
        hyphen fixes, paragraph reflow).
    """

    texttype: TextType
    text: str
    page: int
    column: int
    order_index: int


# --- mapping: TOC -> PDF page -------------------------------------------------


def compute_pdf_index_for_article(magazine: Magazine, article: Article) -> int:
    """
    Map an article's printed start page (TOC) to a 0-based PDF page index.

    Formula:
      pdf_index = article.page + magazine.pdf_index_offset

    Inputs:
      magazine: contains pdf_index_offset
      article: contains printed start page (article.page)

    Output:
      0-based PDF page index where the article starts.

    Raises:
      ValueError if required fields are missing or the index is invalid.
    """

    if article.page is None:
        # raise ValueError(f"BMS-{magazine.issue_number}, Article '{article.title}' has no page number from TOC.")
        print(
            f"[ERROR] BMS-{magazine.issue_number}, Article '{article.chapot}' has no page number from TOC."
        )
        return -1
    if magazine.pdf_index_offset is None:
        raise ValueError(
            f"BMS-{magazine.issue_number}, Magazine.pdf_index_offset is None."
        )

    pdf_index = article.page + magazine.pdf_index_offset
    if pdf_index < 0:
        raise ValueError(
            f"BMS-{magazine.issue_number}, Computed negative pdf_index={pdf_index} "
            f"(page={article.page}, offset={magazine.pdf_index_offset})"
        )
    return pdf_index


def get_article_start_page(
    doc: fitz.Document, magazine: Magazine, article: Article
) -> fitz.Page:
    """
    Convenience wrapper returning the fitz.Page where an article starts.

    Inputs:
      doc: open PyMuPDF document
      magazine/article: used to compute start PDF index

    Output:
      fitz.Page for the computed start index.

    Raises:
      ValueError if the index is out of bounds or metadata is missing.
    """

    idx = compute_pdf_index_for_article(magazine, article)
    if idx >= len(doc):
        raise ValueError(
            f"BMS-{magazine.issue_number}, pdf_index={idx} out of range for document with {len(doc)} pages."
        )
    return doc[idx]


# --- per-page line extraction -------------------------------------------------


def collect_page_lines(page: fitz.Page, page_index: int) -> List[ArticlePageLine]:
    """
    Convert one PDF page into a list of ArticlePageLine objects.

    What it does:
      - Reads PyMuPDF text structure (get_text('dict'))
      - Builds line objects with text, bbox, and font signals
      - Sorts top-to-bottom then left-to-right (stable baseline order)

    Inputs:
      page: fitz.Page to parse
      page_index: 0-based index for bookkeeping/debug

    Output:
      List of ArticlePageLine for that page (no filtering yet).
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
            x_left = x0
            y_top = y0

            lines.append(
                ArticlePageLine(
                    page_index=page_index,
                    text=text,
                    bbox=(x0, y0, x1, y1),
                    x_center=x_center,
                    x_left=x_left,
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
    Decide whether a line is likely part of the article's main reading flow.

    Current heuristic:
      - Size approximately 9pt
      - Font family Minion or Univers

    Input:
      line: ArticlePageLine

    Output:
      True if the line should be considered for article extraction.
    """

    if line.max_font_size < RELEVANT_SIZE_MIN or line.max_font_size > RELEVANT_SIZE_MAX:
        return False
    if not (line.has_univers or line.has_minion):
        return False
    return True


def assign_columns(lines: List[ArticlePageLine]) -> None:
    """
    Assign a column index (0..2) to each line based on x-position clustering.

    Why:
      - Articles are laid out in columns; we need column order + within-column order
        to reconstruct reading flow.

    Input:
      lines: list of relevant ArticlePageLine objects (mutated in-place)

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
    anchors_sorted = sorted(anchors, key=lambda ln: ln.x_left)
    lefts: List[float] = []

    for ln in anchors_sorted:
        x = ln.x_left
        if not lefts:
            lefts.append(x)
        else:
            # new column only if far from all existing centers
            if all(abs(x - c) > COLUMN_GAP_THRESHOLD for c in lefts):
                lefts.append(x)

    # Safety: cap at 3 columns and ensure at least one center
    if not lefts:
        lefts = [anchors_sorted[0].x_left]
    lefts = lefts[:3]

    # 4) Assign each line to the nearest center
    for ln in lines:
        distances = [abs(ln.x_left - c) for c in lefts]
        ln.column_index = distances.index(min(distances))


# --- classification & end marker ----------------------------------------------


def determine_line_texttype(
    line: ArticlePageLine, body_seen: bool
) -> Optional[TextType]:
    """
    Classify a relevant line into the article structure: intro, subheading, or body.

    High-level rule:
      - Minion ~9pt -> body
      - Univers ~9pt bold:
          * before any body: intro
          * after body started: subheading

    Inputs:
      line: candidate line (expected relevant)
      body_seen: whether body text has already started

    Output:
      TextType.INTRO | TextType.SUBHEADING | TextType.BODY or None if not relevant.
    """

    if not is_relevant_main_text(line):
        return None

    # Pure Minion 9 -> body text
    if line.has_minion and not line.has_univers:
        return TextType.BODY

    # Univers 9 (bold or not) without Minion mixed in
    if line.has_univers and not line.has_minion:
        if line.is_bold_like:
            # Bold Univers 9: intro before body, subheading after body
            if not body_seen:
                return TextType.INTRO
            return TextType.SUBHEADING
        else:
            # Non-bold Univers 9 -> treat as body text
            return TextType.BODY
    # Mixed fonts or anything else in 9 pt -> body
    return TextType.BODY


def check_end_marker(line: ArticlePageLine) -> Tuple[bool, str]:
    """
    Detect the end-of-article marker within a line and strip it for output.

    Convention in these magazines:
      - Articles end with a bullet marker like '. •' (with optional closing quotes)

    Input:
      line: ArticlePageLine (text inspected)

    Output:
      (is_end, cleaned_text)
        - is_end: True if this line marks end of the article
        - cleaned_text: text with the bullet marker and any trailing content removed
    """

    t = line.text
    if "•" not in t:
        return False, t

    # Avoid bullet lists: line that starts with a bullet is probably not an end marker
    if t.lstrip().startswith("•"):
        return False, t

    bullet_idx = t.rfind("•")
    if bullet_idx == -1:
        return False, t

    # Walk backwards from the bullet:
    #  - skip whitespace
    #  - then optionally consume closing quotes
    #  - require a '.' before those quotes
    j = bullet_idx - 1

    # Skip whitespace before the bullet
    while j >= 0 and t[j].isspace():
        j -= 1
    if j < 0:
        return False, t

    # Optional closing quotes between '.' and bullet
    closing_quotes = ["'", "’", '"', "»", "”"]
    quote_end = j
    while j >= 0 and t[j] in closing_quotes:
        j -= 1

    # Now t[j] should be the '.', otherwise this is not an end marker
    if j < 0 or t[j] != ".":
        return False, t

    # We want to keep:
    #   everything up to and including the '.' and any closing quotes,
    #   but remove the bullet and anything after it.
    # 'quote_end' is the last non-space, non-bullet character before the bullet.
    end_pos = quote_end

    trimmed = t[: end_pos + 1].rstrip()
    return True, trimmed


# --- main extraction logic ----------------------------------------------------


def extract_article_blocks(
    doc: fitz.Document, magazine: Magazine, article: Article
) -> Tuple[List[ArticleBlock], int]:
    """
    Extract an article as an ordered stream of ArticleBlock objects.

    What it does:
      - Starts at the article's computed start page
      - Walks forward page-by-page
      - Collects relevant lines, assigns columns, classifies intro/subheading/body
      - Stops when the end marker is detected

    Inputs:
      doc: open PyMuPDF document
      magazine/article: used for page mapping and metadata

    Outputs:
      (blocks, last_page_with_content_pdf_index)

    Side effects:
      - Prints a warning if no end marker is found (quality signal).
    """

    start_index = compute_pdf_index_for_article(magazine, article)

    blocks: List[ArticleBlock] = []
    order_index = 0
    body_seen = False
    end_reached = False

    # Track the last PDF page where we actually appended article blocks
    last_page_with_content: Optional[int] = None

    for page_index in range(start_index, len(doc)):
        if "Luchtkasteel met perspectief" in article.title:
            pass
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
            texttype = determine_line_texttype(ln, body_seen)
            if texttype is None:
                continue

            if texttype is TextType.BODY:
                body_seen = True

            is_end, cleaned_text = check_end_marker(ln)

            # map 'body' to 'paragraph' for block textype
            block_texttype = TextType.PARAGRAPH  # DEFAULT
            if texttype is TextType.INTRO:
                block_texttype = TextType.INTRO
            elif texttype is TextType.SUBHEADING:
                block_texttype = TextType.SUBHEADING
            # This page definitely contains article content
            last_page_with_content = page_index

            blocks.append(
                ArticleBlock(
                    texttype=block_texttype,
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

    # If we never found any content (very unlikely), fall back to start_index
    if last_page_with_content is None:
        last_page_with_content = start_index

    # NEW: warn if no explicit end marker was found for this article
    if not end_reached:
        # We keep the collected blocks (article content) but notify via stdout.
        print(
            f"[WARN] No end-of-article marker ('•') found for article "
            f"'{article.chapot}' (start page {article.page}, "
            f"issue {magazine.issue_number}). "
            f"Last page with content: PDF index {last_page_with_content} "
            f"(PDF page {last_page_with_content + 1})."
        )

    return blocks, last_page_with_content


# --- Removing Hyphened text --------------------------------------------------
def fix_hyphenation_across_block_breaks(
    blocks: List[ArticleBlock],
) -> List[ArticleBlock]:
    """
    Fix word breaks caused by line/column breaks *between consecutive blocks*.

    Only merges when:
      - consecutive blocks are same texttype (intro/subheading/paragraph)
      - first ends with a hyphen-like character
      - next starts with letters

    Input:
      blocks: ArticleBlock stream in reading order

    Output:
      New list of blocks with safe cross-break hyphenation repaired.
    """

    import re

    if not blocks:
        return blocks

    # Hyphen characters that often appear in PDFs
    hyphen_chars = r"\-\u00AD\u2010\u2011\u2012\u2013\u2014"  # - soft hyphen ‐ - ‒ – —

    # We only merge inside the same logical flow type to avoid cross-type surprises
    mergeable_texttypes = {TextType.PARAGRAPH, TextType.INTRO, TextType.SUBHEADING}

    # Capture: (prefix)(letters)(hyphen-like)(optional trailing junk)
    # We require the hyphen-like char to be at the *end* of the block text.
    re_end_hyphen = re.compile(
        rf"^(?P<prefix>.*?)(?P<left>[A-Za-zÀ-ÿ]+)\s*[{hyphen_chars}]\s*$",
        flags=re.UNICODE,
    )

    # Capture: (leading junk)(letters)(rest)
    re_start_letters = re.compile(
        r"^(?P<lead>\s*[^A-Za-zÀ-ÿ]*)(?P<right>[A-Za-zÀ-ÿ]+)(?P<rest>.*)$",
        flags=re.UNICODE,
    )

    fixed: List[ArticleBlock] = []
    i = 0
    n = len(blocks)

    while i < n:
        current = blocks[i]

        # Default: keep current block
        if (
            current.texttype in mergeable_texttypes
            and i + 1 < n
            and blocks[i + 1].texttype is current.texttype
        ):
            nxt = blocks[i + 1]

            cur_text = (current.text or "").rstrip()
            nxt_text = nxt.text or ""

            m1 = re_end_hyphen.match(cur_text)
            m2 = re_start_letters.match(nxt_text)

            if m1 and m2:
                # Join letters only across the break
                merged_text = (
                    m1.group("prefix")
                    + m1.group("left")
                    + m2.group("right")
                    + m2.group("rest")
                )

                # Keep a single block; consume the next one
                fixed.append(
                    ArticleBlock(
                        texttype=current.texttype,
                        text=merged_text,
                        page=current.page,
                        column=current.column,
                        order_index=current.order_index,
                    )
                )
                i += 2
                continue

        fixed.append(current)
        i += 1

    return fixed


def _dehyphenate_and_reflow(text: str, width: int = 80) -> str:
    """
    Clean and reflow extracted article text for readable plain-text output.

    Responsibilities:
      - Remove line-break hyphenation inside paragraphs
      - Normalize whitespace artifacts from PDFs
      - Rewrap paragraphs to a fixed width while preserving blank-line boundaries

    Input:
      text: rendered article text with line breaks
      width: target wrap width (characters)

    Output:
      Cleaned, reflowed plain text.
    """

    # Normalize common problematic Unicode whitespace early
    text = (
        text.replace("\u00ad", "")  # soft hyphen
        .replace("\u00a0", " ")  # non-breaking space
        .replace("\u2009", " ")  # thin space
    )

    # Split into paragraphs (blank line = paragraph boundary)
    paragraphs = re.split(r"\n\s*\n", text.strip("\n"))

    processed_paragraphs = []

    for para in paragraphs:
        lines = [ln.rstrip() for ln in para.splitlines() if ln.strip()]
        if not lines:
            continue

        buffer = lines[0]

        for next_line in lines[1:]:
            nxt = next_line.lstrip()

            # --- robust hyphenation fix ---
            # Match:
            #   word-ending + hyphen + optional whitespace + word-start
            if re.search(r"[A-Za-zÀ-ÿ]-$", buffer) and re.match(r"[A-Za-zÀ-ÿ]", nxt):
                # Remove trailing hyphen and glue directly
                buffer = buffer[:-1] + nxt
            else:
                # Normal intra-paragraph line break
                buffer = buffer + " " + nxt

        # Normalize internal whitespace
        buffer = re.sub(r"\s+", " ", buffer).strip()

        # Rewrap paragraph
        wrapped = textwrap.fill(buffer, width=width)
        processed_paragraphs.append(wrapped)

    return "\n\n".join(processed_paragraphs) + "\n"


# ------  Multi sub header helper ---------------------------------------------


def merge_multiline_headers(blocks: List[ArticleBlock]) -> List[ArticleBlock]:
    """
    Merge consecutive header blocks that belong to the same printed header.

    Why:
      - In the PDF, long headers can wrap across lines and appear as multiple
        adjacent intro/subheading blocks, which is not meaningful downstream.

    Behavior:
      - Merges adjacent blocks of the same header kind (intro or subheading)
      - Fixes hyphen splits across header lines (e.g. 'Sub-' + 'titel')

    Input:
      blocks: ArticleBlock list in reading order

    Output:
      New list where multi-line headers are represented as single blocks.
    """

    if not blocks:
        return blocks

    header_texttypes = {TextType.INTRO, TextType.SUBHEADING}
    merged: List[ArticleBlock] = []

    i = 0
    n = len(blocks)

    while i < n:
        current = blocks[i]

        # Only merge sequences of the same header kind
        if current.texttype in header_texttypes:
            base_texttype = current.texttype

            # Start merged header text with current block's text
            merged_text = current.text.strip()
            first_block = current

            j = i + 1
            while j < n and blocks[j].texttype is base_texttype:
                next_block = blocks[j]
                next_text = next_block.text.strip()
                if not next_text:
                    j += 1
                    continue

                # Hyphen join logic:
                if merged_text.endswith("-") and next_text and next_text[0].isalpha():
                    # Remove trailing '-' and glue without extra space
                    merged_text = merged_text[:-1] + next_text
                else:
                    # Normal join with space
                    merged_text = merged_text + " " + next_text

                j += 1

            # Create a single merged header block
            new_block = ArticleBlock(
                texttype=base_texttype,
                text=merged_text,
                page=first_block.page,
                column=first_block.column,
                order_index=first_block.order_index,
            )
            merged.append(new_block)

            # Skip all header blocks we just consumed
            i = j
        else:
            # Non-header block: keep as-is
            merged.append(current)
            i += 1

    return merged


# --- rendering to plain text -------------------------------------------------


def render_article_to_text(article: Article, blocks: List[ArticleBlock]) -> str:
    """
    Convert ArticleBlock stream into plain text (content only).

    Output rules:
      - intro lines appear where they occur, followed by a blank line before body
      - subheadings are standalone with blank lines around them
      - paragraphs are emitted line-based (later reflowed by post-processing)

    Inputs:
      article: used for context only (no metadata printed here)
      blocks: extracted blocks in reading order

    Output:
      Plain article text (no metadata header).
    """

    # Ensure blocks are in reading order
    blocks = sorted(blocks, key=lambda b: b.order_index)

    lines: List[str] = []

    intro_done = False
    intro_present = any(b.texttype is TextType.INTRO for b in blocks)

    article_text: ArticleText = None

    intro_text: str = None
    first_paragraph: str = None

    paragraph_header: str = ""
    raw_paragraph_text: str = ""

    paragraph_text_list: List[ParagraphText] = []

    new_paragraph: bool = True

    for b in blocks:
        txt = b.text.strip()
        if not txt:
            continue

        if b.texttype is TextType.INTRO:
            # Intro is printed where it appears
            lines.append(txt)
            intro_text = txt
            continue

        # When we see the first non-intro block after intro(s),
        # insert one blank line once.
        if intro_present and not intro_done and b.texttype is not TextType.INTRO:
            intro_done = True
            if lines and lines[-1] != "":
                lines.append("")

        if b.texttype is TextType.SUBHEADING:
            # Blank line before subheading
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(txt)
            # Blank line after subheading
            lines.append("")
        else:
            # Normal paragraph/body line
            lines.append(txt)

        # AJOR: extra for article text class filling
        if b.texttype is TextType.INTRO:
            intro_text = txt

        if b.texttype is TextType.PARAGRAPH:
            raw_paragraph_text += txt + "\n"
            if b == blocks[-1]:
                paragraph_text_instance = ParagraphText(
                    text=raw_paragraph_text, header=paragraph_header
                )
                paragraph_text_list.append(paragraph_text_instance)

        if b.texttype is TextType.SUBHEADING:
            if new_paragraph is True:
                if first_paragraph is None:
                    first_paragraph = raw_paragraph_text
                    raw_paragraph_text = ""
                    paragraph_header = txt
                else:
                    paragraph_text_instance = ParagraphText(
                        text=raw_paragraph_text, header=paragraph_header
                    )
                    paragraph_text_list.append(paragraph_text_instance)
                    paragraph_header = txt
                    raw_paragraph_text = ""
                    paragraph_text_instance = ""

    # TODO laatste artikel opslaan
    # TODO hoe om te gaan met artikelen waarin deze structuur niet aanwezig is.

    # Remove trailing blank lines
    while lines and lines[-1] == "":
        lines.pop()

    article_text = ArticleText(
        intro_text=intro_text,
        first_paragraph=first_paragraph,
        paragraph_texts=paragraph_text_list,
    )
    article.article_text = article_text

    return "\n".join(lines).rstrip() + "\n"


def extract_article_text_plain(
    doc: fitz.Document, magazine: Magazine, article: Article
) -> str:
    """
    High-level API: extract one article as cleaned plain text.

    Pipeline:
      1) Extract ArticleBlock stream until end marker
      2) Merge multi-line headers
      3) Fix hyphenation across block breaks
      4) Render to text
      5) Dehyphenate + reflow for readability

    Inputs:
      doc: open PyMuPDF document
      magazine/article: mapping + metadata container

    Output:
      Final cleaned plain-text content of the article.

    Side effects:
      - Fills article.start_page_pdf, article.end_page_pdf, article.end_page
      - Prints a warning signal for unusually low hyphenation (quality check)
    """

    # 1) Extract blocks and the PDF index of the last page with content
    blocks, end_page_pdf = extract_article_blocks(doc, magazine, article)

    # 2) Merge multi-line headers (intro/subheading sequences)
    blocks = merge_multiline_headers(blocks)

    # 2b) Fix hyphenation only across line/column breaks (between consecutive blocks)
    blocks = fix_hyphenation_across_block_breaks(blocks)

    # 3) Compute start PDF index using the same mapping as everywhere else
    start_page_pdf = compute_pdf_index_for_article(magazine, article)

    # 4) Derive the printed end page from the PDF index and offset
    end_page_printed: Optional[int] = None
    if magazine.pdf_index_offset is not None:
        end_page_printed = end_page_pdf - magazine.pdf_index_offset

    # 5) Store the range on the article
    article.start_page_pdf = start_page_pdf
    article.end_page_pdf = end_page_pdf
    article.end_page = end_page_printed

    # 6) Render raw text from blocks (still line-based, but with cleaned headers)
    raw_text = render_article_to_text(article, blocks)

    # 7) Post-process: fix hyphenation and reflow paragraphs
    clean_text = _dehyphenate_and_reflow(raw_text, width=80)

    warn_if_unusually_low_hyphenation(
        text=clean_text,
        article_title=article.chapot,
        issue_number=magazine.issue_number,
    )

    return clean_text


def warn_if_unusually_low_hyphenation(
    text: str,
    article_title: str,
    issue_number: int | None,
    min_chars: int = 2000,
    min_hyphens_per_1000: float = 1.0,
) -> None:
    """
    Print a warning if the extracted article text contains an unusually low
    number of hyphen characters relative to its length.

    This is a quality signal only. It does not modify the text.

    Parameters:
      - text: final extracted article text
      - article_title: used in the warning message
      - issue_number: used in the warning message (if available)
      - min_chars: only evaluate texts longer than this
      - min_hyphens_per_1000: warn if hyphens per 1000 chars is below this
    """
    if not text or len(text) < min_chars:
        return

    hyphen_chars = [
        "-",  # hyphen-minus
        "\u2011",  # non-breaking hyphen
        "\u00ad",  # soft hyphen
        "–",  # en dash (sometimes used as hyphen)
    ]

    total_chars = len(text)
    hyphen_count = sum(text.count(h) for h in hyphen_chars)
    hyphens_per_1000 = (hyphen_count / total_chars) * 1000.0

    if hyphens_per_1000 < min_hyphens_per_1000:
        issue_str = f"{issue_number}" if issue_number is not None else "unknown issue"
        print(
            f"[WARN] Low hyphenation detected in article '{article_title}' "
            f"({issue_str}): {hyphen_count} hyphens over {total_chars} chars "
            f"({hyphens_per_1000:.2f} per 1000)."
        )


# --- convenience for standalone testing (NOT needed)--------------------------------------


def main() -> None:
    """
    Manual test entrypoint (not required for batch use).

    Behavior:
      - Opens a PDF path provided via CLI
      - Builds Magazine from TOC
      - Extracts and prints the first article

    Intended for quick local debugging of extraction heuristics.
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
