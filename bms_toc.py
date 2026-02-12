# -*- coding: utf-8 -*-
#!/usr/bin/env python3
import re
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple

import fitz

from core.article_text import ArticleText  # PyMuPDF


# ---------- helpers & dataclasses ----------


def clean_text(t: str) -> str:
    """
    Normalize extracted PDF text into a single readable line.

    Why this exists:
    - PyMuPDF can emit runs with irregular spacing and carriage returns.
    - Downstream parsing (TOC detection, role classification) expects
      stable whitespace.

    Behavior:
    - Replace carriage returns with a space
    - Collapse all whitespace to single spaces
    - Trim leading/trailing spaces
    """
    t = t.replace("\r", " ")
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class TocLine:
    """
    One *visual line* of text extracted from the TOC page.

    This is the raw unit we parse into articles. We store both:
    - the readable text (already whitespace-normalized)
    - its geometry (bbox) so we can:
        * split left vs right column via x position
        * keep reading order (y then x)
    - the underlying spans so we can classify role by font/size.

    Attributes
    ----------
    text:
        Cleaned text of the line (concatenated from spans).
    bbox:
        Bounding box of the full line: (x0, y0, x1, y1) in PDF points.
    spans:
        Raw PyMuPDF span dictionaries for this line (font, size, bbox, etc.).
        Used for font-based role classification (title/subtitle/author).
    page:
        0-based PDF page index where this line was extracted (the TOC page).
    """

    text: str
    bbox: Tuple[float, float, float, float]
    spans: List[Dict[str, Any]]
    page: int

    @property
    def x_center(self) -> float:
        """
        Horizontal midpoint of the line.

        Used for:
        - splitting the TOC into left and right columns
        - secondary sort key for stable reading order
        """
        x0, _, x1, _ = self.bbox
        return 0.5 * (x0 + x1)

    @property
    def y_top(self) -> float:
        """
        Top y-coordinate of the line.

        Used as the primary sort key (top-to-bottom reading order).
        """
        _, y0, _, _ = self.bbox
        return y0


@dataclass
class ArticleInfo:
    """
    Parsed metadata for a single article, primarily from the TOC.

    This object is created in bms_toc.py (TOC parsing) and then *enriched*
    in bms_article_text.py (text extraction) with PDF page range fields.

    Attributes
    ----------
    section:
        Which TOC section the article belongs to. Currently "Projecten" or "Techniek".
    page:
        Printed start page number as shown in the magazine (from TOC).
        Not the PDF index. Mapping to PDF happens via Magazine.pdf_index_offset.
    chapot / title:
        Article chapot and optional title from TOC.
        Multi-line chapots/titles are concatenated during parsing.
    author:
        Raw author line as extracted from TOC (single string).
    authors:
        Parsed list of individual author names (split from `author`).
        Used for cleaner metadata output later.

    Enrichment fields (filled by text extraction)
    ---------------------------------------------
    start_page_pdf:
        0-based PDF index where the article starts.
    end_page:
        Printed end page (inclusive), derived from end_page_pdf and pdf_index_offset.
    end_page_pdf:
        0-based PDF index where the article ends (last page that contained content).
    """

    section: str
    page: Optional[int] = None
    chapot: str = ""
    title: str = ""
    author: str = ""
    authors: List[str] = field(default_factory=list)

    # Filled later by the article text extractor (bms_article_text.py)
    start_page_pdf: Optional[int] = None
    end_page: Optional[int] = None
    end_page_pdf: Optional[int] = None
    article_text: ArticleText = None

    def pretty(self) -> str:
        """
        Human-readable multi-line summary for debugging/logging.

        Note:
        - Prefers `authors` list if available; otherwise falls back to raw `author`.
        """
        out = [f"[{self.section}]"]
        if self.page is not None:
            out.append(f"  Page    : {self.page}")
        out.append(f"  Title   : {self.chapot}")
        if self.title:
            out.append(f"  Subtitle: {self.title}")
        if self.authors:
            out.append(f"  Authors : {', '.join(self.authors)}")
        elif self.author:
            out.append(f"  Author  : {self.author}")
        return "\n".join(out)


