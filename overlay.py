#!/usr/bin/env python3
"""
b-roll text overlay engine + CLI.

Importable API (used by app.py):
    FONTS                       display-name -> ttf filename
    probe(video)                -> (width, height, duration, has_audio)
    render(input, output, text, opts)     burn text, write a video
    still(input, out_png, at, text, opts) burn text, write one PNG frame

opts is a dict (see DEFAULTS). Text is the raw multi-line string to burn on.
Layouts:
    block     whole text centered as one auto-fit block (the list look)
    captions  one caption per line (bottom), optional [m:ss-m:ss] timing
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

from PIL import ImageFont
from fontTools.ttLib import TTFont as _FTFont

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(HERE, "fonts")

# Every file here is SIL Open Font License — safe to ship in a public repo and
# to burn into content posted commercially.
#
# TikTok Sans is TikTok's real brand font; they released it under the OFL, so
# these are the genuine article. Instagram Sans is NOT: Meta commissioned it and
# licenses it to nobody, so there is no lawful copy to ship. The "(IG look)"
# entries are the closest open-licensed geometric sans stand-ins for the Reels
# caption look — Plus Jakarta is the nearest match.
#
# All weights are static instances. drawtext goes through libfreetype, which
# ignores variation axes and renders a variable font at its default weight — so
# a variable file would silently come out Regular no matter which weight is picked.
FONTS = {
    "TikTok Sans": "TikTokSans-Bold.ttf",              # default; the caption weight
    "TikTok Sans SemiBold": "TikTokSans-SemiBold.ttf",
    "TikTok Sans Medium": "TikTokSans-Medium.ttf",
    "TikTok Sans Regular": "TikTokSans-Regular.ttf",
    "TikTok Sans Light": "TikTokSans.ttf",
    "Plus Jakarta Sans (IG look)": "PlusJakartaSans-ExtraBold.ttf",
    "Figtree (IG look)": "Figtree-ExtraBold.ttf",
    "Montserrat (IG look)": "Montserrat-Bold.ttf",
    "Poppins": "Poppins-Bold.ttf",
    "Bebas Neue": "BebasNeue-Regular.ttf",
    "Anton": "Anton-Regular.ttf",
}
DEFAULT_FONT_FILE = "TikTokSans-Bold.ttf"

# Split-screen layout: a clip from the bank on top, a black band holding the
# text, the user's own clip underneath. Fractions of the working canvas height.
SPLIT_PANEL = 0.45          # each video panel
SPLIT_BAND = 0.10           # the black text band between them
# The split output is always a standard vertical frame. Unlike the single
# layout — which draws on the user's own frame and so inherits its size — split
# composes a NEW canvas out of two unrelated clips, and must not inherit the
# b-roll's dimensions: a 200x352 source would otherwise yield a 200x352 export.
SPLIT_W, SPLIT_H = 1080, 1920


def canvas(video, opts=None):
    """The frame a render draws into: the source's own for the single layout,
    a fixed 9:16 frame for split."""
    if (opts or {}).get("layout") == "split":
        return SPLIT_W, SPLIT_H
    W, H, _, _ = probe(video)
    return W, H


def split_geom(H):
    """(panel_h, band_y0, band_y1) in working px. Both panels are the same
    height and any rounding slack lands in the band, so the two clips always
    match exactly — a 1px difference between them is very visible."""
    panel = _even(H * SPLIT_PANEL)
    return panel, panel, H - panel


DEFAULTS = {
    "mode": "block",
    "layout": "single",  # "single" = text over one clip; "split" = stacked pair
    "bank": None,        # split only: filename of the top clip, from bank/
    "font": "TikTok Sans",
    "size": None,        # None = auto (auto-fit in block mode)
    "position": None,    # None = center for block, bottom for captions
    "color": "white",
    "outline": "black",
    "border": None,      # None = auto from size
    "spacing": 1.0,      # body line leading as a fraction of size
    "header_gap": 1.6,   # gap after the top section (fraction of size)
    "footer_gap": 1.6,   # gap before the bottom section (fraction of size)
    "upper": False,
    "shadow": False,     # off = flat outline (no 3D drop shadow)
    "shadow_alpha": 0.55,
    "shadow_off": 3,
    "speed": 1.0,        # video speed multiplier
    "music": None,       # filename of a background track from music/, or None
    "dx": 0.0,           # drag offset, pixels (horizontal)
    "dy": 0.0,           # drag offset, pixels (vertical)
}

# Background music mix levels. When the clip already has sound (a voiceover,
# say) the song sits UNDER it so the words stay clear; when the clip is silent
# — or in split mode, which is silent by design — the song carries the whole
# track, so it comes up near full.
MUSIC_VOL_UNDER = 0.28      # song volume when mixed beneath existing audio
MUSIC_VOL_SOLO = 0.85       # song volume when it's the only audio


# ---------------------------------------------------------- font helpers -----
_factor_cache = {}
_pil_cache = {}


def resolve_font(font):
    if font and os.path.isabs(font) and os.path.exists(font):
        return font
    if font in FONTS:
        return os.path.join(FONTS_DIR, FONTS[font])
    p = os.path.join(FONTS_DIR, font or "")
    if os.path.exists(p):
        return p
    return os.path.join(FONTS_DIR, DEFAULT_FONT_FILE)


def line_factor(path):
    """
    Effective line-advance / font size for ffmpeg's *textfile* drawtext path
    (with line_spacing=0). Empirically this equals 2x the font's hhea line
    height, and line_spacing is also applied at 2x. So:
        advance = line_factor(path) * size + 2 * line_spacing
    """
    if path not in _factor_cache:
        t = _FTFont(path)
        h, upm = t["hhea"], t["head"].unitsPerEm
        _factor_cache[path] = 2.0 * (h.ascent - h.descent + h.lineGap) / upm
    return _factor_cache[path]


def spacing_px(leading, factor, size):
    """line_spacing value so the rendered advance equals leading * size."""
    return round((leading - factor) * size / 2.0)


def _pil(path, size):
    k = (path, size)
    if k not in _pil_cache:
        _pil_cache[k] = ImageFont.truetype(path, size)
    return _pil_cache[k]


def text_w(path, size, s):
    b = _pil(path, size).getbbox(s or " ")
    return b[2] - b[0]


def wrap_px(text, path, size, max_w):
    """Word-wrap by measured pixel width. Blank text stays blank."""
    if not text.strip():
        return ""
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if cur and text_w(path, size, t) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return "\n".join(lines)


# ---------------------------------------------------------------- ffprobe ----
# The working canvas. Layout, drag offsets, bboxes and the browser editor all
# use this one coordinate space, and every render normalises the source into it
# first — so a 4K source costs the same to edit and export as a 1080p one.
# Vertical platforms top out at 1080x1920 anyway.
MAX_DIM = 1920


def _even(n):
    return max(2, int(round(n)) & ~1)


def work_dims(w, h):
    """Cap the long side at MAX_DIM, preserving aspect. Dims stay even (x264)."""
    m = max(w, h)
    if m <= MAX_DIM:
        return _even(w), _even(h)
    s = MAX_DIM / float(m)
    return _even(w * s), _even(h * s)


def _raw_probe(video):
    """(width, height, rotation_deg, duration, has_audio) exactly as stored."""
    def q(args):
        return subprocess.run(["ffprobe", "-v", "error"] + args + [video],
                              capture_output=True, text=True).stdout.strip()
    wh = q(["-select_streams", "v:0", "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x"]).split("x")
    w, h = int(wh[0]), int(wh[1])
    rot = q(["-select_streams", "v:0", "-show_entries",
             "stream_side_data=rotation:stream_tags=rotate",
             "-of", "default=noprint_wrappers=1:nokey=1"])
    dur = q(["-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1"])
    has_audio = bool(q(["-select_streams", "a:0", "-show_entries",
                        "stream=index", "-of", "csv=p=0"]))
    deg = 0
    for tok in rot.split():          # side_data rotation, else the legacy tag
        try:
            deg = int(round(float(tok)))
            break
        except ValueError:
            pass
    return w, h, deg, float(dur) if dur else 0.0, has_audio


def _oriented(video):
    """Stored size with the display matrix applied — i.e. what the filter graph
    actually receives. A phone shoots portrait but stores the frame landscape
    with a 90-degree rotation matrix, and ffmpeg auto-rotates on decode: a
    3840x2160 iPhone file reaches drawtext as 2160x3840. Trusting the stored
    size here is what puts text in the wrong place on those videos."""
    w, h, deg, dur, has_audio = _raw_probe(video)
    if deg % 180:                    # 90 / -90 / 270 -> ffmpeg swaps the axes
        w, h = h, w
    return w, h, dur, has_audio


def probe(video):
    """(W, H, duration, has_audio) on the working canvas."""
    w, h, dur, has_audio = _oriented(video)
    W, H = work_dims(w, h)
    return W, H, dur, has_audio


def source_chain(video):
    """Filters that turn the decoded source into the working canvas: HDR->SDR
    tone-map plus the downscale. Everything after this draws in working px."""
    w, h, _, _ = _oriented(video)
    W, H = work_dims(w, h)
    if is_hdr(video):
        return tonemap_chain(W, H)
    return f"scale={W}:{H}:flags=lanczos" if (W, H) != (w, h) else ""


_hdr_cache = {}


def is_hdr(video):
    """True for HDR (PQ / HLG / bt2020) sources that need tone-mapping to SDR."""
    if video not in _hdr_cache:
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer,color_primaries,color_space",
             "-of", "default=noprint_wrappers=1:nokey=1", video],
            capture_output=True, text=True).stdout.lower()
        _hdr_cache[video] = any(t in info for t in
                                ("smpte2084", "arib-std-b67", "bt2020"))
    return _hdr_cache[video]


# HDR -> SDR (bt709). iPhone footage is HLG/PQ HDR; converting it to plain SDR
# without proper tone-mapping is what makes exports look dark and washed-out.
#
# libplacebo (GPU) gives by far the best result — brighter, richer, more shadow
# detail — so we prefer it and fall back to a CPU zscale chain only when a GPU
# isn't available (e.g. a headless container). A small eq lift compensates for
# the slight flatness any HDR->SDR conversion leaves behind.
TONEMAP_GPU = ("libplacebo={geom}tonemapping=bt.2390:colorspace=bt709:"
               "color_primaries=bt709:color_trc=bt709:range=tv:format=yuv420p,"
               "eq=saturation=1.12:brightness=0.02")
TONEMAP_CPU = ("zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,"
               "tonemap=tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv,"
               "format=yuv420p,eq=saturation=1.08:brightness=0.02")
# Output tags so players read the tone-mapped result as SDR.
SDR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv"]

_placebo_ok = None


def tonemap_chain(w=None, h=None):
    """Return the HDR->SDR filter string, preferring libplacebo when the build
    can actually initialise it (needs a GPU/Vulkan device); else the CPU chain.
    Result is probed once and cached. When w/h are given the downscale to the
    working canvas is folded in: libplacebo resamples in linear light in the
    same shader pass, and the CPU chain scales up front so the expensive
    zscale/tonemap stage runs on 1080p instead of 4K."""
    global _placebo_ok
    if _placebo_ok is None:
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "lavfi", "-i",
                 "color=c=gray:s=64x64", "-vf", "libplacebo=format=yuv420p",
                 "-frames:v", "1", "-f", "null", "-"],
                capture_output=True, text=True, timeout=30)
            _placebo_ok = (r.returncode == 0)
        except Exception:
            _placebo_ok = False
    if _placebo_ok:
        return TONEMAP_GPU.format(geom=(f"w={w}:h={h}:" if w else ""))
    return (f"scale={w}:{h}:flags=lanczos," if w else "") + TONEMAP_CPU


# --------------------------------------------------------- text parsing ------
TS = re.compile(r"^\s*\[\s*(\d+):(\d{1,2})(?:\.(\d+))?\s*-\s*(\d+):(\d{1,2})(?:\.(\d+))?\s*\]\s*(.*)$")


def _sec(m, s, frac):
    v = int(m) * 60 + int(s)
    return v + float("0." + frac) if frac else v


def clean_lines(text):
    lines = [l.rstrip() for l in text.replace("\r\n", "\n").split("\n")]
    lines = [l for l in lines if not l.strip().startswith("#")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def parse_captions(lines, duration):
    text_lines = [l.strip() for l in lines if l.strip()]
    timed, untimed = [], []
    for line in text_lines:
        m = TS.match(line)
        if m:
            timed.append({"text": m.group(7).strip(),
                          "start": _sec(m.group(1), m.group(2), m.group(3)),
                          "end": _sec(m.group(4), m.group(5), m.group(6))})
        else:
            untimed.append(line)
    caps = list(timed)
    if untimed:
        if len(untimed) == 1 and not timed:
            caps.append({"text": untimed[0], "start": 0.0, "end": duration or 999999})
        else:
            span = (duration or 999999) / len(untimed)
            for i, t in enumerate(untimed):
                caps.append({"text": t, "start": i * span, "end": (i + 1) * span})
    caps.sort(key=lambda c: c["start"])
    return caps


# --------------------------------------------------------- drawtext build ----
def _color(c):
    return ("0x" + c[1:]) if c.startswith("#") else c


def _text_budget(o, H):
    """How much vertical room the text may use. In split mode it has to live
    inside the black band; otherwise it's the whole frame."""
    if o.get("layout") == "split":
        _, y0, y1 = split_geom(H)
        return (y1 - y0) * 0.90, y0, y1
    return H * 0.92, 0.0, float(H)


