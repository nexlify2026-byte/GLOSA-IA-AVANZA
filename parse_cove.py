#!/usr/bin/env python3
"""
Parser de Detalle de COVE (PDF) -> JSON estructurado para glosa factura_vs_cove.

Usa extract_tables() de pdfplumber (no texto plano) porque el COVE es un
documento de tablas con bordes — la extracción por celdas separa limpiamente
campos ambiguos como Marca/Modelo/Sub-Modelo/Serie.

Soporta múltiples COVEs concatenados en un mismo PDF: cada COVE inicia con el
marcador "Datos del Acuse de Valor COVExxxxx".

Uso:  python3 parse_cove.py <ruta_pdf> [salida.json]
Import: from parse_cove import parse_coves, es_cove
"""

import json
import re
import sys

import pdfplumber


# ─────────────────────────────────────────────────────────────
# Detección
# ─────────────────────────────────────────────────────────────
def es_cove(ruta_pdf: str) -> bool:
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = pdf.pages[0].extract_text() or ""
        return "Datos del Acuse de Valor" in texto or "Información de Valor y de Comercialización" in texto
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────────────────────
def _num(valor) -> float | None:
    """'$20,397.600000' / '1,440.000' -> float. None si no es número."""
    if valor is None:
        return None
    s = str(valor).replace("$", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _limpio(celda) -> str:
    return ("" if celda is None else str(celda)).strip()


def _fila_de_datos(tabla, etiqueta_fila: str, col: int = 0) -> str:
    """Devuelve el valor de la fila SIGUIENTE a la que contiene `etiqueta_fila`."""
    for i, fila in enumerate(tabla):
        if fila and _limpio(fila[0]).upper().startswith(etiqueta_fila.upper()):
            if i + 1 < len(tabla):
                return _limpio(tabla[i + 1][col])
    return ""


def _col_index(header_row, etiqueta: str) -> int | None:
    for idx, celda in enumerate(header_row):
        if _limpio(celda).upper().startswith(etiqueta.upper()):
            return idx
    return None


# ─────────────────────────────────────────────────────────────
# Parsers de bloques específicos
# ─────────────────────────────────────────────────────────────
def _es_tabla_operacion(t) -> bool:
    return any("Tipo de Operación" in _limpio(c) for row in t for c in row)


def _es_tabla_identificador(t) -> bool:
    return t and any("Tipo de identificador" in _limpio(c) for c in t[0])


def _es_tabla_domicilio(t) -> bool:
    return t and any(_limpio(c).startswith("Calle") for c in t[0])


def _es_tabla_mercancia(t) -> bool:
    return t and any("Descripción genérica" in _limpio(c) for c in t[0])


def _es_tabla_marca(t) -> bool:
    return t and _limpio(t[0][0]).startswith("Marca")


def _marca_tiene_datos(t) -> bool:
    return _es_tabla_marca(t) and len(t) > 1 and _limpio(t[1][0]) != ""


def _parse_operacion(t) -> dict:
    out = {"tipo_operacion": "", "relacion_factura": "", "num_factura": "",
           "fecha_exp_cove": ""}
    for i, fila in enumerate(t):
        c0 = _limpio(fila[0])
        c1 = _limpio(fila[1]) if len(fila) > 1 else ""
        if c0 == "Tipo de Operación" and i + 1 < len(t):
            out["tipo_operacion"] = _limpio(t[i + 1][0])
            out["relacion_factura"] = _limpio(t[i + 1][1]) if len(t[i + 1]) > 1 else ""
        elif c0 == "No. de factura" and i + 1 < len(t):
            out["num_factura"] = _limpio(t[i + 1][0])
        elif c0 == "Tipo de figura" and i + 1 < len(t):
            # la fecha exp está en col 1 de la fila siguiente
            out["fecha_exp_cove"] = _limpio(t[i + 1][1]) if len(t[i + 1]) > 1 else ""
    return out


def _parse_parte(tabla_id, tabla_dom) -> dict:
    """Proveedor o destinatario: tabla de identificador + tabla de domicilio."""
    parte = {"nombre": "", "tax_id": "", "rfc": "", "pais": ""}

    # Identificador + nombre
    for i, fila in enumerate(tabla_id):
        c0 = _limpio(fila[0])
        if c0 in ("TAX ID", "RFC", "CURP", "Sin Tax ID") and len(fila) > 1:
            ident = _limpio(fila[1])
            if c0 == "RFC":
                parte["rfc"] = ident
            else:
                parte["tax_id"] = ident
        if c0 == "Nombre(s) o Razón Social" and i + 1 < len(tabla_id):
            parte["nombre"] = _limpio(tabla_id[i + 1][0])

    # País (del domicilio)
    if tabla_dom:
        for i, fila in enumerate(tabla_dom):
            idx_pais = _col_index(fila, "País")
            if idx_pais is not None and i + 1 < len(tabla_dom):
                parte["pais"] = _limpio(tabla_dom[i + 1][idx_pais])
                break
    return parte


def _parse_mercancia(tabla_merc, tabla_marca) -> dict:
    m = {"descripcion": "", "umc": "", "cantidad": None, "moneda": "",
         "valor_unitario": None, "valor_total": None,
         "marca": "", "modelo": "", "submodelo": "", "serie": ""}

    # Tabla mercancía: fila1 = desc/umc/cantidad, fila3 = moneda/valores
    for i, fila in enumerate(tabla_merc):
        c0 = _limpio(fila[0])
        if "Descripción genérica" in c0 and i + 1 < len(tabla_merc):
            datos = tabla_merc[i + 1]
            vals = [_limpio(x) for x in datos if x is not None]
            # estructura: [descripcion, umc, cantidad]
            if len(vals) >= 3:
                m["descripcion"] = vals[0]
                m["umc"] = vals[1]
                m["cantidad"] = _num(vals[2])
            elif len(vals) == 2:
                m["descripcion"] = vals[0]
                m["cantidad"] = _num(vals[1])
        if c0 == "Tipo Moneda" and i + 1 < len(tabla_merc):
            datos = tabla_merc[i + 1]
            vals = [_limpio(x) for x in datos if x is not None]
            # [moneda, valor_unitario, valor_total, valor_dolares]
            if len(vals) >= 3:
                m["moneda"] = vals[0]
                m["valor_unitario"] = _num(vals[1])
                m["valor_total"] = _num(vals[2])

    # Tabla marca: fila1 = [marca, modelo, submodelo, serie]
    if tabla_marca and len(tabla_marca) > 1:
        fila = tabla_marca[1]
        m["marca"] = _limpio(fila[0]) if len(fila) > 0 else ""
        m["modelo"] = _limpio(fila[1]) if len(fila) > 1 else ""
        m["submodelo"] = _limpio(fila[2]) if len(fila) > 2 else ""
        m["serie"] = _limpio(fila[3]) if len(fila) > 3 else ""
    return m


# ─────────────────────────────────────────────────────────────
# Parser principal
# ─────────────────────────────────────────────────────────────
def parse_coves(ruta_pdf: str) -> list[dict]:
    """Devuelve una lista de COVEs (uno o varios) parseados del PDF."""
    # Segmentar páginas por COVE
    segmentos = []  # [(cove_num, [tablas...]), ...]
    cove_re = re.compile(r"Datos del Acuse de Valor\s+(COVE\w+)")

    with pdfplumber.open(ruta_pdf) as pdf:
        actual = None
        for page in pdf.pages:
            texto = page.extract_text() or ""
            m = cove_re.search(texto)
            if m:
                actual = {"cove": m.group(1), "tablas": []}
                segmentos.append(actual)
            if actual is None:
                # página antes del primer marcador (raro) -> crear genérico
                actual = {"cove": "", "tablas": []}
                segmentos.append(actual)
            actual["tablas"].extend(page.extract_tables() or [])

    resultados = []
    for seg in segmentos:
        tablas = seg["tablas"]
        cove = {
            "cove": seg["cove"],
            "num_factura": "",
            "fecha_exp_cove": "",
            "relacion_factura": "",
            "proveedor": {},
            "importador": {},
            "mercancias": [],
            "valor_total_cove": 0.0,
        }

        # Operación
        for t in tablas:
            if _es_tabla_operacion(t):
                op = _parse_operacion(t)
                cove["num_factura"] = op["num_factura"]
                cove["fecha_exp_cove"] = op["fecha_exp_cove"]
                cove["relacion_factura"] = op["relacion_factura"]
                break

        # Proveedor (1er id+dom) y destinatario (2do id+dom), en orden
        ids = [t for t in tablas if _es_tabla_identificador(t)]
        doms = [t for t in tablas if _es_tabla_domicilio(t)]
        if len(ids) >= 1:
            cove["proveedor"] = _parse_parte(ids[0], doms[0] if len(doms) >= 1 else None)
        if len(ids) >= 2:
            cove["importador"] = _parse_parte(ids[1], doms[1] if len(doms) >= 2 else None)

        # Mercancías: emparejar tabla-mercancía con la siguiente tabla-marca con datos
        merc_tabs = [t for t in tablas if _es_tabla_mercancia(t)]
        marca_tabs = [t for t in tablas if _marca_tiene_datos(t)]
        for i, mt in enumerate(merc_tabs):
            marca = marca_tabs[i] if i < len(marca_tabs) else None
            m = _parse_mercancia(mt, marca)
            cove["mercancias"].append(m)

        cove["valor_total_cove"] = round(
            sum(m["valor_total"] or 0 for m in cove["mercancias"]), 2
        )
        resultados.append(cove)

    return resultados


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 parse_cove.py <ruta_pdf> [salida.json]")
        sys.exit(1)
    data = parse_coves(sys.argv[1])
    salida = json.dumps(data, ensure_ascii=False, indent=2)
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(salida)
        print(f"OK -> {sys.argv[2]} ({len(data)} COVE(s))")
    else:
        print(salida)
