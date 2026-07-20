import argparse
import json
import re
import sys
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("Не встановлено pdfplumber. Виконайте: pip install -r requirements.txt")


# 1. РОЗКЛАДАННЯ ТЕКСТУ ПО КОЛОНКАХ
def _column_starts(words, k=3, min_sep=40):
    """Ліві краї колонок = найчастіші x0 (кожен рядок кладе перше слово на старт колонки)."""
    from collections import Counter
    cnt = Counter(round(w["x0"] / 3) * 3 for w in words)
    starts = []
    for x, _ in cnt.most_common():
        if all(abs(x - s) >= min_sep for s in starts):
            starts.append(x)
        if len(starts) >= k:
            break
    return sorted(starts) or [0]


def _column_of(x, starts):
    """Колонка = найправіший старт, що ≤ x (широкі поля не розриваються між колонками)."""
    col = 0
    for i, s in enumerate(starts):
        if x >= s - 1:
            col = i
    return col


def _linearize(words, y_tol=3):
    """Слова однієї колонки -> текст: групуємо в рядки за top, сортуємо зліва направо."""
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(w["top"] - lines[-1]["top"]) <= y_tol:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    out = []
    for ln in lines:
        toks = sorted(ln["words"], key=lambda w: w["x0"])
        out.append(" ".join(t["text"] for t in toks))
    return "\n".join(out)


def extract_text_by_columns(pdf_path: Path) -> str:
    """Повертає текст, де кожна колонка кожної сторінки лінеаризована окремо."""
    blocks = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            if not words:
                continue
            starts = _column_starts(words)
            cols = {}
            for w in words:
                cols.setdefault(_column_of(w["x0"], starts), []).append(w)
            for c in sorted(cols):
                blocks.append(_linearize(cols[c]))
    return "\n\n".join(blocks)


# 2. ОПИС ПОЛІВ
_F = re.IGNORECASE | re.UNICODE
_FD = _F | re.DOTALL


def _to_number(raw):
    """'4 210,96 zł' -> 4210.96"""
    s = re.sub(r"[^\d,.\-]", "", raw).replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _clean(v):
    return re.sub(r"\s+", " ", v).strip(" .,:;-\n")


# key -> (pattern, flags, cast|None)
FIELDS = {
    # --- Poliса / Umowa ----------------------------------------------------
    "numer_polisy":              (r"numer\s+polisy:?\s*([0-9]{5,})", _F, None),
    # мітка й значення часто на різних рядках, між ними — текст сусідньої колонки
    "okres_ubezpieczenia":       (r"okres\s+ubezpieczenia:.*?(\d{2}\.\d{2}\.\d{4}\s*r\.\s*godz\.\s*\d{2}:\d{2}\s*[–\-]\s*\d{2}\.\d{2}\.\d{4}\s*r\.)", _FD, _clean),
    "nr_umowy_generalnej_pzu":   (r"nr\s+umowy\s+generalnej\s+PZU:?\s*([A-Z0-9]+)", _F, None),
    "nr_umowy_generalnej_klienta": (r"nr\s+umowy\s+generalnej\s+klienta:?\s*([A-Z0-9]+)", _F, None),

    # --- Ubezpieczający (страхувальник) -----------------------------------
    "ubezpieczajacy_nazwa":      (r"Ubezpieczaj[ąa]cy\s*(.+?)\s*REGON", _FD, _clean),
    "ubezpieczajacy_regon":      (r"REGON:?\s*(\d{8,14})", _F, None),
    # перша "adres:" — блок Właściciel (повна адреса; у колонці Ubezpieczający вона розірвана)
    "ubezpieczajacy_adres":      (r"adres:?\s*(.+?)\s*e-?mail", _FD, _clean),
    "ubezpieczajacy_email":      (r"e-?mail:?\s*([^\s]+@[^\s]+)", _F, None),
    "ubezpieczajacy_telefon":    (r"telefon:?\s*(\+?\d[\d ]{6,20})", _F, _clean),

    # --- Płatności ---------------------------------------------------------
    "ubezpieczyciel_odbiorca":   (r"odbiorca:?\s*(.+?)(?=\s+\d|\n|$)", _F, _clean),  # страховик (PZU SA)
    "nr_rachunku":               (r"nr\s+rachunku:?\s*(\d{2}(?:\s?\d{4}){6})", _F, _clean),
    "kwota":                     (r"kwota:?\s*([\d ]+,\d{2})\s*z[łl]", _F, _to_number),
    "platnosc_czestotliwosc":    (r"p[łl]atno[śs][ćc]\s*:\s*(\w+)", _F, _clean),  # двокрапка обов'язкова, щоб не ловити "Płatności"
}

# Гармонограм платежів: усі рядки виду "10.06.2026 r. – 1 054,96 zł"
_HARMONOGRAM = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s*r\.\s*[–\-]\s*([\d ]+,\d{2})\s*z[łl]", _F)


# 3. ЛОГІКА
def parse_fields(text: str) -> dict:
    result = {}
    for key, (pattern, flags, cast) in FIELDS.items():
        m = re.search(pattern, text, flags)
        if not m:
            result[key] = None
            continue
        val = m.group(1)
        result[key] = cast(val) if cast else val.strip()

    # похідні дати з okres_ubezpieczenia
    okres = result.get("okres_ubezpieczenia") or ""
    dates = re.findall(r"\d{2}\.\d{2}\.\d{4}", okres)
    result["data_od"] = dates[0] if dates else None
    result["data_do"] = dates[-1] if len(dates) > 1 else None

    # гармонограм платежів
    result["harmonogram_platnosci"] = [
        {"data": d, "kwota": _to_number(k)} for d, k in _HARMONOGRAM.findall(text)
    ]
    return result


def parse_pdf(pdf_path: Path) -> dict:
    text = extract_text_by_columns(pdf_path)
    if not text.strip():
        return {
            "source_file": pdf_path.name,
            "_warning": "Порожній текстовий шар — ймовірно скан. Потрібен OCR.",
            "fields": {},
        }
    return {"source_file": pdf_path.name, "fields": parse_fields(text)}


# 4. CLI
def main():
    ap = argparse.ArgumentParser(description="Парсер польського полісу PZU → JSON")
    ap.add_argument("pdf", type=Path, help="Шлях до PDF")
    ap.add_argument("-o", "--output", type=Path, help="Куди зберегти JSON")
    ap.add_argument("--dump-text", action="store_true", help="Показати текст по колонках і вийти")
    args = ap.parse_args()

    if not args.pdf.exists():
        sys.exit(f"Файл не знайдено: {args.pdf}")

    if args.dump_text:
        print(extract_text_by_columns(args.pdf))
        return

    result = parse_pdf(args.pdf)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
        print(f"Збережено: {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