def _text_offset(o, H, extent, pos):
    """Pixel y of the top of the text block, given its measured height."""
    if o.get("layout") == "split":
        _, y0, y1 = split_geom(H)
        return y0 + ((y1 - y0) - extent) / 2.0   # always centred in the band
    if pos == "top":
        return H * 0.10
    if pos == "bottom":
        return H * 0.88 - extent
    return (H - extent) / 2.0


def _line_drawtext(text, font, size, o, y_val, tf_path, enable=None,
                   dx=0, dy=0, border=None):
    """One single-line drawtext, horizontally centered, at an exact y pixel.
    dx/dy shift this line (per-section drag offset); border overrides o['border']."""
    with open(tf_path, "w", encoding="utf-8") as f:
        f.write(text)
    fe = font.replace("\\", "/").replace(":", "\\:")
    te = tf_path.replace("\\", "/").replace(":", "\\:")
    dx = int(round(dx or 0))
    dy = int(round(dy or 0))
    bw = o["border"] if border is None else border
    parts = [
        f"fontfile='{fe}'", f"textfile='{te}'",
        f"fontcolor={_color(o['color'])}", f"fontsize={size}",
        f"borderw={bw}", f"bordercolor={_color(o['outline'])}",
        (f"x=(w-text_w)/2+({dx})" if dx else "x=(w-text_w)/2"),
        f"y={round(y_val) + dy}", "expansion=none",
    ]
    if o["shadow"]:
        parts += [f"shadowcolor=black@{o['shadow_alpha']}",
                  f"shadowx={o['shadow_off']}", f"shadowy={o['shadow_off']}"]
    if enable:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


