import io
import json
import math
import os
import random
import re
import time
import uuid
import warnings
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, UnidentifiedImageError

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 80 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 25_000_000
warnings.simplefilter('error', Image.DecompressionBombWarning)
OUTPUT = Path(__file__).resolve().parent / 'results'
OUTPUT.mkdir(exist_ok=True)
FONT_DIR = Path('/usr/share/fonts/truetype/dejavu')

# --- Ganchos (frases gancho / titulares guardados) ---
# Lista local, NO forma parte del repositorio de GitHub (no se sincroniza
# entre servidores): vive solo en este servidor, en un archivo junto al
# codigo, para que sobreviva a los despliegues automaticos (que solo pisan
# los archivos que SI estan en git).
HOOKS_FILE = Path(__file__).resolve().parent / 'hooks.json'
MAX_HOOKS = 2000
MAX_HOOK_LEN = 300

# --- IA local (Ollama) para sugerir ganchos alternativos a partir de texto ---
# Se asume una instalacion de Ollama corriendo en este mismo servidor
# (por defecto: http://127.0.0.1:11434). No es necesaria ninguna clave ni
# conexion a internet: todo corre en la maquina de la oficina.
OLLAMA_URL = os.environ.get('OLLAMA_URL', 'http://127.0.0.1:11434')
OLLAMA_MODEL = os.environ.get('OLLAMA_MODEL', 'llama3.2')

# --- Registro de generaciones (para el futuro aprendizaje por performance) ---
# Cada imagen generada (manual o automatica) queda anotada aqui: que se
# decidio (logo, color, sombra, banda, si se incluyo precio/zona/titulo/
# gancho, formato). No es una base de datos: es un archivo de texto simple,
# una linea JSON por imagen, pensado para poder cruzarlo mas adelante con
# estadisticas reales de cada anuncio y asi aprender que combinaciones
# funcionan mejor. Tampoco esta en git: vive solo en este servidor.
GENERATION_LOG = Path(__file__).resolve().parent / 'generation_log.jsonl'


def _log_generation(record):
    try:
        with GENERATION_LOG.open('a', encoding='utf-8') as file:
            file.write(json.dumps(record, ensure_ascii=False) + '\n')
    except OSError:
        pass


def _load_hooks():
    try:
        with HOOKS_FILE.open('r', encoding='utf-8') as file:
            data = json.load(file)
        if isinstance(data, list):
            return [str(item).strip() for item in data if str(item).strip()]
    except (OSError, ValueError):
        pass
    return []


def _save_hooks(hooks):
    cleaned = []
    seen = set()
    for item in hooks:
        text = str(item).strip()[:MAX_HOOK_LEN]
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
        if len(cleaned) >= MAX_HOOKS:
            break
    with HOOKS_FILE.open('w', encoding='utf-8') as file:
        json.dump(cleaned, file, ensure_ascii=False, indent=0)
    return cleaned

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

# Nombre de "red social" y de "formato" que se usan para nombrar cada imagen
# generada: {RedSocial}_{Formato}_{Ancho}x{Alto}_{Referencia}.png
# Ej: Instagram_Vertical_Feed_1080x1350_BF-1F5E2.png
FORMAT_META = {
    'ig_square': ('Instagram', 'Cuadrado'),
    'ig_portrait': ('Instagram', 'Vertical_Feed'),
    'ig_story': ('Instagram', 'Story'),
    'fb_square': ('Facebook', 'Cuadrado'),
    'fb_landscape': ('Facebook', 'Horizontal'),
    'fb_cover': ('Facebook', 'Portada'),
    'li_square': ('LinkedIn', 'Cuadrado'),
    'li_landscape': ('LinkedIn', 'Horizontal'),
    'li_cover': ('LinkedIn', 'Portada'),
    'original': ('Manual', 'Original'),
}
# Solo estos formatos (excluye "original") participan del sorteo del modo
# automatico: siempre apunta a un tamano real de red social.
AUTO_FORMAT_KEYS = [key for key in FORMATS if key != 'original']