@dataclass
class Magazine:
    """
    Container for one full Bouwen met Staal issue (one PDF).

    This is the shared “data contract” between:
    - bms_toc.py (builds the object + TOC-derived metadata)
    - bms_article_text.py (uses pdf_index_offset to map printed pages -> PDF indices
      and fills page ranges per ArticleInfo)
    - bms_run_extraction.py (batch runner exporting one TXT per article)

    Attributes
    ----------
    issue_number:
        Issue number from footer on the TOC page (e.g. 305). Optional because
        footer parsing can fail on malformed scans.
    release_year / release_month:
        Parsed from footer label (e.g. MAART 2025 -> month=3, year=2025).
    original_label:
        Raw month-year label as printed (e.g. 'MAART 2025').
    pdf_index_offset:
        Mapping between printed page numbers and 0-based PDF page indices.

        Definition:
            pdf_index_offset = toc_page_index - toc_printed_page

        So for any printed page P:
            pdf_index = P + pdf_index_offset

        This is the core link that lets the extractor jump to the correct PDF page.
    articles:
        All articles parsed from the TOC (both sections). Each is an ArticleInfo.
    """

    issue_number: Optional[int]
    release_year: Optional[int]
    release_month: Optional[int]
    original_label: Optional[str]
    pdf_index_offset: Optional[int]
    articles: List[ArticleInfo]


MONTHS_NL = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


# ---------- your existing TOC finder (relaxed with left/right) ----------


def find_toc(doc: fitz.Document, left_half_header: str, right_half_header: str) -> int | None:
    """
    Locate the TOC page within a magazine PDF.

    Heuristic:
    - Find a page where 'Projecten' appears in the left half
      and 'Techniek' appears in the right half (same page).

    Input:
      doc: open PyMuPDF document.

    Output:
      0-based PDF page index of the TOC page, or None if not found.
    """
    for pno in range(len(doc)):
        page = doc[pno]
        pw, ph = page.rect.width, page.rect.height

        has_proj_left = False
        has_tech_right = False

        data = page.get_text("dict")

        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:  # text blocks only
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue

                text = clean_text("".join(s.get("text", "") for s in spans))
                if not text:
                    continue

                lower = text.lower()
                contains_proj = left_half_header.lower() in lower
                contains_tech = right_half_header.lower() in lower

                if not (contains_proj or contains_tech):
                    continue

                x0 = min(s["bbox"][0] for s in spans)
                x1 = max(s["bbox"][2] for s in spans)
                x_center = 0.5 * (x0 + x1)

                if contains_proj and x_center <= pw * 0.5:
                    has_proj_left = True
                if contains_tech and x_center >= pw * 0.5:
                    has_tech_right = True

        if has_proj_left and has_tech_right:
            return pno
        
        if has_proj_left and right_half_header == left_half_header: #Edge case for specials e.g. Rotterdam centraal, Utrech centraal, Cargo
            return pno

    return None


# ---------- collect lines on TOC page ----------


def collect_toc_lines(doc: fitz.Document, pno: int) -> List[TocLine]:
    """
    Extract all readable text lines from the TOC page as TocLine objects.

    What it does:
    - Reads PyMuPDF 'dict' text structure for the given page
    - Builds TocLine items with text + bbox + spans (needed for font-based parsing)
    - Sorts lines in a stable reading order (top-to-bottom, then left-to-right)

    Inputs:
      doc: open PyMuPDF document
      pno: 0-based PDF page index (must be the TOC page)

    Output:
      List of TocLine entries for that page.
    """
    page = doc[pno]
    data = page.get_text("dict")
    lines: List[TocLine] = []

    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = clean_text("".join(s.get("text", "") for s in spans))
            if not text:
                continue
            xs, ys, xe, ye = [], [], [], []
            for s in spans:
                x0, y0, x1, y1 = s.get("bbox", (0, 0, 0, 0))
                xs.append(x0)
                ys.append(y0)
                xe.append(x1)
                ye.append(y1)
            bbox = (min(xs or [0]), min(ys or [0]), max(xe or [0]), max(ye or [0]))
            lines.append(TocLine(text=text, bbox=bbox, spans=spans, page=pno))

    lines.sort(key=lambda ln: (round(ln.y_top, 1), round(ln.x_center, 1)))
    return lines