def _flatten(lines, font, size, max_w):
    """Wrap each source line; return list of visual lines (blank strings kept)."""
    out = []
    for l in lines:
        w = wrap_px(l, font, size, max_w)
        out.extend(w.split("\n") if w else [""])
    return out


def _sections(lines):
    """Split source lines into sections (groups separated by blank lines)."""
    secs, cur = [], []
    for l in lines:
        if l.strip() == "":
            if cur:
                secs.append(cur); cur = []
        else:
            cur.append(l)
    if cur:
        secs.append(cur)
    return secs


def _start_y(pos, H, n, advance, size):
    extent = (n - 1) * advance + size
    if pos == "top":
        return H * 0.10
    if pos == "bottom":
        return H * 0.88 - extent
    return (H - extent) / 2.0  # center


def _block_layout(text, W, H, o):
    """Shared block-mode layout (auto-fit + section gaps). Returns
    (placed, size, border, offset, font) where `placed` is a list of
    (visual_line, y) with y relative to the top of the whole text block
    (add `offset` to get the actual pixel y)."""
    font = resolve_font(o["font"])
    leading = float(o["spacing"])
    upper = o["upper"]
    pos = o["position"] or "center"
    secs = _sections([(l.upper() if upper else l) for l in clean_lines(text)])
    nsec = len(secs)
    hgap = float(o["header_gap"])
    fgap = float(o["footer_gap"])
    autofit = o["size"] is None
    base = int(o["size"] or max(22, round(W * 0.058)))
    budget, _, _ = _text_budget(o, H)

    def layout(size):
        border = (o["border"] if o["border"] is not None else max(1, round(size * 0.06)))
        max_w = W * 0.90 - 2 * border
        advance = leading * size
        placed, y = [], 0.0
        for si, sec in enumerate(secs):
            vlines = _flatten(sec, font, size, max_w)
            for j, vl in enumerate(vlines):
                placed.append((vl, y))
                if j < len(vlines) - 1:
                    y += advance
            if si < nsec - 1:  # gap to next section
                g = hgap if (nsec >= 3 and si == 0) else fgap
                y += g * size
        extent = (placed[-1][1] + size) if placed else size
        return placed, extent, border

    size = base
    placed, extent, border = layout(size)
    while autofit and extent > budget and size > 16:
        size -= 1
        placed, extent, border = layout(size)

    offset = _text_offset(o, H, extent, pos)
    return placed, size, border, offset, font