# --- Modo automatico: variedad de tipografias, colores, y posiciones de logo ---
# Se revisa cual de estas fuentes esta realmente instalada en el servidor: si
# solo esta la DejaVu (la que ya usa el modo manual) no pasa nada, el modo
# automatico simplemente tiene menos variedad de tipografia hasta que se
# instalen mas (paquetes fonts-liberation / fonts-freefont-ttf en Ubuntu).
_FONT_CANDIDATES = [
    ('DejaVu Sans', FONT_DIR / 'DejaVuSans.ttf', FONT_DIR / 'DejaVuSans-Bold.ttf'),
    ('DejaVu Sans Condensed', FONT_DIR / 'DejaVuSansCondensed.ttf', FONT_DIR / 'DejaVuSansCondensed-Bold.ttf'),
    ('DejaVu Serif', FONT_DIR / 'DejaVuSerif.ttf', FONT_DIR / 'DejaVuSerif-Bold.ttf'),
    ('Liberation Sans', Path('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf'),
     Path('/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf')),
    ('Liberation Serif', Path('/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf'),
     Path('/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf')),
    ('FreeSans', Path('/usr/share/fonts/truetype/freefont/FreeSans.ttf'),
     Path('/usr/share/fonts/truetype/freefont/FreeSansBold.ttf')),
]
FONT_LIBRARY = [
    {'name': name, 'regular': regular, 'bold': bold}
    for name, regular, bold in _FONT_CANDIDATES
    if regular.is_file() and bold.is_file()
] or [{'name': 'DejaVu Sans', 'regular': FONT_DIR / 'DejaVuSans.ttf', 'bold': FONT_DIR / 'DejaVuSans-Bold.ttf'}]

COLOR_PALETTE = [
    {'name': 'Blanco', 'fill': (255, 255, 255, 255), 'stroke': (0, 0, 0, 255)},
    {'name': 'Negro', 'fill': (20, 20, 20, 255), 'stroke': (255, 255, 255, 255)},
    {'name': 'Dorado', 'fill': (232, 191, 115, 255), 'stroke': (35, 28, 12, 255)},
    {'name': 'Azul marca', 'fill': (255, 255, 255, 255), 'stroke': (17, 36, 48, 255)},
]

# Esquinas donde puede ir el logo en modo automatico. Se evita la esquina
# superior derecha a proposito: ahi siempre va la etiqueta de referencia /
# precio / zona, y un logo se superpondria con ella.
LOGO_POSITIONS = ['bottom-left', 'bottom-right', 'top-left']


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


