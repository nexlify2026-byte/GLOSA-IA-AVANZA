#!/usr/bin/env python3
"""
Parser de Pedimento PDF -> JSON estructurado y limpio para glosa con IA.

Diseño:
- Jerárquico: cada partida contiene sus propios NOMs, contribuciones e
  identificadores anidados (imposible mezclar entre fracciones).
- Sin redundancia: PEDIMENTO/FRACCION/SECUENCIA no se repiten en cada registro.
- Sin datos inservibles para glosa: e.firma, certificado, línea de captura,
  operación bancaria, transacción SAT, agente aduanal, encabezados de página.
- Valores corregidos: valor_aduana_mxn, precio_pagado_mxn, precio_unitario_mxn
  y valor_dolares_calc (= precio_pagado / tipo_cambio), que es lo que cruza
  con el COVE y el PDF NO imprime por partida.

Uso: python3 parse_pedimento.py <ruta_pdf> <ruta_salida.json>
"""

import sys
import re
import json
import pdfplumber


SKIP_PATTERNS = [
    r"^AGENTE ADUANAL",
    r"^NOMBRE O RAZ\. SOC:",
    r"^Clave en el RFC:ALO",
    r"^NUMERO DE SERIE DEL CERTIFICADO",
    r"^e\.firma:",
    r"^El pago de las contribuciones",
    r"^posibilidad de que la cuenta",
    r"^El agente aduanal,",
    r"^campo correspondiente",
    r"^solicitar la certificación",
    r"^elaboración del pedimento",
    r"^2a\. COPIA:",
    r"^ANEXO DEL PEDIMENTO",
    r"^PEDIMENTO PAGINA",
    r"^NUM\. PEDIMENTO:.*TIPO\.OPER",
    r"^CURP:\s*$",
    r"^[A-Za-z0-9+/]{100,}",  # firma electronica (base64-like)
    r"^PARTIDAS$",
    r"^FRACCION SUBD\. / N",
    r"^IDENTIFICAC-$",
    r"^SEC I[ÓO]N COMERCIAL",
    r"^DESCRIPCION$",
    r"^VAL\. ADU / USD",
    r"^IDENTIF\. COMPLEMENTO",
    r"^\*{8} FIN DE PEDIMENTO",
    r"^DATOS DEL PROVEEDOR",
    r"^DEPOSITO REFERENCIADO",
    r"^\*\*\*PAGO ELECTRONICO",
    r"^BANCO:",
    r"^LINEA DE CAPTURA:",
    r"^IMPORTE PAGADO:",
    r"^NUMERO DE OPERACION",
    r"^NUMERO DE TRANSACCION",
    r"^MEDIO DE PRESENTACION",
    r"^MEDIO DE RECEPCION",
    r"^CODIGO DE BARRAS",
    r"^CERTIFICACIONES",
]

LEGAL_SUFFIX_RE = re.compile(
    r"(.*?(?:S\.A\. DE C\.V\.|S DE RL DE CV|S\. DE R\.L\. DE C\.V\.|"
    r"CO\.,? LTD\.?|CO\.,? INC\.?|,? ?INC\.?|,? ?CORP\.?|,? ?LLC|,? ?LTD\.?|"
    r"GMBH|S\.A\.|CO\.))(?:\s+|$)",
    re.IGNORECASE,
)


def is_noise(line):
    for pat in SKIP_PATTERNS:
        if re.match(pat, line):
            return True
    return False


def get_clean_lines(full_text):
    out = []
    for line in full_text.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if is_noise(line):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# HEADER
# ---------------------------------------------------------------------------