def block_sections(text, W, H, o):
    """Per-section base geometry for the stacked auto-fit block layout.
    Each blank-line-separated section becomes an independently placeable box.
    Returns (base_size, font, sections) where each section is
    {'src': [source lines], 'cx': centre x, 'cy': centre y} at the base size."""
    font = resolve_font(o["font"])
    leading = float(o["spacing"])
    upper = o["upper"]
    pos = o["position"] or "center"
    hgap = float(o["header_gap"])
    fgap = float(o["footer_gap"])
    secs_src = _sections([(l.upper() if upper else l) for l in clean_lines(text)])
    nsec = len(secs_src)
    autofit = o["size"] is None
    base = int(o["size"] or max(22, round(W * 0.058)))
    budget, _, _ = _text_budget(o, H)

    def layout(size):
        border = (o["border"] if o["border"] is not None else max(1, round(size * 0.06)))
        max_w = W * 0.90 - 2 * border
        advance = leading * size
        spans, y = [], 0.0
        for si, sec in enumerate(secs_src):
            vlines = _flatten(sec, font, size, max_w)
            top = y
            y += (len(vlines) - 1) * advance     # step to the last line's top
            spans.append((top, y + size))        # (top, bottom) of this section
            if si < nsec - 1:
                g = hgap if (nsec >= 3 and si == 0) else fgap
                y += g * size
        extent = spans[-1][1] if spans else size
        return spans, extent

    size = base
    spans, extent = layout(size)
    while autofit and extent > budget and size > 16:
        size -= 1
        spans, extent = layout(size)

    offset = _text_offset(o, H, extent, pos)

    sections = []
    for (top, bottom), src in zip(spans, secs_src):
        sections.append({"src": src, "cx": W / 2.0,
                         "cy": offset + (top + bottom) / 2.0})
    return size, font, sections


def _section_geom(sec_src, cx, cy, size, dx, dy, W, H, o, font):
    """Measure one section rendered at `size`, centred at (cx+dx, cy+dy).
    Returns (visual_lines, advance, top_base, border, bbox). No file writes."""
    leading = float(o["spacing"])
    border = (o["border"] if o["border"] is not None else max(1, round(size * 0.06)))
    max_w = W * 0.90 - 2 * border
    upper = o["upper"]
    vlines = _flatten([(l.upper() if upper else l) for l in sec_src], font, size, max_w)
    advance = leading * size
    block_h = (len(vlines) - 1) * advance + size
    top = cy - block_h / 2.0                       # base top (offset added by caller)
    widths = [text_w(font, size, vl) for vl in vlines if vl.strip()]
    w = max(widths) if widths else 0
    x0 = (W - w) / 2.0
    return vlines, advance, top, border, (x0 + dx, top + dy, x0 + w + dx, top + block_h + dy)


def _section_chain(sec_src, cx, cy, size, dx, dy, W, H, o, font, tf_fn):
    """drawtext chain (list) for one section + its bbox."""
    vlines, advance, top, border, bb = _section_geom(
        sec_src, cx, cy, size, dx, dy, W, H, o, font)
    chain = []
    for i, vl in enumerate(vlines):
        if vl.strip():
            chain.append(_line_drawtext(vl, font, size, o, top + i * advance, tf_fn(),
                                        dx=dx, dy=dy, border=border))
    return chain, bb


def _sec_size(o, base, i):
    """Per-section size override (opts['sizes'][i]) or the auto-fit base."""
    sizes = o.get("sizes") or []
    if i < len(sizes) and sizes[i]:
        try:
            return int(sizes[i])
        except (ValueError, TypeError):
            pass
    return base


