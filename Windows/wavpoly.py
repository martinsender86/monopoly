#!/usr/bin/env python3
"""MonoPoly — unir monos en poly / separar poly en monos / editar metadata. Bit-exacto.

Reglas:
  * Las muestras se copian byte a byte (solo se intercalan / desintercalan).
  * Sample rate, bits, formato, duración (samples) y timecode deben coincidir
    EXACTO para unir. Nunca se rellena, recorta ni convierte.
  * Todos los chunks de metadata (bext, iXML, LIST, cue, etc.) se copian tal cual,
    en el mismo orden y aunque estén repetidos. Solo cambia lo que describe los
    canales (fmt y TRACK_LIST del iXML) o lo que el usuario edita.
  * Contenedor: un RF64/BW64 sigue siendo RF64/BW64; un RIFF pasa a RF64 solo si
    supera 4 GB.
  * Tiempo sin floats: los frame rates son racionales exactos (Fraction) y toda la
    conversión TC <-> samples es aritmética entera.
"""
import argparse
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction

BLOCK_FRAMES = 1 << 20
PAD_CHUNKS = {b'JUNK', b'junk', b'FLLR', b'PAD '}          # relleno sin información
STRUCT_CHUNKS = {b'fmt ', b'data', b'ds64'}
RIFF_LIMIT = 0xFFFFFFFF


class WavError(Exception):
    pass


# =========================================================================== tiempo (enteros)
FPS_RATES = {  # etiqueta -> (TIMECODE_RATE, TIMECODE_FLAG) como se escriben en iXML
    '23.976': ('24000/1001', 'NDF'), '24': ('24/1', 'NDF'), '25': ('25/1', 'NDF'),
    '29.97 ND': ('30000/1001', 'NDF'), '29.97 DF': ('30000/1001', 'DF'), '30': ('30/1', 'NDF'),
    '48': ('48/1', 'NDF'), '50': ('50/1', 'NDF'), '59.94 ND': ('60000/1001', 'NDF'),
    '59.94 DF': ('60000/1001', 'DF'), '60': ('60/1', 'NDF')}
_KNOWN_RATES = sorted({Fraction(*map(int, r.split('/'))) for r, _ in FPS_RATES.values()})


def parse_rate(text):
    """'24000/1001' | '24' | '23.976' -> Fraction exacta (los decimales se ajustan a la tasa
    estándar más cercana; '23.976' significa 24000/1001, no 2997/125)."""
    if text is None:
        return None
    t = str(text).strip()
    try:
        if '/' in t:
            n, d = t.split('/', 1)
            r = Fraction(int(n), int(d))
        else:
            r = Fraction(t)                     # Fraction('23.976') es exacta, sin float
    except (ValueError, ZeroDivisionError):
        return None
    if r <= 0:
        return None
    if r.denominator != 1 or '/' not in t:
        near = min(_KNOWN_RATES, key=lambda k: abs(k - r))
        if abs(near - r) <= Fraction(1, 100):
            return near
    return r


rate_value = parse_rate                         # compatibilidad


def _as_rate(fps):
    if fps is None or isinstance(fps, Fraction):
        return fps
    if isinstance(fps, int):
        return Fraction(fps)
    if isinstance(fps, str):
        return parse_rate(fps)
    raise TypeError('fps debe ser Fraction, int o str (sin floats)')