def parse_header(full_text):
    h = {}

    m = re.search(r"NUM\. PEDIMENTO:\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)", full_text)
    if m:
        h["anio"], h["aduana_pedimento"], h["patente"], h["num_pedimento"] = m.groups()

    for key, pat in [
        ("tipo_operacion", r"T\.OPER:\s*(\S+)"),
        ("clave_pedimento", r"CVE\. PEDIMENTO:\s*(\S+)"),
        ("regimen", r"REGIMEN:\s*(\S+)"),
        ("destino", r"DESTINO:\s*(\d+)"),
        ("tipo_cambio", r"TIPO CAMBIO:\s*([\d.]+)"),
        ("peso_bruto", r"PESO BRUTO:\s*([\d.,]+)"),
        ("aduana_entrada_salida", r"ADUANA E/S:\s*(\d+)"),
        ("valor_dolares", r"VALOR DOLARES:\s*([\d.,]+)"),
        ("valor_aduana", r"VALOR ADUANA:\s*([\d.,]+)"),
        ("precio_pagado_valor_comercial", r"PRECIO PAGADO/VALOR COMERCIAL:\s*([\d.,]+)"),
        ("fecha_entrada", r"ENTRADA:\s*(\d{2}/\d{2}/\d{4})"),
        ("fecha_pago", r"PAGO:\s*(\d{2}/\d{2}/\d{4})"),
        ("bultos", r"MARCAS, NUMEROS Y TOTAL DE BULTOS:\s*(.+)"),
    ]:
        m = re.search(pat, full_text)
        if m:
            h[key] = m.group(1).strip()

    # Importador
    importador = {}
    m = re.search(r"Clave en el RFC:\s*(\S+)\s*NOMBRE", full_text)
    if m:
        importador["rfc"] = m.group(1)
    m = re.search(r"NOMBRE, DENOMINACION O RAZON SOCIAL\s*\n(?:CURP:\s*\n)?(.+)", full_text)
    if m:
        nombre = re.sub(r"^CURP:\s*", "", m.group(1).strip()).strip()
        importador["nombre"] = nombre
    m = re.search(r"DOMICILIO:\s*(.+)", full_text)
    if m:
        importador["domicilio"] = m.group(1).strip()
    h["importador"] = importador

    # Incrementables
    m = re.search(
        r"VAL\. SEGUROS\s+SEGUROS\s+FLETES\s+EMBALAJES\s+OTROS INCREMENTABLES\s*\n\s*"
        r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
        full_text,
    )
    if m:
        h["incrementables"] = {
            "valor_comercial": m.group(1), "seguros": m.group(2),
            "fletes": m.group(3), "embalajes": m.group(4),
            "otros_incrementables": m.group(5),
        }

    # Tasas a nivel pedimento (seccion "TASAS A NIVEL PEDIMENTO")
    tasas = []
    sec = re.search(r"TASAS A NIVEL PEDIMENTO(.+?)CUADRO DE LIQUIDACION", full_text, re.DOTALL)
    if sec:
        for tm in re.finditer(
            r"\b(DTA|PRV|IVA PRV|IGI|IVA|CC|ISAN|ISARTA|TRA|REC)\s+(\d+)\s+([\d.]+)",
            sec.group(1),
        ):
            tasas.append({"contribucion": tm.group(1), "cve_tasa": tm.group(2), "tasa": tm.group(3)})
    h["tasas_pedimento"] = tasas

    # Cuadro de liquidacion (seccion "CUADRO DE LIQUIDACION")
    liq = []
    sec = re.search(r"CUADRO DE LIQUIDACION(.+?)(?:DEPOSITO REFERENCIADO|DATOS DEL PROVEEDOR)",
                    full_text, re.DOTALL)
    if sec:
        for lm in re.finditer(
            r"\b(DTA|IVA PRV|IVA|PRV|IGI|CC|ISAN|ISARTA|TRA|REC)\s+(\d+)\s+(\d+)\b",
            sec.group(1),
        ):
            liq.append({"concepto": lm.group(1), "fp": lm.group(2), "importe": lm.group(3)})
        tm = re.search(r"TOTAL\s+(\d+)", sec.group(1))
        if tm:
            h["total_pagado"] = tm.group(1)
    h["liquidacion"] = liq

    return h


# ---------------------------------------------------------------------------
# IDENTIFICADORES A NIVEL PEDIMENTO (ED, PP, SO, etc.)
# ---------------------------------------------------------------------------