def _sec_offset(o, i):
    offsets = o.get("offsets") or []
    if i < len(offsets) and offsets[i]:
        try:
            return float(offsets[i][0] or 0), float(offsets[i][1] or 0)
        except (ValueError, TypeError, IndexError):
            pass
    return 0.0, 0.0


def build_filter(text, W, H, opts, tmp, duration, only_section=None, zero_offsets=False):
    """Return (drawtext_chain, used_size). No speed filter here.

    Block mode places each blank-line section independently (its own size +
    dx/dy). only_section renders just that section; zero_offsets ignores the
    per-section drag offsets (used for the transparent editor layers, whose
    offset is applied in CSS instead)."""
    o = dict(DEFAULTS)
    o.update({k: v for k, v in opts.items() if v is not None})
    font = resolve_font(o["font"])
    leading = float(o["spacing"])
    upper = o["upper"]
    pos = o["position"] or ("center" if o["mode"] == "block" else "bottom")
    counter = [0]

    def tf():
        counter[0] += 1
        return os.path.join(tmp, f"l{counter[0]}.txt")

    def case(s):
        return s.upper() if upper else s

    # ---- captions mode: each caption is its own stack, anchored at pos ----
    if o["mode"] == "captions":
        caps = parse_captions(clean_lines(text), duration)
        size = int(o["size"] or max(22, round(W * 0.058)))
        border = (o["border"] if o["border"] is not None else max(1, round(size * 0.06)))
        o["border"] = border
        advance = leading * size
        max_w = W * 0.90 - 2 * border
        single = len(caps) == 1 and caps[0]["start"] <= 0.001
        chain = []
        for c in caps:
            vlines = _flatten([case(c["text"])], font, size, max_w)
            n = len(vlines)
            start = _start_y(pos, H, n, advance, size)
            en = None if single else f"between(t,{c['start']:.3f},{c['end']:.3f})"
            for i, vl in enumerate(vlines):
                if vl.strip():
                    chain.append(_line_drawtext(vl, font, size, o,
                                                start + i * advance, tf(), en))
        return ",".join(chain), size

    # ---- block mode: each section is an independent, movable/resizable box ----
    size, font, sections = block_sections(text, W, H, o)
    idxs = range(len(sections)) if only_section is None else [only_section]
    chain = []
    for i in idxs:
        if i < 0 or i >= len(sections):
            continue
        sec = sections[i]
        sz = _sec_size(o, size, i)
        dx, dy = (0.0, 0.0) if zero_offsets else _sec_offset(o, i)
        c, _ = _section_chain(sec["src"], sec["cx"], sec["cy"], sz, dx, dy, W, H, o, font, tf)
        chain.extend(c)
    return ",".join(chain), size


def section_bboxes(input_path, text, opts):
    """Per-section base bounding boxes (dx=dy=0, at each section's current
    size) for drawing the editor's selection boxes + resize handles."""
    _, _, dur, _ = probe(input_path)
    W, H = canvas(input_path, opts)
    o = dict(DEFAULTS)
    o.update({k: v for k, v in opts.items() if v is not None})
    if o["mode"] != "block":
        x0, y0, x1, y1, size = text_bbox(text, W, H, opts, dur, 0)
        return [{"x0": x0, "y0": y0, "x1": x1, "y1": y1, "size": size}]
    size, font, sections = block_sections(text, W, H, o)
    out = []
    for i, sec in enumerate(sections):
        sz = _sec_size(o, size, i)
        _, _, _, _, bb = _section_geom(sec["src"], sec["cx"], sec["cy"], sz,
                                       0, 0, W, H, o, font)
        out.append({"x0": bb[0], "y0": bb[1], "x1": bb[2], "y1": bb[3], "size": sz})
    return out


def text_bbox(text, W, H, opts, duration, at=0.0):
    """Bounding box (x0, y0, x1, y1, size) of the rendered text in pixel
    space, including the dx/dy drag offset. Mirrors build_filter's layout
    math so the UI can draw resize handles aligned with the real render."""
    o = dict(DEFAULTS)
    o.update({k: v for k, v in opts.items() if v is not None})
    upper = o["upper"]
    dx = float(o.get("dx", 0) or 0)
    dy = float(o.get("dy", 0) or 0)

    def case(s):
        return s.upper() if upper else s

    if o["mode"] == "captions":
        font = resolve_font(o["font"])
        leading = float(o["spacing"])
        pos = o["position"] or "bottom"
        caps = parse_captions(clean_lines(text), duration)
        if not caps:
            return (0.0, 0.0, 0.0, 0.0, 0)
        active = next((c for c in caps if c["start"] <= at < c["end"]), caps[0])
        size = int(o["size"] or max(22, round(W * 0.058)))
        border = (o["border"] if o["border"] is not None else max(1, round(size * 0.06)))
        advance = leading * size
        max_w = W * 0.90 - 2 * border
        vlines = _flatten([case(active["text"])], font, size, max_w)
        n = len(vlines)
        start = _start_y(pos, H, n, advance, size)
        widths = [text_w(font, size, vl) for vl in vlines if vl.strip()]
        w = max(widths) if widths else 0
        x0 = (W - w) / 2.0
        return (x0 + dx, start + dy, x0 + w + dx, start + (n - 1) * advance + size + dy, size)

    placed, size, border, offset, font = _block_layout(text, W, H, o)
    widths = [text_w(font, size, vl) for vl, _ in placed if vl.strip()]
    w = max(widths) if widths else 0
    x0 = (W - w) / 2.0
    extent = (placed[-1][1] + size) if placed else size
    return (x0 + dx, offset + dy, x0 + w + dx, offset + extent + dy, size)