def _nominal(fps):
    return -(-fps.numerator // fps.denominator)            # ceil: 23.976 -> 24, 29.97 -> 30


def _drop_frames(nominal):
    return {30: 2, 60: 4}.get(nominal, 0)


def tc_string(samples, sr, fps=None, drop=False):
    """samples desde medianoche -> HH:MM:SS:FF (entero exacto)."""
    if samples is None:
        return '--'
    fps = _as_rate(fps)
    if not fps:
        secs, rem = divmod(samples, sr)
        h, r = divmod(secs, 3600)
        return f'{h:02d}:{r // 60:02d}:{r % 60:02d}+{rem}smp'
    nominal = _nominal(fps)
    total = (samples * fps.numerator) // (sr * fps.denominator)      # frames enteros (floor)
    sep = ':'
    d = _drop_frames(nominal) if drop else 0
    if d:
        fpm = nominal * 60 - d
        fp10 = nominal * 600 - d * 9
        q, r = divmod(total, fp10)
        total += d * 9 * q + (d * ((r - d) // fpm) if r > d else 0)
        sep = ';'
    f = total % nominal
    s_ = total // nominal
    return f'{s_ // 3600:02d}:{s_ // 60 % 60:02d}:{s_ % 60:02d}{sep}{f:02d}'


def rate_label(fps, drop=False):
    fps = _as_rate(fps)
    for lab, (r, fl) in FPS_RATES.items():
        if parse_rate(r) == fps and (fl == 'DF') == bool(drop):
            return lab
    return f'{fps.numerator}/{fps.denominator}'


def tc_to_samples(tc, sr, fps, drop=False):
    """HH:MM:SS:FF -> samples desde medianoche. Inverso exacto de tc_string (enteros)."""
    fps = _as_rate(fps)
    m = re.fullmatch(r'\s*(\d{1,2})[:;.,](\d{1,2})[:;.,](\d{1,2})[:;.,](\d{1,2})\s*', tc or '')
    if not m:
        raise WavError(f'TC inválido: "{tc}" (formato HH:MM:SS:FF)')
    h, mi, s, f = map(int, m.groups())
    nominal = _nominal(fps)
    if h > 23 or mi > 59 or s > 59 or f >= nominal:
        raise WavError(f'TC fuera de rango: "{tc}" a {rate_label(fps, drop)} fps')
    frames = ((h * 60 + mi) * 60 + s) * nominal + f
    d = _drop_frames(nominal) if drop else 0
    if d:
        if s == 0 and mi % 10 and f < d:
            raise WavError(f'"{tc}" no existe en drop-frame')
        tm = h * 60 + mi
        frames -= d * (tm - tm // 10)
    # primer sample cuyo frame es 'frames' = techo de frames*sr*den/num, en enteros
    return -((-frames * sr * fps.denominator) // fps.numerator)


# =========================================================================== iXML (árbol real)
_TOK = re.compile(
    r'<!--.*?-->|<!\[CDATA\[.*?\]\]>|<\?.*?\?>|<!DOCTYPE[^>]*>'
    r'|<(/?)([A-Za-z_][\w.\-:]*)((?:"[^"]*"|\'[^\']*\'|[^\'">])*?)(/?)>', re.S)
_BAD_XML_CHARS = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')


def _xml_elements(x):
    """Elementos con su ruta exacta desde la raíz y sus posiciones en el texto.
    No reserializa nada: permite editar un elemento y dejar el resto byte a byte igual."""
    stack, out = [], []
    for m in _TOK.finditer(x):
        if m.group(2) is None:
            continue
        close, name, selfc = m.group(1), m.group(2), m.group(4)
        if close:
            if not stack or stack[-1][0] != name:
                raise WavError('iXML mal formado')
            n, s, ist = stack.pop()
            out.append({'path': tuple(e[0] for e in stack) + (n,), 'start': s, 'end': m.end(),
                        'istart': ist, 'iend': m.start(), 'empty': False})
        elif selfc:
            out.append({'path': tuple(e[0] for e in stack) + (name,), 'start': m.start(), 'end': m.end(),
                        'istart': m.end(), 'iend': m.end(), 'empty': True})
        else:
            stack.append((name, m.start(), m.end()))
    if stack:
        raise WavError('iXML mal formado')
    out.sort(key=lambda e: e['start'])
    return out


def _find(els, path):
    return next((e for e in els if e['path'] == path), None)


def _esc(s):
    s = _BAD_XML_CHARS.sub('', str(s))
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def xml_get(x, path):
    """Texto del primer elemento con esa ruta exacta (('BWFXML','SCENE')), con entidades resueltas."""
    if not x:
        return None
    try:
        e = _find(_xml_elements(x), path)
        if e is None:
            return None
        return ET.fromstring(x[e['start']:e['end']]).text or ''
    except (WavError, ET.ParseError):
        return _lenient_get(x, path[-1])


def xml_set(x, path, value=None, raw=None):
    """Reemplaza (o crea) el elemento de ruta exacta `path`. Solo toca ese elemento.
    value: texto (se escapa); raw: XML ya armado del elemento completo."""
    tag = path[-1]
    new = raw if raw is not None else f'<{tag}>{_esc(value)}</{tag}>'
    els = _xml_elements(x)
    e = _find(els, path)
    if e is not None:
        return x[:e['start']] + new + x[e['end']:]
    parent = _find(els, path[:-1])
    if parent is None:
        if len(path) <= 2:
            raise WavError('iXML sin raíz BWFXML')
        x = xml_set(x, path[:-1], raw=f'<{path[-2]}></{path[-2]}>')
        parent = _find(_xml_elements(x), path[:-1])
    if parent['empty']:
        pt = path[-2]
        return x[:parent['start']] + f'<{pt}>{new}</{pt}>' + x[parent['end']:]
    return x[:parent['iend']] + new + x[parent['iend']:]


def xml_set_tolerant(x, path, value):
    """xml_set; si el iXML está tan roto que no se puede recorrer como árbol, reemplaza la
    primera aparición de la etiqueta (fuera de TRACK_LIST) o la agrega antes de </BWFXML>."""
    try:
        return xml_set(x, path, value)
    except WavError:
        pass
    tag = path[-1]
    new = f'<{tag}>{_esc(value)}</{tag}>'
    tl = re.search(r'<TRACK_LIST>.*?</TRACK_LIST>', x, re.S)
    for m in re.finditer(rf'<{tag}>.*?</{tag}>|<{tag}\s*/>', x, re.S):
        if tl and tl.start() <= m.start() < tl.end():
            continue
        return x[:m.start()] + new + x[m.end():]
    if len(path) == 3:
        sp = re.search(rf'</{path[1]}>', x)
        if sp:
            return x[:sp.start()] + new + x[sp.start():]
        new = f'<{path[1]}>{new}</{path[1]}>'
    if '</BWFXML>' not in x:
        raise WavError('iXML sin </BWFXML>: no se puede editar')
    return x.replace('</BWFXML>', new + '</BWFXML>', 1)


def _ixml_codec(raw):
    m = re.match(rb'\s*<\?xml[^>]*encoding\s*=\s*["\']([\w.\-]+)', raw)
    enc = m.group(1).decode('ascii').lower() if m else 'utf-8'
    try:
        ''.encode(enc)
    except LookupError:
        enc = 'utf-8'
    return enc


def ixml_decode(raw):
    enc = _ixml_codec(raw)
    return raw.decode(enc, 'replace').lstrip('﻿').rstrip('\0 \r\n\t'), enc


def ixml_is_valid(text, enc='utf-8'):
    try:
        ET.fromstring(text.encode(enc, 'xmlcharrefreplace'))
        return True
    except ET.ParseError:
        return False


def ixml_encode(text, enc, original=None):
    """Codifica el iXML a escribir. Si el original era XML válido, exige que el resultado
    también lo sea (no rompemos nada). Si el original YA venía mal formado del grabador
    (un '&' suelto, caracteres de control...), no bloquea: se escribe con el mismo defecto."""
    data = text.encode(enc, 'xmlcharrefreplace')
    if original is not None and not ixml_is_valid(original, enc):
        return data
    try:
        ET.fromstring(data)
    except ET.ParseError as e:
        raise WavError(f'el iXML resultante no es válido ({e}); no se escribió nada') from e
    return data


_ENT = {'&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'", '&amp;': '&'}


def _lenient_text(s):
    s = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), s)
    s = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r'&(lt|gt|quot|apos|amp);', lambda m: _ENT[m.group(0)], s)


def _lenient_get(x, tag):
    """Lectura de respaldo para iXML mal formado: primer <tag> fuera de TRACK_LIST."""
    tl = re.search(r'<TRACK_LIST>.*?</TRACK_LIST>', x, re.S)
    for m in re.finditer(rf'<{tag}>(.*?)</{tag}>', x, re.S):
        if tl and tl.start() <= m.start() < tl.end():
            continue
        return _lenient_text(m.group(1))
    return None


def ixml_tracks_from(x):
    if not x:
        return []
    try:
        e = _find(_xml_elements(x), ('BWFXML', 'TRACK_LIST'))
        if e is None:
            return []
        root = ET.fromstring(x[e['start']:e['end']])
        return [{c.tag: (c.text or '') for c in t} for t in root.findall('TRACK')]
    except (WavError, ET.ParseError):
        pass
    tl = re.search(r'<TRACK_LIST>(.*?)</TRACK_LIST>', x, re.S)        # respaldo tolerante
    if not tl:
        return []
    return [{k: _lenient_text(v) for k, v in re.findall(r'<([A-Za-z_][\w.\-]*)>(.*?)</\1>', t, re.S)}
            for t in re.findall(r'<TRACK>(.*?)</TRACK>', tl.group(1), re.S)]


def _track_list_xml(tracks):
    parts = [f'<TRACK_LIST><TRACK_COUNT>{len(tracks)}</TRACK_COUNT>']
    for t in tracks:
        parts.append('<TRACK>' + ''.join(f'<{k}>{_esc(v)}</{k}>' for k, v in t.items()) + '</TRACK>')
    parts.append('</TRACK_LIST>')
    return ''.join(parts)


def _replace_track_list(x, tracks):
    new = _track_list_xml(tracks)
    try:
        return xml_set(x, ('BWFXML', 'TRACK_LIST'), raw=new)
    except WavError:
        # iXML mal formado: se reemplaza solo el bloque TRACK_LIST, el resto queda igual
        if re.search(r'<TRACK_LIST>.*?</TRACK_LIST>', x, re.S):
            return re.sub(r'<TRACK_LIST>.*?</TRACK_LIST>', lambda _: new, x, count=1, flags=re.S)
        if '</BWFXML>' in x:
            return x.replace('</BWFXML>', new + '</BWFXML>', 1)
        return x


# =========================================================================== lectura
class Wav:
    def __init__(self, path):
        self.path = path
        self.chunks = []          # (id, offset_cuerpo, size) en orden
        self.meta = []            # (id, cuerpo, después_de_data) en orden; fmt como marcador
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            hdr = f.read(12)
            if len(hdr) < 12 or hdr[8:12] != b'WAVE' or hdr[:4] not in (b'RIFF', b'RF64', b'BW64'):
                raise WavError('no es un WAV/BWF válido')
            self.kind = hdr[:4]
            ds64_data = None
            pos = 12
            while pos + 8 <= size:
                f.seek(pos)
                cid, sz = struct.unpack('<4sI', f.read(8))
                if cid == b'ds64':
                    if sz < 24:
                        raise WavError('chunk ds64 demasiado corto')
                    ds = f.read(24)
                    if len(ds) < 24:
                        raise WavError('chunk ds64 truncado')
                    ds64_data = struct.unpack('<Q', ds[8:16])[0]
                if cid == b'data' and self.kind != b'RIFF' and sz == 0xFFFFFFFF:
                    if ds64_data is None:
                        raise WavError('RF64 sin ds64: no se puede saber el tamaño del audio')
                    sz = ds64_data
                if pos + 8 + sz > size:
                    if cid == b'data':
                        raise WavError('chunk data truncado (archivo incompleto)')
                    break
                self.chunks.append((cid, pos + 8, sz))
                pos += 8 + sz + (sz & 1)
            ids = [c[0] for c in self.chunks]
            if b'fmt ' not in ids or b'data' not in ids:
                raise WavError('falta chunk fmt o data')
            seen_data = seen_fmt = False
            for cid, off, sz in self.chunks:
                if cid == b'data':
                    seen_data = True
                elif cid == b'fmt ':
                    if not seen_fmt:
                        f.seek(off)
                        self.fmt_raw = f.read(sz)
                        self.meta.append((b'fmt ', None, seen_data))
                        seen_fmt = True
                elif cid not in STRUCT_CHUNKS and cid not in PAD_CHUNKS:
                    f.seek(off)
                    self.meta.append((cid, f.read(sz), seen_data))
        self.chunk_bodies = {}                    # primera aparición, para lectura
        for cid, body, _ in self.meta:
            if body is not None:
                self.chunk_bodies.setdefault(cid, body)
        if len(self.fmt_raw) < 16:
            raise WavError('chunk fmt corto')
        (self.tag, self.channels, self.sr, self.byterate,
         self.blockalign, self.bits) = struct.unpack('<HHIIHH', self.fmt_raw[:16])
        if self.channels == 0 or self.sr == 0:
            raise WavError('fmt inválido (0 canales o 0 Hz)')
        self.eff_tag = self.tag
        if self.tag == 0xFFFE and len(self.fmt_raw) >= 40:
            self.eff_tag = struct.unpack('<H', self.fmt_raw[24:26])[0]
        _, self.data_off, self.data_size = next(c for c in self.chunks if c[0] == b'data')
        if self.blockalign == 0 or self.data_size % self.blockalign:
            raise WavError('tamaño de data no es múltiplo del block align')
        self.frames = self.data_size // self.blockalign

    # metadata --------------------------------------------------------------
    @property
    def channel_mask(self):
        if self.tag == 0xFFFE and len(self.fmt_raw) >= 24:
            return struct.unpack('<I', self.fmt_raw[20:24])[0]
        return None

    @property
    def time_reference(self):
        b = self.chunk_bodies.get(b'bext')
        if b is None or len(b) < 346:
            return None
        return struct.unpack('<Q', b[338:346])[0]

    @property
    def ixml(self):
        b = self.chunk_bodies.get(b'iXML')
        return ixml_decode(b)[0] if b is not None else None

    def ixml_get(self, tag):
        return xml_get(self.ixml, ('BWFXML', tag)) or ''

    def ixml_fps(self):
        """(Fraction, 'NDF'|'DF') o (None, None)."""
        x = self.ixml
        rate = parse_rate(xml_get(x, ('BWFXML', 'SPEED', 'TIMECODE_RATE')))
        if not rate:
            return None, None
        flag = (xml_get(x, ('BWFXML', 'SPEED', 'TIMECODE_FLAG')) or 'NDF').strip().upper()
        return rate, ('DF' if flag == 'DF' else 'NDF')

    def ixml_tracks(self):
        return ixml_tracks_from(self.ixml)


# =========================================================================== escritura
def _single_bits(mask):
    return [1 << i for i in range(32) if mask >> i & 1]


def _fmt_with_channels(src, n, mask=0):
    raw = bytearray(src.fmt_raw)
    ba = src.blockalign // src.channels * n
    raw[2:4] = struct.pack('<H', n)
    struct.pack_into('<I', raw, 8, src.sr * ba)
    struct.pack_into('<H', raw, 12, ba)
    if src.tag == 0xFFFE and len(raw) >= 24:
        struct.pack_into('<I', raw, 20, mask)
    return bytes(raw)


def _layout(src, override=None):
    """Chunks de src en su orden original (repetidos incluidos), con reemplazos.
    override {cid: cuerpo} reemplaza la PRIMERA aparición; si no existe, se agrega
    (bext al principio, iXML después del bext)."""
    override = dict(override or {})
    before, after = [], []
    for cid, body, aft in src.meta:
        if cid in override:
            body = override.pop(cid)
        (after if aft else before).append((cid, body))
    if b'bext' in override:
        before.insert(0, (b'bext', override.pop(b'bext')))
    if b'iXML' in override:
        idx = next((i + 1 for i, (c, _) in enumerate(before) if c == b'bext'), len(before))
        before.insert(idx, (b'iXML', override.pop(b'iXML')))
    for cid, body in override.items():
        before.append((cid, body))
    return before, after


def _chunk_bytes(cid, body):
    return struct.pack('<4sI', cid, len(body)) + body + (b'\0' if len(body) & 1 else b'')


class Writer:
    """Escribe a <path>.part y renombra al final. kind: RIFF / RF64 / BW64."""

    def __init__(self, path, fmt_raw, before, after, data_size, blockalign, kind=b'RIFF'):
        self.path = path
        if not any(c == b'fmt ' for c, _ in before):
            before = [(b'fmt ', None)] + list(before)
        self.pre = b''.join(_chunk_bytes(c, fmt_raw if c == b'fmt ' else b) for c, b in before)
        self.post = b''.join(_chunk_bytes(c, b) for c, b in after)
        self.data_size = data_size
        body = 4 + len(self.pre) + 8 + data_size + (data_size & 1) + len(self.post)
        big = body + 36 > RIFF_LIMIT
        self.rf64 = kind in (b'RF64', b'BW64') or big
        self.f = open(path + '.part', 'wb')
        if self.rf64:
            magic = kind if kind in (b'RF64', b'BW64') else b'RF64'
            body += 36                                     # chunk ds64 (8 + 28)
            sample_count = data_size // blockalign         # EBU Tech 3306: cantidad de frames
            ds64 = struct.pack('<QQQI', body, data_size, sample_count, 0)
            self.f.write(magic + struct.pack('<I', 0xFFFFFFFF) + b'WAVE')
            self.f.write(_chunk_bytes(b'ds64', ds64))
            dsz = 0xFFFFFFFF
        else:
            self.f.write(b'RIFF' + struct.pack('<I', body) + b'WAVE')
            dsz = data_size
        self.f.write(self.pre + struct.pack('<4sI', b'data', dsz))
        self.written = 0

    def write(self, b):
        self.f.write(b)
        self.written += len(b)

    def close(self):
        if self.written != self.data_size:
            self.abort()
            raise WavError('tamaño escrito no coincide')
        if self.data_size & 1:
            self.f.write(b'\0')
        self.f.write(self.post)
        self.f.close()
        os.replace(self.path + '.part', self.path)

    def abort(self):
        try:
            self.f.close()
        except OSError:
            pass
        try:
            os.remove(self.path + '.part')
        except OSError:
            pass


def _out_kind(wavs):
    return next((w.kind for w in wavs if w.kind != b'RIFF'), b'RIFF')


# =========================================================================== unir
def check_mergeable(wavs):
    problems = []
    a = wavs[0]
    for w in wavs:
        n = os.path.basename(w.path)
        if w.channels != 1:
            problems.append(f'{n}: no es mono ({w.channels} canales)')
        if (w.sr, w.bits, w.eff_tag, w.tag, w.blockalign // max(w.channels, 1)) != \
                (a.sr, a.bits, a.eff_tag, a.tag, a.blockalign // max(a.channels, 1)):
            problems.append(f'{n}: formato distinto ({w.sr} Hz / {w.bits} bit vs {a.sr} Hz / {a.bits} bit)')
        if w.frames != a.frames:
            problems.append(f'{n}: duración distinta ({w.frames} vs {a.frames} samples)')
        if w.time_reference != a.time_reference:
            problems.append(f'{n}: timecode distinto ({w.time_reference} vs {a.time_reference} samples)')
    return problems


def _merged_mask(wavs):
    """Si cada mono declara UNA posición de parlante distinta y en orden ascendente
    (ej. FL 0x1 + FR 0x2), el poly las conserva (0x3). Si no, 0 = canales sin posición."""
    masks = [w.channel_mask for w in wavs]
    if any(m is None or len(_single_bits(m)) != 1 for m in masks):
        return 0
    if any(masks[i] >= masks[i + 1] for i in range(len(masks) - 1)):
        return 0
    out = 0
    for m in masks:
        out |= m
    return out


def merge(wavs, out_path, labels=None, progress=None):
    problems = check_mergeable(wavs)
    if problems:
        raise WavError('; '.join(problems))
    base = wavs[0]
    n = len(wavs)
    fmt_raw = _fmt_with_channels(base, n, _merged_mask(wavs))
    override = {}
    if b'iXML' in base.chunk_bodies:
        x, enc = ixml_decode(base.chunk_bodies[b'iXML'])
        order = ['CHANNEL_INDEX', 'INTERLEAVE_INDEX', 'NAME', 'FUNCTION']
        tracks = []
        for i, w in enumerate(wavs):
            wt = w.ixml_tracks()
            t = dict(wt[0]) if len(wt) == 1 else {}
            t.setdefault('CHANNEL_INDEX', str(i + 1))
            t['INTERLEAVE_INDEX'] = str(i + 1)
            if not t.get('NAME') and labels:
                t['NAME'] = labels[i]
            tracks.append({k: t[k] for k in order if k in t} | {k: v for k, v in t.items() if k not in order})
        override[b'iXML'] = ixml_encode(_replace_track_list(x, tracks), enc, original=x)
    before, after = _layout(base, override)
    bps = base.blockalign
    w = Writer(out_path, fmt_raw, before, after, base.frames * bps * n, bps * n, _out_kind(wavs))
    files = []
    try:
        files = [open(x.path, 'rb') for x in wavs]
        for f, x in zip(files, wavs):
            f.seek(x.data_off)
        left = base.frames
        step = bps * n
        while left:
            k = min(BLOCK_FRAMES, left)
            buf = bytearray(k * step)
            for c, f in enumerate(files):
                mono = f.read(k * bps)
                if len(mono) != k * bps:
                    raise WavError('lectura incompleta de ' + os.path.basename(wavs[c].path))
                for b in range(bps):
                    buf[c * bps + b::step] = mono[b::bps]
            w.write(bytes(buf))
            left -= k
            if progress:
                progress(base.frames - left, base.frames)
        w.close()
    except BaseException:
        w.abort()
        raise
    finally:
        for f in files:
            f.close()


# =========================================================================== separar
def split(poly, out_paths, progress=None):
    n = poly.channels
    if len(out_paths) != n:
        raise WavError('cantidad de salidas != canales')
    bps = poly.blockalign // n
    pm = poly.channel_mask
    bits = _single_bits(pm) if pm else []
    tracks = poly.ixml_tracks()
    x, enc = ixml_decode(poly.chunk_bodies[b'iXML']) if b'iXML' in poly.chunk_bodies else (None, None)
    writers = []
    try:
        for i, p in enumerate(out_paths):
            override = {}
            if x is not None:
                t = next((dict(t) for t in tracks if t.get('INTERLEAVE_INDEX') == str(i + 1)),
                         dict(tracks[i]) if i < len(tracks) else {'CHANNEL_INDEX': str(i + 1)})
                t['INTERLEAVE_INDEX'] = '1'
                override[b'iXML'] = ixml_encode(_replace_track_list(x, [t]), enc, original=x)
            before, after = _layout(poly, override)
            mono_fmt = _fmt_with_channels(poly, 1, bits[i] if len(bits) == n else 0)
            writers.append(Writer(p, mono_fmt, before, after, poly.frames * bps, bps, poly.kind))
        with open(poly.path, 'rb') as f:
            f.seek(poly.data_off)
            left = poly.frames
            step = poly.blockalign
            while left:
                k = min(BLOCK_FRAMES, left)
                a = f.read(k * step)
                if len(a) != k * step:
                    raise WavError('lectura incompleta del poly')
                for i, wr in enumerate(writers):
                    out = bytearray(k * bps)
                    for b in range(bps):
                        out[b::bps] = a[i * bps + b::step]
                    wr.write(bytes(out))
                left -= k
                if progress:
                    progress(poly.frames - left, poly.frames)
        for wr in writers:
            wr.close()
    except BaseException:
        for wr in writers:
            wr.abort()
        raise


def safe_name(s):
    """Texto apto para nombre de archivo en Windows y Mac (conserva tildes y ñ)."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', s or '')
    s = re.sub(r'\s+', '_', s.strip())
    s = re.sub(r'_+', '_', s).strip('._ ')
    return s


def split_labels(poly, mode='num_name'):
    """Sufijo de cada mono al separar, según el iXML del poly.
    mode: 'num_name' -> Tr1_Boom (o solo Tr1 si el track no tiene nombre)
          'num'      -> Tr1
          'name'     -> Boom (o Tr1 si no tiene nombre)
    El número es el CHANNEL_INDEX del grabador si existe; si no, la posición."""
    tracks = poly.ixml_tracks()
    labels, used = [], set()
    for i in range(poly.channels):
        t = next((t for t in tracks if (t.get('INTERLEAVE_INDEX') or '').strip() == str(i + 1)), None)
        if t is None and len(tracks) == poly.channels and not any(x.get('INTERLEAVE_INDEX') for x in tracks):
            t = tracks[i]
        ci = (t.get('CHANNEL_INDEX') or '').strip() if t else ''
        num = 'Tr' + (str(int(ci)) if ci.isdigit() else str(i + 1))
        name = safe_name(t.get('NAME')) if t else ''
        if mode == 'num':
            lab = num
        elif mode == 'name':
            lab = name or num
        else:
            lab = f'{num}_{name}' if name else num
        if lab in used:
            lab = f'{lab}_{i + 1}'
        used.add(lab)
        labels.append(lab)
    return labels


# =========================================================================== metadata
IXML_FIELDS = ['PROJECT', 'SCENE', 'TAKE', 'TAPE', 'CIRCLED', 'WILD_TRACK', 'UBITS', 'NOTE']
# claves estilo Sound Devices dentro de bext Description (solo se actualizan si ya existen)
BEXT_KEYS = {'PROJECT': 'sPROJECT', 'SCENE': 'sSCENE', 'TAKE': 'sTAKE', 'TAPE': 'sTAPE',
             'CIRCLED': 'sCIRCLED', 'NOTE': 'sNOTE', 'UBITS': 'sUBITS'}


def _new_bext():
    b = bytearray(602)
    struct.pack_into('<H', b, 346, 1)
    return b


def write_meta_copy(src, out_path, fields=None, track_names=None, time_reference=None,
                    fps=None, progress=None):
    """Escribe una COPIA de src con metadata cambiada. El audio se copia byte a byte.

    fields:          {'SCENE': '12', 'TAKE': '3', ...}  (iXML; y bext sKEY= si existe)
    track_names:     {interleave_index (1..n): 'Boom', ...}
    time_reference:  samples desde medianoche (bext TimeReference)
    fps:             ('24000/1001', 'NDF')  -> iXML SPEED
    """
    if os.path.abspath(out_path) == os.path.abspath(src.path) or \
            (os.path.exists(out_path) and os.path.samefile(out_path, src.path)):
        raise WavError('la salida no puede ser el archivo original')
    fields = {k: v for k, v in (fields or {}).items() if v is not None}
    track_names = track_names or {}
    override = {}

    # ---- bext
    if time_reference is not None or fields or track_names:
        b = bytearray(src.chunk_bodies[b'bext']) if b'bext' in src.chunk_bodies else None
        if b is None and time_reference is not None:
            b = _new_bext()
        if b is not None:
            if time_reference is not None:
                if not 0 <= time_reference < 1 << 64:
                    raise WavError('TimeReference fuera de rango')
                if len(b) < 346:
                    raise WavError('bext corto: no se puede escribir el TimeReference')
                struct.pack_into('<Q', b, 338, time_reference)
            desc = bytes(b[:256]).split(b'\0')[0].decode('latin-1')
            nd = desc
            for tag, val in fields.items():
                key = BEXT_KEYS.get(tag)
                if key:
                    nd = re.sub(rf'(?m)^{key}=[^\r\n]*', lambda _, k=key, v=val: f'{k}={v}', nd)
            if track_names:
                tr = {t.get('INTERLEAVE_INDEX'): t.get('CHANNEL_INDEX') for t in src.ixml_tracks()}
                for il, name in track_names.items():
                    ch = tr.get(str(il)) or str(il)
                    nd = re.sub(rf'(?m)^sTRK{ch}=[^\r\n]*', lambda _, c=ch, v=name: f'sTRK{c}={v}', nd)
            if nd != desc:
                enc = nd.encode('latin-1', 'replace')
                if len(enc) <= 256:
                    b[:256] = enc.ljust(256, b'\0')
            if b'bext' in src.chunk_bodies or time_reference is not None:
                override[b'bext'] = bytes(b)

    # ---- iXML
    has_ixml = b'iXML' in src.chunk_bodies
    if fields or track_names or fps or (time_reference is not None and has_ixml):
        if has_ixml:
            x, enc = ixml_decode(src.chunk_bodies[b'iXML'])
            x0 = x
        else:
            x = '<?xml version="1.0" encoding="UTF-8"?>\n<BWFXML><IXML_VERSION>1.61</IXML_VERSION></BWFXML>'
            enc = 'utf-8'
            x0 = None
        for tag in IXML_FIELDS:
            if tag in fields:
                x = xml_set_tolerant(x, ('BWFXML', tag), fields[tag])
        if fps:
            x = xml_set_tolerant(x, ('BWFXML', 'SPEED', 'TIMECODE_RATE'), fps[0])
            x = xml_set_tolerant(x, ('BWFXML', 'SPEED', 'TIMECODE_FLAG'), fps[1])
        if time_reference is not None:
            if xml_get(x, ('BWFXML', 'SPEED', 'TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO')) is not None or not has_ixml:
                x = xml_set_tolerant(x, ('BWFXML', 'SPEED', 'TIMESTAMP_SAMPLE_RATE'), str(src.sr))
                x = xml_set_tolerant(x, ('BWFXML', 'SPEED', 'TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI'), str(time_reference >> 32))
                x = xml_set_tolerant(x, ('BWFXML', 'SPEED', 'TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO'),
                            str(time_reference & 0xFFFFFFFF))
        if track_names:
            tracks = ixml_tracks_from(x) or [{'CHANNEL_INDEX': str(i + 1), 'INTERLEAVE_INDEX': str(i + 1)}
                                              for i in range(src.channels)]
            for t in tracks:
                il = int(t.get('INTERLEAVE_INDEX') or 0)
                if il in track_names:
                    t['NAME'] = track_names[il]
            x = _replace_track_list(x, tracks)
        override[b'iXML'] = ixml_encode(x, enc, original=x0)

    before, after = _layout(src, override)
    w = Writer(out_path, src.fmt_raw, before, after, src.data_size, src.blockalign, src.kind)
    try:
        with open(src.path, 'rb') as f:
            f.seek(src.data_off)
            left = src.data_size
            chunk = BLOCK_FRAMES * src.blockalign
            while left:
                k = min(chunk, left)
                buf = f.read(k)
                if len(buf) != k:
                    raise WavError('lectura incompleta')
                w.write(buf)
                left -= k
                if progress:
                    progress(src.data_size - left, src.data_size)
        w.close()
    except BaseException:
        w.abort()
        raise


# =========================================================================== agrupar
DEFAULT_NAME_RE = r'^(?P<base>.+?)[_\-\. ](?P<track>[^_\-\. ]+)$'


def natkey(s):
    return [(0, int(t), '') if t.isdigit() else (1, 0, t.lower()) for t in re.split(r'(\d+)', s or '')]


def _last_number(s):
    m = re.findall(r'\d+', s or '')
    return str(int(m[-1])) if m else None


def track_ids(items):
    """Número de cada track para el nombre del poly (ZOOM0018_1-2, ZOOM0018_3-4).
    Prioridad: CHANNEL_INDEX del iXML > número del sufijo del nombre (Tr3 -> 3) >
    número de la carpeta (Tr3/) > sufijo textual (Boom) > posición.
    items: [wav, base, etiqueta, sufijo_original] ya ordenados."""
    def ok(ids):
        return all(ids) and len(set(ids)) == len(ids)
    ids = []
    for it in items:
        t = it[0].ixml_tracks()
        ci = (t[0].get('CHANNEL_INDEX') or '').strip() if len(t) == 1 else ''
        ids.append(str(int(ci)) if ci.isdigit() else None)
    if ok(ids):
        return ids
    ids = [_last_number(it[3]) for it in items]
    if ok(ids):
        return ids
    ids = [_last_number(os.path.basename(os.path.dirname(os.path.abspath(it[0].path)))) for it in items]
    if ok(ids):
        return ids
    ids = [re.sub(r'[\\/:*?"<>|\s]+', '', it[3] or '') or None for it in items]
    if ok(ids):
        return ids
    return [str(i + 1) for i in range(len(items))]


def find_groups(wavs, by=('name', 'tc', 'duration'), name_re=DEFAULT_NAME_RE, same_dir=True):
    """Agrupa monos correspondientes.
    same_dir=False permite unir monos en carpetas distintas (ej. Tr1/, Tr2/); en ese
    caso es obligatorio el criterio de timecode, para no mezclar tomas de días distintos."""
    if not same_dir and 'tc' not in by:
        raise WavError('para agrupar entre carpetas distintas hay que usar también el timecode')
    groups = {}
    for w in wavs:
        if w.channels != 1:
            continue
        stem = os.path.splitext(os.path.basename(w.path))[0]
        wt = w.ixml_tracks()
        nm = safe_name(wt[0].get('NAME')) if len(wt) == 1 else ''
        if nm and stem.endswith('_' + nm) and len(stem) > len(nm) + 1:
            stem = stem[:-(len(nm) + 1)]           # ZOOM0018_1_Boom -> ZOOM0018_1 (separado por MonoPoly)
        m = re.match(name_re, stem)
        base, track = (m.group('base'), m.group('track')) if m else (stem, stem)
        key = [os.path.dirname(os.path.abspath(w.path))] if same_dir else []
        if 'name' in by:
            key.append(base)
        if 'tc' in by:
            key.append(w.time_reference)
        if 'duration' in by:
            key.append(w.frames)
        groups.setdefault(tuple(key), []).append([w, base, track, track])
    out = []
    for items in groups.values():
        if len(items) < 2:
            continue
        labels = [it[2] for it in items]
        if len(set(labels)) < len(labels):          # mismo nombre en carpetas distintas
            for it in items:
                it[2] = os.path.basename(os.path.dirname(os.path.abspath(it[0].path))) + '_' + it[2]

        def order(it):
            t = it[0].ixml_tracks()
            ci = t[0].get('CHANNEL_INDEX') if len(t) == 1 else None
            return (0, int(ci), []) if ci and ci.isdigit() else (1, 0, natkey(it[2]))
        items.sort(key=order)
        bases = {it[1] for it in items}
        name = items[0][1] if len(bases) == 1 else os.path.commonprefix(
            [os.path.splitext(os.path.basename(it[0].path))[0] for it in items]).rstrip('_-. ') or items[0][1]
        ids = track_ids(items)
        dirs = sorted({os.path.dirname(os.path.abspath(i[0].path)) for i in items})
        try:
            gdir = dirs[0] if len(dirs) == 1 else os.path.commonpath(dirs)
        except ValueError:                           # discos distintos en Windows
            gdir = dirs[0]
        out.append({'name': name, 'poly_name': f"{name}_{'-'.join(ids)}", 'track_ids': ids,
                    'dir': gdir, 'wavs': [i[0] for i in items], 'labels': [i[2] for i in items],
                    'problems': check_mergeable([i[0] for i in items])})
    return sorted(out, key=lambda g: natkey(g['name']))


# =========================================================================== CLI
def collect(paths, errors=None):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, fs in os.walk(p):
                files += [os.path.join(root, x) for x in fs if x.lower().endswith(('.wav', '.bwf'))]
        else:
            files.append(p)
    out = []
    for p in sorted(files, key=natkey):
        try:
            out.append(Wav(p))
        except (WavError, OSError, struct.error) as e:
            if errors is not None:
                errors.append(f'{p}: {e}')
            elif sys.stderr:
                print(f'  ! {p}: {e}', file=sys.stderr)
    return out


def _dur(frames, sr):
    s, r = divmod(frames, sr)
    return f'{s}.{r * 1000 // sr:03d}s'


def describe(w):
    fps, flag = w.ixml_fps()
    return (f'{w.channels}ch {w.sr}Hz {w.bits}bit  TC {tc_string(w.time_reference, w.sr, fps, flag == "DF")}'
            f'  {_dur(w.frames, w.sr)} ({w.frames} smp)')


def cmd_merge(a):
    wavs = collect(a.inputs)
    groups = find_groups(wavs, tuple(a.by.split(',')), a.name_re, same_dir=not a.cross_dirs)
    grouped = {w.path for g in groups for w in g['wavs']}
    loose = [w for w in wavs if w.channels == 1 and w.path not in grouped]
    rc = 0
    if loose:
        rc = 2
        print('Monos sin grupo (no coinciden con ningún otro en nombre/TC/duración):')
        for w in loose:
            print(f'   {os.path.basename(w.path)}  {describe(w)}')
    if not groups:
        print('No se encontraron grupos de monos para unir.')
        return 1
    for g in groups:
        ext = os.path.splitext(g['wavs'][0].path)[1]
        outdir = a.out or g['dir']
        out = os.path.join(outdir, g['poly_name'] + a.suffix + ext)
        print(f"\n[{g['poly_name']}] {len(g['wavs'])} monos -> {out}")
        for w, lab in zip(g['wavs'], g['labels']):
            print(f'   {lab:>8}: {w.path}  {describe(w)}')
        if g['problems']:
            rc = 2
            for p in g['problems']:
                print('   NO SE UNE:', p)
            continue
        if a.dry_run:
            continue
        if os.path.exists(out) and not a.overwrite:
            print('   ya existe, salteado (usar --overwrite)')
            continue
        os.makedirs(outdir, exist_ok=True)
        merge(g['wavs'], out, g['labels'])
        print('   OK')
    return rc


def cmd_split(a):
    rc = 0
    for w in collect(a.inputs):
        if w.channels < 2:
            continue
        stem, ext = os.path.splitext(os.path.basename(w.path))
        outdir = a.out or os.path.dirname(w.path)
        outs = [os.path.join(outdir, f'{stem}_{lab}{ext}') for lab in split_labels(w, a.names)]
        print(f'\n[{stem}] {describe(w)}')
        for o in outs:
            print('   ->', o)
        if a.dry_run:
            continue
        if any(os.path.exists(o) for o in outs) and not a.overwrite:
            print('   ya existen, salteado (usar --overwrite)')
            rc = 2
            continue
        os.makedirs(outdir, exist_ok=True)
        split(w, outs)
        print('   OK')
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description='Unir monos en poly / separar poly en monos (bit-exacto)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('merge', help='unir monos -> poly')
    m.add_argument('inputs', nargs='+')
    m.add_argument('-o', '--out', help='carpeta de salida (default: la de los monos)')
    m.add_argument('--by', default='name,tc,duration', help='criterios: name,tc,duration')
    m.add_argument('--name-re', default=DEFAULT_NAME_RE)
    m.add_argument('--cross-dirs', action='store_true',
                   help='permitir unir monos de carpetas distintas (requiere tc)')
    m.add_argument('--suffix', default='', help='sufijo para el nombre del poly')
    m.add_argument('-n', '--dry-run', action='store_true')
    m.add_argument('--overwrite', action='store_true')
    m.set_defaults(fn=cmd_merge)
    s = sub.add_parser('split', help='separar poly -> monos')
    s.add_argument('inputs', nargs='+')
    s.add_argument('-o', '--out')
    s.add_argument('--names', choices=['num_name', 'num', 'name'], default='num_name',
                   help='sufijo de los monos: num_name (Tr1_Boom, default), num (Tr1) o name (Boom)')
    s.add_argument('-n', '--dry-run', action='store_true')
    s.add_argument('--overwrite', action='store_true')
    s.set_defaults(fn=cmd_split)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except WavError as e:
        print('ERROR:', e, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