# ---------- find header Y for Projecten / Techniek ----------


def find_header_y(lines: List[TocLine], keyword: str) -> float:
    """
    Find the vertical position (y) of a section header on the TOC page.

    Used to ignore everything above the 'Projecten'/'Techniek' header line when
    parsing the column content below it.

    Inputs:
      lines: TocLine list from the TOC page
      keyword: header keyword to find (case-insensitive)

    Output:
      y_top of the first matching line, or 0.0 if not found.
    """
    ys = [ln.y_top for ln in lines if keyword.lower() in ln.text.lower()]
    return min(ys) if ys else 0.0


# ---------- role classification via font ----------


def classify_role(line: TocLine) -> str:
    """
    Classify a TOC line as 'title', 'subtitle', 'author', or 'other' using fonts.

    Bigger picture:
    - TOC parsing relies on typography: titles/subtitles/authors use different
      fonts and sizes in the magazine layout.
    - This function converts the raw spans of a TocLine into a semantic role.

    Input:
      line: TocLine with span font/size info

    Output:
      One of: 'title', 'subtitle', 'author', 'other'
    """

    is_title = False
    is_subtitle = False
    is_author = False

    for s in line.spans:
        font_name = (s.get("font") or "").lower()
        size = float(s.get("size", 0.0))

        if "univers" in font_name and 9.5 <= size <= 12.5:
            is_title = True
        if "minion" in font_name and 11.5 <= size <= 12.5:
            is_subtitle = True
        if "univers" in font_name and 6.5 <= size <= 8.5:
            is_author = True

    # priority: author > subtitle > title
    if is_author:
        return "author"
    if is_subtitle:
        return "subtitle"
    if is_title:
        return "title"
    return "other"


# ----------- Toegevoegde regels --------


def split_page_prefix(text: str) -> tuple[Optional[int], str]:
    """
    Parse a TOC title line that may start with a printed page number.

    Example:
      '12 Een nieuw project' -> (12, 'Een nieuw project')

    Input:
      text: raw line text from the TOC

    Output:
      (page_number_or_None, remaining_text_without_prefix)
    """
    t = text.replace("\u00a0", " ")
    m = re.match(r"^\s*(\d{1,2})(.*)$", t)
    if m:
        page = int(m.group(1))
        title_rest = m.group(2).lstrip(" .:-–—\t\u00a0").strip()
        return page, title_rest
    return None, text.strip()


