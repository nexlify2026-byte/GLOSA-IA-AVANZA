#!/usr/bin/env python3
"""
Extractor POSICIONAL de identificadores de partida del pedimento.

El texto plano de pdfplumber colapsa las dos columnas (IDENTIF. izquierda y
derecha) en una sola línea y parte complementos largos (ej. NOM-050-SCFI-2004)
en dos renglones. Esto rompe el parser por regex.

Aquí se usan las coordenadas x de cada palabra para reconstruir con precisión:
  - Columna izquierda:  clave≈37  comp1≈67  comp2≈127 comp3≈187
  - Columna derecha:    clave≈247 comp1≈277 comp2≈335 comp3≈392
  - Corte entre columnas: x = 240

Devuelve: { secuencia_partida: [ {clave, complemento1, complemento2, complemento3}, ... ] }
"""

import re

import pdfplumber

COL_SPLIT = 240
LEFT_ANCHORS  = {"clave": 37,  "complemento1": 67,  "complemento2": 127, "complemento3": 187}
RIGHT_ANCHORS = {"clave": 247, "complemento1": 277, "complemento2": 335, "complemento3": 392}

PARTIDA_ROW_RE = re.compile(r"^\d+\s+\d{8}\b")


def _campo_por_x(x0: float):
    """Devuelve ('L'|'R', campo) según la posición x de la palabra."""
    if x0 < COL_SPLIT:
        anchors, col = LEFT_ANCHORS, "L"
    else:
        anchors, col = RIGHT_ANCHORS, "R"
    campo = min(anchors, key=lambda k: abs(anchors[k] - x0))
    return col, campo


def _agrupar_filas(words, tol: int = 3):
    """Agrupa palabras en filas por coordenada 'top' (con tolerancia)."""
    filas = []
    for w in sorted(words, key=lambda x: (x["top"], x["x0"])):
        if filas and abs(w["top"] - filas[-1][0]) <= tol:
            filas[-1][1].append(w)
        else:
            filas.append([w["top"], [w]])
    return [sorted(ws, key=lambda x: x["x0"]) for _, ws in filas]


def _juntar(prev: str, extra: str) -> str:
    """Une un wrap al complemento previo: sin espacio si hay guion de corte."""
    if not prev:
        return extra
    if prev.endswith("-") or extra.startswith("-"):
        return (prev + extra).strip()
    return (prev + " " + extra).strip()


def _aplicar_fila(fila, ids_columna):
    """Procesa una fila ya separada por columna -> agrega o continúa identificador."""
    campos = {"clave": [], "complemento1": [], "complemento2": [], "complemento3": []}
    for w in fila:
        _, campo = _campo_por_x(w["x0"])
        campos[campo].append(w["text"])

    clave = " ".join(campos["clave"]).strip()
    if clave:
        if clave == "PO":
            # PO: número (comp1) + nombre completo (resto) en un solo campo,
            # para evitar que el nombre se parta entre comp2/comp3.
            resto = (campos["complemento1"][1:] if campos["complemento1"] else []) \
                    + campos["complemento2"] + campos["complemento3"]
            ids_columna.append({
                "clave": "PO",
                "complemento1": (campos["complemento1"][0] if campos["complemento1"] else ""),
                "complemento2": " ".join(resto).strip(),
                "complemento3": "",
            })
        else:
            ids_columna.append({
                "clave": clave,
                "complemento1": " ".join(campos["complemento1"]).strip(),
                "complemento2": " ".join(campos["complemento2"]).strip(),
                "complemento3": " ".join(campos["complemento3"]).strip(),
            })
    elif ids_columna:
        # Fila sin clave = continuación (wrap) del identificador anterior
        prev = ids_columna[-1]
        if prev["clave"] == "PO":
            extra = " ".join(campos["complemento1"] + campos["complemento2"]
                             + campos["complemento3"]).strip()
            if extra:
                prev["complemento2"] = _juntar(prev["complemento2"], extra)
        else:
            for ck in ("complemento1", "complemento2", "complemento3"):
                extra = " ".join(campos[ck]).strip()
                if extra:
                    prev[ck] = _juntar(prev[ck], extra)


def extraer_identificadores_posicional(ruta_pdf: str) -> dict:
    resultado = {}
    seq_actual = None
    en_bloque = False
    ids_izq, ids_der = [], []

    def _flush():
        if seq_actual is not None and (ids_izq or ids_der):
            limpio = []
            for ident in ids_izq + ids_der:
                if not ident["clave"]:
                    continue
                ident = {k: v for k, v in ident.items() if v or k == "clave"}
                limpio.append(ident)
            if limpio:
                resultado.setdefault(seq_actual, []).extend(limpio)

    with pdfplumber.open(ruta_pdf) as pdf:
        filas_globales = []
        for page in pdf.pages:
            for fila in _agrupar_filas(page.extract_words()):
                filas_globales.append(fila)

    for fila in filas_globales:
        texto = " ".join(w["text"] for w in fila).strip()

        if PARTIDA_ROW_RE.match(texto):
            _flush()
            ids_izq, ids_der = [], []
            en_bloque = False
            seq_actual = int(texto.split()[0])
            continue

        if texto.startswith("IDENTIF."):
            en_bloque = True
            ids_izq, ids_der = [], []
            continue

        if texto.startswith("OBSERVACIONES"):
            _flush()
            ids_izq, ids_der = [], []
            en_bloque = False
            continue

        if en_bloque:
            izq = [w for w in fila if w["x0"] < COL_SPLIT]
            der = [w for w in fila if w["x0"] >= COL_SPLIT]
            if izq:
                _aplicar_fila(izq, ids_izq)
            if der:
                _aplicar_fila(der, ids_der)

    _flush()
    return resultado


if __name__ == "__main__":
    import json
    import sys
    data = extraer_identificadores_posicional(sys.argv[1])
    print(json.dumps(data, ensure_ascii=False, indent=2))