def bbox(input_path, at, text, opts):
    """Convenience wrapper: probe the video, then compute text_bbox."""
    _, _, dur, _ = probe(input_path)
    W, H = canvas(input_path, opts)
    return text_bbox(text, W, H, opts, dur, at)


# ------------------------------------------------------------- atempo --------
def _atempo(speed):
    """Chain atempo filters to cover speeds outside 0.5-2.0."""
    s = speed
    parts = []
    while s > 2.0:
        parts.append("atempo=2.0"); s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5"); s /= 0.5
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


def _audio_graph(music_idx, has_source_audio, speed):
    """filter_complex audio branch + its output label for a music mix.

    When the clip already has sound, the song is mixed UNDER it (so a voiceover
    stays clear) and the mix ends with the finite source audio — otherwise the
    infinitely-looped song would stretch the file past the video. When the clip
    is silent the song carries the whole track and the caller adds -shortest to
    trim the loop back to the video length. Returns (graph, label, need_shortest)."""
    if has_source_audio:
        atempo = _atempo(speed) if abs(speed - 1.0) > 1e-3 else None
        src = f"[0:a]{atempo + ',' if atempo else ''}volume=1.0[a0]"
        mus = f"[{music_idx}:a]volume={MUSIC_VOL_UNDER}[a1]"
        # normalize=0 keeps the voice at full level (amix otherwise divides every
        # input by the input count, halving both); duration=first ends on the clip.
        mix = ("[a0][a1]amix=inputs=2:duration=first:"
               "dropout_transition=0:normalize=0[aout]")
        return f"{src};{mus};{mix}", "[aout]", False
    return f"[{music_idx}:a]volume={MUSIC_VOL_SOLO}[aout]", "[aout]", True


# ------------------------------------------------------------- render --------
def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-1600:])
    return r


BANK_DIR = os.path.join(HERE, "bank")


def bank_clips():
    """The overlay clips available for the top panel, sorted by name."""
    if not os.path.isdir(BANK_DIR):
        return []
    return sorted(f for f in os.listdir(BANK_DIR)
                  if os.path.splitext(f)[1].lower() in
                  (".mp4", ".mov", ".m4v", ".webm", ".mkv"))


def bank_path(name):
    """Resolve a bank clip by name. Rejects anything outside bank/ — this comes
    straight off a request, so '../../etc/passwd' must not resolve."""
    if not name or name not in bank_clips():
        return None
    return os.path.join(BANK_DIR, name)


MUSIC_DIR = os.path.join(HERE, "music")
MUSIC_EXT = (".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac")


def music_clips():
    """The background-music tracks available to pick, sorted by name."""
    if not os.path.isdir(MUSIC_DIR):
        return []
    return sorted(f for f in os.listdir(MUSIC_DIR)
                  if os.path.splitext(f)[1].lower() in MUSIC_EXT)


def music_path(name):
    """Resolve a music track by name. Rejects anything outside music/ — this
    comes straight off a request, so '../../etc/passwd' must not resolve."""
    if not name or name not in music_clips():
        return None
    return os.path.join(MUSIC_DIR, name)


def safe_stem(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(name)[0])


def bank_thumb(src, out_jpg, at=1.0):
    """A small poster frame for the bank picker."""
    W, H, dur, _ = probe(src)
    at = min(at, max(0.0, dur - 0.05))
    _run(["ffmpeg", "-y", "-ss", f"{at:.3f}", "-i", src,
          "-vf", "scale=160:-2", "-frames:v", "1", "-update", "1", out_jpg])


def _split_graph(input_path, W, H, speed=1.0):
    """Compose the split canvas from two inputs and return (graph, out_label).

    Input 0 is the user's clip, input 1 the bank clip (fed with -stream_loop so
    a short clip tiles under a long one). Each is scaled to cover its panel and
    centre-cropped, never letterboxed. Padding the bottom panel up to the full
    canvas is what creates the black band — no separate colour source needed.

    shortest=1 is load-bearing: overlay defaults to running until its LONGEST
    input ends, and -stream_loop -1 makes the bank clip infinite, so without it
    the render never terminates."""
    panel, _, _ = split_geom(H)
    fill = (f"scale={W}:{panel}:force_original_aspect_ratio=increase,"
            f"crop={W}:{panel}")
    src = source_chain(input_path)
    sp = f",setpts=PTS/{speed}" if abs(speed - 1.0) > 1e-3 else ""
    return (
        f"[0:v]{src + ',' if src else ''}{fill},"
        f"pad={W}:{H}:0:{H - panel}:black[base];"
        f"[1:v]{fill}[top];"
        f"[base][top]overlay=0:0:shortest=1{sp}[sv]"
    ), "[sv]"


