#!/usr/bin/env python3
"""
verificar_asuntos.py
Analyzes .eml filenames in a folder and reports which ones will fail
to parse in the pipeline, before wasting API credits on them.

Usage:
    python verificar_asuntos.py <folder>

Example:
    python verificar_asuntos.py E:\\Tania\\Test
"""

import re
import sys
import unicodedata
from pathlib import Path

# ── Same constants as tania_pipeline.py ──────────────────────────────────────

MONTHS_ES = {
    "ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4,
    "MAYO": 5, "JUNIO": 6, "JULIO": 7, "AGOSTO": 8,
    "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12,
}

BLOG_ORIGIN_NAMES = {
    "BLOG IVAN":            "El Blog de Iván García",
    "BLOG DE IVAN":         "El Blog de Iván García",
    "BLOG TANIA":           "El Blog de Tania Quintero",
    "BLOG DE TANIA":        "El Blog de Tania Quintero",
    "BLOG IVAN Y TANIA":    "El Blog de Iván García",
    "BLOG DE TANIA":        "El Blog de Tania Quintero",
    "BLOG DESDE LA HABANA": "Blog Desde La Habana",
}

# Common month typos seen in the wild
MONTH_TYPOS = {
    "NOVIEMVBRE": "NOVIEMBRE",
    "NOVIEMRBE":  "NOVIEMBRE",
    "NOVIMBRE":   "NOVIEMBRE",
    "SEPTIEMRE":  "SEPTIEMBRE",
    "SEPIEMBRE":  "SEPTIEMBRE",
    "SEPTIEMBRE": "SEPTIEMBRE",
    "DICIEMRBE":  "DICIEMBRE",
    "DCIEMBRE":   "DICIEMBRE",
    "DICIEMBR":   "DICIEMBRE",
    "FEBREO":     "FEBRERO",
    "OCUBRE":     "OCTUBRE",
    "OCTUBER":    "OCTUBRE",
    "ENRO":       "ENERO",
    "JUIO":       "JULIO",
    "JUNIO":      "JUNIO",
    "LUZNES":     "LUNES",  # weekday typo
}

# ── Parse functions (identical to tania_pipeline.py) ─────────────────────────

def parse_subject_new_format(subject):
    m = re.match(
        r'(\d{4})-(\d{2})-(\d{2})\s+(.+?)\s+(BLOG\s+[A-Z\s]+?)\s*$',
        subject.strip(), re.IGNORECASE
    )
    if not m:
        return None
    try:
        from datetime import datetime
        dt = datetime.strptime(m.group(1) + '-' + m.group(2) + '-' + m.group(3), '%Y-%m-%d')
        return {"day": dt.day, "month": dt.month}
    except ValueError:
        return None


def parse_subject(subject):
    WORD = r'[A-Za-z\u00c0-\u024f]+'
    blog_pattern = '|'.join(re.escape(k) for k in BLOG_ORIGIN_NAMES)

    m = re.match(
        rf'(\d+)\)\s+(?:{WORD}\s+)?(\d+)\s+(?:DE\s+)?({WORD})(?:\s+(\d{{4}}))?\s*[\s.:]*(.+?)\s+({blog_pattern})',
        subject, re.IGNORECASE
    )
    if m:
        month_str = m.group(3).upper()
        if month_str in MONTHS_ES:
            return {"day": int(m.group(2)), "month": MONTHS_ES[month_str]}

    m2 = re.match(
        rf'(\d+)\)\s+(?:{WORD}\s+)?(\d+)\s+(?:DE\s+)?({WORD})(?:\s+(\d{{4}}))?\s*[\s.:]*(.+)',
        subject, re.IGNORECASE
    )
    if m2:
        month_str = m2.group(3).upper()
        if month_str in MONTHS_ES:
            return {"day": int(m2.group(2)), "month": MONTHS_ES[month_str]}

    return None


# ── Extract subject from filename ─────────────────────────────────────────────

def extract_subject(filename):
    """
    Filename format: {subject} - {Name} ({email}) - {YYYY-MM-DD} {HHMM}.eml
    Extract just the subject part.
    """
    # Strip .eml
    name = filename[:-4] if filename.lower().endswith('.eml') else filename
    # Remove trailing date/time: ' - YYYY-MM-DD HHMM' (with optional -N suffix)
    name = re.sub(r'\s*-\s*\d{4}-\d{2}-\d{2}\s+\d+(?:-\d+)?\s*$', '', name).strip()
    # Remove sender: ' - Name (email)'
    name = re.sub(r'\s*-\s*[^(]+ \([^)]+\)\s*$', '', name).strip()
    return name


# ── Diagnosis ─────────────────────────────────────────────────────────────────

