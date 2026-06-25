#!/usr/bin/env python3
"""
Parser de Relación de Facturas (PDF) -> JSON estructurado para glosa.

Su único rol en la glosa factura_vs_cove: aportar el mapa
    num_factura -> fecha de factura
(además de datos de encabezado útiles). El cotejo de Puntos 2-6 sale del COVE.

Uso:  python3 parse_relacion.py <ruta_pdf> [salida.json]
Import: from parse_relacion import parse_relacion, es_relacion
"""

import json
import re
import sys

import pdfplumber


def es_relacion(ruta_pdf: str) -> bool:
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = pdf.pages[0].extract_text() or ""
        return "NO. PEDIMENTO" in texto and "CANTIDAD TOTAL (UMC)" in texto
    except Exception:
        return False


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _buscar(patron: str, texto: str, grupo: int = 1) -> str:
    m = re.search(patron, texto)
    return m.group(grupo).strip() if m else ""


def parse_relacion(ruta_pdf: str) -> dict:
    with pdfplumber.open(ruta_pdf) as pdf:
        texto = "\n".join(p.extract_text() or "" for p in pdf.pages)

    lineas = texto.split("\n")

    encabezado = {
        "pedimento":     _buscar(r"NO\. PEDIMENTO\s+(\d+)", texto),
        "cve_documento": _buscar(r"CVE\. DOCUMENTO\s+(\w+)", texto),
        "remesa":        _buscar(r"REMESA\s+(\d+)", texto),
        "fecha_relacion": _buscar(r"FECHA\s+(\d{2}/\d{2}/\d{4})", texto),
        "aduana":        _buscar(r"ADUANA/SECCIÓN\s+(\d+)", texto),
        "patente":       _buscar(r"PATENTE\s+(\d+)", texto),
        "referencia":    _buscar(r"REFERENCIA\s+(\S+)", texto),
        "tipo_cambio":   _num(_buscar(r"TIPO DE CAMBIO\s+([\d.,]+)", texto)),
    }
    importador = {
        "rfc":    _buscar(r"RFC DEL IMPORTADOR/EXPORTADOR\s+(\S+)", texto),
        "nombre": _buscar(r"NOMBRE DEL IMPORTADOR/EXPORTADOR\s+(.+)", texto),
    }

    # Filas de facturas: entre el encabezado de tabla y "Totales"
    fila_re = re.compile(
        r"^(\d+)\s+(.+?)\s+(\d{2}/\d{2}/\d{4})\s+"
        r"([\d,]+\.\d+)\s+([\d,]+\.\d+)(?:\s+(.*))?$"
    )
    facturas = []
    en_tabla = False
    for ln in lineas:
        if "NO. FACTURA" in ln and "CANTIDAD TOTAL" in ln:
            en_tabla = True
            continue
        if ln.strip().startswith("Totales"):
            en_tabla = False
            continue
        if en_tabla:
            m = fila_re.match(ln.strip())
            if m:
                facturas.append({
                    "consecutivo":  int(m.group(1)),
                    "num_factura":  m.group(2).strip(),
                    "fecha":        m.group(3),
                    "valor_dlls":   _num(m.group(4)),
                    "cantidad_umc": _num(m.group(5)),
                    "certificaciones": (m.group(6) or "").strip(),
                })

    return {
        **encabezado,
        "importador": importador,
        "facturas": facturas,
        "total_facturas": len(facturas),
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 parse_relacion.py <ruta_pdf> [salida.json]")
        sys.exit(1)
    data = parse_relacion(sys.argv[1])
    salida = json.dumps(data, ensure_ascii=False, indent=2)
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(salida)
        print(f"OK -> {sys.argv[2]} ({data['total_facturas']} factura(s))")
    else:
        print(salida)