def _split_cmd(input_path, bank, W, H, chain, speed=1.0, seek=None, bank_dur=0.0,
               music=None, clip_pre=None):
    """ffmpeg argv for a split render. `seek` renders a single frame at that
    time; the bank clip is seeked modulo its own length so it matches the
    looping it would get in the full render. `music` (a full render only) adds a
    looped background track as the sole audio. `clip_pre` are input options
    (-ss/-t) that trim the user's clip to the kept section before compositing."""
    graph, last = _split_graph(input_path, W, H, speed)
    if chain:
        graph += f";{last}{chain}[out]"
        last = "[out]"
    cmd = ["ffmpeg", "-y"]
    if seek is not None:
        cmd += ["-ss", f"{seek:.3f}", "-i", input_path,
                "-ss", f"{(seek % bank_dur) if bank_dur else 0:.3f}", "-i", bank]
    else:
        cmd += (clip_pre or []) + ["-i", input_path, "-stream_loop", "-1", "-i", bank]
    # The split export is silent by design — the bank clip's audio is filler and
    # the two tracks fighting each other is never what you want. A background
    # song is the one sound we do add: it's input 2, and -shortest trims the
    # infinite loop back to the (finite) composed video.
    if music and seek is None:
        graph += f";[2:a]volume={MUSIC_VOL_SOLO}[aout]"
        cmd += ["-stream_loop", "-1", "-i", music,
                "-filter_complex", graph, "-map", last, "-map", "[aout]",
                "-c:a", "aac", "-b:a", "192k", "-shortest"]
    else:
        cmd += ["-filter_complex", graph, "-map", last, "-an"]
    return cmd


def _split_bank(opts):
    """(bank_path, bank_duration) for a split render, or (None, 0) if not split.
    Raises if split is asked for without a usable bank clip, rather than
    silently falling back to a normal render and confusing the user."""
    if (opts.get("layout") or "single") != "split":
        return None, 0.0
    bank = bank_path(opts.get("bank"))
    if not bank:
        have = bank_clips()
        raise RuntimeError(
            f"Split screen needs a clip for the top panel. "
            + (f"Pick one of: {', '.join(have)}." if have
               else "The bank/ folder is empty — add some clips to it."))
    return bank, _oriented(bank)[2]


def _trim_window(opts, dur):
    """Input-seek args (-ss/-t) that keep only the chosen [start, end] section
    of the source, applied BEFORE any speed change. Empty when the whole clip is
    kept. Speed is handled downstream, so a 6s section at 2x still exports 3s."""
    def f(key, default):
        v = opts.get(key)
        try:
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default
    start = max(0.0, f("trim_start", 0.0))
    end = f("trim_end", dur or 0.0)
    if dur:
        start = min(start, max(0.0, dur - 0.1))
        end = min(end, dur)
    seg = end - start
    pre = []
    if start > 0.01:
        pre += ["-ss", f"{start:.3f}"]
    # only cap the length when a real sub-section (not the whole clip) was picked
    if seg > 0.05 and (start > 0.01 or (dur and seg < dur - 0.05)):
        pre += ["-t", f"{seg:.3f}"]
    return pre


def render(input_path, output_path, text, opts, draft=False):
    """Burn text and write a video. draft=True renders fast/small for preview."""
    _, _, dur, has_audio = probe(input_path)
    W, H = canvas(input_path, opts)
    speed = float(opts.get("speed") or 1.0)
    hdr = is_hdr(input_path)
    bank, _ = _split_bank(opts)
    music = music_path(opts.get("music"))
    clip_pre = _trim_window(opts, dur)
    with tempfile.TemporaryDirectory() as tmp:
        chain, size = build_filter(text, W, H, opts, tmp, dur)
        if draft:
            enc = ["-preset", "veryfast", "-crf", "23"]
        else:
            # Final export. crf 18 at 1080p is already well above what IG/TikTok
            # keep — they recompress to ~8-10 Mbps on upload — so going
            # near-lossless just spent encode time on bits nobody ever sees.
            enc = ["-preset", "slow", "-crf", "18"]
        venc = ["-c:v", "libx264"] + enc + ["-pix_fmt", "yuv420p",
                "-movflags", "+faststart"] + (SDR_TAGS if hdr else [])
        shrink = ("scale=1280:1280:force_original_aspect_ratio=decrease"
                  ":force_divisible_by=2")

        if bank:
            if draft and max(W, H) > 1280:
                chain = (chain + "," if chain else "") + shrink
            cmd = _split_cmd(input_path, bank, W, H, chain, speed, music=music,
                             clip_pre=clip_pre)
            cmd += venc
        elif music:
            # A background song means a second input, so the video filters move
            # into filter_complex (a simple -vf can't coexist with mapping a
            # second stream). The song is input 1.
            vparts = [source_chain(input_path),
                      (f"setpts=PTS/{speed}" if abs(speed - 1.0) > 1e-3 else ""),
                      chain]
            if draft and max(W, H) > 1280:
                vparts.append(shrink)
            vbody = ",".join(p for p in vparts if p) or "null"
            aud, alabel, need_short = _audio_graph(1, has_audio, speed)
            graph = f"[0:v]{vbody}[vout];{aud}"
            cmd = (["ffmpeg", "-y"] + clip_pre + ["-i", input_path,
                    "-stream_loop", "-1", "-i", music,
                    "-filter_complex", graph, "-map", "[vout]", "-map", alabel]
                   + venc + ["-c:a", "aac", "-b:a", "192k"]
                   + (["-shortest"] if need_short else []))
        else:
            vf = ",".join(p for p in [
                source_chain(input_path),
                (f"setpts=PTS/{speed}" if abs(speed - 1.0) > 1e-3 else ""),
                chain,
            ] if p)
            if draft and max(W, H) > 1280:
                vf += "," + shrink      # only downscale very large sources
            cmd = ["ffmpeg", "-y"] + clip_pre + ["-i", input_path] + (["-vf", vf] if vf else []) + venc
            if has_audio and abs(speed - 1.0) > 1e-3:
                cmd += ["-filter:a", _atempo(speed), "-c:a", "aac", "-b:a", "192k"]
            elif has_audio:
                cmd += ["-c:a", "copy"]
            else:
                cmd += ["-an"]
        cmd.append(output_path)
        _run(cmd)
    return size


