#!/usr/bin/env python3
"""
mag_pdf_to_text.py
------------------
Converteer een (magazine) PDF naar een tekstbestand waarin elk artikel begint met de titel,
gevolgd door de doorlopende tekst. Afbeeldingen en opmaak worden genegeerd.

Heuristieken:
- Titelregels worden gedetecteerd op basis van relatief grote fontgrootte (percentiel over het document)
  en/of vetgedrukte fontnamen ("Bold", "Black", "Heavy", "Semibold").
- Herhaalde kop- en voetteksten (per pagina) worden gedetecteerd en verwijderd.
- Eenvoudige de-hyphenatie op regeleinden (woorden die met '-' afbreken).
- De leesvolgorde is gebaseerd op de volgorde van tekstblokken zoals door PyMuPDF aangeleverd.

Benodigdheden:
    pip install pymupdf

Gebruik:
    python mag_pdf_to_text.py input.pdf output.txt
Opties:
    --title-quantile 0.92   # percentiel voor titel-drempel (0-1)
    --min-title-len 8       # minimale lengte (tekens) van een titelregel
    --min-repeats 5         # minimum aantal pagina's dat een header/footer-regel moet voorkomen om te verwijderen
    --strip-toc             # simpele poging om een "Inhoud"/"Contents"-pagina te negeren

Let op: PDF's verschillen sterk; mogelijk moeten heuristieken soms worden bijgesteld.
"""
from __future__ import annotations

import argparse
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import List, Tuple, Dict, Any, Optional
from pathlib import Path

import fitz  # PyMuPDF


HEADER_ZONE_PT = 72         # bovenste 1 inch voor header-detectie
FOOTER_ZONE_PT = 72         # onderste 1 inch voor footer-detectie
DROP_CAP_MAXLEN = 3         # max lengte om een (te) grote initiaal te negeren


@dataclass
class Line:
    page: int
    text: str
    bbox: Tuple[float, float, float, float]
    max_size: float
    is_bold: bool
    block_no: int


@dataclass
class Article:
    title: str
    body_lines: List[str] = field(default_factory=list)

    def add_line(self, t: str):
        if t.strip():
            self.body_lines.append(t.strip())

    def render(self) -> str:
        body = normalize_paragraphs(self.body_lines)
        return f"{self.title.strip()}\n\n{body}\n"


def clean_text(t: str) -> str:
    # Vervang meerdere whitespace door enkelvoudig en verwijder control chars
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
    """ Join regels waarbij een woord aan het einde met '-' afbreekt. """
    out: List[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i].rstrip()
        if cur.endswith("-") and i + 1 < len(lines):
            nextl = lines[i + 1].lstrip()
            # Alleen samenvoegen als volgende regel niet met hoofdletter of cijfer begint (meestal doorlopende zin)
            if nextl and (nextl[0].islower() or nextl[0] in "’'"):
                out.append(cur[:-1] + nextl)
                i += 2
                continue
        out.append(cur)
        i += 1
    return out


def normalize_paragraphs(lines: List[str]) -> str:
    # Eenvoudige paragraaf-normalisatie: dehyphenate en voeg regels samen met spaties
    lines = [clean_text(l) for l in lines if l and clean_text(l)]
    lines = dehyphenate_lines(lines)
    # Voeg regels samen; voeg een nieuwe paragraaf toe als er een lege regel staat
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
    """Haal alle lijnen met fontinfo op; retourneer ook page-heights per pagina."""
    all_lines: List[Line] = []
    page_heights: Dict[int, Tuple[float, float]] = {}

    for pno in range(len(doc)):
        page = doc[pno]
        page_heights[pno] = (page.rect.width, page.rect.height)
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
                # Tekst en fontinfo
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
                # bbox van de lijn: neem min/max van span-bboxen
                xs, ys, xe, ye = [], [], [], []
                for s in spans:
                    (x0, y0, x1, y1) = s.get("bbox", (0, 0, 0, 0))
                    xs.append(x0); ys.append(y0); xe.append(x1); ye.append(y1)
                bbox = (min(xs or [0]), min(ys or [0]), max(xe or [0]), max(ye or [0]))
                all_lines.append(Line(page=pno, text=line_text, bbox=bbox, max_size=max_size, is_bold=any_bold, block_no=block_no))

    # Sorteer globaal op pagina, daarna Y, daarna X
    all_lines.sort(key=lambda L: (L.page, round(L.bbox[1], 1), round(L.bbox[0], 1)))
    return all_lines, page_heights


def detect_headers_footers(lines: List[Line], page_heights: Dict[int, Tuple[float, float]], min_repeats: int = 5) -> Tuple[set, set]:
    """Zoek herhalende kop- en voettekstregels over veel pagina's."""
    header_counts = Counter()
    footer_counts = Counter()

    for ln in lines:
        width, height = page_heights[ln.page]
        y0 = ln.bbox[1]
        y1 = ln.bbox[3]
        # Header-zone
        if y0 < HEADER_ZONE_PT:
            header_counts[ln.text] += 1
        # Footer-zone
        if (height - y1) < FOOTER_ZONE_PT:
            footer_counts[ln.text] += 1

    headers = {t for t, c in header_counts.items() if c >= min_repeats and len(t) >= 4}
    footers = {t for t, c in footer_counts.items() if c >= min_repeats and len(t) >= 4}
    return headers, footers