def diagnose(subject, filename):
    """
    Return a list of problems, or empty list if all OK.
    """
    problems = []

    # 1. Try parsing as-is
    result = parse_subject_new_format(subject) or parse_subject(subject)
    if result:
        return []  # All good

    # 2. Look for clues about WHY it fails

    # Check for space before/after post number parenthesis: "6 )"
    if re.match(r'^\d+\s+\)', subject):
        problems.append(f'Espacio antes del paréntesis: "{subject[:6]}..." → cambiar a "{subject.lstrip()[:3].replace(" )", ")")}"')

    # Check for month typos
    words = subject.upper().split()
    for word in words:
        clean = re.sub(r'[^A-Z]', '', word)
        if clean in MONTH_TYPOS:
            problems.append(f'Posible error tipográfico en el mes: "{clean}" → ¿quizás "{MONTH_TYPOS[clean]}"?')
        # Check for close matches to valid months
        elif len(clean) >= 4:
            for valid_month in MONTHS_ES:
                if clean != valid_month and len(clean) >= len(valid_month) - 2:
                    # Simple similarity: same first 4 chars but different
                    if clean[:4] == valid_month[:4] and clean != valid_month:
                        problems.append(f'Posible error tipográfico en el mes: "{clean}" → ¿quizás "{valid_month}"?')
                        break

    # Check for missing month (day number followed directly by title/blog tag)
    m_no_month = re.match(r'(\d+)\)\s+(?:\w+\s+)?(\d+)\s+[^A-Za-z\u00c0-\u024f]', subject)
    if m_no_month or re.match(r'\d+\)\s+\w+\s+\d+\s+(BLOG|[-:])', subject, re.IGNORECASE):
        problems.append('Falta el mes en el asunto (solo aparece el número del día)')

    # Check for date range format: "25 A DOMINGO 29" or "30 A JUEVES 2 ENERO"
    if re.search(r'\d+\s+A\s+\w+\s+\d+', subject, re.IGNORECASE):
        problems.append('El asunto tiene un rango de fechas (ej: "20 AL DOMINGO 2 ENERO"). '
                        'Eliminar la segunda fecha y dejar solo la primera.')

    # No month found at all
    WORD = r'[A-Za-z\u00c0-\u024f]+'
    m_any = re.match(rf'\d+\)\s+(?:{WORD}\s+)?(\d+)\s+({WORD})', subject, re.IGNORECASE)
    if m_any:
        word_found = m_any.group(2).upper()
        if word_found not in MONTHS_ES:
            # Check if it looks like a weekday (LUNES, MARTES, etc.)
            weekdays = {'LUNES', 'MARTES', 'MIERCOLES', 'MIÉRCOLES', 'JUEVES',
                       'VIERNES', 'SABADO', 'SÁBADO', 'DOMINGO'}
            if word_found in weekdays:
                problems.append(f'Falta el mes — solo aparece el día de la semana y el número. '
                                f'Añadir el mes después del número del día.')
            elif not any(problems):
                problems.append(f'No se reconoce el mes "{word_found}". '
                                f'Verificar si hay un error tipográfico.')

    if not problems:
        problems.append('No se pudo determinar la causa exacta del fallo. '
                        'Revisar manualmente el asunto.')

    return problems


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"Error: '{folder}' no es una carpeta válida.")
        sys.exit(1)

    eml_files = sorted(folder.glob('*.eml'))
    if not eml_files:
        print(f"No se encontraron archivos .eml en {folder}")
        sys.exit(1)

    print(f"\nAnalizando {len(eml_files)} archivos en {folder}...\n")

    failures = []
    warnings = []

    for eml_path in eml_files:
        filename = eml_path.name
        subject  = extract_subject(filename)
        problems = diagnose(subject, filename)

        if problems:
            failures.append((filename, subject, problems))

    # Summary
    ok_count = len(eml_files) - len(failures)
    print(f"{'='*70}")
    print(f"  Total archivos : {len(eml_files)}")
    print(f"  ✓ Sin problemas: {ok_count}")
    print(f"  ✗ Con problemas: {len(failures)}")
    print(f"{'='*70}\n")

    if not failures:
        print("✓ Todos los archivos deberían procesarse correctamente.")
        return

    print("Archivos con problemas:\n")
    for i, (filename, subject, problems) in enumerate(failures, 1):
        print(f"  [{i}] {filename}")
        print(f"       Asunto extraído: {subject}")
        for p in problems:
            print(f"       ⚠ {p}")
        print()

    # Also save to a text file
    report_path = folder / "problemas_asuntos.txt"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(f"Reporte de problemas en asuntos de correo\n")
        f.write(f"Carpeta: {folder}\n")
        f.write(f"Total: {len(eml_files)} archivos | {len(failures)} con problemas\n\n")
        for i, (filename, subject, problems) in enumerate(failures, 1):
            f.write(f"[{i}] {filename}\n")
            f.write(f"     Asunto: {subject}\n")
            for p in problems:
                f.write(f"     ⚠ {p}\n")
            f.write("\n")

    print(f"Reporte guardado en: {report_path}")


if __name__ == '__main__':
    main()
