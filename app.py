import io
import math
import os
import re
import time
import uuid
import warnings
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 80 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)
OUTPUT = Path(__file__).resolve().parent / 'results'
OUTPUT.mkdir(exist_ok=True)
FONT_DIR = Path('/usr/share/fonts/truetype/dejavu')

# --- Buscador de propiedades (CRM Buil & Fogwill / Lovable + Supabase) ---
# SUPABASE_URL / SUPABASE_ANON_KEY: se pueden sobreescribir con variables de entorno
# si algun dia cambian, pero por defecto usan la misma clave publica ("anon") que ya
# usa la propia web publica de Buil & Fogwill (no da acceso a nada privado: solo lee
# la vista public_properties, pensada para ser publica).
SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://mionqthbgtygphowaomc.supabase.co')
SUPABASE_ANON_KEY = os.environ.get(
    'SUPABASE_ANON_KEY',
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1pb25xdGhiZ3R5Z3Bob3dhb21jIiwicm9sZSI6ImFub24iLCJpYXQiOjE3Njc3ODAxMTIsImV4cCI6MjA4MzM1NjExMn0.-XfXaaAxesiNpqACvI84Y1JzNzYU9Wz-rrt6Ofs0wGI',
)
# Solo se permite descargar imagenes desde estos dominios conocidos: el CDN de fotos
# de propiedades y el almacenamiento de Supabase donde viven los logos de las marcas.
ALLOWED_IMAGE_HOSTS = {'cdn.resales-online.com', 'mionqthbgtygphowaomc.supabase.co'}

# --- Marcas (tenants): logos ya subidos y listos para elegir con un clic ---
# No hay que subir ningun archivo a mano: estos son los logos reales que ya
# estan alojados en el almacenamiento publico de Supabase para cada marca.
# "ref_prefix" es solo informativo (para que sepas que prefijo de referencia
# interna corresponde a cada marca); no se inserta automaticamente en ningun
# campo.
_STORAGE = f'{SUPABASE_URL}/storage/v1/object/public'
BRANDS = {
    'bf': {
        'label': 'Buil & Fogwill',
        'ref_prefix': 'BF',
        'logos': [
            ('Horizontal · blanco', f'{_STORAGE}/social-brand-assets/logos/1773416961413-logo-horizontal-sin-fondo---blanco.png'),
            ('Horizontal · negro', f'{_STORAGE}/social-brand-assets/logos/1773416974401-logo-horizontal-sin-fondo---color-negro.png'),
            ('Horizontal · azul', f'{_STORAGE}/social-brand-assets/logos/1773417064517-logo-horizontal-sin-fondo---color-azul.png'),
            ('Horizontal · blanco (2)', f'{_STORAGE}/social-brand-assets/logos/1773417077920-logo-horizontal-sin-fondo---color-blanco.png'),
            ('Isotipo · blanco', f'{_STORAGE}/social-brand-assets/logos/1773416985714-logo-sin-fondo---blanco.png'),
            ('Isotipo · negro', f'{_STORAGE}/social-brand-assets/logos/1773417016421-logo-sin-fondo---negro.png'),
            ('Isotipo · negro (2)', f'{_STORAGE}/social-brand-assets/logos/1773417004668-logo-sin-fondo---negro-2.png'),
            ('Isotipo · azul', f'{_STORAGE}/social-brand-assets/logos/1773417089009-logo-sin-fondo---color-azul.png'),
            ('Isotipo · blanco (2)', f'{_STORAGE}/social-brand-assets/logos/1773417098481-logo-sin-fondo---color-blanco.png'),
            ('Vertical · blanco', f'{_STORAGE}/social-brand-assets/logos/1773417030449-logo-vertical-sin-fondo---blanco.png'),
            ('Vertical · negro', f'{_STORAGE}/social-brand-assets/logos/1773417048770-logo-vertical-sin-fondo---negro.png'),
            ('Vertical · azul', f'{_STORAGE}/social-brand-assets/logos/1773417109302-logo-vertical-sin-fondo---color-azul.png'),
            ('Vertical · blanco (2)', f'{_STORAGE}/social-brand-assets/logos/1773417117267-logo-vertical-sin-fondo---color-blanco.png'),
        ],
    },
    'mijasin': {
        'label': 'Mijasin Properties',
        'ref_prefix': 'MI',
        'logos': [
            ('Logo 1', f'{_STORAGE}/tenant-logos/logo-1785680033256-1.png'),
            ('Logo 2', f'{_STORAGE}/tenant-logos/logo-1785680044022-2.png'),
            ('Logo 3', f'{_STORAGE}/tenant-logos/logo-1785680053220-3.png'),
        ],
    },
}