def choose_title_threshold(lines: List[Line], title_quantile: float = 0.92) -> Tuple[float, float]:
    """Bepaal body- en titel-drempel op basis van fontgroottes."""
    sizes = [round(ln.max_size, 1) for ln in lines if len(ln.text) > 6 and not looks_all_caps(ln.text)]
    if not sizes:
        return 12.0, 16.0  # val terug op redelijke standaard
    size_counts = Counter(sizes)
    body_size = max(size_counts.items(), key=lambda kv: kv[1])[0]  # modus
    # robuust: minstens median als body
    body_size = max(body_size, median(sizes))
    # titel-drempel als percentiel
    try:
        ss = sorted(sizes)
        idx = int(title_quantile * (len(ss) - 1))
        title_thr = ss[idx]
    except Exception:
        title_thr = max(sizes)
    # Garandeer dat titel > body
    title_thr = max(title_thr, body_size * 1.25)
    return float(body_size), float(title_thr)


def is_probable_title(ln: Line, body_size: float, title_thr: float, min_title_len: int) -> bool:
    t = ln.text.strip()
    # Allow multi-line titles: check if line is likely part of a title, not just a single line
    # Heuristic: if font size is large enough, bold, or all caps, and not a drop cap
    if len(t) < 2:  # ignore very short lines
        return False
    if len(t) <= DROP_CAP_MAXLEN and ln.max_size >= title_thr:
        return False
    # Main title detection
    if ln.max_size >= title_thr:
        return True
    if ln.is_bold and ln.max_size >= body_size * 1.2:
        return True
    if looks_all_caps(t) and ln.max_size >= body_size * 1.1 and len(t) >= (min_title_len + 2):
        return True
    # If line is reasonably long and font size is close to title threshold, allow as part of title
    if len(t) >= min_title_len and ln.max_size >= (title_thr * 0.95):
        return True
    return False


def parse_articles(lines: List[Line], headers: set, footers: set, body_size: float, title_thr: float, min_title_len: int, strip_toc: bool) -> List[Article]:
    articles: List[Article] = []
    cur: Optional[Article] = None
    seen_titles: set = set()

    # Optioneel: oversla eenvoudige TOC-pagina's (zoek naar "Inhoud" of "Contents")
    toc_pages: set = set()
    if strip_toc:
        for ln in lines[: min(80, len(lines))]:  # check eerste ~80 lijnen
            if re.search(r"\b(Inhoud|Contents|Inhaltsverzeichnis)\b", ln.text, flags=re.I):
                toc_pages.add(ln.page)

    for ln in lines:
        if ln.text in headers or ln.text in footers:
            continue
        if strip_toc and ln.page in toc_pages:
            continue
        # Titel?
        if is_probable_title(ln, body_size, title_thr, min_title_len):
            title = ln.text.strip()
            # Start nieuw artikel
            if cur and (cur.title.strip() or cur.body_lines):
                articles.append(cur)
            # vermijd duplicaten (soms kop/voettekst-achtige blokken)
            if title in seen_titles and (not cur or cur.body_lines):
                cur = Article(title=f"{title} (vervolg)")
            else:
                cur = Article(title=title)
                seen_titles.add(title)
            continue

        # Body
        if cur is None:
            # Soms begint het document met doorlopende tekst; maak een generieke titel
            cur = Article(title="(Zonder titel)")
        cur.add_line(ln.text)

    if cur and (cur.title.strip() or cur.body_lines):
        articles.append(cur)

    return articles


def write_output(articles: List[Article], out_path: str):
    with open(out_path, "w", encoding="utf-8") as f:
        for i, art in enumerate(articles, 1):
            f.write("# " + art.title.strip() + "\n\n")
            f.write(art.render())
            if i < len(articles):
                f.write("\n" + "-" * 80 + "\n\n")


def main(input_pdf=None, output_txt=None):
    ap = argparse.ArgumentParser(description="Converteer magazine-PDF naar tekst per artikel.")
    ap.add_argument("input_pdf", nargs='?', help="Pad naar input PDF")
    ap.add_argument("output_txt", nargs='?', help="Pad naar output TXT")
    ap.add_argument("--title-quantile", type=float, default=0.92, help="Percentiel voor titel-drempel (0-1)")
    ap.add_argument("--min-title-len", type=int, default=8, help="Minimale lengte van een titelregel")
    ap.add_argument("--min-repeats", type=int, default=5, help="Min. aantal herhalingen voor header/footer-detectie")
    ap.add_argument("--strip-toc", action="store_true", help="Probeer 'Inhoud/Contents'-pagina te negeren")
    args = ap.parse_args()

    # Gebruik argumenten uit functieparameters als ze zijn opgegeven
    if input_pdf is not None:
        args.input_pdf = input_pdf
    if output_txt is not None:
        args.output_txt = output_txt

    if not args.input_pdf or not args.output_txt:
        ap.error("input_pdf en output_txt zijn verplicht.")

    # Open document
    doc = fitz.open(args.input_pdf)

    # 1) Verzamelen van lijnen + fontinfo
    lines, page_heights = collect_lines(doc)

    # 2) Detecteer herhaalde headers/footers
    headers, footers = detect_headers_footers(lines, page_heights, min_repeats=args.min_repeats)

    # 3) Bepaal body- en titel-drempel
    body_size, title_thr = choose_title_threshold(lines, title_quantile=args.title_quantile)

    # 4) Parse artikelen
    articles = parse_articles(lines, headers, footers, body_size, title_thr, args.min_title_len, args.strip_toc)

    # 5) Schrijf uit
    write_output(articles, args.output_txt)

    print(f"Gereed. Gevonden artikelen: {len(articles)}")
    print(f"Uitvoer geschreven naar: {args.output_txt}")


if __name__ == "__main__":
    input_folder = Path(r"C:\Users\AJOR\Bouwen met Staal\ChatBmS - General\BmS_magazine_pdfs")
    input_pdf_path = input_folder / "295_BmS_magazine_lr_compleet.pdf"
    output_txt_path = input_folder / "295_BmS_magazine_lr_compleet_2.txt"
    main(str(input_pdf_path), str(output_txt_path))