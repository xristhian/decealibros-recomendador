import re
import os
import time
import requests
from collections import defaultdict
from flask import Flask, jsonify, request
from html.parser import HTMLParser

app = Flask(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
STORE_URL         = "https://decealibros.cl"

ALLOWED_ORIGINS = [
    "https://decealibros.cl",
    "https://www.decealibros.cl",
    "https://xristhian.github.io",
]

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

# ─── Validación ISBN-13 ───────────────────────────────────────────────────────
ISBN13_RE = re.compile(r"^\d{13}$")

def valid_isbn13(isbn: str) -> bool:
    if not ISBN13_RE.match(isbn):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(isbn[:12]))
    return (10 - (total % 10)) % 10 == int(isbn[12])

# ─── Scraper HTML ─────────────────────────────────────────────────────────────
class ProductParser(HTMLParser):
    """Extrae datos del primer product-block encontrado en el HTML de búsqueda."""

    def __init__(self):
        super().__init__()
        self.result       = None   # dict con datos del producto
        self._in_article  = False
        self._article_depth = 0
        self._depth       = 0
        self._in_title    = False
        self._in_price    = False
        self._in_sku      = False
        self._buf         = ""

    def handle_starttag(self, tag, attrs):
        self._depth += 1
        attrs = dict(attrs)
        classes = attrs.get("class", "")

        # Detectar inicio del article del producto
        if tag == "article" and "product-block" in classes and not self._in_article:
            self._in_article   = True
            self._article_depth = self._depth
            self.result        = {
                "titulo": "", "precio": None, "imagen": None,
                "url": None, "stock": False
            }

        if not self._in_article or self.result is None:
            return

        # URL del producto (anchor principal)
        if tag == "a" and "product-block__anchor" in classes and not self.result["url"]:
            href = attrs.get("href", "")
            if href:
                self.result["url"] = STORE_URL + href if href.startswith("/") else href

        # Imagen de mayor resolución (source con media 1200px)
        if tag == "source" and "1200px" in attrs.get("media", ""):
            srcset = attrs.get("srcset", "")
            if srcset and not self.result["imagen"]:
                self.result["imagen"] = _safe_img_url(srcset.split(",")[0].strip().split(" ")[0])

        # Imagen fallback
        if tag == "img" and "product-block__image" in classes and not self.result["imagen"]:
            self.result["imagen"] = _safe_img_url(attrs.get("src", ""))

        # Título
        if tag == "a" and "product-block__name" in classes:
            self._in_title = True
            self._buf = ""

        # Precio
        if tag == "div" and "product-block__price" in classes:
            self._in_price = True
            self._buf = ""

        # SKU (para confirmar ISBN coincide)
        if tag == "span" and "product-block__sku" in classes:
            self._in_sku = True
            self._buf = ""

        # Stock: input qty con max > 0
        if tag == "input" and attrs.get("name") == "qty":
            try:
                max_val = int(attrs.get("max", "0") or "0")
                if max_val > 0:
                    self.result["stock"] = True
            except ValueError:
                pass

    def handle_endtag(self, tag):
        if self._in_title and tag == "a":
            self.result["titulo"] = self._buf.strip()
            self._in_title = False
            self._buf = ""

        if self._in_price and tag == "div":
            raw = self._buf.strip()
            # "$26.990" → 26990.0
            num = re.sub(r"[^\d]", "", raw)
            if num:
                self.result["precio"] = float(num)
            self._in_price = False
            self._buf = ""

        if self._in_sku and tag == "span":
            self._in_sku = False
            self._buf = ""

        # Salir del article al cerrar el tag al mismo nivel
        if self._in_article and tag == "article" and self._depth == self._article_depth:
            self._in_article = False

        self._depth -= 1

    def handle_data(self, data):
        if self._in_title or self._in_price or self._in_sku:
            self._buf += data


