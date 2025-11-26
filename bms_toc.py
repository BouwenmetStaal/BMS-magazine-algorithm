# -*- coding: utf-8 -*-
"""
Created on Mon Nov 24 18:07:27 2025

@author: erikf
"""

#!/usr/bin/env python3
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple


import fitz  # PyMuPDF


# ---------- helpers & dataclasses ----------

def clean_text(t: str) -> str:
    t = t.replace("\r", " ")
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class TocLine:
    text: str
    bbox: Tuple[float, float, float, float]
    spans: List[Dict[str, Any]]
    page: int

    @property
    def x_center(self) -> float:
        x0, _, x1, _ = self.bbox
        return 0.5 * (x0 + x1)

    @property
    def y_top(self) -> float:
        _, y0, _, _ = self.bbox
        return y0





@dataclass
class ArticleInfo:
    section: str           # "Projecten" / "Techniek"
    page: Optional[int] = None
    title: str = ""
    subtitle: str = ""
    author: str = ""
    authors: List[str] = field(default_factory=list)
    # you can keep pdf_index here or add later if you want

    def pretty(self) -> str:
        out = [f"[{self.section}]"]
        if self.page is not None:
            out.append(f"  Page    : {self.page}")
        out.append(f"  Title   : {self.title}")
        if self.subtitle:
            out.append(f"  Subtitle: {self.subtitle}")
        if self.authors:
            out.append(f"  Authors : {', '.join(self.authors)}")
        elif self.author:
            out.append(f"  Author  : {self.author}")
        return "\n".join(out)


@dataclass
class Magazine:
    """
    Eén nummer van Bouwen met Staal.

    issue_number       : bijvoorbeeld 305
    release_year       : bijvoorbeeld 2025
    release_month      : bijvoorbeeld 3 (maart)
    original_label     : originele tekst uit de footer, bijv. 'MAART 2025'
    pdf_index_offset   : verschil tussen PDF index en gedrukte pagina
    articles           : alle artikelen in de TOC
    """
    issue_number: Optional[int]
    release_year: Optional[int]
    release_month: Optional[int]
    original_label: Optional[str]
    pdf_index_offset: Optional[int]
    articles: List[ArticleInfo]

MONTHS_NL = {
    "januari": 1, "februari": 2, "maart": 3,
    "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "augustus": 8, "september": 9,
    "oktober": 10, "november": 11, "december": 12,
}



# ---------- your existing TOC finder (relaxed with left/right) ----------

def find_toc(doc: fitz.Document) -> int | None:
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
                contains_proj = "projecten" in lower
                contains_tech = "techniek" in lower

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

    return None


# ---------- collect lines on TOC page ----------

def collect_toc_lines(doc: fitz.Document, pno: int) -> List[TocLine]:
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
    ys = [ln.y_top for ln in lines if keyword.lower() in ln.text.lower()]
    return min(ys) if ys else 0.0


# ---------- role classification via font ----------

def classify_role(line: TocLine) -> str:
    """
    Titel:   Univers LT Std, size ≈ 12/10
    Subtitel:Minion Pro,     size ≈ 12
    Auteur:  Univers LT Std, size ≈ 7/7.5
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
    Haal een  paginanummer aan het begin van de regel weg.
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
    Extract magazine metadata from the footer line on the TOC page.

    Expected format on the bottom-left:
        'BOUWEN MET STAAL XXX | MAAND JAAR'

    And on the same height (bottom-right):
        '<printed page number>'
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

            footer_lines.append({
                "text": text,
                "y_center": y_center,
                "x_center": x_center,
            })

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

    # --- step 2: find printed page number on same row, right side ---
    if target_y is not None:
        tol = 3.0  # y-alignment tolerance
        candidates = [
            line for line in footer_lines
            if (line["x_center"] > pw * 0.5) and (abs(line["y_center"] - target_y) <= tol)
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
    articles: List[ArticleInfo] = []
    cur: Optional[ArticleInfo] = None

    for ln in lines:
        role = classify_role(ln)   # unchanged
        raw_txt = ln.text.strip()

        if role == "title":
            page_num, title_txt = split_page_prefix(raw_txt)

            if cur is None:
                # start first article in this section
                cur = ArticleInfo(section=section)
            else:
                # Als huidige artikel al een titel heeft én (subtitel of auteur),
                # dan is dit waarschijnlijk een nieuw artikel.
                if cur.title and (cur.subtitle or cur.author):
                    articles.append(cur)
                    cur = ArticleInfo(section=section)

            # zet paginanummer (alleen als nog niet gezet)
            if page_num is not None and cur.page is None:
                cur.page = page_num

            # voeg titel toe (multi-line titels worden samengevoegd)
            if cur.title:
                cur.title = (cur.title + " " + title_txt).strip()
            else:
                cur.title = title_txt

        elif role == "subtitle":
            if cur is None:
                cur = ArticleInfo(section=section)
            if cur.subtitle:
                cur.subtitle = (cur.subtitle + " " + raw_txt).strip()
            else:
                cur.subtitle = raw_txt

        elif role == "author":
            if cur is None:
                cur = ArticleInfo(section=section)
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
    toc_page = find_toc(doc)
    if toc_page is None:
        raise ValueError("Geen TOC-pagina gevonden.")

    lines = collect_toc_lines(doc, toc_page)
    page = doc[toc_page]
    pw, ph = page.rect.width, page.rect.height

    projecten_y = find_header_y(lines, "projecten")
    techniek_y  = find_header_y(lines, "techniek")

    proj_lines = [ln for ln in lines if ln.x_center <= pw * 0.5 and ln.y_top > projecten_y]
    tech_lines = [ln for ln in lines if ln.x_center >  pw * 0.5 and ln.y_top > techniek_y]

    projecten_articles = parse_column(proj_lines, "Projecten")
    techniek_articles  = parse_column(tech_lines, "Techniek")

    # parse authors
    all_articles = projecten_articles + techniek_articles
    for art in all_articles:
        if art.author:
            art.authors = split_authors_text(art.author)

    # build Magazine object
    magazine = extract_magazine_from_toc(doc, toc_page, all_articles)
    return magazine