def draw_text(base, text, box, bold=False, font_regular=None, font_bold=None,
              fill='white', stroke_fill='black', shadow=False, band=False,
              band_color=(17, 36, 48, 165)):
    if not text:
        return
    x, y, width, height = box
    if width < 4 or height < 4:
        return
    draw = ImageDraw.Draw(base)
    path = (font_bold if bold else font_regular) or FONT_DIR / ('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf')
    path = Path(path)
    if not path.is_file():
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
            tx, ty = x - bounds[0], y - bounds[1]
            if band:
                pad = max(6, stroke * 4)
                band_box = (x - pad, y - pad, x + (bounds[2] - bounds[0]) + pad, y + (bounds[3] - bounds[1]) + pad)
                overlay = Image.new('RGBA', base.size, (0, 0, 0, 0))
                ImageDraw.Draw(overlay).rounded_rectangle(band_box, radius=pad, fill=band_color)
                base.alpha_composite(overlay)
                draw = ImageDraw.Draw(base)
            if shadow:
                offset = max(2, size // 14)
                shadow_layer = Image.new('RGBA', base.size, (0, 0, 0, 0))
                ImageDraw.Draw(shadow_layer).multiline_text((tx + offset, ty + offset), text_lines, font=font,
                                                              spacing=spacing, fill=(0, 0, 0, 200))
                shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=max(1, size // 12)))
                base.alpha_composite(shadow_layer)
                draw = ImageDraw.Draw(base)
            draw.multiline_text((tx, ty), text_lines, font=font,
                                spacing=spacing, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
            return
    raise ValueError('Text is too long for this image. Shorten it or use a larger image.')


def _sanitize_filename_part(text):
    # Se conservan letras, numeros y guiones (las referencias del CRM suelen
    # llevar guion, ej. "BF-1F5E2") para que el nombre del archivo siga
    # siendo legible; cualquier otra cosa (espacios, acentos, simbolos) se
    # convierte en guion bajo.
    cleaned = re.sub(r'[^A-Za-z0-9-]+', '_', str(text)).strip('_-')
    return cleaned or 'X'


def build_filename(red_social, formato_label, width, height, ref):
    """Nombra cada imagen generada como {RedSocial}_{Formato}_{Ancho}x{Alto}_{Referencia}.png
    (ej: Instagram_Vertical_Feed_1080x1350_BF-1F5E2.png), para que siempre se
    pueda saber, con solo mirar el nombre del archivo, para que red social y
    formato se hizo y de que propiedad se trata. Si dos fotos generan
    exactamente el mismo nombre (misma red social, formato y referencia, dentro
    del mismo lote) se agrega un numero al final para no pisar el archivo
    anterior."""
    ref_part = _sanitize_filename_part(ref) if ref else 'SINREF-' + uuid.uuid4().hex[:8]
    base_name = f'{_sanitize_filename_part(red_social)}_{_sanitize_filename_part(formato_label)}_{width}x{height}_{ref_part}'
    name = base_name + '.png'
    counter = 2
    while (OUTPUT / name).exists():
        name = f'{base_name}_{counter}.png'
        counter += 1
    return name


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


def process(photo, logos, title='', information='', fmt_key='original', zone='', price='', ref=''):
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
    ref_text = f'Ref. {ref}' if ref else ''
    badge_text = ' · '.join(part for part in (ref_text, zone, price) if part)
    reserved = draw_badge(base, badge_text)
    title_width = max(1, w - 2 * margin - reserved)
    draw_text(base, title, (margin, margin, title_width, int(h * .22)), bold=True)
    draw_text(base, information, (margin, margin + int(h * .25), w - 2 * margin, int(h * .27)))
    text_done = time.perf_counter()
    red_social, formato_label = FORMAT_META.get(fmt_key, ('Manual', 'Original'))
    name = build_filename(red_social, formato_label, w, h, ref)
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
    _log_generation(dict(file=name, ref=ref, mode='manual', format=fmt_key, ts=time.time()))
    return dict(file=name, width=w, height=h, logos=len(logos), timing={
        'decode_ms': (loaded-start)*1000,
        'logos_ms': (logos_done-loaded)*1000,
        'text_ms': (text_done-logos_done)*1000,
        'save_ms': (saved-text_done)*1000,
        'total_ms': (saved-start)*1000,
    })


def process_auto(photo, logo_pool, ref, zone='', price='', hooks_pool=None, fallback_title=''):
    """Modo automatico: para esta unica foto, decide al azar (sin llamar a la
    IA, para que sea instantaneo) una combinacion distinta de estilo y de que
    incluir, pensada para que cada foto de un lote salga distinta y mas
    adelante se pueda comparar cual combinacion funciona mejor. La referencia
    de la propiedad SIEMPRE se dibuja, sin importar el resto de lo sorteado."""
    start = time.perf_counter()
    base = decode(photo)
    if min(base.size) < 256:
        raise ValueError('Photos must be at least 256 × 256 pixels.')
    fmt_key = random.choice(AUTO_FORMAT_KEYS)
    base = resize_cover(base, *FORMATS[fmt_key])
    loaded = time.perf_counter()
    w, h = base.size
    margin = max(4, round(min(w, h) * .03))

    choices = {'logo': False}
    top_left_logo_height = 0
    use_logo = bool(logo_pool) and random.random() < 0.75
    if use_logo:
        try:
            mark = decode(random.choice(logo_pool), logo=True)
            mark.thumbnail((max(1, round(w * .26)), max(1, round(h * .15))), Image.Resampling.LANCZOS)
            position = random.choice(LOGO_POSITIONS)
            if position == 'top-left':
                x, y = margin, margin
                top_left_logo_height = mark.height
            elif position == 'bottom-left':
                x, y = margin, h - margin - mark.height
            else:
                x, y = w - margin - mark.width, h - margin - mark.height
            base.alpha_composite(mark, (x, y))
            choices['logo'] = True
            choices['logo_position'] = position
        except (ValueError, UnidentifiedImageError):
            pass  # Un logo invalido en el conjunto: se omite esta vez, sin romper el lote.
    logos_done = time.perf_counter()

    include_price = bool(price) and random.random() < 0.7
    include_zone = bool(zone) and random.random() < 0.7
    ref_text = f'Ref. {ref}' if ref else ''
    badge_text = ' · '.join(part for part in (ref_text, zone if include_zone else '', price if include_price else '') if part)
    reserved = draw_badge(base, badge_text)
    choices['price'] = include_price
    choices['zone'] = include_zone

    pool = [h for h in (hooks_pool or []) if h and h.strip()]
    include_title = bool(fallback_title) and random.random() < 0.7
    include_hook = bool(pool) and random.random() < 0.7
    hook_text = random.choice(pool) if include_hook else ''
    text_lines = [t for t in (fallback_title if include_title else '', hook_text) if t]
    combined_text = '\n'.join(text_lines)
    choices['title'] = include_title
    choices['hook'] = include_hook
    if hook_text:
        choices['hook_text'] = hook_text

    font_choice = random.choice(FONT_LIBRARY)
    color_choice = random.choice(COLOR_PALETTE)
    shadow_on = random.random() < 0.5
    band_on = random.random() < 0.35
    choices.update(font=font_choice['name'], color=color_choice['name'], shadow=shadow_on, band=band_on)

    title_width = max(1, w - 2 * margin - reserved)
    title_top = margin + (top_left_logo_height + margin if top_left_logo_height else 0)
    title_height = max(30, int(h * .28) - (top_left_logo_height + margin if top_left_logo_height else 0))
    if combined_text:
        draw_text(base, combined_text, (margin, title_top, title_width, title_height), bold=True,
                  font_regular=font_choice['regular'], font_bold=font_choice['bold'],
                  fill=color_choice['fill'], stroke_fill=color_choice['stroke'],
                  shadow=shadow_on, band=band_on)
    text_done = time.perf_counter()

    red_social, formato_label = FORMAT_META.get(fmt_key, ('Social', 'Formato'))
    name = build_filename(red_social, formato_label, w, h, ref)
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
    _log_generation(dict(file=name, ref=ref, mode='auto', format=fmt_key, choices=choices, ts=time.time()))
    return dict(file=name, width=w, height=h, logos=1 if choices['logo'] else 0,
                format=fmt_key, red_social=red_social, formato=formato_label, choices=choices, timing={
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
    ref = request.form.get('ref', '').strip()[:40]
    fmt_key = request.form.get('format', 'original').strip()
    if photo is None or len(logos) > 16:
        return jsonify(error='Choose a photo and up to 16 logos.'), 400
    if not logos and not title and not information and not zone and not price and not ref:
        return jsonify(error='Add at least one logo or some text.'), 400
    if len(title) > 150 or len(information) > 500:
        return jsonify(error='Title limit: 150 characters. Information limit: 500 characters.'), 400
    photo_data = photo.read()
    logo_data = [logo.read() for logo in logos]
    try:
        return jsonify(process(photo_data, logo_data, title, information, fmt_key, zone, price, ref))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return jsonify(error='Invalid image or image exceeds the 25 megapixel limit.'), 400
    except OSError:
        app.logger.exception('Image read or output write failed')
        return jsonify(error='Could not read the image or write the result. Check the image and server disk space.'), 400


@app.post('/process-auto')
def run_auto():
    """Modo automatico: procesa UNA foto, sorteando su propia combinacion de
    estilo (logo, tipografia, color, sombra, banda, que incluir y formato de
    salida). Se llama una vez por foto para que cada una salga distinta."""
    photo = request.files.get('photo')
    logo_files = request.files.getlist('logos')
    ref = request.form.get('ref', '').strip()[:40]
    zone = request.form.get('zone', '').strip()[:80]
    price = request.form.get('price', '').strip()[:40]
    fallback_title = request.form.get('title', '').strip()[:150]
    if photo is None or len(logo_files) > 16:
        return jsonify(error='Choose a photo and up to 16 logos for the pool.'), 400
    hooks_pool = []
    hooks_raw = request.form.get('hooks_json', '')
    if hooks_raw:
        try:
            parsed = json.loads(hooks_raw)
            if isinstance(parsed, list):
                hooks_pool = [str(item).strip()[:150] for item in parsed if str(item).strip()][:500]
        except ValueError:
            hooks_pool = []
    photo_data = photo.read()
    logo_pool = [logo.read() for logo in logo_files]
    try:
        return jsonify(process_auto(photo_data, logo_pool, ref, zone, price, hooks_pool, fallback_title))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return jsonify(error='Invalid image or image exceeds the 25 megapixel limit.'), 400
    except OSError:
        app.logger.exception('Auto image processing failed')
        return jsonify(error='Could not read the image or write the result. Check the image and server disk space.'), 400


@app.post('/archive')
def archive():
    payload = request.get_json(silent=True) or {}
    names = payload.get('files') if isinstance(payload, dict) else None
    if not isinstance(names, list) or not 1 <= len(names) <= 100:
        return jsonify(error='Choose 1–100 generated images.'), 400
    if any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}\.png', n) or not (OUTPUT/n).is_file() for n in names):
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
    urbanizacion o titulo, con filtros opcionales de zona (y subzona, igual
    que en el admin del CRM), precio maximo y tipologia. Solo lee datos ya
    pensados como publicos (la vista public_properties de la web de Buil &
    Fogwill)."""
    term = _clean_search_term(request.args.get('q', ''))
    zone_filter = request.args.get('zone', '').strip()
    subzone_filter = request.args.get('subzone', '').strip()
    type_filter = request.args.get('type', '').strip()
    max_price_raw = request.args.get('max_price', '').strip()

    if len(term) < 2 and not zone_filter and not subzone_filter and not type_filter and not max_price_raw:
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
    # Coincidencia exacta (como en el desplegable de Zonas del admin del
    # CRM, que tambien filtra por el nombre exacto de zona / subzona, no
    # por texto libre).
    if zone_filter:
        params['zone'] = f'eq.{zone_filter}'
    if subzone_filter:
        params['subzone'] = f'eq.{subzone_filter}'
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


@app.get('/api/hooks')
def get_hooks():
    """Lista de frases 'gancho' guardadas a mano (para usar como titular)."""
    return jsonify(hooks=_load_hooks())


@app.post('/api/hooks')
def save_hooks():
    """Reemplaza la lista completa de ganchos guardados (se manda la lista
    entera desde el textarea de gestion, una frase por linea)."""
    data = request.get_json(silent=True) or {}
    hooks = data.get('hooks')
    if not isinstance(hooks, list):
        return jsonify(error='Formato invalido: se esperaba una lista de frases.'), 400
    saved = _save_hooks(hooks)
    return jsonify(hooks=saved)


@app.post('/api/hooks/generate')
def generate_hooks():
    """Sugiere frases 'gancho' alternativas a partir de un texto pegado o de
    un archivo .md subido, usando un modelo de IA instalado en este mismo
    servidor (Ollama, sin conexion a internet ni claves). No guarda nada por
    si solo: devuelve sugerencias para que el usuario elija cuales agregar."""
    text = request.form.get('text', '').strip()
    md_file = request.files.get('file')
    if md_file and md_file.filename:
        try:
            text = (text + '\n' + md_file.read().decode('utf-8', errors='ignore')).strip()
        except Exception:
            return jsonify(error='No se pudo leer el archivo .md.'), 400
    text = text[:8000]
    if len(text) < 10:
        return jsonify(error='Pega algo de texto o sube un archivo .md con contenido.'), 400
    try:
        count = max(1, min(20, int(request.form.get('count', 8) or 8)))
    except ValueError:
        count = 8
    prompt = (
        'Eres un copywriter inmobiliario. A partir del siguiente texto, escribe '
        f'{count} frases "gancho" cortas, llamativas y distintas entre si, en '
        'espanol, para usar como titular de un anuncio de Instagram/Facebook de '
        'una propiedad inmobiliaria. Una frase por linea, sin numeros ni guiones '
        'al principio, maximo 90 caracteres cada una.\n\nTEXTO:\n' + text
    )
    try:
        resp = requests.post(
            f'{OLLAMA_URL}/api/generate',
            json={'model': OLLAMA_MODEL, 'prompt': prompt, 'stream': False},
            timeout=90,
        )
        resp.raise_for_status()
        raw = resp.json().get('response', '')
    except requests.RequestException as exc:
        return jsonify(error=(
            'No se pudo contactar con la IA local (Ollama) en este servidor. '
            'Verifica que este instalada y corriendo ("ollama serve") con el '
            f'modelo "{OLLAMA_MODEL}" descargado ("ollama pull {OLLAMA_MODEL}"). '
            f'Detalle: {exc}'
        )), 502
    suggestions = [line.strip(' -•\t*').strip() for line in raw.splitlines()]
    suggestions = [line for line in suggestions if line][:count]
    if not suggestions:
        return jsonify(error='La IA no devolvio ninguna frase utilizable. Probá de nuevo.'), 502
    return jsonify(hooks=suggestions)


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