def still(input_path, out_png, at, text, opts):
    """Render a single preview frame (speed doesn't change the look)."""
    _, _, dur, _ = probe(input_path)
    W, H = canvas(input_path, opts)
    at = max(0.0, min(at, max(0.0, dur - 0.05)))
    bank, bdur = _split_bank(opts)
    with tempfile.TemporaryDirectory() as tmp:
        chain, size = build_filter(text, W, H, opts, tmp, dur)
        if bank:
            cmd = _split_cmd(input_path, bank, W, H, chain, seek=at, bank_dur=bdur)
        else:
            vf = ",".join(p for p in [source_chain(input_path), chain] if p)
            cmd = ["ffmpeg", "-y", "-ss", f"{at:.3f}", "-i", input_path, "-vf", vf]
        _run(cmd + ["-frames:v", "1", "-update", "1", out_png])
    return size


def still_clean(input_path, out_png, at, opts=None):
    """One video frame with NO text (tone-mapped to SDR). The live editor uses
    this as the background and overlays the text as a separate transparent PNG,
    so dragging/resizing the text is pure client-side CSS (no ffmpeg round-trip).
    In split mode this is the whole composed canvas — both panels and the black
    band — so what the editor shows is what gets rendered."""
    _, _, dur, _ = probe(input_path)
    W, H = canvas(input_path, opts)
    at = max(0.0, min(at, max(0.0, dur - 0.05)))
    bank, bdur = _split_bank(opts or {})
    if bank:
        _run(_split_cmd(input_path, bank, W, H, "", seek=at, bank_dur=bdur)
             + ["-frames:v", "1", "-update", "1", out_png])
        return W, H
    vf = source_chain(input_path)
    cmd = ["ffmpeg", "-y", "-ss", f"{at:.3f}", "-i", input_path]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", "1", "-update", "1", out_png]
    _run(cmd)
    return W, H


def still_textlayer(input_path, out_png, at, text, opts, section=None):
    """Render ONLY the text (fill + outline + shadow) on a fully transparent
    canvas at the video's resolution. Offsets are forced to 0 (the client
    applies the drag offset via CSS). `section` renders just one block section
    so each section is its own independently movable overlay."""
    _, _, dur, _ = probe(input_path)
    W, H = canvas(input_path, opts)
    with tempfile.TemporaryDirectory() as tmp:
        chain, size = build_filter(text, W, H, opts, tmp, dur,
                                   only_section=section, zero_offsets=True)
        # format=rgba must be in the INPUT graph so the transparent color source
        # keeps its alpha; putting it in -vf flattens the source to opaque black.
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
               f"color=c=#00000000:s={W}x{H},format=rgba"]
        if chain:
            cmd += ["-vf", chain]
        cmd += ["-frames:v", "1", "-update", "1", "-pix_fmt", "rgba", out_png]
        _run(cmd)
    return size


# ------------------------------------------------------------------ CLI ------
def main():
    ap = argparse.ArgumentParser(description="Burn TikTok-style text onto a video.")
    ap.add_argument("input")
    ap.add_argument("-t", "--text", default=os.path.join(HERE, "captions.md"))
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--mode", choices=["block", "captions"], default="block")
    ap.add_argument("--font", default="TikTok Sans",
                    help="name (%s) or a .ttf path" % ", ".join(FONTS))
    ap.add_argument("--size", type=int, default=None)
    ap.add_argument("--position", choices=["bottom", "center", "top"], default=None)
    ap.add_argument("--color", default="white")
    ap.add_argument("--outline", default="black")
    ap.add_argument("--border", type=int, default=None)
    ap.add_argument("--spacing", type=float, default=1.0,
                    help="line leading as fraction of size (default 1.0)")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--upper", action="store_true")
    ap.add_argument("--no-shadow", dest="shadow", action="store_false")
    ap.set_defaults(shadow=True)
    a = ap.parse_args()

    if not os.path.exists(a.input):
        sys.exit(f"Input not found: {a.input}")
    with open(a.text, encoding="utf-8") as f:
        text = f.read()

    out = a.output
    if not out:
        base = os.path.splitext(os.path.basename(a.input))[0]
        os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
        out = os.path.join(HERE, "output", f"{base}_captioned.mp4")

    opts = {k: getattr(a, k) for k in
            ("mode", "font", "size", "position", "color", "outline",
             "border", "spacing", "speed", "upper", "shadow")}
    print(f"rendering ({a.mode}, font={a.font}, speed={a.speed})...")
    size = render(a.input, out, text, opts)
    print(f"done (size {size}px) -> {out}")


if __name__ == "__main__":
    main()
