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

FONTS = {
    "TikTok Sans": "TikTokSans-Bold.ttf",
    "Poppins": "Poppins-Bold.ttf",
    "Bebas Neue": "BebasNeue-Regular.ttf",
    "Anton": "Anton-Regular.ttf",
}
DEFAULT_FONT_FILE = "TikTokSans-Bold.ttf"

DEFAULTS = {
    "mode": "block",
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
}


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
def probe(video):
    def q(args):
        return subprocess.run(["ffprobe", "-v", "error"] + args + [video],
                              capture_output=True, text=True).stdout.strip()
    wh = q(["-select_streams", "v:0", "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x"]).split("x")
    w, h = int(wh[0]), int(wh[1])
    dur = q(["-show_entries", "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1"])
    has_audio = bool(q(["-select_streams", "a:0", "-show_entries",
                        "stream=index", "-of", "csv=p=0"]))
    return w, h, float(dur) if dur else 0.0, has_audio


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


# HDR -> SDR (bt709) tone-map chain. Prevents the dark/washed-out look you get
# when HDR (iPhone HLG/PQ) video is re-encoded to plain SDR without conversion.
TONEMAP = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
           "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,"
           "format=yuv420p")
# Output tags so players read the tone-mapped result as SDR.
SDR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709",
            "-color_trc", "bt709", "-color_range", "tv"]


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


def _line_drawtext(text, font, size, o, y_val, tf_path, enable=None):
    """One single-line drawtext, horizontally centered, at an exact y pixel."""
    with open(tf_path, "w", encoding="utf-8") as f:
        f.write(text)
    fe = font.replace("\\", "/").replace(":", "\\:")
    te = tf_path.replace("\\", "/").replace(":", "\\:")
    parts = [
        f"fontfile='{fe}'", f"textfile='{te}'",
        f"fontcolor={_color(o['color'])}", f"fontsize={size}",
        f"borderw={o['border']}", f"bordercolor={_color(o['outline'])}",
        "x=(w-text_w)/2", f"y={round(y_val)}", "expansion=none",
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


def build_filter(text, W, H, opts, tmp, duration):
    """Return (drawtext_chain, used_size). No speed filter here.

    Each visual line is its own drawtext placed at an exact y, so the line
    advance is exactly `leading * size` for any font."""
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

    # ---- block mode: sections stacked & centered, per-section gaps, auto-fit ----
    secs = _sections([case(l) for l in clean_lines(text)])
    nsec = len(secs)
    hgap = float(o["header_gap"])
    fgap = float(o["footer_gap"])
    base = int(o["size"] or max(22, round(W * 0.058)))
    autofit = opts.get("size") in (None, 0, "")
    budget = H * 0.92

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
    o["border"] = border

    if pos == "top":
        offset = H * 0.10
    elif pos == "bottom":
        offset = H * 0.88 - extent
    else:
        offset = (H - extent) / 2.0

    chain = []
    for vl, ry in placed:
        if vl.strip():
            chain.append(_line_drawtext(vl, font, size, o, offset + ry, tf()))
    return ",".join(chain), size


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


# ------------------------------------------------------------- render --------
def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[-1600:])
    return r


def render(input_path, output_path, text, opts, draft=False):
    """Burn text and write a video. draft=True renders fast/small for preview."""
    W, H, dur, has_audio = probe(input_path)
    speed = float(opts.get("speed") or 1.0)
    hdr = is_hdr(input_path)
    with tempfile.TemporaryDirectory() as tmp:
        chain, size = build_filter(text, W, H, opts, tmp, dur)
        vf = (TONEMAP + "," if hdr else "") \
            + (f"setpts=PTS/{speed}," if abs(speed - 1.0) > 1e-3 else "") + chain
        if draft:
            # only downscale very large sources; keep it sharp for watching
            if max(W, H) > 1280:
                vf += ",scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2"
            enc = ["-preset", "veryfast", "-crf", "23"]
        else:
            # final export: high quality
            enc = ["-preset", "slow", "-crf", "16"]
        cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", vf,
               "-c:v", "libx264"] + enc + ["-pix_fmt", "yuv420p",
               "-movflags", "+faststart"] + (SDR_TAGS if hdr else [])
        if has_audio and abs(speed - 1.0) > 1e-3:
            cmd += ["-filter:a", _atempo(speed), "-c:a", "aac"]
        elif has_audio:
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd.append(output_path)
        _run(cmd)
    return size


def still(input_path, out_png, at, text, opts):
    """Render a single preview frame (speed doesn't change the look)."""
    W, H, dur, _ = probe(input_path)
    at = max(0.0, min(at, max(0.0, dur - 0.05)))
    with tempfile.TemporaryDirectory() as tmp:
        chain, size = build_filter(text, W, H, opts, tmp, dur)
        vf = (TONEMAP + "," if is_hdr(input_path) else "") + chain
        _run(["ffmpeg", "-y", "-ss", f"{at:.3f}", "-i", input_path,
              "-vf", vf, "-frames:v", "1", "-update", "1", out_png])
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