def extract_magazine_from_toc(doc, toc_page_index, articles):
    """
    Extract issue-level metadata from the TOC page footer and build a Magazine.

    What it does:
    - Reads the TOC page footer to find:
        * issue number (e.g. 305)
        * release month/year label (e.g. MAART 2025)
        * printed page number shown in the footer row
    - Computes pdf_index_offset to map printed pages -> PDF indices.
    - Returns a Magazine object containing the parsed articles list.

    Inputs:
      doc: open PyMuPDF document
      toc_page_index: 0-based PDF page index where the TOC was found
      articles: list of ArticleInfo already parsed from the TOC columns

    Output:
      Magazine object (with pdf_index_offset possibly None if footer parsing fails).
    """

    page = doc[toc_page_index]
    pw, ph = page.rect.width, page.rect.height
    data = page.get_text("dict")

    # --- collect footer lines (bottom 25% of page) ---
    footer_lines = []
    for block in data.get("blocks", []):
        if block.get("type", 0) != 0:
            continue

        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue

            text = clean_text("".join(s.get("text", "") for s in spans))
            if not text:
                continue

            xs = [s["bbox"][0] for s in spans]
            ys = [s["bbox"][1] for s in spans]
            xe = [s["bbox"][2] for s in spans]
            ye = [s["bbox"][3] for s in spans]

            x0, y0, x1, y1 = min(xs), min(ys), max(xe), max(ye)
            y_center = 0.5 * (y0 + y1)
            x_center = 0.5 * (x0 + x1)

            # Only bottom quarter of page = footer area
            if y_center < ph * 0.75:
                continue

            footer_lines.append(
                {
                    "text": text,
                    "y_center": y_center,
                    "x_center": x_center,
                }
            )

    # Sort bottom-up
    footer_lines.sort(key=lambda d: d["y_center"], reverse=True)

    # --- initialize fields ---
    issue_number = None
    release_month = None
    release_year = None
    original_label = None
    toc_printed_page = None
    pdf_index_offset = None

    target_y = None

    # --- step 1: find the left footer line with "BOUWEN MET STAAL" ---
    for line in footer_lines:
        txt = line["text"]
        x_center = line["x_center"]
        y_center = line["y_center"]
        lower = txt.lower()

        # must be on the left half of page
        if x_center > pw * 0.5:
            continue

        if "bouwen met staal" in lower:
            # match pattern: 'BOUWEN MET STAAL 305 | MAART 2025'
            m = re.search(r"bouwen met staal\s+(\d{1,3})\s*\|\s*(.+)", txt, flags=re.I)
            if m:
                # parse issue number
                try:
                    issue_number = int(m.group(1))
                except:
                    issue_number = None

                # store original label (MAAND JAAR)
                original_label = m.group(2).strip()

                # parse MAAND + JAAR
                parts = original_label.split()
                if len(parts) >= 2:
                    month_name = parts[0].lower()
                    if month_name in MONTHS_NL:
                        release_month = MONTHS_NL[month_name]
                    try:
                        release_year = int(parts[1])
                    except:
                        release_year = None

                target_y = y_center
                break
            #for edition 206,204,203,199,197,196,194 is the pattern 'BOUWEN MET STAAL 266' instead of ''BOUWEN MET STAAL 266 | DECEMBER 2018'
            m = re.search(r"bouwen met staal\s+(\d{1,3})", txt, flags=re.I)
            if m:
                try:
                    issue_number = int(m.group(1))
                except:
                    issue_number = None
                target_y = y_center
                break


    # --- step 2: find printed page number on same row, right side ---
    if target_y is not None:
        tol = 3.0  # y-alignment tolerance
        candidates = [
            line
            for line in footer_lines
            if (line["x_center"] > pw * 0.5)
            and (abs(line["y_center"] - target_y) <= tol)
        ]

        for c in candidates:
            nums = re.findall(r"\b(\d{1,3})\b", c["text"])
            if nums:
                try:
                    toc_printed_page = int(nums[-1])
                    break
                except:
                    continue

    # --- step 3: compute PDF index offset ---
    if toc_printed_page is not None:
        pdf_index_offset = toc_page_index - toc_printed_page


    #if nothing is found try the previous page
    if issue_number is None and release_year is None and release_month is None:
        page = doc[toc_page_index-1]#select the previous page
        pw, ph = page.rect.width, page.rect.height
        data = page.get_text("dict")

        # --- collect footer lines (bottom 25% of page) ---
        footer_lines = []
        for block in data.get("blocks", []):
            if block.get("type", 0) != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue

                text = clean_text("".join(s.get("text", "") for s in spans))
                if not text:
                    continue

                xs = [s["bbox"][0] for s in spans]
                ys = [s["bbox"][1] for s in spans]
                xe = [s["bbox"][2] for s in spans]
                ye = [s["bbox"][3] for s in spans]

                x0, y0, x1, y1 = min(xs), min(ys), max(xe), max(ye)
                y_center = 0.5 * (y0 + y1)
                x_center = 0.5 * (x0 + x1)

                # Only bottom quarter of page = footer area
                if y_center < ph * 0.75:
                    continue

                footer_lines.append(
                    {
                        "text": text,
                        "y_center": y_center,
                        "x_center": x_center,
                    }
                )

        # Sort bottom-up
        footer_lines.sort(key=lambda d: d["y_center"], reverse=True)


        # --- step 1: find the right footer line with "BOUWEN MET STAAL" ---
        for line in footer_lines:
            txt = line["text"]
            x_center = line["x_center"]
            y_center = line["y_center"]
            lower = txt.lower()

            # must be on the right half of page
            if x_center < pw * 0.5:
                continue

            if "bouwen met staal" in lower:
                # match pattern: ' DECEMBER 2018 | BOUWEN MET STAAL 266' and not 'BOUWEN MET STAAL 266 | DECEMBER 2018'
                m = re.search(r"(.+?)\s*\|\s*bouwen met staal\s+(\d{1,3})", txt, flags=re.I)
                if m:
                    # parse issue number
                    try:
                        issue_number = int(m.group(2))
                    except:
                        issue_number = None

                    # store original label (MAAND JAAR)
                    original_label = m.group(1).strip()

                    # parse MAAND + JAAR
                    parts = original_label.split()
                    if len(parts) >= 2:
                        month_name = parts[0].lower()
                        if month_name in MONTHS_NL:
                            release_month = MONTHS_NL[month_name]
                        try:
                            release_year = int(parts[1])
                        except:
                            release_year = None

                    target_y = y_center
                    break

        # --- step 2: find printed page number on same row, left side ---
        if target_y is not None:
            tol = 3.0  # y-alignment tolerance
            candidates = [
                line
                for line in footer_lines
                if (line["x_center"] < pw * 0.5)
                and (abs(line["y_center"] - target_y) <= tol)
            ]

            for c in candidates:
                nums = re.findall(r"\b(\d{1,3})\b", c["text"])
                if nums:
                    try:
                        toc_printed_page = int(nums[-1])+1
                        break
                    except:
                        continue

        # --- step 3: compute PDF index offset ---
        if toc_printed_page is not None:
            pdf_index_offset = toc_page_index - toc_printed_page

    # --- build Magazine object ---
    mag = Magazine(
        issue_number=issue_number,
        release_year=release_year,
        release_month=release_month,
        original_label=original_label,
        pdf_index_offset=pdf_index_offset,
        articles=articles,
    )

    return mag


