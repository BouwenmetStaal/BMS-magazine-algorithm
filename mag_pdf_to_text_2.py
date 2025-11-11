#!/usr/bin/env python3
"""
mag_pdf_to_text.py
------------------
Converteer een (magazine) PDF naar een tekstbestand waarin elk artikel begint met de titel,
gevolgd door een (vetgedrukte) intro en daarna de doorlopende tekst. Afbeeldingen en opmaak
worden genegeerd.

Nieuw in deze versie:
- Meerdere titelregels worden samengevoegd op basis van lettergrootte.
- Kolomvolgorde per pagina: eerst de meest linker kolom, daarna rechts, en binnen kolommen van boven naar beneden.
- Vetgedrukte eerste alinea('s) na de titel worden als "intro" opgeslagen.
- Klasse-structuur met Artikel(titel, intro_text, body_text).

Benodigdheden:
    pip install pymupdf

Gebruik (CLI blijft beschikbaar):
    python mag_pdf_to_text.py input.pdf output.txt

Of programmeermatig via main(input_pdf, output_txt).
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import List, Tuple, Dict, Optional

import fitz  # PyMuPDF


HEADER_ZONE_PT = 72         # bovenste 1 inch voor header-detectie
FOOTER_ZONE_PT = 72         # onderste 1 inch voor footer-detectie
DROP_CAP_MAXLEN = 3         # max lengte om een (te) grote initiaal te negeren
COLUMN_X_TOLERANCE_MIN = 20.0  # minimale tolerantie in punten voor kolom-clustering
COLUMN_X_TOLERANCE_FRAC = 0.015  # fractie van paginabreedte voor tolerantie


@dataclass
class Line:
    page: int
    text: str
    bbox: Tuple[float, float, float, float]
    max_size: float
    is_bold: bool
    block_no: int
    col: int = 0  # wordt later gezet


@dataclass
class Article:
    title: str
    intro_lines: List[str] = field(default_factory=list)
    body_lines: List[str] = field(default_factory=list) #groter dan 20 woorden?
    auteurs: List[str] = field(default_factory=list)

    def add_intro_line(self, t: str):
        t = t.strip()
        if t:
            self.intro_lines.append(t)

    def add_body_line(self, t: str):
        t = t.strip()
        if t:
            self.body_lines.append(t)

    @property
    def intro_text(self) -> str:
        return normalize_paragraphs(self.intro_lines)

    @property
    def body_text(self) -> str:
        return normalize_paragraphs(self.body_lines)

    def render(self) -> str:
        parts = [self.title.strip(), ""]
        intro = self.intro_text
        if intro:
            parts.append(intro)
            parts.append("")  # lege regel tussen intro en body
        body = self.body_text
        if body:
            parts.append(body)
        return "\n".join(parts).rstrip() + "\n"


def clean_text(t: str) -> str:
    t = t.replace("\r", " ")
    t = re.sub(r"[ \t\f\v]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    return t.strip()


def looks_all_caps(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return s.upper() == s and any(ch.isalpha() for ch in s)


def dehyphenate_lines(lines: List[str]) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i].rstrip()
        if cur.endswith("-") and i + 1 < len(lines):
            nextl = lines[i + 1].lstrip()
            # Voeg samen als volgende regel "doorloopt" (kleine letter of apostrof)
            if nextl and (nextl[0].islower() or nextl[0] in "’'"):
                out.append(cur[:-1] + nextl)
                i += 2
                continue
        out.append(cur)
        i += 1
    return out


def normalize_paragraphs(lines: List[str]) -> str:
    lines = [clean_text(l) for l in lines if l and clean_text(l)]
    lines = dehyphenate_lines(lines)
    paras: List[str] = []
    buff: List[str] = []
    for l in lines:
        if not l.strip():
            if buff:
                paras.append(" ".join(buff).strip())
                buff = []
            continue
        buff.append(l.strip())
    if buff:
        paras.append(" ".join(buff).strip())
    return "\n\n".join(paras)


def collect_lines(doc: fitz.Document) -> Tuple[List[Line], Dict[int, Tuple[float, float]]]:
    all_lines: List[Line] = []
    page_sizes: Dict[int, Tuple[float, float]] = {}

    for pno in range(len(doc)):
        page = doc[pno]
        page_sizes[pno] = (page.rect.width, page.rect.height)
        data = page.get_text("dict")
        block_no = -1
        for b in data.get("blocks", []):
            if b.get("type", 0) != 0:  # 0 = text
                continue
            block_no += 1
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                texts = [s.get("text", "") for s in spans]
                line_text = clean_text("".join(texts))
                if not line_text:
                    continue
                max_size = max(s.get("size", 0.0) for s in spans)
                any_bold = any(("Bold" in (s.get("font", "") or "")
                                or "Black" in (s.get("font", "") or "")
                                or "Heavy" in (s.get("font", "") or "")
                                or "Semibold" in (s.get("font", "") or "")
                                or "Demi" in (s.get("font", "") or ""))
                               for s in spans)
                xs, ys, xe, ye = [], [], [], []
                for s in spans:
                    (x0, y0, x1, y1) = s.get("bbox", (0, 0, 0, 0))
                    xs.append(x0); ys.append(y0); xe.append(x1); ye.append(y1)
                bbox = (min(xs or [0]), min(ys or [0]), max(xe or [0]), max(ye or [0]))
                all_lines.append(Line(page=pno, text=line_text, bbox=bbox, max_size=max_size, is_bold=any_bold, block_no=block_no))

    return all_lines, page_sizes


def detect_headers_footers(lines: List[Line], page_sizes: Dict[int, Tuple[float, float]], min_repeats: int = 5) -> Tuple[set, set]:
    header_counts = Counter()
    footer_counts = Counter()

    for ln in lines:
        width, height = page_sizes[ln.page]
        y0 = ln.bbox[1]
        y1 = ln.bbox[3]
        if y0 < HEADER_ZONE_PT:
            header_counts[ln.text] += 1
        if (height - y1) < FOOTER_ZONE_PT:
            footer_counts[ln.text] += 1

    headers = {t for t, c in header_counts.items() if c >= min_repeats and len(t) >= 4}
    footers = {t for t, c in footer_counts.items() if c >= min_repeats and len(t) >= 4}
    return headers, footers


def choose_title_threshold(lines: List[Line], title_quantile: float = 0.92) -> Tuple[float, float]:
    sizes = [round(ln.max_size, 1) for ln in lines if len(ln.text) > 6 and not looks_all_caps(ln.text)]
    if not sizes:
        return 12.0, 16.0
    size_counts = Counter(sizes)
    body_size = max(size_counts.items(), key=lambda kv: kv[1])[0]  # modus
    body_size = max(body_size, median(sizes))
    try:
        ss = sorted(sizes)
        idx = int(title_quantile * (len(ss) - 1))
        title_thr = ss[idx]
    except Exception:
        title_thr = max(sizes)
    title_thr = max(title_thr, body_size * 1.25)
    return float(body_size), float(title_thr)


def assign_columns(lines: List[Line], page_sizes: Dict[int, Tuple[float, float]]):
    """Wijs per pagina een kolomindex toe op basis van x0 (linker) van de bbox."""
    by_page: Dict[int, List[Line]] = defaultdict(list)
    for ln in lines:
        by_page[ln.page].append(ln)

    for page, items in by_page.items():
        width, _ = page_sizes[page]
        xs = sorted([ln.bbox[0] for ln in items])
        if not xs:
            continue
        tol = max(COLUMN_X_TOLERANCE_MIN, width * COLUMN_X_TOLERANCE_FRAC)
        bins: List[List[float]] = []
        centers: List[float] = []
        for x in xs:
            if not centers:
                centers.append(x)
                bins.append([x])
                continue
            diffs = [abs(x - c) for c in centers]
            j = min(range(len(centers)), key=lambda k: diffs[k])
            if diffs[j] <= tol:
                bins[j].append(x)
                centers[j] = sum(bins[j]) / len(bins[j])
            else:
                centers.append(x)
                bins.append([x])
        order = sorted(range(len(centers)), key=lambda idx: centers[idx])
        center_to_col = {centers[idx]: i for i, idx in enumerate(order)}
        for ln in items:
            diffs = [(abs(ln.bbox[0] - c), c) for c in centers]
            _, nearest_c = min(diffs, key=lambda t: t[0])
            ln.col = center_to_col[nearest_c]


def is_probable_title_line(ln: Line, body_size: float, title_thr: float, min_title_len: int) -> bool:
    t = ln.text.strip()
    if len(t) < min_title_len:
        return False
    if len(t) <= DROP_CAP_MAXLEN and ln.max_size >= title_thr:
        return False
    if ln.max_size >= title_thr:
        return True
    if looks_all_caps(t) and ln.max_size >= body_size * 1.1 and len(t) >= (min_title_len + 2):
        return True
    return False


def collect_multiline_title(lines_sorted: List[Line], start_idx: int, title_thr: float) -> Tuple[str, int]:
    base = lines_sorted[start_idx]
    base_size = base.max_size
    page = base.page
    col = base.col
    title_parts = [base.text.strip()]
    i = start_idx + 1
    while i < len(lines_sorted):
        ln = lines_sorted[i]
        if ln.page != page or ln.col != col:
            break
        if ln.max_size >= max(title_thr, 0.9 * base_size):
            title_parts.append(ln.text.strip())
            i += 1
            continue
        break
    title = re.sub(r"\s+", " ", " ".join(title_parts)).strip()
    return title, i


def parse_articles(lines: List[Line], headers: set, footers: set, body_size: float, title_thr: float, min_title_len: int, strip_toc: bool) -> List[Article]:
    filtered = [ln for ln in lines if ln.text not in headers and ln.text not in footers]

    toc_pages: set = set()
    if strip_toc and filtered:
        first_page = filtered[0].page
        for ln in filtered[: min(120, len(filtered))]:
            if ln.page != first_page:
                break
            if re.search(r"\b(Inhoud|Contents|Inhaltsverzeichnis)\b", ln.text, flags=re.I):
                toc_pages.add(ln.page)
                break

    filtered.sort(key=lambda L: (L.page, L.col, round(L.bbox[1], 1), round(L.bbox[0], 1)))

    articles: List[Article] = []
    cur: Optional[Article] = None
    collecting_intro: bool = False

    i = 0
    n = len(filtered)
    while i < n:
        ln = filtered[i]
        if strip_toc and ln.page in toc_pages:
            i += 1
            continue

        if is_probable_title_line(ln, body_size, title_thr, min_title_len):
            title, next_i = collect_multiline_title(filtered, i, title_thr)
            if cur and (cur.title.strip() or cur.body_lines or cur.intro_lines):
                articles.append(cur)
            cur = Article(title=title)
            collecting_intro = True
            i = next_i
            continue

        if cur is None:
            cur = Article(title="(Zonder titel)")
            collecting_intro = True

        text = ln.text.strip()
        if not text:
            i += 1
            continue

        if collecting_intro:
            if ln.is_bold and ln.max_size < (title_thr * 0.95):
                cur.add_intro_line(text)
                i += 1
                continue
            else:
                collecting_intro = False

        cur.add_body_line(text)
        i += 1

    if cur and (cur.title.strip() or cur.body_lines or cur.intro_lines):
        articles.append(cur)

    return articles


def write_output(articles: List[Article], out_path: str):
    with open(out_path, "w", encoding="utf-8") as f:
        for idx, art in enumerate(articles, 1):
            f.write("# " + art.title.strip() + "\n\n")
            if art.intro_text:
                f.write(art.intro_text + "\n\n")
            if art.body_text:
                f.write(art.body_text + "\n")
            if idx < len(articles):
                f.write("\n" + "-" * 80 + "\n\n")


def main(input_pdf: str, output_txt: str, *, title_quantile: float = 0.92, min_title_len: int = 8, min_repeats: int = 5, strip_toc: bool = False):
    doc = fitz.open(input_pdf)
    lines, page_sizes = collect_lines(doc)
    headers, footers = detect_headers_footers(lines, page_sizes, min_repeats=min_repeats)
    body_size, title_thr = choose_title_threshold(lines, title_quantile=title_quantile)
    assign_columns(lines, page_sizes)
    articles = parse_articles(lines, headers, footers, body_size, title_thr, min_title_len, strip_toc=strip_toc)
    write_output(articles, output_txt)
    print(f"Gereed. Gevonden artikelen: {len(articles)}")
    print(f"Uitvoer geschreven naar: {output_txt}")


def cli():
    ap = argparse.ArgumentParser(description="Converteer magazine-PDF naar tekst per artikel (titel, intro, body).")
    ap.add_argument("input_pdf", help="Pad naar input PDF")
    ap.add_argument("output_txt", help="Pad naar output TXT")
    ap.add_argument("--title-quantile", type=float, default=0.92, help="Percentiel voor titel-drempel (0-1)")
    ap.add_argument("--min-title-len", type=int, default=8, help="Minimale lengte van een titelregel")
    ap.add_argument("--min-repeats", type=int, default=5, help="Min. aantal herhalingen voor header/footer-detectie")
    ap.add_argument("--strip-toc", action="store_true", help="Probeer 'Inhoud/Contents'-pagina te negeren")
    args = ap.parse_args()
    main(args.input_pdf, args.output_txt, title_quantile=args.title_quantile, min_title_len=args.min_title_len, min_repeats=args.min_repeats, strip_toc=args.strip_toc)


# === Door gebruiker gevraagde __main__-invocatie ===
if __name__ == "__main__":
    from pathlib import Path as _Path
    input_folder = _Path(r"C:\Users\AJOR\Bouwen met Staal\ChatBmS - General\BmS_magazine_pdfs")
    input_pdf_path = input_folder / "295_BmS_magazine_lr_compleet.pdf"
    output_txt_path = input_folder / "295_BmS_magazine_lr_compleet_3.txt"
    main(str(input_pdf_path), str(output_txt_path))
