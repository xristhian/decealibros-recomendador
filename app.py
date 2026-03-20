import re
import os
import time
import random
import requests
from collections import defaultdict
from flask import Flask, jsonify, request

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
JUMPSELLER_LOGIN  = os.environ.get("JUMPSELLER_LOGIN")
JUMPSELLER_TOKEN  = os.environ.get("JUMPSELLER_TOKEN")
JUMPSELLER_BASE   = "https://api.jumpseller.com/v1"
STORE_URL         = "https://decealibros.cl"

ALLOWED_ORIGINS = [
    "https://decealibros.cl",
    "https://www.decealibros.cl",
    "https://xristhian.github.io",
]

# ─── Mapa género → categoría Jumpseller ──────────────────────────────────────
GENERO_CATEGORIA = {
    "Literatura chilena":          "novela-chilena",
    "Thriller y suspenso":         "novela/novela-negra",
    "Literatura juvenil":          "libros/infantil-y-juvenil/juvenil",
    "Ciencia ficción":             None,
    "Romance":                     "novela-romantica",
    "Historia y biografías":       "biografias-1",
    "Autoayuda y desarrollo personal": "libros/desarrollo-personal/autoayuda",
    "Novela literaria":            "novela",
    "Terror y horror":             "novela/novela-negra",
    "Fantasía":                    None,
    "No ficción":                  None,
}

# ─── Rate limiting ────────────────────────────────────────────────────────────
_rate_buckets: dict = defaultdict(list)
RATE_LIMIT  = 10
RATE_WINDOW = 60

def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    _rate_buckets[ip] = [t for t in _rate_buckets[ip] if now - t < RATE_WINDOW]
    if len(_rate_buckets[ip]) >= RATE_LIMIT:
        return False
    _rate_buckets[ip].append(now)
    return True

# ─── CORS ─────────────────────────────────────────────────────────────────────
def add_cors(response, origin: str):
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

def cors_options(origin):
    return add_cors(jsonify({}), origin)

def get_origin():
    return request.headers.get("Origin", "")

def get_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr).split(",")[0].strip()