# ---------- parse a single column into articles ----------


def parse_column(lines: List[TocLine], section: str) -> List[ArticleInfo]:
    """
    Parse one TOC column (either Projecten or Techniek) into ArticleInfo objects.

    How it works (high level):
    - Walk through TocLine entries in reading order
    - Use classify_role() to assign title/subtitle/author lines to an article
    - Start a new ArticleInfo when a new title appears after a completed article
    - Protect against footer mis-detection by requiring authors to appear close
      below the last title/subtitle (MAX_AUTHOR_VERTICAL_GAP)

    Inputs:
      lines: TocLine entries belonging to a single column and section area
      section: section label to store in each ArticleInfo

    Output:
      List of ArticleInfo for that column.
    """

    # Maximum allowed vertical distance (in PDF points) between the last
    # title/subtitle and its author line. In the magazine layout, the real
    # author block sits only a few lines below the title/subtitle
    # (~30–40 pt). The footer is much further away (>150 pt).
    # 80 pt is a safe upper bound that captures the real author lines,
    # but excludes the distant footer text.
    MAX_AUTHOR_VERTICAL_GAP = 80.0

    articles: List[ArticleInfo] = []
    cur: Optional[ArticleInfo] = None

    # Track the vertical position of the last header line (title or subtitle)
    # for the current article. Author lines must stay close to this anchor.
    current_header_y: Optional[float] = None

    for ln in lines:
        role = classify_role(ln)  # unchanged
        raw_txt = ln.text.strip()

        if raw_txt == "NIEUWS":
            # Edge case: skip "NIEUWS" line that appears in some TOC, NIEUW does not follow the format of the articles
            continue

        if role == "title":
            page_num, title_txt = split_page_prefix(raw_txt)

            if cur is None:
                # start first article in this section
                cur = ArticleInfo(section=section)
            else:
                # Als huidige artikel al een titel heeft én (subtitel of auteur),
                # dan is dit waarschijnlijk een nieuw artikel.
                if cur.chapot and (cur.title or cur.author):
                    articles.append(cur)
                    cur = ArticleInfo(section=section)

            # zet paginanummer (alleen als nog niet gezet)
            if page_num is not None and cur.page is None:
                cur.page = page_num

            # voeg titel toe (multi-line titels worden samengevoegd)
            if cur.chapot:
                cur.chapot = (cur.chapot + " " + title_txt).strip()
            else:
                cur.chapot = title_txt

            # update header anchor for this article
            current_header_y = ln.y_top

        elif role == "subtitle":
            if cur is None:
                cur = ArticleInfo(section=section)
            if cur.title:
                cur.title = (cur.title + " " + raw_txt).strip()
            else:
                cur.title = raw_txt

            # subtitle also acts as the nearest header anchor
            current_header_y = ln.y_top

        elif role == "author":
            if cur is None:
                cur = ArticleInfo(section=section)

            # New: enforce maximum vertical distance from last header line.
            # If the candidate author line is too far below the last title/
            # subtitle, we assume it is not part of this article (e.g. footer)
            # and skip it.
            if current_header_y is not None:
                dy = ln.y_top - current_header_y
                if dy > MAX_AUTHOR_VERTICAL_GAP:
                    # too far away from the article header -> ignore
                    continue

            if cur.author:
                cur.author = (cur.author + " " + raw_txt).strip()
            else:
                cur.author = raw_txt

        else:
            # 'other' -> negeren
            continue

    if cur is not None:
        articles.append(cur)

    return articles