def parse_identificadores_pedimento(full_text):
    ids = []
    in_block = False
    for line in full_text.splitlines():
        line = line.strip()
        if "CLAVE /COMPL.IDENTIFICADOR" in line:
            in_block = True
            continue
        if in_block:
            pm = re.match(r"^(PP|ED|SO|CT|CO)\s+(\S+)", line)
            if pm:
                ids.append({"clave": pm.group(1), "valor": pm.group(2)})
            elif line.startswith("PARTIDAS") or re.match(r"^\d{1,2}\s+\d{8}", line):
                break
    return ids


# ---------------------------------------------------------------------------
# PROVEEDORES (505)
# ---------------------------------------------------------------------------

def split_nombre_domicilio(texto):
    """Separa razon social del domicilio usando sufijos de forma legal."""
    m = LEGAL_SUFFIX_RE.match(texto)
    if m and m.group(1).strip():
        nombre = m.group(1).strip()
        domicilio = texto[m.end():].strip()
        return nombre, domicilio
    return texto.strip(), ""


def parse_proveedores(clean_lines):
    proveedores = []
    i, n = 0, len(clean_lines)

    while i < n:
        line = clean_lines[i]
        if line.startswith("ID. FISCAL NOMBRE"):
            i += 1
            if i >= n:
                break
            prov_line = clean_lines[i]
            pm = re.match(r"^(\S+)\s+(.+?)\s{2,}(.+?)\s+(SI|NO)\s*$", prov_line)
            if pm:
                id_fiscal, nombre, domicilio, vinc = pm.groups()
            else:
                pm2 = re.match(r"^(\S+)\s+(.+)\s+(SI|NO)\s*$", prov_line)
                if not pm2:
                    i += 1
                    continue
                id_fiscal, resto, vinc = pm2.groups()
                nombre, domicilio = split_nombre_domicilio(resto)

            i += 1
            if i < n and not clean_lines[i].startswith("NUM.FACTURA"):
                domicilio = (domicilio + " " + clean_lines[i].strip()).strip()
                i += 1

            pais = ""
            pm = re.search(r"\b([A-Z]{3})\s*$", domicilio)
            if pm:
                pais = pm.group(1)

            facturas, i = _read_facturas(clean_lines, i, n)
            proveedores.append({
                "id_fiscal": id_fiscal, "nombre": nombre.strip(),
                "domicilio": domicilio.strip(), "pais": pais,
                "vinculacion": vinc, "facturas": facturas,
            })
            continue

        # Continuacion de facturas (cruce de pagina) para el ultimo proveedor
        if line.startswith("NUM.FACTURA") and proveedores:
            facturas, i = _read_facturas(clean_lines, i, n, header_present=True)
            proveedores[-1]["facturas"].extend(facturas)
            continue

        i += 1

    return proveedores


def _read_facturas(clean_lines, i, n, header_present=None):
    facturas = []
    if header_present or (i < n and clean_lines[i].startswith("NUM.FACTURA")):
        if i < n and clean_lines[i].startswith("NUM.FACTURA"):
            i += 1
        while i + 1 <= n - 1:
            num_factura = clean_lines[i].strip()
            cove_line = clean_lines[i + 1].strip()
            fm = re.match(
                r"^(\S+)\s+(\d{2}/\d{2}/\d{4})\s+(\S+)\s+(\S+)\s+([\d.,]+)\s+([\d.]+)\s+([\d.,]+)$",
                cove_line,
            )
            if not fm:
                break
            facturas.append({
                "num_factura": num_factura, "cove": fm.group(1), "fecha": fm.group(2),
                "incoterm": fm.group(3), "moneda": fm.group(4),
                "valor_factura": fm.group(5), "factor": fm.group(6),
                "valor_dolares": fm.group(7),
            })
            i += 2
    return facturas, i


# ---------------------------------------------------------------------------
# PARTIDAS (551/554/556/557/558)
# ---------------------------------------------------------------------------