# ─── Obtener productos con stock desde Jumpseller ─────────────────────────────
def obtener_productos(genero: str, limit: int = 30) -> list:
    """Trae productos con stock desde Jumpseller según el género."""
    categoria = GENERO_CATEGORIA.get(genero)

    if categoria:
        # Buscar por categoría
        params = {
            "login":     JUMPSELLER_LOGIN,
            "authtoken": JUMPSELLER_TOKEN,
            "category":  categoria,
            "status":    "available",
            "limit":     limit,
            "page":      1,
        }
    else:
        # Buscar por query de texto para géneros sin categoría directa
        query_map = {
            "Ciencia ficción":  "ciencia ficcion",
            "Fantasía":         "fantasia",
            "No ficción":       "no ficcion",
        }
        params = {
            "login":     JUMPSELLER_LOGIN,
            "authtoken": JUMPSELLER_TOKEN,
            "q":         query_map.get(genero, genero),
            "status":    "available",
            "limit":     limit,
            "page":      1,
        }

    try:
        r = requests.get(
            f"{JUMPSELLER_BASE}/products.json",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        productos = r.json()
    except Exception as e:
        print(f"ERROR Jumpseller: {e}", flush=True)
        return []

    resultado = []
    for item in productos:
        p = item.get("product", item)

        # Verificar stock
        variantes = p.get("variants", [])
        stock = sum(v.get("stock", 0) or 0 for v in variantes) or (p.get("stock", 0) or 0)
        if stock <= 0:
            continue

        # Precio
        precios = [float(v["price"]) for v in variantes if (v.get("stock") or 0) > 0 and v.get("price")]
        precio = min(precios) if precios else (float(p["price"]) if p.get("price") else None)

        # Imagen
        imagenes = p.get("images", [])
        imagen = _safe_img_url(imagenes[0].get("url") if imagenes else None)

        # Autor desde additional_fields
        autor = ""
        for mf in p.get("additional_fields", []):
            if "autor" in (mf.get("label") or "").lower():
                autor = str(mf.get("value", ""))
                break

        resultado.append({
            "titulo":  p.get("name", ""),
            "autor":   autor,
            "precio":  precio,
            "imagen":  imagen,
            "url":     f"{STORE_URL}/store/productos/{p.get('permalink', '')}",
            "stock":   stock,
        })

    # Mezclar para variedad
    random.shuffle(resultado)
    return resultado


# ─── /recomendar ──────────────────────────────────────────────────────────────
@app.route("/recomendar", methods=["GET", "OPTIONS"])
def recomendar():
    origin = get_origin()
    if request.method == "OPTIONS":
        return cors_options(origin)
    if not _check_rate_limit(get_ip()):
        return add_cors(jsonify({"error": "Demasiadas solicitudes"}), origin), 429

    genero    = request.args.get("genero",    "").strip()[:100]
    mood      = request.args.get("mood",      "").strip()[:100]
    last_book = request.args.get("lastBook",  "").strip()[:200]
    extension = request.args.get("extension", "").strip()[:100]

    if not all([genero, mood, last_book, extension]):
        return add_cors(jsonify({"error": "Faltan parámetros"}), origin), 400

    # 1. Obtener catálogo real desde Jumpseller
    productos = obtener_productos(genero, limit=40)
    if not productos:
        return add_cors(jsonify({"intro": "", "libros": []}), origin)

    # Preparar lista de títulos para Claude (máximo 25)
    muestra = productos[:25]
    lista_titulos = "\n".join(
        f"- {p['titulo']}" + (f" ({p['autor']})" if p['autor'] else "")
        for p in muestra
    )

    # 2. Pedirle a Claude que elija 3 de esa lista
    prompt = (
        'Eres un experto recomendador de libros para la librería chilena "De Cea Libros".\n\n'
        "Un usuario ha respondido:\n"
        f"- Género preferido: {genero}\n"
        f"- Lo que busca en la lectura: {mood}\n"
        f'- Último libro que le gustó: "{last_book}"\n'
        f"- Extensión preferida: {extension}\n\n"
        "Estos son los libros disponibles actualmente en nuestra tienda:\n"
        f"{lista_titulos}\n\n"
        "Elige exactamente 3 libros de esa lista que mejor se adapten a las preferencias del usuario.\n"
        "IMPORTANTE: Solo puedes elegir títulos que aparezcan EXACTAMENTE en la lista de arriba.\n\n"
        "Responde ÚNICAMENTE con JSON válido, sin texto adicional:\n"
        '{"intro":"Frase breve y cálida (máximo 15 palabras)",'
        '"seleccion":["Título exacto 1","Título exacto 2","Título exacto 3"]}'
    )

    cr = None
    try:
        cr = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 500,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        cr.raise_for_status()
        result = _parse_json(cr.json()["content"][0]["text"])
    except Exception as e:
        err_detail = cr.text[:200] if cr is not None else ""
        print(f"ERROR Claude: {e} | {err_detail}", flush=True)
        return add_cors(jsonify({"error": str(e)[:100]}), origin), 502

    # 3. Cruzar selección de Claude con datos reales de Jumpseller
    seleccion = result.get("seleccion", [])
    productos_map = {p["titulo"].lower(): p for p in muestra}

    libros_finales = []
    for titulo_sel in seleccion:
        match = productos_map.get(titulo_sel.lower())
        if not match:
            # Búsqueda aproximada
            for key, prod in productos_map.items():
                if titulo_sel.lower() in key or key in titulo_sel.lower():
                    match = prod
                    break
        if match:
            libros_finales.append({
                "titulo":      match["titulo"],
                "autor":       match["autor"],
                "precio":      match["precio"],
                "imagen":      match["imagen"],
                "url":         match["url"],
                "descripcion": "",
            })
        if len(libros_finales) >= 3:
            break

    # Si Claude no matcheó bien, tomar los primeros 3 con stock
    if not libros_finales:
        libros_finales = [
            {"titulo": p["titulo"], "autor": p["autor"], "precio": p["precio"],
             "imagen": p["imagen"], "url": p["url"], "descripcion": ""}
            for p in muestra[:3]
        ]

    return add_cors(jsonify({
        "intro":  result.get("intro", "Tus próximas lecturas:"),
        "libros": libros_finales,
    }), origin)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _safe_img_url(url):
    if not url:
        return None
    allowed = {"cdn.jumpseller.com", "cdnx.jumpseller.com", "images.jumpseller.com"}
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.scheme != "https":
            return None
        if not any(p.hostname == d or (p.hostname or "").endswith("." + d) for d in allowed):
            return None
        return url
    except Exception:
        return None

def _parse_json(raw):
    import json
    clean = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
    return json.loads(clean)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