def split_authors_text(author_text: str) -> List[str]:
    """
    Splits an author string like:
      'H.L. Luu, S.C.B.L.M. van Hellenberg Hubar en P. Peters'
    into:
      ['H.L. Luu', 'S.C.B.L.M. van Hellenberg Hubar', 'P. Peters']

    Rules:
    - Split on commas ','
    - Also treat the Dutch ' en ' (and) as a separator
    """
    if not author_text:
        return []

    # Normalize non-breaking spaces, just in case
    t = author_text.replace("\u00a0", " ")

    # Replace ' en ' (with spaces around) by a comma so we have one separator type
    t = re.sub(r"\s+en\s+", ",", t, flags=re.IGNORECASE)

    # Split on commas
    parts = [p.strip(" ,;") for p in t.split(",")]

    # Filter empty chunks
    authors = [p for p in parts if p]

    return authors


def build_magazine_from_pdf(doc):
    """
    High-level entry point: build a Magazine object from a PDF.

    Pipeline:
    1) Find the TOC page
    2) Extract all TOC lines (geometry + spans)
    3) Split lines into left/right columns and parse into articles
    4) Parse authors into a list
    5) Extract footer metadata and compute pdf_index_offset
    6) Return a complete Magazine container

    Input:
      doc: open PyMuPDF document

    Output:
      Magazine with articles filled; pdf_index_offset may be None if footer parsing fails.
    """
    editionnumber = int(doc.name.split("\\")[-1].split("_")[0])
    left_half_str = "projecten"
    right_half_str = "techniek"
    previous_page_str = "rubrieken"
    if editionnumber == 307:
        left_half_str = "tornado"
        right_half_str = "projecten"
    elif editionnumber ==288:
        left_half_str = "rubrieken"
        right_half_str = "techniek"
        previous_page_str = "projecten"
    elif editionnumber == 284:
        left_half_str = "biopartner"
        right_half_str = "techniek"
    elif editionnumber == 278:
        left_half_str = "projecten"
        right_half_str = "veiligheid"
    elif editionnumber == 267:
        left_half_str = "projecten"
        right_half_str = "markt"
    elif editionnumber == 261:
        left_half_str = "rubrieken"
        right_half_str = "techniek"
        previous_page_str = "projecten"
    elif editionnumber == 258:
        left_half_str = "zandhazenbrug"
        right_half_str = "projecten"
    elif editionnumber == 255: #TODO: onderstaande tags zijn al artikelen maar worden niet herkent aangezien hij wsl iets lager begint met zoeken
        left_half_str = "aansprakelijkheid van de mijnbouw"
        right_half_str = "versterken en bouwkundig detailleren"
    elif editionnumber == 254: #TODO: deze werkt niet omdat de paginas van de TOC zijn ingescand
        left_half_str = "utrecht centraal"
        right_half_str = "utrecht centraal"
    elif editionnumber == 251:
        left_half_str = "rubrieken"
        right_half_str = "projecten"
        previous_page_str = "central security"
    elif editionnumber == 247:
        left_half_str = "marktsignalen"
        right_half_str = "marktsignalen"
        previous_page_str = "techniek"
    elif editionnumber == 238:
        left_half_str = "Special: kargo"
        right_half_str = "Special: kargo"
    elif editionnumber == 240:
        left_half_str = "Rotterdam centraal"
        right_half_str = "Rotterdam centraal"
    elif editionnumber == 226:
        left_half_str = "overkapping ijsei, amsterdam"
        right_half_str = "overkapping ijsei, amsterdam"
    elif editionnumber == 220:
        left_half_str = "Vak"
        right_half_str = "Visie"
        previous_page_str = "vereniging"

        

    toc_page = find_toc(doc, left_half_str, right_half_str)

    if editionnumber == 266:#TODO: dit werkt niet ook issue maand en jaar moet gevonden worden 
        toc_page = 4
    elif editionnumber == 261:
        toc_page = 4

    
    if toc_page is None:
        raise ValueError(f"BMS-{editionnumber}, Geen TOC-pagina gevonden.")

    lines = collect_toc_lines(doc, toc_page)
    page = doc[toc_page]
    pw, ph = page.rect.width, page.rect.height

    

    left_header_y = find_header_y(lines, left_half_str)
    right_header_y = find_header_y(lines, right_half_str)
    if left_half_str == right_half_str:
        right_header_y = left_header_y 

    proj_lines = [
        ln for ln in lines if ln.x_center <= pw * 0.5 and ln.y_top > left_header_y
    ]
    tech_lines = [
        ln for ln in lines if ln.x_center > pw * 0.5 and ln.y_top > right_header_y
    ]

    projecten_articles = parse_column(proj_lines, left_half_str)
    techniek_articles = parse_column(tech_lines, right_half_str)

    lines = collect_toc_lines(doc, toc_page-1)
    page = doc[toc_page-1]
    pw, ph = page.rect.width, page.rect.height
    rubrieken_header_y = find_header_y(lines, previous_page_str)
    rubriek_lines = [
        ln for ln in lines if ln.x_center > pw * 0.5 and ln.y_top > rubrieken_header_y
    ]
    rubriek_articles = parse_column(rubriek_lines, previous_page_str)

    # parse authors
    all_articles = rubriek_articles+ projecten_articles + techniek_articles
    for art in all_articles:
        if art.author:
            art.authors = split_authors_text(art.author)

    # build Magazine object
    magazine = extract_magazine_from_toc(doc, toc_page, all_articles)
    return magazine