# Formatos de salida para redes sociales. "original" (sin entrada aqui) deja la foto
# tal cual viene. Para el resto, la foto se recorta para llenar exactamente ese
# tamano (como al compartir una foto en Instagram: se ve entera de ancho o de alto,
# y se recorta lo que sobre del otro lado).
FORMATS = {
    'ig_square': (1080, 1080),
    'ig_portrait': (1080, 1350),
    'ig_story': (1080, 1920),
    'fb_square': (1080, 1080),
    'fb_landscape': (1200, 630),
    'fb_cover': (820, 312),
    'li_square': (1200, 1200),
    'li_landscape': (1200, 627),
    'li_cover': (1584, 396),
}


def resize_cover(img, target_w, target_h):
    """Escala la foto para que cubra todo el rectangulo objetivo y recorta
    lo que sobre, centrado (como el recorte automatico de Instagram)."""
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, (new_w - target_w) // 2)
    top = max(0, (new_h - target_h) // 2)
    return resized.crop((left, top, left + target_w, top + target_h))


def decode(data, logo=False):
    with Image.open(io.BytesIO(data)) as source:
        if source.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError('Supported image formats: PNG, JPG and WebP.')
        if source.width * source.height > 25_000_000:
            raise ValueError('Maximum image size: 25 megapixels.')
        if logo and source.format != 'PNG':
            raise ValueError('Logos must be transparent PNG files.')
        result = ImageOps.exif_transpose(source).convert('RGBA')
        result.load()
    if logo:
        alpha = result.getchannel('A')
        if alpha.getextrema()[0] == 255:
            raise ValueError('A logo has no transparency. Upload a PNG without a background.')
        bounds = alpha.getbbox()
        if not bounds:
            raise ValueError('A logo is completely transparent.')
        result = result.crop(bounds)
    return result


def wrap_text(draw, text, font, width):
    lines = []
    for paragraph in text.splitlines():
        line = ''
        for word in paragraph.split():
            candidate = (line + ' ' + word).strip()
            if draw.textlength(candidate, font=font) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ''
            for char in word:
                if line and draw.textlength(line + char, font=font) > width:
                    lines.append(line)
                    line = ''
                line += char
        lines.append(line)
    return '\n'.join(lines)


def draw_text(base, text, box, bold=False):
    if not text:
        return
    x, y, width, height = box
    if width < 4 or height < 4:
        return
    draw = ImageDraw.Draw(base)
    path = FONT_DIR / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')
    if not path.is_file():
        raise ValueError('Font missing. Install fonts-dejavu-core on the server.')
    for size in range(max(8, round(min(base.size) * (.065 if bold else .035))), 3, -1):
        font = ImageFont.truetype(str(path), size)
        stroke = max(1, round(size / 20))
        text_lines = wrap_text(draw, text, font, max(1, width - 2 * stroke))
        spacing = max(1, size // 5)
        bounds = draw.multiline_textbbox((0, 0), text_lines, font=font, spacing=spacing, stroke_width=stroke)
        if bounds[2] - bounds[0] <= width and bounds[3] - bounds[1] <= height:
            draw.multiline_text((x - bounds[0], y - bounds[1]), text_lines, font=font,
                                spacing=spacing, fill='white', stroke_width=stroke, stroke_fill='black')
            return
    raise ValueError('Text is too long for this image. Shorten it or use a larger image.')


def draw_badge(base, text):
    """Dibuja la etiqueta de zona/precio en la esquina superior derecha.
    Devuelve el ancho que hay que reservar (etiqueta + hueco) para que el
    titulo no se dibuje por debajo."""
    if not text:
        return 0
    w, h = base.size
    margin = max(4, round(min(w, h) * .03))
    size = max(10, round(min(w, h) * .028))
    path = FONT_DIR / 'DejaVuSans-Bold.ttf'
    if not path.is_file():
        return 0
    font = ImageFont.truetype(str(path), size)
    draw = ImageDraw.Draw(base)
    pad_x, pad_y = round(size * .9), round(size * .55)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    box_w, box_h = text_w + 2 * pad_x, text_h + 2 * pad_y
    x1, y0 = w - margin, margin
    x0, y1 = x1 - box_w, y0 + box_h
    draw.rounded_rectangle((x0, y0, x1, y1), radius=box_h / 2, fill=(17, 36, 48, 235))
    draw.text((x0 + pad_x - bbox[0], y0 + pad_y - bbox[1]), text, font=font, fill='white')
    return box_w + margin


def process(photo, logos, title='', information='', fmt_key='original', zone='', price=''):
    start = time.perf_counter()
    base = decode(photo)
    if min(base.size) < 256:
        raise ValueError('Photos must be at least 256 × 256 pixels.')
    target = FORMATS.get(fmt_key)
    if target:
        base = resize_cover(base, *target)
    loaded = time.perf_counter()
    w, h = base.size
    margin = max(4, round(min(w, h) * .03))
    columns = min(4, len(logos))
    rows = math.ceil(len(logos) / columns) if logos else 0
    cell_w = (w - 2 * margin) // max(1, columns)
    cell_h = max(1, int(h * .30) // max(1, rows))
    for i, logo in enumerate(logos):
        # Decode one logo at a time to keep memory use bounded.
        mark = decode(logo, logo=True)
        mark.thumbnail((max(1, cell_w - 2 * margin), max(1, cell_h - margin)), Image.Resampling.LANCZOS)
        x = margin + (i % columns) * cell_w + (cell_w - mark.width) // 2
        y = h - margin - rows * cell_h + (i // columns) * cell_h + (cell_h - mark.height) // 2
        base.alpha_composite(mark, (x, y))
    logos_done = time.perf_counter()
    badge_text = ' · '.join(part for part in (zone, price) if part)
    reserved = draw_badge(base, badge_text)
    title_width = max(1, w - 2 * margin - reserved)
    draw_text(base, title, (margin, margin, title_width, int(h * .22)), bold=True)
    draw_text(base, information, (margin, margin + int(h * .25), w - 2 * margin, int(h * .27)))
    text_done = time.perf_counter()
    name = uuid.uuid4().hex + '.png'
    path = OUTPUT / name
    try:
        with path.open('wb') as file:
            base.save(file, 'PNG')
            file.flush()
            os.fsync(file.fileno())
    except OSError:
        path.unlink(missing_ok=True)
        raise
    saved = time.perf_counter()
    return dict(file=name, width=w, height=h, logos=len(logos), timing={
        'decode_ms': (loaded-start)*1000,
        'logos_ms': (logos_done-loaded)*1000,
        'text_ms': (text_done-logos_done)*1000,
        'save_ms': (saved-text_done)*1000,
        'total_ms': (saved-start)*1000,
    })


@app.get('/')
def home():
    return render_template('index.html')


@app.post('/process')
def run():
    photo = request.files.get('photo')
    logos = request.files.getlist('logos')
    title = request.form.get('title', '').strip()
    information = request.form.get('information', '').strip()
    zone = request.form.get('zone', '').strip()[:80]
    price = request.form.get('price', '').strip()[:40]
    fmt_key = request.form.get('format', 'original').strip()
    if photo is None or len(logos) > 16:
        return jsonify(error='Choose a photo and up to 16 logos.'), 400
    if not logos and not title and not information and not zone and not price:
        return jsonify(error='Add at least one logo or some text.'), 400
    if len(title) > 150 or len(information) > 500:
        return jsonify(error='Title limit: 150 characters. Information limit: 500 characters.'), 400
    photo_data = photo.read()
    logo_data = [logo.read() for logo in logos]
    try:
        return jsonify(process(photo_data, logo_data, title, information, fmt_key, zone, price))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return jsonify(error='Invalid image or image exceeds the 25 megapixel limit.'), 400
    except OSError:
        app.logger.exception('Image read or output write failed')
        return jsonify(error='Could not read the image or write the result. Check the image and server disk space.'), 400


@app.post('/archive')
def archive():
    payload = request.get_json(silent=True) or {}
    names = payload.get('files') if isinstance(payload, dict) else None
    if not isinstance(names, list) or not 1 <= len(names) <= 100:
        return jsonify(error='Choose 1–100 generated images.'), 400
    if any(not isinstance(n, str) or not re.fullmatch(r'[0-9a-f]{32}\.png', n) or not (OUTPUT/n).is_file() for n in names):
        return jsonify(error='A generated image is missing or invalid.'), 400
    name = uuid.uuid4().hex + '.zip'
    try:
        with zipfile.ZipFile(OUTPUT/name, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for i, image in enumerate(names, 1):
                archive.write(OUTPUT/image, f'{i:03d}_{image}')
    except OSError:
        (OUTPUT/name).unlink(missing_ok=True)
        return jsonify(error='Unable to create ZIP. Check free disk space.'), 500
    return jsonify(file=name)


@app.get('/results/<name>')
def result(name):
    return send_from_directory(OUTPUT, name, as_attachment=request.args.get('download') == '1')


def _clean_search_term(term):
    # Quitamos caracteres que tienen significado especial para el filtro
    # "ilike" / "or" de Supabase (PostgREST), para que una busqueda con
    # comas o asteriscos no rompa el filtro.
    return term.replace('%', ' ').replace(',', ' ').replace('*', ' ').replace('(', ' ').replace(')', ' ').strip()


def _normalize_images(images):
    """Las fotos vienen a veces como lista de URLs (texto) y a veces como
    lista de objetos {"url": "..."} - las dejamos siempre como URLs."""
    if not isinstance(images, list):
        return []
    urls = []
    for img in images:
        if isinstance(img, str) and img:
            urls.append(img)
        elif isinstance(img, dict):
            url = img.get('url') or img.get('src') or img.get('image') or img.get('href')
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


@app.get('/api/search-properties')
def search_properties():
    """Busca propiedades en el CRM (Lovable/Supabase) por referencia, zona,
    urbanizacion o titulo, con filtros opcionales de zona, precio maximo y
    tipologia. Solo lee datos ya pensados como publicos (la vista
    public_properties de la web de Buil & Fogwill)."""
    term = _clean_search_term(request.args.get('q', ''))
    zone_filter = _clean_search_term(request.args.get('zone', ''))
    type_filter = request.args.get('type', '').strip()
    max_price_raw = request.args.get('max_price', '').strip()

    if len(term) < 2 and not zone_filter and not type_filter and not max_price_raw:
        return jsonify(results=[])

    params = {
        'select': 'id,public_ref,title_es,title_en,zone,subzone,urbanization,price,'
                  'bedrooms,bathrooms,built_area,property_type,images',
        'order': 'updated_at.desc',
        'limit': '30',
    }
    if len(term) >= 2:
        pattern = f'*{term}*'
        fields = ('public_ref', 'title_es', 'title_en', 'zone', 'subzone', 'urbanization', 'location')
        params['or'] = '(' + ','.join(f'{field}.ilike.{pattern}' for field in fields) + ')'
    if zone_filter:
        params['zone'] = f'ilike.*{zone_filter}*'
    if type_filter:
        params['property_type'] = f'eq.{type_filter}'
    if max_price_raw:
        try:
            params['price'] = f'lte.{float(max_price_raw)}'
        except ValueError:
            pass

    try:
        resp = requests.get(
            f'{SUPABASE_URL}/rest/v1/public_properties',
            params=params,
            headers={'apikey': SUPABASE_ANON_KEY, 'Authorization': f'Bearer {SUPABASE_ANON_KEY}'},
            timeout=8,
        )
        resp.raise_for_status()
    except requests.RequestException:
        app.logger.exception('search_properties failed')
        return jsonify(error='No se pudo contactar con el CRM. Revisa la conexión a internet del servidor.'), 502

    results = []
    for p in resp.json():
        images = _normalize_images(p.get('images'))
        title = p.get('title_es') or p.get('title_en') or ''
        headline = title.replace('\r\n', '\n').split('\n')[0].strip()
        results.append(dict(
            id=p.get('id'),
            ref=p.get('public_ref'),
            headline=headline,
            zone=p.get('zone'),
            subzone=p.get('subzone'),
            urbanization=p.get('urbanization'),
            price=p.get('price'),
            bedrooms=p.get('bedrooms'),
            bathrooms=p.get('bathrooms'),
            built_area=p.get('built_area'),
            property_type=p.get('property_type'),
            image_count=len(images),
            thumbnail=images[0] if images else None,
            images=images,
        ))
    return jsonify(results=results)


@app.get('/api/brands')
def brands():
    """Marcas disponibles y sus logos ya precargados (listos para elegir con
    un clic, sin subir nada)."""
    return jsonify({
        key: dict(
            label=b['label'],
            ref_prefix=b['ref_prefix'],
            logos=[dict(name=name, url=url) for name, url in b['logos']],
        )
        for key, b in BRANDS.items()
    })


@app.get('/api/proxy-image')
def proxy_image():
    """Descarga una foto o logo en el servidor y se lo pasa al navegador.
    Asi evitamos el bloqueo CORS del navegador (la pagina y la foto viven en
    dominios distintos) y de paso comprobamos que la URL es de un dominio
    conocido antes de pedirla."""
    url = request.args.get('url', '')
    host = urlparse(url).hostname or ''
    if host not in ALLOWED_IMAGE_HOSTS:
        return jsonify(error='Origen de imagen no permitido.'), 400
    try:
        upstream = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        upstream.raise_for_status()
    except requests.RequestException:
        app.logger.exception('proxy_image failed for %s', url)
        return jsonify(error='No se pudo descargar la imagen.'), 502
    return Response(upstream.content, content_type=upstream.headers.get('Content-Type', 'image/jpeg'))


@app.errorhandler(413)
def too_large(_):
    return jsonify(error='Each photo together with all logos must fit within 80 MB.'), 413


@app.errorhandler(500)
def failed(_):
    return jsonify(error='Server error. Check the server terminal.'), 500
