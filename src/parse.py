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

KNOWN_PASSENGERS = ["Andre Ziemke", "Thomas Stoeckel"]

HIN_HINTS = [r"\bHinflug\b", r"\bOutbound\b", r"\bDeparture\b"]
RUECK_HINTS = [r"\bRückflug\b", r"\bRueckflug\b", r"\bReturn\b"]

FLIGHT_ROW_HINT = re.compile(r"\b(EW\s?\d{2,4})\b", re.IGNORECASE)


def sanitize_filename_component(s: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_dates_from_text(lines: List[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Parses flight dates and numbers from lines like:
    - 'Flight: 13.10.2025 | Flight Number EW 9083 (BIZClass AS)'
    - 'Flug: 06.10.2025 | Flugnummer: EW 9083 (BASIC H)'

    Returns (departure_date, return_date)
    """
    # Combined pattern for both German & English
    flight_pattern = re.compile(
        r"(?:Flight|Flug):\s*(\d{1,2}\.\d{1,2}\.\d{4}).*?(?:Flight Number|Flugnummer)[:\s]*?(EW\s*\d{3,4})",
        re.IGNORECASE,
    )

    flights = []
    for line in lines:
        m = flight_pattern.search(line)
        if m:
            date_str, flight_no = m.groups()
            try:
                date = datetime.strptime(date_str, "%d.%m.%Y")
                flight_no = flight_no.replace(" ", "")
                flights.append((date, flight_no))
            except ValueError:
                continue

    if not flights:
        return None, None

    # Sort by date just in case the order in the PDF is reversed
    flights.sort(key=lambda x: x[0])

    dep = flights[0][0]
    ret = flights[1][0] if len(flights) > 1 else None

    return dep, ret


def parse_name_from_text(lines: list[str]) -> str | None:
    """Return the passenger name by checking against a predefined list."""
    text = " ".join(lines).lower()
    for name in KNOWN_PASSENGERS:
        # match both "First Last" and "LAST/FIRST" variants
        normalized = name.lower()
        reversed_variant = " ".join(normalized.split()[::-1])
        slash_variant = normalized.replace(" ", "/")
        if normalized in text or reversed_variant in text or slash_variant in text:
            return name
    return None


def parse_net_cost_from_text(lines: List[str]) -> Optional[float]:
    """
    Extracts the net cost from a line containing '19% VAT' or '19 % MwSt'
    and three Euro amounts. Example:
      (3)* 19% VAT (17.95 €) 94.47 € 112.42 €
    or
      (3)* 19 % MwSt (17,95 €) 94,47 € 112,42 €
    """
    vat_pattern = re.compile(r"19\s?%[\s]*(?:VAT|MwSt)", re.IGNORECASE)
    euro_pattern = re.compile(r"(\d{1,3}(?:[.,]\d{2}))\s*€")

    for line in lines:
        if vat_pattern.search(line):
            euro_values = euro_pattern.findall(line)
            # Expecting 2 amounts: (VAT, net; gross is in a separate cell, because the layout is a table)
            if len(euro_values) >= 2:
                net_str = euro_values[1].replace(",", ".")
                try:
                    return float(net_str)
                except ValueError:
                    pass
    return None


def format_date(d: datetime, label: str = "") -> str:
    # Change here if you prefer DD.MM.YYYY:
    if label == "hinflug":
        return d.strftime("%d.%m.")
    else:
        return d.strftime("%d.%m.%Y")
    # return d.strftime("%Y-%m-%d")


def extract_lines(pdf_path: Path) -> List[str]:
    text = extract_text(str(pdf_path)) or ""
    # Normalize some whitespace and split into lines
    text = re.sub(r"\r", "\n", text)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    return lines


def build_filename(
    passenger: Optional[str], dep: Optional[datetime], ret: Optional[datetime], net_cost: Optional[float]
) -> Optional[str]:
    if not passenger or not dep:
        return None

    base = f"Flug, {sanitize_filename_component(passenger)}, {format_date(dep, 'hinflug')}"
    if ret:
        base += f"-{format_date(ret, 'rueckflug')}"
    if net_cost:
        base += f", netcost {net_cost:.2f}"

    return base + ".pdf"


def process_file(pdf_path: Path, dry_run: bool = False) -> Tuple[bool, str]:
    try:
        lines = extract_lines(pdf_path)
        passenger = parse_name_from_text(lines)
        dep, ret = parse_dates_from_text(lines)
        net_cost = parse_net_cost_from_text(lines)
        new_name = build_filename(passenger, dep, ret, net_cost)
        if not new_name:
            return (
                False,
                f"[{pdf_path.name}] Konnte erforderliche Daten nicht sicher ermitteln (Name oder Abflugdatum fehlt).",
            )

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
