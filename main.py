"""
Glosador IA — Avanza v4.0 (Gemini)
Motor: Gemini Flash 2.5 (base) + Gemini Pro 2.5 (RRNAs siempre)
Conserva el parser de pedimento PDF→JSON para restricciones_rrna y factura_vs_pedimento.
"""

import json
import logging
import math
import os
import tempfile
import time
from pathlib import Path

import pdfplumber
import google.generativeai as genai
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from prompts import GLOSAS
from parse_pedimento import (
    get_clean_lines,
    parse_header,
    parse_identificadores_pedimento,
    parse_partidas,
    parse_proveedores,
)

# ─────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("glosador")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY no configurada")

genai.configure(api_key=GEMINI_API_KEY)

FLASH_MODEL = "gemini-2.5-flash"
PRO_MODEL   = "gemini-2.5-pro"
LOTE_MAXIMO = 10

# Glosas que SIEMPRE usan Pro
GLOSAS_PRO = {"restricciones_rrna"}

# Glosas que usan el parser de pedimento (inyectan JSON)
GLOSAS_CON_PARSER = {"restricciones_rrna", "factura_vs_pedimento"}

app = FastAPI(title="Glosador IA — Avanza", version="4.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ─────────────────────────────────────────────────────────────
# Detección y parser de pedimento
# ─────────────────────────────────────────────────────────────
def es_pedimento(ruta_pdf: str) -> bool:
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = pdf.pages[0].extract_text() or ""
        return "NUM. PEDIMENTO" in texto or "NUM.PEDIMENTO" in texto
    except Exception:
        return False


def es_restricciones(ruta_pdf: str) -> bool:
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            texto = pdf.pages[0].extract_text() or ""
        return "ALERTAS POR FRACCIÓN" in texto or "ALERTAS POR FRACCION" in texto
    except Exception:
        return False


def parsear_pedimento_pdf(ruta_pdf: str) -> dict:
    with pdfplumber.open(ruta_pdf) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    header = parse_header(full_text)
    header["identificadores_pedimento"] = parse_identificadores_pedimento(full_text)
    clean_lines = get_clean_lines(full_text)
    proveedores = parse_proveedores(clean_lines)
    partidas    = parse_partidas(clean_lines, header.get("tipo_cambio"), proveedores)
    return {
        "pedimento":   header,
        "proveedores": proveedores,
        "partidas":    partidas,
        "resumen": {
            "total_proveedores": len(proveedores),
            "total_facturas":    sum(len(p["facturas"]) for p in proveedores),
            "total_partidas":    len(partidas),
        },
    }


# ─────────────────────────────────────────────────────────────
# Utilidades de respuesta
# ─────────────────────────────────────────────────────────────
def detectar_tipo_respuesta(texto: str) -> str:
    t = texto.strip()
    if t.startswith("DOCUMENTO_FALTANTE:"):
        return "doc_faltante"
    if t.startswith("REQUIERE_PRO:"):
        return "requiere_pro"
    return "completa"


def parsear_doc_faltante(texto: str) -> dict:
    r = {"documento": "", "motivo": "", "punto": ""}
    for linea in texto.strip().split("\n"):
        if linea.startswith("DOCUMENTO_FALTANTE:"):
            r["documento"] = linea.replace("DOCUMENTO_FALTANTE:", "").strip()
        elif linea.startswith("MOTIVO:"):
            r["motivo"] = linea.replace("MOTIVO:", "").strip()
        elif linea.startswith("PUNTO_AFECTADO:"):
            r["punto"] = linea.replace("PUNTO_AFECTADO:", "").strip()
    return r


# ─────────────────────────────────────────────────────────────
# Motor Gemini
# ─────────────────────────────────────────────────────────────
def subir_pdf_a_gemini(ruta_local: str, nombre_display: str):
    archivo = genai.upload_file(path=ruta_local, display_name=nombre_display,
                                 mime_type="application/pdf")
    intentos = 0
    while archivo.state.name == "PROCESSING" and intentos < 30:
        time.sleep(2)
        intentos += 1
        archivo = genai.get_file(archivo.name)
    if archivo.state.name != "ACTIVE":
        raise RuntimeError(f"'{nombre_display}' no procesado: {archivo.state.name}")
    return archivo


def llamar_gemini(modelo_id: str, partes: list, prompt_texto: str) -> str:
    contenido = list(partes) + [prompt_texto]
    modelo = genai.GenerativeModel(model_name=modelo_id)
    resp = modelo.generate_content(
        contenido,
        generation_config=genai.GenerationConfig(temperature=0.1, max_output_tokens=65536),
        request_options={"timeout": 300},
    )
    if not resp or not resp.text:
        raise RuntimeError("Gemini no retornó respuesta")
    return resp.text


def procesar_en_lotes(archivos_gemini: list, nombres: list, prompt_texto: str,
                       modelo_id: str, bloque_json: str = "") -> str:
    """Lotes para muchos archivos. bloque_json se antepone al prompt en cada lote si existe."""
    prompt_full = (bloque_json + "\n\n" + prompt_texto) if bloque_json else prompt_texto

    if len(archivos_gemini) <= LOTE_MAXIMO:
        return llamar_gemini(modelo_id, archivos_gemini, prompt_full)

    total_lotes = math.ceil(len(archivos_gemini) / LOTE_MAXIMO)
    resultados, t_fact, t_corr, t_disc = [], 0, 0, 0

    for i in range(total_lotes):
        ini = i * LOTE_MAXIMO
        fin = min(ini + LOTE_MAXIMO, len(archivos_gemini))
        prompt_lote = (
            f"{prompt_full}\n\n"
            f"NOTA: Lote {i+1} de {total_lotes}. Procesa ÚNICAMENTE los {fin-ini} "
            f"documentos de este lote y reporta solo su subtotal."
        )
        texto_lote = llamar_gemini(modelo_id, archivos_gemini[ini:fin], prompt_lote)

        if detectar_tipo_respuesta(texto_lote) == "doc_faltante":
            return texto_lote

        resultados.append(f"--- Lote {i+1}/{total_lotes} ({', '.join(nombres[ini:fin])}) ---\n{texto_lote}")

        for linea in texto_lote.split("\n"):
            if "TOTAL:" in linea:
                for parte in linea.split("|"):
                    p = parte.strip().lower()
                    try:
                        n = int("".join(filter(str.isdigit, p.split()[0])))
                    except Exception:
                        continue
                    if "factura" in p:   t_fact += n
                    elif "correcta" in p: t_corr += n
                    elif "discrepancia" in p: t_disc += n

        if i < total_lotes - 1:
            time.sleep(3)

    resumen = (
        f"\n{'='*60}\nRESUMEN CONSOLIDADO — {total_lotes} lotes procesados\n"
        f"TOTAL: {t_fact} facturas | {t_corr} correctas | {t_disc} con discrepancias"
    )
    return "\n\n".join(resultados) + resumen


# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = Path(__file__).parent / "templates" / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/glosas")
async def listar_glosas():
    return {
        key: {
            "label":           v["label"],
            "icono":           v["icono"],
            "descripcion":     v["descripcion"],
            "docs_requeridos": v["docs_requeridos"],
        }
        for key, v in GLOSAS.items()
    }


@app.post("/api/glosar")
async def glosar(
    tipo_glosa:      str = Form(...),
    archivos:        list[UploadFile] = File(...),
    contexto_previo: str = Form(""),
):
    if tipo_glosa not in GLOSAS:
        raise HTTPException(400, f"Tipo '{tipo_glosa}' no reconocido")
    if not archivos:
        raise HTTPException(400, "Adjunta al menos un documento")

    config_glosa = GLOSAS[tipo_glosa]
    prompt_texto = config_glosa["prompt"]

    # Modelo según glosa: RRNAs siempre Pro, resto Flash
    modelo_id = PRO_MODEL if tipo_glosa in GLOSAS_PRO else FLASH_MODEL
    log.info("Glosa '%s' → modelo %s", tipo_glosa, modelo_id)

    if contexto_previo.strip():
        prompt_texto = (
            "CONTEXTO PREVIO: Se había detectado un documento faltante que ahora se proporciona.\n"
            f"{contexto_previo.strip()[:2000]}\n\n"
            "Con todos los documentos disponibles, realiza la glosa completa.\n\n"
            f"{prompt_texto}"
        )

    for archivo in archivos:
        if not archivo.filename.lower().endswith(".pdf"):
            raise HTTPException(400, f"'{archivo.filename}' no es PDF")

    temp_paths, nombres = [], []
    archivos_gemini = []
    bloque_json = ""

    try:
        # Guardar todos los PDFs localmente
        for archivo in archivos:
            contenido = await archivo.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(contenido)
                temp_paths.append(tmp.name)
            nombres.append(archivo.filename)

        # ── Glosas con parser: detectar pedimento, parsear, separar ──
        if tipo_glosa in GLOSAS_CON_PARSER:
            idx_ped = next((i for i, r in enumerate(temp_paths) if es_pedimento(r)), None)
            if idx_ped is None:
                raise HTTPException(400,
                    "No se encontró el pedimento. Incluye el PDF del pedimento.")

            try:
                pedimento_json = parsear_pedimento_pdf(temp_paths[idx_ped])
            except Exception as e:
                raise HTTPException(400, f"No se pudo parsear el pedimento: {e}")

            if pedimento_json["resumen"]["total_partidas"] == 0:
                raise HTTPException(400, "No se encontraron partidas en el pedimento.")

            bloque_json = (
                "════════════════════════════════════════\n"
                "DATOS DEL PEDIMENTO (JSON estructurado)\n"
                "════════════════════════════════════════\n"
                f"{json.dumps(pedimento_json, ensure_ascii=False, indent=2)}\n"
            )

            # Para restricciones: validar que exista el archivo de restricciones
            if tipo_glosa == "restricciones_rrna":
                idx_res = next((i for i, r in enumerate(temp_paths)
                                if i != idx_ped and es_restricciones(r)), None)
                if idx_res is None:
                    return JSONResponse({
                        "tipo": "doc_faltante",
                        "documento_faltante": "Archivo de Restricciones",
                        "motivo": "Sin este archivo no es posible verificar RRNAs",
                        "punto_afectado": "Punto 1 — Restricciones completo",
                        "tipo_glosa": tipo_glosa,
                        "label": config_glosa["label"],
                        "archivos_procesados": nombres,
                    })
                # PDFs a Gemini: restricciones primero, luego soporte (todo menos pedimento)
                orden = [idx_res] + [i for i in range(len(temp_paths))
                                      if i not in (idx_ped, idx_res)]
            else:
                # factura_vs_pedimento: todos menos el pedimento
                orden = [i for i in range(len(temp_paths)) if i != idx_ped]

            rutas_subir   = [temp_paths[i] for i in orden]
            nombres_subir = [nombres[i] for i in orden]
        else:
            # Glosas sin parser: subir todo
            rutas_subir   = temp_paths
            nombres_subir = nombres

        # Subir PDFs a Gemini
        for ruta, nom in zip(rutas_subir, nombres_subir):
            archivos_gemini.append(subir_pdf_a_gemini(ruta, nom))

        # Ejecutar
        texto_respuesta = procesar_en_lotes(
            archivos_gemini, nombres_subir, prompt_texto, modelo_id, bloque_json
        )

        # Post-proceso
        tipo_resp = detectar_tipo_respuesta(texto_respuesta)
        if tipo_resp == "doc_faltante":
            info = parsear_doc_faltante(texto_respuesta)
            return JSONResponse({
                "tipo":               "doc_faltante",
                "documento_faltante": info["documento"],
                "motivo":             info["motivo"],
                "punto_afectado":     info["punto"],
                "tipo_glosa":         tipo_glosa,
                "label":              config_glosa["label"],
                "archivos_procesados": nombres,
            })

        return JSONResponse({
            "tipo":               "completa",
            "resultado":          texto_respuesta,
            "label":              config_glosa["label"],
            "modelo":             modelo_id,
            "archivos_procesados": nombres,
        })

    except HTTPException:
        raise
    except Exception as e:
        log.exception("Error en glosa '%s'", tipo_glosa)
        raise HTTPException(500, f"Error al procesar: {e}")
    finally:
        for path in temp_paths:
            try:
                os.unlink(path)
            except Exception:
                pass


@app.get("/api/health")
async def health():
    return {
        "status":      "ok",
        "version":     "4.0.0",
        "flash":       FLASH_MODEL,
        "pro":         PRO_MODEL,
        "glosas_pro":  list(GLOSAS_PRO),
        "lote_maximo": LOTE_MAXIMO,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