def scrape_isbn(isbn: str) -> dict:
    """Busca el ISBN en la web de la tienda y retorna datos del producto."""
    url = f"{STORE_URL}/search?q={isbn}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; DecealibrosBot/1.0)",
        "Accept-Language": "es-CL,es;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        raise RuntimeError("timeout")
    except Exception:
        raise RuntimeError("fetch_error")

    # Verificar si hay resultados (texto "No hay resultados" en el HTML)
    if "No hay resultados disponibles" in r.text:
        return {"encontrado": False}

    parser = ProductParser()
    parser.feed(r.text)

    if not parser.result or not parser.result.get("titulo"):
        return {"encontrado": False}

    p = parser.result
    return {
        "encontrado":  True,
        "stock":       p["stock"],
        "titulo":      p["titulo"],
        "precio":      p["precio"],
        "imagen":      p["imagen"],
        "url":         p["url"] or url,
        "descripcion": "",
    }


# ─── /libro?isbn=… ───────────────────────────────────────────────────────────
@app.route("/libro", methods=["GET", "OPTIONS"])
def buscar_libro():
    origin = get_origin()
    if request.method == "OPTIONS":
        return cors_options(origin)

    if not _check_rate_limit(get_ip()):
        return add_cors(jsonify({"error": "Demasiadas solicitudes"}), origin), 429

    isbn = request.args.get("isbn", "").strip()
    if not isbn or not valid_isbn13(isbn):
        return add_cors(jsonify({"error": "ISBN inválido"}), origin), 400

    try:
        datos = scrape_isbn(isbn)
    except RuntimeError as e:
        return add_cors(jsonify({"error": str(e)}), origin), 502

    return add_cors(jsonify(datos), origin)


# ─── /recomendar?genero=…&mood=…&lastBook=…&extension=… ──────────────────────
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

    prompt = (
        'Eres un experto recomendador de libros para la librería chilena "De Cea Libros".\n\n'
        "Un usuario ha respondido:\n"
        f"- Género preferido: {genero}\n"
        f"- Lo que busca en la lectura: {mood}\n"
        f'- Último libro que le gustó: "{last_book}"\n'
        f"- Extensión preferida: {extension}\n\n"
        "Recomienda exactamente 5 libros comerciales conocidos, disponibles habitualmente en librerías chilenas.\n"
        "Para cada libro proporciona el ISBN-13 (13 dígitos exactos, sin guiones ni espacios).\n\n"
        "Responde ÚNICAMENTE con JSON válido, sin texto adicional:\n"
        '{"intro":"Frase breve y cálida (máximo 15 palabras)",'
        '"libros":[{"titulo":"Título exacto","autor":"Nombre Apellido","isbn":"9789999999999",'
        '"por_que":"2-3 frases explicando por qué es ideal para este usuario"}]}'
    )

    try:
        cr = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type":      "application/json",
            },
            json={
                "model":    "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        cr.raise_for_status()
        result = _parse_json(cr.json()["content"][0]["text"])
    except Exception:
        return add_cors(jsonify({"error": "Error al generar recomendaciones"}), origin), 502

    libros_con_stock = []
    for libro in result.get("libros", []):
        if len(libros_con_stock) >= 3:
            break
        isbn = str(libro.get("isbn", "")).replace("-", "").strip()
        if not valid_isbn13(isbn):
            continue
        try:
            datos = scrape_isbn(isbn)
        except RuntimeError:
            continue
        if not datos.get("encontrado") or not datos.get("stock"):
            continue
        libros_con_stock.append({
            "titulo":      datos.get("titulo") or libro.get("titulo", ""),
            "autor":       libro.get("autor", ""),
            "precio":      datos.get("precio"),
            "imagen":      datos.get("imagen"),
            "url":         datos.get("url"),
            "descripcion": libro.get("por_que", ""),
        })

    return add_cors(jsonify({
        "intro":  result.get("intro", "Tus próximas lecturas:"),
        "libros": libros_con_stock,
    }), origin)


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _safe_img_url(url):
    if not url:
        return None
    allowed = {"cdnx.jumpseller.com", "cdn.jumpseller.com", "images.jumpseller.com"}
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
