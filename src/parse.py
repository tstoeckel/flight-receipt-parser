#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Rename Eurowings PDF invoices to:
  "Flug, {PassengerName}, {DepDate}-{RetDate}.pdf"

Heuristics:
- Tries to find the passenger name near keywords: "Passagier", "Passenger", "Reisender", "Reisende", "Name".
- Tries to find flight dates labeled "Hinflug", "Rückflug" (German) or "Outbound", "Return" (English).
- Falls back to the earliest and latest realistic travel dates found near flight numbers (EWxxx) if labels not present.
- Ignores obvious invoice dates by context when possible.

Usage:
  python rename_eurowings_invoice.py /path/to/file.pdf
  python rename_eurowings_invoice.py /path/to/*.pdf --dry-run
"""

import re
import sys
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List

from pdfminer.high_level import extract_text

# -------- helpers --------

DATE_PATTERNS = [
    # 13.11.2025
    r"\b([0-3]?\d)\.([01]?\d)\.(20\d{2})\b",
    # 2025-11-13
    r"\b(20\d{2})-([01]?\d)-([0-3]?\d)\b",
    # 13/11/2025 or 13-11-2025
    r"\b([0-3]?\d)[./-]([01]?\d)[./-](20\d{2})\b",
]

NAME_HINTS = [
    r"\bPassagier(?:in)?:?\s*(.+)",
    r"\bPassenger(?: name)?:?\s*(.+)",
    r"\bReisende[rn]?:?\s*(.+)",
    r"\bName:?\s*(.+)",
]

HIN_HINTS = [r"\bHinflug\b", r"\bOutbound\b", r"\bDeparture\b"]
RUECK_HINTS = [r"\bRückflug\b", r"\bRueckflug\b", r"\bReturn\b"]

FLIGHT_ROW_HINT = re.compile(r"\b(EW\s?\d{2,4})\b", re.IGNORECASE)

def sanitize_filename_component(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parse_dates_from_text(lines: List[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
    def first_date_in(s: str) -> Optional[datetime]:
        for pat in DATE_PATTERNS:
            m = re.search(pat, s)
            if m:
                groups = m.groups()
                try:
                    if len(groups) == 3:
                        # normalize to yyyy-mm-dd
                        if len(groups[0]) == 4:
                            y, mth, d = int(groups[0]), int(groups[1]), int(groups[2])
                        else:
                            # dd.mm.yyyy
                            d, mth, y = int(groups[0]), int(groups[1]), int(groups[2])
                        return datetime(y, mth, d)
                except ValueError:
                    pass
        return None

    dep, ret = None, None

    # 1) Look for explicit "Hinflug"/"Rückflug" sections
    for i, line in enumerate(lines):
        if any(re.search(h, line, re.IGNORECASE) for h in HIN_HINTS):
            d = first_date_in(line) or (first_date_in(lines[i+1]) if i+1 < len(lines) else None)
            if d: dep = d
        if any(re.search(h, line, re.IGNORECASE) for h in RUECK_HINTS):
            d = first_date_in(line) or (first_date_in(lines[i+1]) if i+1 < len(lines) else None)
            if d: ret = d

    if dep and ret:
        return dep, ret

    # 2) Look near flight rows (lines containing EWxxxx)
    candidates: List[datetime] = []
    for i, line in enumerate(lines):
        if FLIGHT_ROW_HINT.search(line):
            window = "\n".join(lines[max(0, i-2):min(len(lines), i+3)])
            d = first_date_in(window)
            if d:
                candidates.append(d)

    if candidates:
        candidates.sort()
        dep = candidates[0]
        ret = candidates[-1] if len(candidates) > 1 else None
        return dep, ret

    # 3) Fallback: pick earliest and latest date in entire text, but try to skip invoice metadata by filtering year range
    all_dates: List[datetime] = []
    for line in lines:
        d = first_date_in(line)
        if d and 2015 <= d.year <= 2100:
            all_dates.append(d)
    if all_dates:
        all_dates.sort()
        return all_dates[0], (all_dates[-1] if len(all_dates) > 1 else None)

    return None, None

def parse_name_from_text(lines: List[str]) -> Optional[str]:
    # Try direct hints
    for line in lines:
        for pat in NAME_HINTS:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                name = m.group(1)
                # Trim trailing artifacts (commas, booking refs, multiple spaces)
                name = re.sub(r"\s*(Buchungsnr\.?|Booking ref\.?).*$", "", name, flags=re.IGNORECASE)
                name = re.sub(r"\s{2,}", " ", name).strip(" ,;-")
                # Invoices sometimes list as LASTNAME/FIRSTNAME
                if "/" in name and " " not in name:
                    parts = name.split("/")
                    if len(parts) == 2:
                        name = f"{parts[1].title()} {parts[0].title()}"
                return name if name else None

    # Heuristic: look for NAME formats like MUSTERMANN MAX directly near "Flug"/"Passenger"
    for i, line in enumerate(lines):
        if re.search(r"\b(Flug|Passenger|Passagier|Reisende)\b", line, re.IGNORECASE):
            window = " ".join(lines[i:i+3])
            m = re.search(r"\b([A-ZÄÖÜß\-]+)\s+([A-ZÄÖÜß][a-zäöüß\-]+)\b", window)
            if m:
                last_upper, first = m.group(1), m.group(2)
                return f"{first} {last_upper.title()}"
    return None

def format_date(d: datetime) -> str:
    # Change here if you prefer DD.MM.YYYY:
    # return d.strftime("%d.%m.%Y")
    return d.strftime("%Y-%m-%d")

def extract_lines(pdf_path: Path) -> List[str]:
    text = extract_text(str(pdf_path)) or ""
    # Normalize some whitespace and split into lines
    text = re.sub(r"\r", "\n", text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return lines

def build_filename(passenger: Optional[str], dep: Optional[datetime], ret: Optional[datetime]) -> Optional[str]:
    if not passenger or not dep:
        return None
    base = f"Flug, {sanitize_filename_component(passenger)}, {format_date(dep)}"
    if ret:
        base += f"-{format_date(ret)}"
    return base + ".pdf"

def process_file(pdf_path: Path, dry_run: bool = False) -> Tuple[bool, str]:
    try:
        lines = extract_lines(pdf_path)
        passenger = parse_name_from_text(lines)
        dep, ret = parse_dates_from_text(lines)
        new_name = build_filename(passenger, dep, ret)
        if not new_name:
            return False, f"[{pdf_path.name}] Konnte erforderliche Daten nicht sicher ermitteln (Name oder Abflugdatum fehlt)."

        new_path = pdf_path.with_name(new_name)
        if dry_run:
            return True, f"DRY-RUN: {pdf_path.name}  →  {new_name}"
        else:
            # Avoid overwriting
            counter = 1
            final_path = new_path
            while final_path.exists():
                stem = final_path.stem
                suffix = final_path.suffix
                final_path = pdf_path.with_name(f"{stem} ({counter}){suffix}")
                counter += 1
            pdf_path.rename(final_path)
            return True, f"Umbenannt: {pdf_path.name}  →  {final_path.name}"
    except Exception as e:
        return False, f"[{pdf_path.name}] Fehler: {e}"

def main():
    ap = argparse.ArgumentParser(description="Rename Eurowings PDF invoices by passenger and trip dates.")
    ap.add_argument("paths", nargs="+", help="PDF-Dateien oder Globs (z. B. ~/Rechnungen/*.pdf)")
    ap.add_argument("--dry-run", action="store_true", help="Nur anzeigen, was umbenannt würde.")
    args = ap.parse_args()

    files: List[Path] = []
    for p in args.paths:
        # Expand globs ourselves for portability
        matches = list(Path().glob(p)) if any(ch in p for ch in "*?[]") else [Path(p)]
        for m in matches:
            if m.is_file() and m.suffix.lower() == ".pdf":
                files.append(m)

    if not files:
        print("Keine passenden PDF-Dateien gefunden.")
        sys.exit(2)

    ok_all = True
    for f in files:
        ok, msg = process_file(f, dry_run=args.dry_run)
        print(msg)
        ok_all = ok_all and ok

    sys.exit(0 if ok_all else 1)

if __name__ == "__main__":
    main()