PARTIDA_START_RE = re.compile(
    r"^(\d{1,2})\s+(\d{8})\s+(\d{2})\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)\s+([\d.]+)\s+"
    r"(\w{3})\s+(\w{3})\s+(.+)$"
)
CONTRIB_RE = re.compile(r"(IGI|IVA|DTA|PRV|CC|ISAN|ISARTA|TRA|REC)\s+([\d.]+)\s+(\d+)\s+(\d+)\s+(\d+)")
VAL_LINE_RE = re.compile(r"^([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)$")
NOM_RE = re.compile(r"NOM-\d{3}-[A-Z]+(?:-\d{4})?")
ID_CODES = ("EO", "PS", "TL", "PO", "MA", "CT", "CO", "ED", "TE", "XP")


def _to_float(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def parse_partidas(clean_lines, tipo_cambio, proveedores):
    partidas = []
    n = len(clean_lines)
    i = 0
    tc = _to_float(tipo_cambio) or 0
    prov_names = [p["nombre"] for p in proveedores if p.get("nombre")]

    while i < n:
        m = PARTIDA_START_RE.match(clean_lines[i])
        if not m:
            i += 1
            continue

        g = m.groups()
        partida = {
            "secuencia": int(g[0]),
            "fraccion": g[1],
            "subdivision": g[2],
            "vinculacion": g[3],
            "metodo_valoracion": g[4],
            "umc": g[5],
            "cantidad_umc": g[6],
            "umt": g[7],
            "cantidad_umt": g[8],
            "pais_origen": g[10],       # P. O/D
            "pais_vendedor": g[9],      # P. V/C
        }
        rest = g[11]

        contribuciones = []
        cm = CONTRIB_RE.search(rest)
        if cm:
            contribuciones.append({"concepto": cm.group(1), "tasa": cm.group(2), "importe": cm.group(5)})

        i += 1
        descripcion = ""
        if i < n:
            desc_line = clean_lines[i]
            cm2 = CONTRIB_RE.search(desc_line)
            if cm2:
                contribuciones.append({"concepto": cm2.group(1), "tasa": cm2.group(2), "importe": cm2.group(5)})
                descripcion = desc_line[:cm2.start()].strip()
            else:
                descripcion = desc_line.strip()
            i += 1
        partida["descripcion"] = descripcion

        # Valores: triplete = valor_aduana | precio_pagado | precio_unitario (MXN)
        if i < n:
            vm = VAL_LINE_RE.match(clean_lines[i].strip())
            if vm:
                partida["valor_aduana_mxn"] = vm.group(1)
                partida["precio_pagado_mxn"] = vm.group(2)
                partida["precio_unitario_mxn"] = vm.group(3)
                pp = _to_float(vm.group(2))
                if pp is not None and tc:
                    partida["valor_dolares_calc"] = round(pp / tc, 2)
                i += 1

        # Bloque de identificadores hasta OBSERVACIONES
        id_text_parts = []
        while i < n and not clean_lines[i].startswith("OBSERVACIONES"):
            id_text_parts.append(clean_lines[i])
            i += 1
        id_text = " ".join(id_text_parts)

        # NOMs (regulatorios). Reasociar años sueltos ("-2013") a NOMs sin año.
        normas_raw = NOM_RE.findall(id_text)
        anios_sueltos = re.findall(r"(?<!\d)-(\d{4})\b", id_text)
        # años ya pegados a un NOM no deben reusarse
        usados = set(re.findall(r"NOM-\d{3}-[A-Z]+-(\d{4})", id_text))
        libres = [a for a in anios_sueltos if a not in usados]
        normas = []
        for nom in normas_raw:
            if re.search(r"-\d{4}$", nom):
                normas.append(nom)
            elif libres:
                normas.append(nom + "-" + libres.pop(0))
            else:
                normas.append(nom)
        partida["normas"] = list(dict.fromkeys(normas))

        # Otros identificadores (excluye NOMs crudos y años sueltos)
        partida["identificadores"] = _extract_identificadores(id_text, normas_raw, prov_names)

        # Observacion + cruce con factura/COVE
        if i < n and clean_lines[i].startswith("OBSERVACIONES"):
            i += 1
            if i < n:
                obs = clean_lines[i].strip()
                om = re.search(r"FACT\s+(.+?)\s+ORD\s+(\d+)\s*\((.+?)\)", obs)
                if om:
                    partida["factura_referida"] = om.group(1).strip()
                    partida["orden"] = om.group(2).strip()
                    partida["modelo"] = om.group(3).strip()
                else:
                    partida["observacion"] = obs
                i += 1

        partidas.append(partida)

    return partidas


def _extract_identificadores(id_text, normas, prov_names):
    """Extrae EO/PS/TL/PO/etc, limpia, y descarta vacios/NOM."""
    txt = id_text
    # quitar NOMs y prefijos de norma para no duplicar
    for nom in normas:
        txt = txt.replace(nom, " ")
    txt = re.sub(r"\bEN\s+(?:U|ENOM|N)\b", " ", txt)
    txt = re.sub(r"(?<!\d)-\d{4}\b", " ", txt)  # años sueltos de NOMs partidos
    txt = re.sub(r"\b\d\.\d\b", " ", txt)        # "2.1" suelto de NOMs
    txt = re.sub(r"\s+", " ", txt).strip()

    ids = []
    tokens = txt.split()
    j = 0
    while j < len(tokens):
        tok = tokens[j]
        if tok in ID_CODES:
            k = j + 1
            valor_parts = []
            while k < len(tokens) and tokens[k] not in ID_CODES:
                valor_parts.append(tokens[k])
                k += 1
            valor = " ".join(valor_parts).strip()
            if tok == "MA" and not valor:
                j = k
                continue  # descartar MA vacio
            # solo PO arrastra nombre de proveedor partido por salto de pagina
            if tok == "PO":
                valor = _limpiar_valor_po(valor, prov_names)
            ids.append({"clave": tok, "valor": valor})
            j = k
        else:
            j += 1
    return ids


def _limpiar_valor_po(valor, prov_names):
    """Reconstituye nombre de proveedor en identificador PO (numero + razon social)."""
    if not valor:
        return valor
    m = re.match(r"^(\d+)\s+(.+)$", valor)
    if not m:
        return valor.strip()
    prefijo, resto = m.group(1) + " ", m.group(2)
    resto_norm = re.sub(r"[\s.,]", "", resto).upper()
    for pn in prov_names:
        pn_norm = re.sub(r"[\s.,]", "", pn).upper()
        if resto_norm and (resto_norm in pn_norm or pn_norm in resto_norm
                           or (len(resto_norm) >= 8 and resto_norm[:8] == pn_norm[:8])):
            return (prefijo + pn).strip()
    return valor.strip()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) != 3:
        print("Uso: python3 parse_pedimento.py <ruta_pdf> <ruta_salida.json>")
        sys.exit(1)

    pdf_path, out_path = sys.argv[1], sys.argv[2]

    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    header = parse_header(full_text)
    header["identificadores_pedimento"] = parse_identificadores_pedimento(full_text)

    clean_lines = get_clean_lines(full_text)
    proveedores = parse_proveedores(clean_lines)
    partidas = parse_partidas(clean_lines, header.get("tipo_cambio"), proveedores)

    resultado = {
        "pedimento": header,
        "proveedores": proveedores,
        "partidas": partidas,
        "resumen": {
            "total_proveedores": len(proveedores),
            "total_facturas": sum(len(p["facturas"]) for p in proveedores),
            "total_partidas": len(partidas),
        },
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, indent=2)

    print(f"Pedimento: {header.get('num_pedimento','')}")
    print(f"Proveedores: {resultado['resumen']['total_proveedores']}")
    print(f"Facturas/COVEs: {resultado['resumen']['total_facturas']}")
    print(f"Partidas: {resultado['resumen']['total_partidas']}")
    print(f"Salida: {out_path}")


if __name__ == "__main__":
    main()
