#!/usr/bin/env python3
"""
Local web editor for the b-roll text overlay tool.

Run:   py app.py
Open:  http://localhost:5000

Upload a b-roll clip once, then tweak text / font / speed / spacing / colors /
layout with a live frame preview. Export renders the full video.
"""

import io
import os
import re
import tempfile
import threading
import traceback
import urllib.parse
import uuid

import requests
from flask import Flask, request, jsonify, send_from_directory, Response

import overlay
from texts import parse_collection

HERE = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(HERE, "uploads")
OUTPUT = os.path.join(HERE, "output")
PREVIEW = os.path.join(HERE, "uploads", "_preview")
COLLECTION_FILE = os.path.join(HERE, "texts.md")
for d in (UPLOADS, OUTPUT, PREVIEW):
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
# Straight-from-the-phone 4K HDR originals (via Drive, not an Instagram
# re-download) run 1-2 GB for a few minutes of footage. The old 500 MB cap
# rejected them with a bare 413 before a frame was ever decoded.
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024

SAFE = re.compile(r"[^A-Za-z0-9._-]+")
VIDEOS = {}          # id -> absolute path
_counter = [0]
IMPORTS = {}         # job id -> {state, got, total, error, result}


@app.errorhandler(413)
def too_large(_):
    """Flask aborts oversize uploads with an HTML 413; the uploader reads JSON."""
    gb = app.config["MAX_CONTENT_LENGTH"] / (1024 ** 3)
    return jsonify(error=f"Video is too large — the limit is {gb:.0f} GB."), 413


# ----------------------------------------------------------- drive import ----
# Pushing a multi-GB 4K original from the browser means it crosses the user's
# upstream link and the host's proxy, which drops the connection long before
# Flask sees a byte (Railway answers a 245 MB POST with a 502 "upstream error").
# Fetching it server-side instead is a datacenter-to-datacenter copy: no browser
# upload, no request-body limit, no edge timeout. The download outlives any
# single request, so it runs on a thread and the page polls for progress.

DRIVE_ID = re.compile(r"/file/d/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})")
# A server-side fetcher pointed at a user-supplied URL is an SSRF hole: it runs
# inside the private network. Only Google's own Drive hosts are ever fetched.
DRIVE_HOSTS = {"drive.google.com", "docs.google.com", "drive.usercontent.google.com"}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"}


def drive_file_id(link):
    """Pull the file id out of any Drive share URL, or accept a bare id."""
    link = (link or "").strip()
    if not link:
        return None
    if "/" not in link and "?" not in link:
        return link if re.fullmatch(r"[A-Za-z0-9_-]{10,}", link) else None
    host = (urllib.parse.urlparse(link).hostname or "").lower()
    if host not in DRIVE_HOSTS:
        return None
    m = DRIVE_ID.search(link)
    return (m.group(1) or m.group(2)) if m else None


def _fetch_drive(job, fid):
    """Stream a Drive file to disk, then probe it. Runs on a worker thread."""
    try:
        s = requests.Session()
        # confirm=t skips the >100MB "can't scan for viruses" interstitial that
        # would otherwise hand us an HTML page instead of the video.
        r = s.get("https://drive.usercontent.google.com/download",
                  params={"id": fid, "export": "download", "confirm": "t"},
                  stream=True, timeout=60)
        r.raise_for_status()

        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            raise RuntimeError(
                "Drive returned a web page, not a file — the link is most likely "
                "private. Set it to 'Anyone with the link' and try again.")

        # Prefer the real filename from Content-Disposition; fall back to the id.
        cd = r.headers.get("Content-Disposition") or ""
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
        name = urllib.parse.unquote(m.group(1)) if m else f"{fid}.mp4"
        stem, ext = safe_name(name)
        if ext.lower() not in VIDEO_EXT:
            raise RuntimeError(f"'{name}' is not a video file.")

        job["total"] = int(r.headers.get("Content-Length") or 0)
        _counter[0] += 1
        vid = f"{stem}-{_counter[0]}"
        path = os.path.join(UPLOADS, vid + ext)

        with open(path, "wb") as fh:
            for chunk in r.iter_content(1 << 20):     # 1 MB
                if not chunk:
                    continue
                fh.write(chunk)
                job["got"] += len(chunk)

        w, h, dur, _ = overlay.probe(path)
        VIDEOS[vid] = path
        job["result"] = {"id": vid, "width": w, "height": h,
                         "duration": dur, "name": name}
        job["state"] = "done"
    except Exception as e:
        traceback.print_exc()
        job["error"] = str(e)[:300]
        job["state"] = "error"


@app.route("/import", methods=["POST"])
def start_import():
    d = request.get_json(force=True, silent=True) or {}
    fid = drive_file_id(d.get("url"))
    if not fid:
        return jsonify(error="Not a Google Drive link. Paste the share URL "
                             "(drive.google.com/file/d/…)."), 400
    jid = uuid.uuid4().hex[:12]
    IMPORTS[jid] = job = {"state": "downloading", "got": 0, "total": 0,
                          "error": None, "result": None}
    threading.Thread(target=_fetch_drive, args=(job, fid), daemon=True).start()
    return jsonify(job=jid)


@app.route("/import/<jid>")
def import_status(jid):
    job = IMPORTS.get(jid)
    if not job:
        return jsonify(error="Unknown import job."), 404
    if job["state"] == "error":
        return jsonify(error=job["error"]), 400
    if job["state"] == "done":
        return jsonify(done=True, **job["result"])
    return jsonify(done=False, got=job["got"], total=job["total"])


def load_collection():
    if os.path.exists(COLLECTION_FILE):
        try:
            return parse_collection(COLLECTION_FILE)
        except Exception as e:
            print("texts.md parse error:", e)
    return []


def safe_name(name):
    stem, ext = os.path.splitext(name)
    stem = SAFE.sub("_", stem).strip("_") or "clip"
    if ext.lower() not in (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"):
        ext = ".mp4"
    return stem[:50], ext.lower()


def safe_label(s):
    """A user-supplied export name reduced to a safe filename stem, or None.
    Lets each export be saved (and tagged in the posting CSV) under its own
    name, so several videos cut from one clip don't all collide on one filename."""
    s = SAFE.sub("_", (s or "").strip()).strip("_")
    return s[:60] or None


def opts_from(d):
    """Build an overlay opts dict from a request payload (all optional)."""
    def num(key, cast, default=None):
        v = d.get(key, None)
        if v in (None, ""):
            return default
        try:
            return cast(v)
        except (ValueError, TypeError):
            return default

    size = num("size", int, 0)
    return {
        "mode": d.get("mode") or "block",
        "layout": "split" if d.get("layout") == "split" else "single",
        "bank": d.get("bank") or None,
        "font": d.get("font") or "TikTok Sans",
        "position": d.get("position") or None,
        "color": d.get("color") or "white",
        "outline": d.get("outline") or "black",
        "border": num("border", int),
        "size": (size or None),
        "spacing": num("spacing", float, 1.0),
        "header_gap": num("header_gap", float, 1.6),
        "footer_gap": num("footer_gap", float, 1.6),
        "speed": num("speed", float, 1.0),
        "trim_start": num("trim_start", float, 0.0),
        "trim_end": num("trim_end", float, None),
        "music": d.get("music") or None,
        "upper": bool(d.get("upper")),
        "shadow": bool(d.get("shadow")),
        # per-section movable/resizable boxes
        "offsets": d.get("offsets") if isinstance(d.get("offsets"), list) else None,
        "sizes": d.get("sizes") if isinstance(d.get("sizes"), list) else None,
    }


# ------------------------------------------------------------------ routes ---
@app.route("/")
def index():
    # never cache the page — otherwise the browser serves a stale editor after
    # every code change and edits look like they "didn't take".
    return Response(PAGE, mimetype="text/html",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                             "Pragma": "no-cache", "Expires": "0"})


@app.route("/fonts")
def fonts():
    return jsonify(list(overlay.FONTS.keys()))


@app.route("/bank")
def bank():
    """The overlay clips available for the split-screen top panel, plus the
    canvas split renders into — the editor stage has to switch to it, since a
    split composition is a fresh 9:16 frame, not the user's own."""
    out = []
    for name in overlay.bank_clips():
        try:
            w, h, dur, _ = overlay.probe(os.path.join(overlay.BANK_DIR, name))
        except Exception:
            continue                      # unreadable clip — just skip it
        label = os.path.splitext(name)[0].replace("_", " ").replace("-", " ")
        out.append({"file": name, "title": label.title(), "duration": round(dur, 1)})
    return jsonify(clips=out,
                   canvas={"w": overlay.SPLIT_W, "h": overlay.SPLIT_H})


@app.route("/bank/<name>/thumb.jpg")
def bank_thumb(name):
    """A frame from a bank clip, so the picker shows what you're choosing."""
    src = overlay.bank_path(name)         # rejects anything outside bank/
    if not src:
        return jsonify(error="Unknown clip."), 404
    out = os.path.join(PREVIEW, f"_bank_{overlay.safe_stem(name)}.jpg")
    if not os.path.exists(out):
        try:
            overlay.bank_thumb(src, out)
        except Exception as e:
            return jsonify(error=str(e)), 500
    return send_from_directory(*os.path.split(out))


@app.route("/music")
def music_list():
    """Background-music tracks the editor can pick from (in music/)."""
    out = []
    for name in overlay.music_clips():
        label = os.path.splitext(name)[0].replace("_", " ").replace("-", " ")
        out.append({"file": name, "title": label.strip()})
    return jsonify(tracks=out)


@app.route("/music/<name>")
def music_file(name):
    """Serve a track so the picker can play a listen preview."""
    src = overlay.music_path(name)          # rejects anything outside music/
    if not src:
        return jsonify(error="Unknown track."), 404
    return send_from_directory(*os.path.split(src), conditional=True)


@app.route("/scripts")
def scripts():
    return jsonify([{"n": s["n"], "title": s["title"]} for s in load_collection()])


@app.route("/script/<int:n>")
def script(n):
    for s in load_collection():
        if s["n"] == n:
            return jsonify(s)
    return jsonify(error="not found"), 404


@app.route("/upload", methods=["POST"])
def upload():
    if "video" not in request.files or request.files["video"].filename == "":
        return jsonify(error="No video."), 400
    f = request.files["video"]
    stem, ext = safe_name(f.filename)
    _counter[0] += 1
    vid = f"{stem}-{_counter[0]}"
    path = os.path.join(UPLOADS, vid + ext)
    f.save(path)
    try:
        w, h, dur, _ = overlay.probe(path)
    except Exception as e:
        return jsonify(error=f"Could not read video: {e}"), 400
    VIDEOS[vid] = path
    return jsonify(id=vid, width=w, height=h, duration=dur, name=f.filename)


@app.route("/preview", methods=["POST"])
def preview():
    d = request.get_json(force=True, silent=True) or {}
    path = VIDEOS.get(d.get("id"))
    if not path or not os.path.exists(path):
        return jsonify(error="Upload a video first."), 400
    text = d.get("text", "")
    if not text.strip():
        return jsonify(error="No text."), 400
    at = float(d.get("at", 0) or 0)
    out = os.path.join(PREVIEW, d["id"] + ".png")
    try:
        overlay.still(path, out, at, text, opts_from(d))
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)[-800:]), 500
    with open(out, "rb") as fh:
        data = fh.read()
    return Response(data, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/frame", methods=["POST"])
def frame():
    """Clean video frame (no text) — the live editor's background layer."""
    d = request.get_json(force=True, silent=True) or {}
    path = VIDEOS.get(d.get("id"))
    if not path or not os.path.exists(path):
        return jsonify(error="Upload a video first."), 400
    at = float(d.get("at", 0) or 0)
    out = os.path.join(PREVIEW, d["id"] + "_frame.png")
    try:
        # opts matter here: in split mode the "clean frame" is the whole composed
        # canvas (both panels + the black band), not just the user's clip.
        overlay.still_clean(path, out, at, opts_from(d))
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)[-800:]), 500
    with open(out, "rb") as fh:
        data = fh.read()
    return Response(data, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/textlayer", methods=["POST"])
def textlayer():
    """Transparent PNG of one block section's text — overlaid on the clean
    frame client-side. `section` selects which section (default: all)."""
    d = request.get_json(force=True, silent=True) or {}
    path = VIDEOS.get(d.get("id"))
    if not path or not os.path.exists(path):
        return jsonify(error="Upload a video first."), 400
    if not d.get("text", "").strip():
        return jsonify(error="No text."), 400
    at = float(d.get("at", 0) or 0)
    sec = d.get("section")
    sec = int(sec) if sec is not None else None
    out = os.path.join(PREVIEW, d["id"] + f"_text{sec if sec is not None else 'A'}.png")
    try:
        overlay.still_textlayer(path, out, at, d.get("text", ""), opts_from(d), section=sec)
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)[-800:]), 500
    with open(out, "rb") as fh:
        data = fh.read()
    return Response(data, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.route("/sections", methods=["POST"])
def sections():
    """Count + base bounding boxes of each movable section (for the editor)."""
    d = request.get_json(force=True, silent=True) or {}
    path = VIDEOS.get(d.get("id"))
    if not path or not os.path.exists(path):
        return jsonify(error="Upload a video first."), 400
    if not d.get("text", "").strip():
        return jsonify(error="No text."), 400
    try:
        boxes = overlay.section_bboxes(path, d.get("text", ""), opts_from(d))
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)[-800:]), 500
    return jsonify(count=len(boxes), sections=boxes)


_renders = [0]


def _render_out(prefix):
    """A fresh filename for every render.

    Both renders used to overwrite one fixed name, so re-exporting after an
    edit returned the same URL — and the browser served the cached first cut.
    A unique URL per render makes staleness impossible. Earlier renders of the
    same clip are pruned so the host's ephemeral disk doesn't fill up."""
    for f in os.listdir(OUTPUT):
        if f.startswith(prefix):
            try:
                os.remove(os.path.join(OUTPUT, f))
            except OSError:
                pass                      # still being served; it'll go next time
    _renders[0] += 1
    name = f"{prefix}{_renders[0]}.mp4"
    return name, os.path.join(OUTPUT, name)


@app.route("/preview_video", methods=["POST"])
def preview_video():
    """Fast draft render so the user can watch it (with speed) before exporting."""
    d = request.get_json(force=True, silent=True) or {}
    path = VIDEOS.get(d.get("id"))
    if not path or not os.path.exists(path):
        return jsonify(error="Upload a video first."), 400
    if not d.get("text", "").strip():
        return jsonify(error="No text."), 400
    out_name, out_path = _render_out(f"{d['id']}_preview-")
    try:
        overlay.render(path, out_path, d["text"], opts_from(d), draft=True)
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)[-1200:]), 500
    return jsonify(url=f"/output/{out_name}")


@app.route("/export", methods=["POST"])
def export():
    d = request.get_json(force=True, silent=True) or {}
    path = VIDEOS.get(d.get("id"))
    if not path or not os.path.exists(path):
        return jsonify(error="Upload a video first."), 400
    text = d.get("text", "")
    if not text.strip():
        return jsonify(error="No text."), 400
    base = os.path.splitext(os.path.basename(path))[0]
    out_name, out_path = _render_out(f"{base}_captioned-")
    try:
        overlay.render(path, out_path, text, opts_from(d))
    except Exception as e:
        traceback.print_exc()
        return jsonify(error=str(e)[-1200:]), 500
    # `name` is what the browser saves it as (and what the posting CSV tags) — the
    # URL is uniquified per render. Prefer a per-export label so several cuts from
    # one clip get distinct names; fall back to the clip's own name.
    label = safe_label(d.get("out_name")) or f"{base}_captioned"
    return jsonify(url=f"/output/{out_name}", name=f"{label}.mp4")


@app.route("/output/<path:fn>")
def output(fn):
    return send_from_directory(OUTPUT, fn, conditional=True)


# ------------------------------------------------------------------ page -----
PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>b-roll editor</title>
<style>
  :root{--bg:#0b0b11;--card:#14141d;--line:#26263a;--fg:#ececf3;--muted:#8a8a9c;
        --accent:#25f4ee;--accent2:#fe2c55;--field:#0f0f18;}
  *{box-sizing:border-box;}
  [hidden]{display:none!important;}
  body{margin:0;background:var(--bg);color:var(--fg);
       font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;}
  header{padding:8px 24px;border-bottom:1px solid var(--line);display:flex;
         align-items:center;justify-content:space-between;}
  header h1{margin:0;font-size:17px;}
  header h1 span{background:linear-gradient(90deg,var(--accent),var(--accent2));
       -webkit-background-clip:text;background-clip:text;color:transparent;}
  header .hint{color:var(--muted);font-size:12px;}
  /* left column HUGS the video (auto), so there's no wasted space around it;
     controls take the rest. The stage size itself is computed in JS from the
     real video aspect + available width/height so it's as big as it can be. */
  main{display:grid;grid-template-columns:auto minmax(340px,560px);gap:24px;
       padding:16px 20px;max-width:1240px;margin:0 auto;align-items:start;
       justify-content:center;}
  @media (max-width:840px){main{grid-template-columns:1fr;}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;}
  .left{position:sticky;top:12px;min-width:280px;}
  #stagewrap{margin:0 auto;}
  label{display:block;font-size:11px;color:var(--muted);margin:0 0 6px;
        text-transform:uppercase;letter-spacing:.5px;}
  #drop{border:2px dashed var(--line);border-radius:12px;padding:30px 14px;
        text-align:center;cursor:pointer;color:var(--muted);transition:.15s;}
  #drop.hover{border-color:var(--accent);color:var(--fg);background:#1b1b28;}
  #drop b{color:var(--fg);}
  #drivebox{margin-top:10px;}
  .drivelbl{font-size:11px;color:var(--muted);margin-bottom:6px;}
  .drivelbl span{opacity:.7;}
  .driverow{display:flex;gap:6px;}
  #driveurl{flex:1;min-width:0;background:var(--field);color:var(--fg);
    border:1px solid var(--line);border-radius:9px;padding:8px 10px;font-size:12px;
    font-family:ui-monospace,monospace;}
  #drivego{margin-top:0;width:auto;flex:0 0 auto;padding:8px 16px;font-size:13px;}
  #drivego:disabled{opacity:.5;cursor:default;}
  .drivenote{font-size:11px;color:var(--muted);margin-top:6px;}
  /* the stage holds its size via aspect-ratio (set from the video on upload),
     so every layer can be absolutely stacked — never side-by-side, never a
     collapse when the still frame is hidden for the draft video. */
  .stage{position:relative;background:#000;border-radius:10px;overflow:hidden;
         aspect-ratio:9/16;min-height:120px;}
  /* #stage fills the stagewrap width; its height comes from the video aspect */
  #stage{width:100%;min-height:0;}
  .stage #frame,.stage #pvid{
    position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block;}
  #layers,#boxes{position:absolute;inset:0;pointer-events:none;}
  /* the result video (in the export section) is a normal in-flow element */
  #resultwrap .stage{aspect-ratio:auto;}
  #resultwrap .stage video{position:static;width:100%;height:auto;}
  .secLayer{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;
    pointer-events:none;will-change:transform;}
  /* each section's selection box — grab anywhere inside to move it */
  .secBox{position:absolute;display:none;border:1.5px dashed var(--accent);
    box-shadow:0 0 0 1px rgba(0,0,0,.35);pointer-events:auto;cursor:grab;
    touch-action:none;will-change:transform;}
  .secBox.grabbing{cursor:grabbing;}
  .posrow{display:flex;align-items:center;justify-content:center;gap:8px;}
  #resetPos{color:var(--accent);font-weight:600;cursor:pointer;font-size:11px;}
  /* video-type toggle (header) */
  .seg{display:inline-flex;background:var(--field);border:1px solid var(--line);
    border-radius:10px;padding:3px;gap:3px;}
  .seg button{margin:0;width:auto;padding:6px 14px;font-size:12px;font-weight:600;
    border-radius:7px;background:transparent;color:var(--muted);}
  .seg button.on{background:var(--accent);color:#08080c;}
  /* split-screen bank picker */
  #bankbox{margin-top:12px;}
  .bankrow{display:flex;gap:8px;align-items:center;}
  #bankthumb{width:64px;height:64px;flex:0 0 auto;object-fit:cover;border-radius:8px;
    border:1px solid var(--line);background:var(--field);}
  #bankSel{flex:1;min-width:0;}
  .bankbtn{margin:0;width:auto;flex:0 0 auto;padding:8px 11px;font-size:13px;
    background:var(--field);color:var(--fg);border:1px solid var(--line);}
  .splitnote{font-size:11px;color:var(--muted);margin-top:6px;}
  /* background-music picker */
  .musicrow{display:flex;gap:8px;align-items:center;}
  #music{flex:1;min-width:0;}
  .musicbtn{margin:0;width:auto;flex:0 0 auto;padding:8px 13px;font-size:13px;
    background:var(--field);color:var(--fg);border:1px solid var(--line);}
  .musicbtn.on{background:var(--accent);color:#08080c;border-color:var(--accent);}
  /* posting session -> bulk CSV */
  #session{margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid var(--line);}
  .sessbtn{margin:0;width:100%;padding:10px;font-size:13px;}
  .sessbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;}
  .sessdot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;background:var(--accent2);
    box-shadow:0 0 0 3px rgba(254,44,85,.18);animation:pulse 1.6s infinite;}
  @keyframes pulse{50%{opacity:.35;}}
  #sessCount{font-size:13px;font-weight:600;flex:1;min-width:120px;}
  .sessbar .sessbtn{width:auto;flex:0 0 auto;padding:8px 14px;font-size:13px;}
  .sesslink{background:none;border:0;color:var(--muted);cursor:pointer;font-size:12px;
    width:auto;margin:0;padding:4px 2px;}
  .sesslink:hover{color:var(--accent2);}
  #sessFiles{margin-bottom:10px;}
  .sessrow{display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid var(--line);}
  .sessrow:first-child{border-top:0;}
  .sessrow .fn{flex:1;min-width:0;font-size:12px;color:var(--fg);overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap;}
  .sessrow select{width:auto;flex:0 0 auto;max-width:160px;font-size:12px;padding:5px 6px;}
  .sessrow .rm{width:auto;margin:0;padding:1px 8px;background:none;border:1px solid var(--line);
    color:var(--muted);font-size:15px;line-height:1.3;border-radius:7px;cursor:pointer;flex:0 0 auto;}
  .sessrow .rm:hover{border-color:var(--accent2);color:var(--accent2);}
  .sessrow.warn select{border-color:var(--accent2);}
  #sessDefaults{background:var(--field);border:1px solid var(--line);border-radius:10px;
    padding:10px 12px;}
  #sessDefaults summary{font-size:11px;color:var(--muted);cursor:pointer;
    text-transform:uppercase;letter-spacing:.5px;}
  .sessgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px 12px;margin:10px 0;}
  .sessgrid .full{grid-column:1/3;}
  .sessgrid label{margin-bottom:4px;}
  #session input[type=text]{width:100%;background:var(--field);color:var(--fg);
    border:1px solid var(--line);border-radius:9px;padding:8px;font-size:13px;}
  #sess_pain_points{height:auto;}
  .handle{position:absolute;width:14px;height:14px;background:var(--accent);
    border:2px solid #08080c;border-radius:3px;pointer-events:auto;touch-action:none;}
  .handle.tl{top:-7px;left:-7px;cursor:nwse-resize;}
  .handle.tr{top:-7px;right:-7px;cursor:nesw-resize;}
  .handle.bl{bottom:-7px;left:-7px;cursor:nesw-resize;}
  .handle.br{bottom:-7px;right:-7px;cursor:nwse-resize;}
  .scrub{width:100%;margin-top:10px;}
  /* trim: dual-handle range to keep a section of the source clip */
  .trim{margin-top:14px;}
  .trimhead{display:flex;justify-content:space-between;align-items:baseline;
    font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;
    margin-bottom:9px;}
  .trimval{color:var(--accent);text-transform:none;letter-spacing:0;font-size:12px;
    font-variant-numeric:tabular-nums;}
  .trimslider{position:relative;height:26px;}
  .trimtrack{position:absolute;top:50%;left:0;right:0;height:5px;transform:translateY(-50%);
    background:var(--field);border:1px solid var(--line);border-radius:4px;}
  .trimfill{position:absolute;top:50%;height:5px;transform:translateY(-50%);
    background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;}
  .trimslider input[type=range]{position:absolute;top:0;left:0;width:100%;height:26px;
    margin:0;-webkit-appearance:none;appearance:none;background:transparent;pointer-events:none;}
  .trimslider input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
    width:16px;height:16px;border-radius:50%;background:#fff;border:2px solid var(--accent);
    cursor:grab;pointer-events:auto;box-shadow:0 1px 4px rgba(0,0,0,.5);}
  .trimslider input[type=range]::-moz-range-thumb{width:16px;height:16px;border-radius:50%;
    background:#fff;border:2px solid var(--accent);cursor:grab;pointer-events:auto;}
  .trimslider input[type=range]::-moz-range-track{background:transparent;border:0;}
  textarea{width:100%;min-height:200px;resize:vertical;background:var(--field);
    color:var(--fg);border:1px solid var(--line);border-radius:10px;padding:11px;
    font:13px/1.5 ui-monospace,Consolas,monospace;}
  select,input[type=number]{width:100%;background:var(--field);color:var(--fg);
    border:1px solid var(--line);border-radius:9px;padding:8px;font-size:13px;}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 14px;margin-top:14px;}
  .full{grid-column:1/3;}
  .ctl{font-size:13px;}
  .ctl .val{color:var(--accent);float:right;font-variant-numeric:tabular-nums;}
  input[type=range]{width:100%;accent-color:var(--accent);}
  .rowchk{display:flex;gap:18px;align-items:center;margin-top:6px;}
  .chk{display:flex;align-items:center;gap:7px;color:var(--fg);font-size:13px;cursor:pointer;}
  .colors{display:flex;gap:14px;}
  .colors label{margin-bottom:4px;}
  input[type=color]{width:100%;height:34px;background:var(--field);border:1px solid var(--line);
    border-radius:8px;padding:2px;cursor:pointer;}
  button{margin-top:16px;width:100%;padding:13px;border:0;border-radius:11px;font-size:15px;
    font-weight:600;cursor:pointer;color:#08080c;
    background:linear-gradient(90deg,var(--accent),var(--accent2));}
  button.secondary{background:transparent;color:var(--fg);border:1px solid var(--line);}
  button.secondary:hover:not(:disabled){border-color:var(--accent);color:var(--accent);}
  button:disabled{opacity:.45;cursor:default;}
  .stage video{width:100%;display:block;}
  .err{color:#ff8b8b;font-size:12px;white-space:pre-wrap;margin-top:8px;font-family:ui-monospace,monospace;}
  a.dl{display:inline-block;margin-top:10px;color:var(--accent);font-weight:600;
    text-decoration:none;font-size:14px;}
  .meta{margin-top:12px;font-size:12px;color:var(--fg);background:var(--field);
    border:1px solid var(--line);border-radius:9px;padding:9px;white-space:pre-wrap;}
  .spin{display:inline-block;width:14px;height:14px;border:2px solid #08080c;
    border-top-color:transparent;border-radius:50%;animation:s .7s linear infinite;
    vertical-align:-2px;margin-right:7px;}
  @keyframes s{to{transform:rotate(360deg);}}
  .badge{font-size:11px;color:var(--muted);margin-top:8px;text-align:center;}
  #outname{width:100%;background:var(--field);color:var(--fg);border:1px solid var(--line);
    border-radius:9px;padding:8px 10px;font-size:13px;}
  #exportmsg{color:var(--accent);font-size:12px;line-height:1.5;}
</style></head><body>
<header>
  <h1><span>b-roll editor</span></h1>
  <div class="seg" id="layoutseg">
    <button type="button" data-layout="single" class="on">Text overlay</button>
    <button type="button" data-layout="split">Split screen</button>
  </div>
  <div class="hint" id="hint">TikTok Sans · live preview · local</div>
</header>
<main>
  <!-- LEFT: stage -->
  <section class="card left">
    <div id="drop">
      <div id="dropmsg"><b>Drop a video</b><br>or click to choose</div>
      <input id="file" type="file" accept="video/*" hidden>
    </div>
    <!-- Big 4K originals can't be pushed through the browser (the host's proxy
         502s partway up). Pulling them straight from Drive skips that entirely. -->
    <div id="drivebox">
      <div class="drivelbl">…or paste a Google&nbsp;Drive link <span>(best for large 4K files)</span></div>
      <div class="driverow">
        <input id="driveurl" type="text" spellcheck="false"
               placeholder="https://drive.google.com/file/d/…/view">
        <button id="drivego" type="button">Import</button>
      </div>
      <div class="drivenote" id="drivenote">Share it as “Anyone with the link”.</div>
    </div>
    <div id="stagewrap" hidden>
      <div class="stage" id="stage">
        <img id="frame" alt="preview">
        <div id="layers"></div>
        <video id="pvid" loop muted playsinline hidden></video>
        <div id="boxes"></div>
      </div>
      <input id="scrub" class="scrub" type="range" min="0" max="100" value="0" step="0.1">
      <div class="badge" id="timebadge">preview frame</div>
      <div class="badge posrow"><span id="posBadge">drag each section · corners resize</span><span id="resetPos" hidden>reset all</span></div>
      <div id="trimwrap" class="trim" hidden>
        <div class="trimhead"><span>Keep section</span><span class="trimval" id="trimReadout"></span></div>
        <div class="trimslider" id="trimSlider">
          <div class="trimtrack"></div>
          <div class="trimfill" id="trimFill"></div>
          <input id="trimStart" type="range" min="0" max="100" value="0" step="0.1">
          <input id="trimEnd" type="range" min="0" max="100" value="100" step="0.1">
        </div>
      </div>
    </div>
    <div id="resultwrap" hidden style="margin-top:16px">
      <label>Exported video</label>
      <div class="stage"><video id="result" controls></video></div>
      <a id="dl" class="dl" download>⬇ Download</a>
    </div>
    <div id="outnamewrap" hidden style="margin-top:14px">
      <label>Export file name</label>
      <input id="outname" type="text" spellcheck="false" placeholder="auto from the first line">
    </div>
    <button id="preview" class="secondary" disabled>▶ Preview (watch it)</button>
    <button id="export" disabled>Export video</button>
    <div id="exportmsg" class="badge" hidden></div>
    <div id="err" class="err"></div>
  </section>

  <!-- RIGHT: controls -->
  <section class="card">
    <!-- posting session: collect every export, then emit a bulk-posting CSV -->
    <div id="session">
      <button type="button" id="sessStart" class="secondary sessbtn">● Start posting session</button>
      <div id="sessActive" hidden>
        <div class="sessbar">
          <span class="sessdot"></span>
          <span id="sessCount">0 videos in this session</span>
          <button type="button" id="sessEnd" class="sessbtn">End · download CSV</button>
          <button type="button" id="sessCancel" class="sesslink">cancel</button>
        </div>
        <div id="sessFiles" hidden></div>
        <details id="sessDefaults" open>
          <summary>Session tags — applied to every video</summary>
          <div class="sessgrid">
            <div><label>Default account (new exports)</label><select id="sess_ig_handle"></select></div>
            <div><label>Experiment id</label><input type="text" id="sess_experiment_id" value="solo" spellcheck="false"></div>
            <div><label>Cohort</label><select id="sess_cohort"></select></div>
            <div><label>Niche</label><select id="sess_niche"></select></div>
            <div><label>Industry</label><select id="sess_industry"></select></div>
            <div><label>Intent stage</label><select id="sess_intent_stage"></select></div>
            <div><label>Hook type</label><select id="sess_hook_type"></select></div>
            <div><label>CTA type</label><select id="sess_cta_type"></select></div>
            <div><label>B-roll source</label><select id="sess_broll_source"></select></div>
            <div><label>Audio source</label><select id="sess_audio_source"></select></div>
            <div><label>Variant axis</label><select id="sess_variant_axis"></select></div>
            <div class="full"><label>Pain points (up to 3)</label><select id="sess_pain_points" multiple size="4"></select></div>
          </div>
          <div class="splitnote">Each export is added below with its own account — switch the default between batches (or edit any row) to split 10 videos 3/3/3/1 across accounts. Filename, format, overlay, hook line &amp; caption are captured automatically; the rest come from these tags.</div>
        </details>
      </div>
    </div>

    <!-- split screen only: which clip fills the top panel -->
    <div id="bankbox" hidden>
      <label>Top clip (overlay bank)</label>
      <div class="bankrow">
        <img id="bankthumb" alt="">
        <select id="bankSel"><option value="">— pick a clip —</option></select>
        <button type="button" class="bankbtn" id="bankPrev" title="previous clip">‹</button>
        <button type="button" class="bankbtn" id="bankNext" title="next clip">›</button>
      </div>
      <div class="splitnote" id="splitnote">Top 45% overlay · 10% text band · bottom 45% your clip.</div>
    </div>

    <label>Text</label>
    <select id="script" style="margin-bottom:9px">
      <option value="">— load one of the scripts —</option>
    </select>
    <textarea id="text" spellcheck="false"></textarea>
    <div id="meta" class="meta" hidden></div>

    <div class="grid">
      <div><label>Layout</label>
        <select id="mode">
          <option value="block">Centered block</option>
          <option value="captions">Captions (per line)</option>
        </select></div>
      <div><label>Font</label><select id="font"></select></div>

      <div><label>Position</label>
        <select id="position">
          <option value="">Auto</option><option value="center">Center</option>
          <option value="bottom">Bottom</option><option value="top">Top</option>
        </select></div>
      <div class="ctl"><label>Speed <span class="val" id="speedV">1.0×</span></label>
        <input type="range" id="speed" min="0.25" max="10" step="0.05" value="1"></div>

      <div class="ctl"><label>Font size <span class="val" id="sizeV">Auto</span></label>
        <input type="range" id="size" min="0" max="140" step="1" value="0"></div>
      <div class="ctl"><label>Body line spacing <span class="val" id="spacingV">1.00</span></label>
        <input type="range" id="spacing" min="0.6" max="10" step="0.1" value="1"></div>

      <div class="ctl"><label>Header gap (top) <span class="val" id="hgapV">1.6</span></label>
        <input type="range" id="header_gap" min="0" max="8" step="0.1" value="1.6"></div>
      <div class="ctl"><label>Footer gap (bottom) <span class="val" id="fgapV">1.6</span></label>
        <input type="range" id="footer_gap" min="0" max="8" step="0.1" value="1.6"></div>

      <div class="ctl"><label>Outline <span class="val" id="borderV">4</span></label>
        <input type="range" id="border" min="0" max="16" step="1" value="4"></div>
      <div class="colors full">
        <div style="flex:1"><label>Text color</label><input type="color" id="color" value="#ffffff"></div>
        <div style="flex:1"><label>Outline color</label><input type="color" id="outline" value="#000000"></div>
      </div>

      <div class="full rowchk">
        <label class="chk"><input type="checkbox" id="upper"> UPPERCASE</label>
        <label class="chk"><input type="checkbox" id="shadow"> Shadow (3D)</label>
      </div>

      <div class="full">
        <label>Background music</label>
        <div class="musicrow">
          <select id="music"><option value="">— none —</option></select>
          <button type="button" class="musicbtn" id="musicPlay" title="listen" disabled>▶</button>
        </div>
        <div class="splitnote" id="musicnote">Loops to fill the clip · mixed under any existing audio. Heard in Preview &amp; Export.</div>
      </div>
    </div>
    <audio id="musicAudio" preload="none"></audio>
    <div class="badge" id="pvstatus"></div>
  </section>
</main>

<script>
const $=s=>document.querySelector(s);
const file=$("#file"),drop=$("#drop"),stagewrap=$("#stagewrap"),frame=$("#frame"),
  scrub=$("#scrub"),exportBtn=$("#export"),previewBtn=$("#preview"),pvid=$("#pvid"),
  err=$("#err"),text=$("#text"),resultwrap=$("#resultwrap"),result=$("#result"),dl=$("#dl"),
  meta=$("#meta"),scriptSel=$("#script"),pvstatus=$("#pvstatus"),timebadge=$("#timebadge"),
  stage=$("#stage"),posBadge=$("#posBadge"),resetPos=$("#resetPos"),
  layersEl=$("#layers"),boxesEl=$("#boxes");
const HJSON={"Content-Type":"application/json"};

// switch stage back to the live still frame (used whenever a control changes)
function showStill(){
  if(!pvid.hidden){ try{pvid.pause();}catch(e){} }
  pvid.hidden=true; frame.hidden=false;
  layersEl.style.display=""; boxesEl.style.display="";
}

// Each blank-line section of the text is an independent movable/resizable box.
// state.secs[i] = {dx, dy, size(null=auto), bbox, layer:<img>, box:<div>}
let state={id:null,duration:0,vw:0,vh:0,frameAt:null,count:0,secs:[],
           layout:"single",bank:null,caption:""};

// display px per video px (frame is shown at width:100%, aspect preserved)
function layerScale(){
  const r=frame.getBoundingClientRect();
  return (state.vw && r.width) ? r.width/state.vw : 1;
}
function cornerPoint(b,c){
  return c==="tl"?[b.x0,b.y0]:c==="tr"?[b.x1,b.y0]:c==="bl"?[b.x0,b.y1]:[b.x1,b.y1];
}

// build (or reuse) one <img> layer + one <div> box per section
function ensureSections(n){
  if(state.count===n) return;
  // carry over any existing offsets/sizes so a text tweak doesn't reset them
  const old=state.secs;
  layersEl.innerHTML=""; boxesEl.innerHTML=""; state.secs=[];
  for(let i=0;i<n;i++){
    const layer=document.createElement("img"); layer.className="secLayer";
    layersEl.appendChild(layer);
    const box=document.createElement("div"); box.className="secBox";
    box.innerHTML='<div class="handle tl"></div><div class="handle tr"></div>'+
                  '<div class="handle bl"></div><div class="handle br"></div>';
    boxesEl.appendChild(box);
    const prev=old[i]||{};
    state.secs[i]={dx:prev.dx||0,dy:prev.dy||0,size:prev.size||null,bbox:null,layer,box};
    wireSection(i);
  }
  state.count=n;
}

// place a section's box over its BASE bbox (offset added separately via transform)
function positionBox(i){
  const s=state.secs[i], b=s.bbox;
  const fr=frame.getBoundingClientRect(), st=stage.getBoundingClientRect();
  if(!b||!state.vw||!state.vh||!fr.width){ s.box.style.display="none"; return; }
  const sc=layerScale();
  const left=(fr.left-st.left)+b.x0*sc, top=(fr.top-st.top)+b.y0*sc;
  s.box.style.cssText=`display:block;left:${left}px;top:${top}px;`+
    `width:${(b.x1-b.x0)*sc}px;height:${(b.y1-b.y0)*sc}px;`;
}

// pure-CSS offset (+ optional live resize scale) for one section — no server hit
function applySection(i,ratio){
  const s=state.secs[i], sc=layerScale(); ratio=ratio||1;
  const t=`translate(${s.dx*sc}px,${s.dy*sc}px) scale(${ratio})`;
  if(s.bbox){
    const cx=(s.bbox.x0+s.bbox.x1)/2, cy=(s.bbox.y0+s.bbox.y1)/2;
    s.layer.style.transformOrigin=`${cx/state.vw*100}% ${cy/state.vh*100}%`;
  }
  s.layer.style.transform=t;
  s.box.style.transformOrigin="center";
  s.box.style.transform=t;
}
// Size the preview as large as physically fits. A portrait clip is bounded by
// viewport HEIGHT; a landscape clip by the available WIDTH — take the smaller so
// it's always maximal without overflowing. Column hugs it (no wasted space).
function sizeStage(){
  if(!state.vw||!state.vh) return;
  const ar=state.vw/state.vh;                          // width / height
  const availW=Math.min(window.innerWidth-400, 820);   // leave room for controls
  const availH=window.innerHeight-96;                  // header + compact upload bar
  let w=Math.min(availW, availH*ar, 640);              // fit both; sane upper cap
  stagewrap.style.width=Math.max(160,Math.round(w))+"px";
}
function refreshOverlay(){ for(let i=0;i<state.count;i++){ positionBox(i); applySection(i); } }
window.addEventListener("resize",()=>{ sizeStage(); refreshOverlay(); });
frame.addEventListener("load",refreshOverlay);

function updatePosBadge(){
  const moved=state.secs.some(s=>Math.round(s.dx)||Math.round(s.dy)||s.size);
  posBadge.textContent=moved?"sections repositioned":"drag each section · corners resize";
  resetPos.hidden=!moved;
}
resetPos.onclick=()=>{ state.secs.forEach(s=>{s.dx=0;s.dy=0;s.size=null;});
  updatePosBadge(); schedulePreview(); };

// attach drag (grab the box) + resize (grab a corner) handlers for section i
function wireSection(i){
  const s=state.secs[i], box=s.box;
  let dragging=false, ds=null;
  box.addEventListener("pointerdown",e=>{
    if(e.target.classList.contains("handle")||!state.id) return;
    e.stopPropagation();
    dragging=true; box.classList.add("grabbing");
    try{box.setPointerCapture(e.pointerId);}catch(_){}
    ds={x:e.clientX,y:e.clientY,dx:s.dx,dy:s.dy,sc:layerScale()||1};
  });
  box.addEventListener("pointermove",e=>{
    if(!dragging) return;
    s.dx=ds.dx+(e.clientX-ds.x)/ds.sc; s.dy=ds.dy+(e.clientY-ds.y)/ds.sc;
    updatePosBadge(); applySection(i);
  });
  const endDrag=e=>{ if(!dragging)return; dragging=false; box.classList.remove("grabbing");
    try{box.releasePointerCapture(e.pointerId);}catch(_){} };
  box.addEventListener("pointerup",endDrag);
  box.addEventListener("pointercancel",endDrag);

  box.querySelectorAll(".handle").forEach(h=>{
    let rez=null;
    h.addEventListener("pointerdown",e=>{
      if(!state.id||!s.bbox) return;
      e.stopPropagation();
      try{h.setPointerCapture(e.pointerId);}catch(_){}
      const b=s.bbox, cx=(b.x0+b.x1)/2, cy=(b.y0+b.y1)/2;
      const corner=h.classList.contains("tl")?"tl":h.classList.contains("tr")?"tr":
                   h.classList.contains("bl")?"bl":"br";
      const [px,py]=cornerPoint(b,corner);
      rez={x:e.clientX,y:e.clientY,cx,cy,corner,dist0:Math.hypot(px-cx,py-cy)||1,
        size0:b.size||s.size||28,sc:layerScale()||1};
    });
    h.addEventListener("pointermove",e=>{
      if(!rez) return;
      const sc=rez.sc;
      const [px0,py0]=cornerPoint(s.bbox,rez.corner);
      const curX=px0+(e.clientX-rez.x)/sc, curY=py0+(e.clientY-rez.y)/sc;
      const ratio=Math.max(0.2,Math.hypot(curX-rez.cx,curY-rez.cy)/rez.dist0);
      s.size=Math.max(8,Math.min(200,Math.round(rez.size0*ratio)));
      updatePosBadge(); applySection(i,ratio);
    });
    const endRez=e=>{ if(!rez)return; rez=null;
      try{h.releasePointerCapture(e.pointerId);}catch(_){}
      schedulePreview(); };   // re-render that section crisp at the new size
    h.addEventListener("pointerup",endRez);
    h.addEventListener("pointercancel",endRez);
  });
}

const DEFAULT_TEXT=`Claude = coding. ($20/mo)
Supabase = backend. (Free)
Vercel = deploying. (Free)
Namecheap = domain. ($12/yr)
Stripe = payments. (2.9%)
GitHub = version control. (Free)
Resend = emails. (Free)
Clerk = auth. (Free)
Cloudflare = DNS. (Free)
PostHog = analytics. (Free)
Sentry = error tracking. (Free)
Upstash = Redis. (Free)
Pinecone = vector DB. (Free)

Total monthly cost to run a
startup: ~$20

There has never been a
cheaper time to build.`;
text.value=DEFAULT_TEXT;

// populate fonts + scripts
fetch("/fonts").then(r=>r.json()).then(list=>{
  const f=$("#font"); for(const n of list){const o=document.createElement("option");o.value=n;o.textContent=n;f.appendChild(o);}
});
fetch("/scripts").then(r=>r.json()).then(list=>{
  for(const s of list){const o=document.createElement("option");o.value=s.n;o.textContent="#"+s.n+" — "+s.title;scriptSel.appendChild(o);}
}).catch(()=>{});
scriptSel.onchange=async()=>{
  if(!scriptSel.value){meta.hidden=true;return;}
  const s=await(await fetch("/script/"+scriptSel.value)).json();
  text.value=s.text;
  const bits=[]; if(s.caption)bits.push(s.caption); if(s.hashtags)bits.push(s.hashtags);
  meta.textContent=bits.join("\n\n"); meta.hidden=!bits.length;
  state.caption=bits.join("\n\n");   // reused as caption_text in the posting CSV
  outnameEdited=false; autoName();    // a freshly loaded script re-derives the name
  schedulePreview();
};

// ---- video type: single (text over one clip) vs split screen ----------------
const bankbox=$("#bankbox"), bankSel=$("#bankSel"), bankThumb=$("#bankthumb"),
      splitnote=$("#splitnote");
let BANK=[], SPLITC={w:1080,h:1920};
fetch("/bank").then(r=>r.json()).then(j=>{
  BANK=j.clips||[]; if(j.canvas) SPLITC=j.canvas;
  for(const b of BANK){
    const o=document.createElement("option");
    o.value=b.file; o.textContent=b.title+" ("+b.duration+"s)";
    bankSel.appendChild(o);
  }
}).catch(()=>{});

function setBank(file){
  state.bank=file||null;
  bankSel.value=file||"";
  if(file){ bankThumb.src="/bank/"+encodeURIComponent(file)+"/thumb.jpg"; }
  else{ bankThumb.removeAttribute("src"); }
  if(state.layout==="split" && !file){
    splitnote.textContent=BANK.length
      ? "Pick a top clip — split screen needs one."
      : "bank/ is empty. Drop clips into the bank/ folder.";
  }else{
    splitnote.textContent="Top 45% overlay · 10% text band · bottom 45% your clip.";
  }
  // the composed background changes with the bank clip, so force a re-fetch
  state.frameAt=null;
  schedulePreview();
}
function cycleBank(step){
  if(!BANK.length) return;
  const i=BANK.findIndex(b=>b.file===state.bank);
  const n=(i<0?0:(i+step+BANK.length)%BANK.length);
  setBank(BANK[n].file);
}
bankSel.onchange=()=>setBank(bankSel.value);
$("#bankPrev").onclick=()=>cycleBank(-1);
$("#bankNext").onclick=()=>cycleBank(1);

// ---- background music: pick a track + listen ------------------------------
const musicSel=$("#music"), musicPlay=$("#musicPlay"), musicAudio=$("#musicAudio");
fetch("/music").then(r=>r.json()).then(j=>{
  for(const t of (j.tracks||[])){
    const o=document.createElement("option");
    o.value=t.file; o.textContent=t.title; musicSel.appendChild(o);
  }
}).catch(()=>{});
function stopListen(){ try{musicAudio.pause();}catch(e){} musicPlay.classList.remove("on"); musicPlay.textContent="▶"; }
musicSel.onchange=()=>{ stopListen(); musicPlay.disabled=!musicSel.value; };
musicPlay.onclick=()=>{
  if(!musicSel.value) return;
  if(!musicPlay.classList.contains("on")){
    musicAudio.src="/music/"+encodeURIComponent(musicSel.value);
    musicAudio.currentTime=0;
    musicAudio.play().then(()=>{ musicPlay.classList.add("on"); musicPlay.textContent="⏸"; })
      .catch(()=>{});
  } else { stopListen(); }
};
musicAudio.addEventListener("ended",stopListen);

function setLayout(l){
  state.layout=l;
  for(const b of document.querySelectorAll("#layoutseg button"))
    b.classList.toggle("on", b.dataset.layout===l);
  bankbox.hidden=(l!=="split");
  // split confines the text to the black band, so the position control (top/
  // center/bottom of the frame) has no meaning there.
  $("#position").disabled=(l==="split");
  // A split render composes a NEW 9:16 frame rather than drawing on the user's
  // own, so the stage — which is the coordinate space the drag layers and
  // resize boxes live in — has to switch with it, or every overlay is offset.
  if(state.srcW){
    state.vw = (l==="split") ? SPLITC.w : state.srcW;
    state.vh = (l==="split") ? SPLITC.h : state.srcH;
    stage.style.aspectRatio = state.vw+"/"+state.vh;
    sizeStage();
  }
  state.secs=[]; state.count=0;         // bboxes are in the old canvas space
  layersEl.innerHTML=""; boxesEl.innerHTML="";
  state.frameAt=null;                  // background composition changed
  if(l==="split" && !state.bank && BANK.length){ setBank(BANK[0].file); return; }
  setBank(state.bank);
}
for(const b of document.querySelectorAll("#layoutseg button"))
  b.onclick=()=>setLayout(b.dataset.layout);

// slider value readouts
const speed=$("#speed"),size=$("#size"),spacing=$("#spacing"),border=$("#border"),
  headerGap=$("#header_gap"),footerGap=$("#footer_gap");
function readouts(){
  $("#speedV").textContent=(+speed.value).toFixed(2)+"×";
  $("#sizeV").textContent=(+size.value===0)?"Auto":size.value+"px";
  $("#spacingV").textContent=(+spacing.value).toFixed(2);
  $("#hgapV").textContent=(+headerGap.value).toFixed(1);
  $("#fgapV").textContent=(+footerGap.value).toFixed(1);
  $("#borderV").textContent=border.value;
}
readouts();

function opts(){
  return {
    id:state.id, text:text.value, at:+scrub.value,
    layout:state.layout, bank:state.bank,
    mode:$("#mode").value, font:$("#font").value, position:$("#position").value,
    color:$("#color").value, outline:$("#outline").value,
    border:+border.value, size:+size.value, spacing:+spacing.value,
    header_gap:+headerGap.value, footer_gap:+footerGap.value, speed:+speed.value,
    trim_start:+trimStart.value, trim_end:+trimEnd.value,
    music:$("#music").value, out_name:$("#outname").value,
    upper:$("#upper").checked, shadow:$("#shadow").checked,
    offsets:state.secs.map(s=>[s.dx,s.dy]),   // per-section drag offset
    sizes:state.secs.map(s=>s.size||0),        // per-section size (0 = auto)
  };
}

// live preview (debounced)
let pvTimer=null, pvBusy=false, pvAgain=false;
function schedulePreview(){ readouts(); if(!state.id) return;
  showStill();  // any change makes the draft video stale -> back to live frame
  clearTimeout(pvTimer); pvTimer=setTimeout(doPreview,220); }
let lastFrameURL=null;
async function doPreview(){
  if(!state.id) return;
  if(pvBusy){ pvAgain=true; return; }
  pvBusy=true; pvAgain=false; err.textContent=""; pvstatus.textContent="rendering preview…";
  const o=opts();
  try{
    // clean background frame — only re-fetch when the scrub time changes
    const framePromise=(state.frameAt!==o.at || !frame.getAttribute("src"))
      ? fetch("/frame",{method:"POST",headers:HJSON,
          // layout+bank matter: in split mode the background IS the composition
          body:JSON.stringify({id:o.id,at:o.at,layout:o.layout,bank:o.bank})})
          .then(r=>r.ok?r.blob():Promise.reject(new Error("frame failed")))
          .then(b=>{ if(lastFrameURL)URL.revokeObjectURL(lastFrameURL);
                     lastFrameURL=URL.createObjectURL(b); frame.src=lastFrameURL; state.frameAt=o.at; })
      : Promise.resolve();
    // how many sections + each one's base bbox (drives the boxes/handles)
    const info=await fetch("/sections",{method:"POST",headers:HJSON,body:JSON.stringify(o)})
      .then(async r=>{ if(!r.ok){throw new Error((await r.json().catch(()=>({}))).error||"layout failed");} return r.json(); });
    ensureSections(info.count);
    for(let i=0;i<info.count;i++) state.secs[i].bbox=info.sections[i];
    // one transparent text PNG per section (offset applied in CSS, not baked)
    const layerJobs=state.secs.map((s,i)=>
      fetch("/textlayer",{method:"POST",headers:HJSON,body:JSON.stringify({...o,section:i})})
        .then(r=>r.ok?r.blob():Promise.reject(new Error("text failed")))
        .then(b=>{ if(s._url)URL.revokeObjectURL(s._url);
                   s._url=URL.createObjectURL(b); s.layer.src=s._url; }));
    await Promise.all([framePromise,...layerJobs]);
    updatePosBadge(); refreshOverlay();
  }catch(e){ err.textContent=String(e.message||e); }
  pvstatus.textContent="";
  pvBusy=false;
  if(pvAgain) doPreview();
}

// wire every control to live preview
["mode","font","position","color","outline","upper","shadow"].forEach(id=>$("#"+id).addEventListener("change",schedulePreview));
["speed","size","spacing","header_gap","footer_gap","border"].forEach(id=>$("#"+id).addEventListener("input",schedulePreview));
text.addEventListener("input",schedulePreview);
scrub.addEventListener("input",()=>{ timebadge.textContent="preview @ "+(+scrub.value).toFixed(1)+"s"; schedulePreview(); });

// ---- export file name: auto-slug from the script, but user-overridable ------
// This is what makes several videos from one clip work: each export gets its
// own name (so downloads don't collide and each becomes its own posting row).
const outname=$("#outname"), outnamewrap=$("#outnamewrap"), exportmsg=$("#exportmsg");
let outnameEdited=false;
function slugFirstLine(){
  const l=(text.value.split("\n").map(s=>s.trim()).filter(Boolean)[0]||"");
  return l.toLowerCase().replace(/[^a-z0-9]+/g,"_").replace(/^_+|_+$/g,"").slice(0,50);
}
function autoName(){ if(!outnameEdited) outname.value=slugFirstLine(); }
outname.addEventListener("input",()=>{ outnameEdited=!!outname.value.trim(); });
text.addEventListener("input",autoName);

// ---- trim: keep a section of the source, then speed it up -------------------
const trimwrap=$("#trimwrap"), trimStart=$("#trimStart"), trimEnd=$("#trimEnd"),
      trimFill=$("#trimFill"), trimReadout=$("#trimReadout");
const MIN_SEG=0.5;   // never let the kept section collapse below this
const fmtS=s=>(Math.round(s*10)/10).toFixed(1)+"s";
function updateTrimUI(){
  const max=parseFloat(trimEnd.max)||0;
  const a=parseFloat(trimStart.value)||0, b=parseFloat(trimEnd.value)||0;
  const pa=max?a/max*100:0, pb=max?b/max*100:100;
  trimFill.style.left=pa+"%"; trimFill.style.width=Math.max(0,pb-pa)+"%";
  const seg=Math.max(0,b-a), sp=+speed.value||1;
  trimReadout.textContent=`${fmtS(a)}–${fmtS(b)} · keep ${fmtS(seg)} → export ${fmtS(seg/sp)} @ ${sp.toFixed(2)}×`;
}
function initTrim(dur){
  const d=Math.max(0.1,dur||0);
  trimStart.max=d; trimEnd.max=d; trimStart.value=0; trimEnd.value=d;
  trimwrap.hidden=false; updateTrimUI();
}
trimStart.addEventListener("input",()=>{
  const b=parseFloat(trimEnd.value)||0;
  if((parseFloat(trimStart.value)||0)>b-MIN_SEG) trimStart.value=Math.max(0,b-MIN_SEG);
  trimStart.style.zIndex="3"; trimEnd.style.zIndex="2"; updateTrimUI();
});
trimEnd.addEventListener("input",()=>{
  const a=parseFloat(trimStart.value)||0, max=parseFloat(trimEnd.max)||0;
  if((parseFloat(trimEnd.value)||0)<a+MIN_SEG) trimEnd.value=Math.min(max,a+MIN_SEG);
  trimEnd.style.zIndex="3"; trimStart.style.zIndex="2"; updateTrimUI();
});
// releasing a handle jumps the preview frame to that cut point so you can see it
trimStart.addEventListener("change",()=>{ scrub.value=trimStart.value;
  scrub.dispatchEvent(new Event("input")); });
trimEnd.addEventListener("change",()=>{
  scrub.value=Math.max(0,(parseFloat(trimEnd.value)||0)-0.1);
  scrub.dispatchEvent(new Event("input")); });
speed.addEventListener("input",updateTrimUI);

// ---- posting session: collect exports -> bulk-posting CSV -------------------
// The vocab + column order mirror BULK_CSV_GUIDE.md exactly, so a downloaded
// file imports cleanly into the posting app. Enum dropdowns can only hold valid
// values (the guide's rule #3). Anything the editor can't know is left to the
// shared "session tags" or blank for you to finish in the app's grid.
const CSV_COLS=["filename","ig_handle","experiment_id","variant_id","variant_axis",
  "control_flag","format","format_subtype","broll_source","demo_surface","audio_source",
  "overlay_type","cohort","niche","industry","intent_stage","hook_type","cta_type",
  "hook_text","pain_points","notes","caption_text","scheduled_at"];
const VOCAB={
  ig_handle:["aiwithaidann","arma.dillo4479","bellabangerbuilds","cadenagents.ai",
    "calvin.codes","chriscodeconsulting","dagundevs","eric.codesai","evanbuilds.dev",
    "hayeswithai","jessica.jsonfile","openswarm.guatemala","openswarm.mexico",
    "productivelypriscilla","vanessavibecodes"],
  cohort:["indie_dev","solo_founder","eng_lead","oss_maintainer","ai_engineer",
    "vibe_coder","agency_owner","student_dev","crypto_builder","general_smb"],
  niche:["agent_orchestration","autonomous_coding","linear_workflows","swe_bench_results",
    "local_llm_setup","claude_code_workflows","openrouter_routing","cost_aware_ai",
    "discord_devops","make_money_with_agents","replace_eng_team","pr_automation",
    "code_registry_bs_detector"],
  industry:["dev_tools","ai_infra","saas","oss","agency","general"],
  intent_stage:["awareness","consideration","conversion","retention"],
  hook_type:["question","bold_claim","list_promise","pain_point","curiosity_gap",
    "social_proof","contrarian","benchmark_flex"],
  cta_type:["github_star","npm_install","discord_join","follow","dm_keyword",
    "link_in_bio","comment_prompt","none"],
  broll_source:["minecraft_parkour","subway_surfers","gta_driving","satisfying_clips",
    "cooking","nature","none"],
  audio_source:["peter_griffin_ai","family_guy_clip","morgan_freeman_ai","original_vo",
    "trending_sound","none"],
  variant_axis:["hook","audio","format","broll","demo_surface","cta","length","caption",
    "cohort_test"],
  pain_points:["ai_cost","model_lock_in","manual_pr_review","linear_backlog_rot",
    "local_model_setup","ci_failures","merge_conflicts","context_loss_across_sessions",
    "frontier_model_pricing","slow_eng_velocity","tool_overload"],
};
function sessFill(id,vals,{placeholder,def}={}){
  const el=$("#sess_"+id); if(!el) return;
  if(placeholder){ const o=document.createElement("option"); o.value=""; o.textContent=placeholder; el.appendChild(o); }
  for(const v of vals){ const o=document.createElement("option"); o.value=v; o.textContent=v; el.appendChild(o); }
  if(def) el.value=def;
}
sessFill("ig_handle",VOCAB.ig_handle,{placeholder:"— pick account —"});
sessFill("cohort",VOCAB.cohort,{def:"indie_dev"});
sessFill("niche",VOCAB.niche,{def:"claude_code_workflows"});
sessFill("industry",VOCAB.industry,{def:"dev_tools"});
sessFill("intent_stage",VOCAB.intent_stage,{def:"awareness"});
sessFill("hook_type",VOCAB.hook_type,{def:"list_promise"});
sessFill("cta_type",VOCAB.cta_type,{def:"follow"});
sessFill("broll_source",VOCAB.broll_source,{def:"satisfying_clips"});
sessFill("variant_axis",VOCAB.variant_axis,{def:"hook"});
sessFill("pain_points",VOCAB.pain_points);
// audio_source gets an "auto" choice: trending_sound when a song is on, else none
(()=>{ const el=$("#sess_audio_source");
  const a=document.createElement("option"); a.value="auto"; a.textContent="auto (from music)"; el.appendChild(a);
  for(const v of VOCAB.audio_source){ const o=document.createElement("option"); o.value=v; o.textContent=v; el.appendChild(o); }
  el.value="auto"; })();

let SESSION=null;   // {rows:[{filename,format,overlay_type,hadMusic,hook_text,caption_text}]}
const sessStart=$("#sessStart"), sessActive=$("#sessActive"), sessEnd=$("#sessEnd"),
      sessCancel=$("#sessCancel"), sessCount=$("#sessCount"), sessFiles=$("#sessFiles");
function updateSess(){
  const n=SESSION?SESSION.rows.length:0;
  sessCount.textContent=n+" video"+(n===1?"":"s")+" in this session";
  renderSessRows();
}
// one editable line per captured export: filename · account picker · remove.
// Splitting across accounts is just setting each row's account (or switching the
// default between export batches, which each new row inherits).
function renderSessRows(){
  sessFiles.innerHTML="";
  if(!SESSION||!SESSION.rows.length){ sessFiles.hidden=true; return; }
  sessFiles.hidden=false;
  SESSION.rows.forEach((r,i)=>{
    const row=document.createElement("div"); row.className="sessrow"+(r.ig_handle?"":" warn");
    const fn=document.createElement("span"); fn.className="fn";
    fn.textContent=(i+1)+". "+r.filename; fn.title=r.filename;
    const sel=document.createElement("select");
    const ph=document.createElement("option"); ph.value=""; ph.textContent="— account —"; sel.appendChild(ph);
    for(const h of VOCAB.ig_handle){ const o=document.createElement("option"); o.value=h; o.textContent=h; sel.appendChild(o); }
    sel.value=r.ig_handle||"";
    sel.onchange=()=>{ r.ig_handle=sel.value; row.classList.toggle("warn",!sel.value); };
    const rm=document.createElement("button"); rm.type="button"; rm.className="rm";
    rm.textContent="×"; rm.title="remove from session";
    rm.onclick=()=>{ SESSION.rows.splice(i,1); updateSess(); };
    row.appendChild(fn); row.appendChild(sel); row.appendChild(rm);
    sessFiles.appendChild(row);
  });
}
sessStart.onclick=()=>{ SESSION={rows:[]}; sessStart.hidden=true; sessActive.hidden=false; updateSess(); };
sessCancel.onclick=()=>{ if(SESSION&&SESSION.rows.length&&!confirm("Discard this session and its "+SESSION.rows.length+" captured video(s)?"))return;
  SESSION=null; sessActive.hidden=true; sessStart.hidden=false; };
sessEnd.onclick=()=>{
  if(!SESSION||!SESSION.rows.length){ err.textContent="No videos in this session yet — export at least one first."; return; }
  // a blank ig_handle rejects the whole file, so every row needs an account
  const missing=SESSION.rows.filter(r=>!r.ig_handle).length;
  if(missing){ err.textContent=missing+" video"+(missing===1?"":"s")+" still need an account — set each row's dropdown in the list above (or the default account, then re-export).";
    return; }
  downloadCSV();
  SESSION=null; sessActive.hidden=true; sessStart.hidden=false;
};
function sessPain(){ return Array.from($("#sess_pain_points").selectedOptions).slice(0,3).map(o=>o.value).join("|"); }
// shared tags (everything EXCEPT the per-video account, which lives on each row)
function sessDefaults(){ const g=id=>$("#sess_"+id).value;
  return {experiment_id:g("experiment_id")||"solo",
    variant_axis:g("variant_axis"),broll_source:g("broll_source"),audio_source:g("audio_source"),
    cohort:g("cohort"),niche:g("niche"),industry:g("industry"),intent_stage:g("intent_stage"),
    hook_type:g("hook_type"),cta_type:g("cta_type"),pain_points:sessPain()}; }
// called on every successful export; per-video fields (incl. the account, snapshot
// from the current default) captured here, shared tags applied at download
function sessCapture(name){
  if(!SESSION||!name) return;
  const firstLine=(text.value.split("\n").map(s=>s.trim()).filter(Boolean)[0]||"").slice(0,200);
  const row={ filename:name, ig_handle:$("#sess_ig_handle").value||"",
    format: state.layout==="split"?"split_view_demo":"list_view_broll",
    overlay_type: ($("#mode").value==="captions")?"single_caption":"text_list",
    hadMusic: !!$("#music").value, hook_text:firstLine, caption_text:(state.caption||"") };
  const i=SESSION.rows.findIndex(r=>r.filename.toLowerCase()===name.toLowerCase());
  // re-export keeps the account you'd already assigned to that filename
  if(i>=0){ row.ig_handle=SESSION.rows[i].ig_handle||row.ig_handle; SESSION.rows[i]=row; }
  else SESSION.rows.push(row);
  updateSess();
}
function buildCSV(){
  const d=sessDefaults();
  const esc=v=>{ v=(v==null?"":String(v)); return /[",\n\r]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v; };
  const out=SESSION.rows.map((r,i)=>({
    filename:r.filename, ig_handle:r.ig_handle, experiment_id:d.experiment_id,
    variant_id:"v"+(i+1), variant_axis:d.variant_axis, control_flag:"",
    format:r.format, format_subtype:"", broll_source:d.broll_source, demo_surface:"",
    audio_source: d.audio_source!=="auto"?d.audio_source:(r.hadMusic?"trending_sound":"none"),
    overlay_type:r.overlay_type, cohort:d.cohort, niche:d.niche, industry:d.industry,
    intent_stage:d.intent_stage, hook_type:d.hook_type, cta_type:d.cta_type,
    hook_text:r.hook_text, pain_points:d.pain_points, notes:"", caption_text:r.caption_text,
    scheduled_at:"" }));
  return [CSV_COLS.join(",")].concat(out.map(r=>CSV_COLS.map(c=>esc(r[c])).join(","))).join("\r\n");
}
function downloadCSV(){
  const blob=new Blob([buildCSV()],{type:"text/csv;charset=utf-8"});
  const url=URL.createObjectURL(blob), a=document.createElement("a");
  const dt=new Date(), p=n=>String(n).padStart(2,"0");
  a.href=url; a.download="bulk_posts_"+dt.getFullYear()+p(dt.getMonth()+1)+p(dt.getDate())+"_"+p(dt.getHours())+p(dt.getMinutes())+".csv";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1500);
}

// upload
function upload(f){
  if(!f) return;
  err.textContent=""; pvstatus.textContent="uploading…";
  const fd=new FormData(); fd.append("video",f);
  const mb=(f.size/1048576).toFixed(0);
  fetch("/upload",{method:"POST",body:fd}).then(async r=>{
    // A proxy/edge failure (Railway's "upstream error", a 502, an HTML 413)
    // is not JSON. Parsing it blindly throws "Unexpected token" and hides the
    // real status — surface the status and the body instead.
    const raw=await r.text();
    try{ return JSON.parse(raw); }
    catch{
      const body=raw.trim().slice(0,120)||"(empty response)";
      throw new Error(`Upload failed — server returned ${r.status} ${r.statusText}: `
                      +`${body}. (${mb} MB file; this is the host/proxy rejecting or `
                      +`dropping the request, not the app.)`);
    }
  }).then(j=>{ loaded(j); }).catch(e=>{err.textContent=String(e);pvstatus.textContent="";});
}

// a video is ready server-side (however it got there — upload or Drive import)
function loaded(j){
  if(j.error){ err.textContent=j.error; pvstatus.textContent=""; return; }
  state.id=j.id; state.duration=j.duration;
  state.srcW=j.width; state.srcH=j.height;      // the clip's own frame
  // in split the canvas is a fresh 9:16 frame, not the clip's
  const sp=(state.layout==="split");
  state.vw=sp?SPLITC.w:j.width; state.vh=sp?SPLITC.h:j.height;
  state.frameAt=null; state.count=0; state.secs=[];
  layersEl.innerHTML=""; boxesEl.innerHTML="";
  if(state.vw&&state.vh) stage.style.aspectRatio=state.vw+"/"+state.vh;
  sizeStage();
  updatePosBadge();
  scrub.max=Math.max(0.1,j.duration); scrub.value=Math.min(j.duration/3,j.duration);
  timebadge.textContent="preview @ "+(+scrub.value).toFixed(1)+"s";
  initTrim(j.duration);
  // hide the big dropzone; expose "change clip" compactly in the header so
  // the video preview gets the full column height.
  drop.style.display="none";
  const db=$("#drivebox"); if(db) db.style.display="none";
  const hint=$("#hint");
  hint.innerHTML='▸ <b>'+j.name+'</b> · <span style="color:var(--accent)">change clip</span>';
  hint.style.cursor="pointer"; hint.onclick=()=>file.click();
  stagewrap.hidden=false; exportBtn.disabled=false; previewBtn.disabled=false;
  outnamewrap.hidden=false; autoName();
  showStill(); doPreview();
}
drop.onclick=()=>file.click();
file.onchange=e=>upload(e.target.files[0]);
["dragover","dragenter"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.add("hover");}));
["dragleave","drop"].forEach(ev=>drop.addEventListener(ev,e=>{e.preventDefault();drop.classList.remove("hover");}));
drop.addEventListener("drop",e=>upload(e.dataTransfer.files[0]));

// Google Drive import — the server pulls the file, so nothing crosses the
// browser or the host proxy. Long downloads outlive a request, hence polling.
const driveurl=$("#driveurl"), drivego=$("#drivego"), drivenote=$("#drivenote");
function driveImport(){
  const url=driveurl.value.trim();
  if(!url) return;
  err.textContent=""; drivego.disabled=true;
  drivenote.textContent="starting…"; pvstatus.textContent="importing…";
  const fail=m=>{ err.textContent=m; drivego.disabled=false;
                  drivenote.textContent="Share it as “Anyone with the link”.";
                  pvstatus.textContent=""; };
  fetch("/import",{method:"POST",headers:{"Content-Type":"application/json"},
                   body:JSON.stringify({url})})
    .then(r=>r.json()).then(j=>{
      if(j.error) return fail(j.error);
      const poll=()=>fetch("/import/"+j.job).then(r=>r.json()).then(s=>{
        if(s.error) return fail(s.error);
        if(s.done){ drivego.disabled=false; drivenote.textContent="imported ✓";
                    loaded(s); return; }
        const mb=(s.got/1048576).toFixed(0);
        const tot=s.total?(s.total/1048576).toFixed(0):null;
        drivenote.textContent=tot?`downloading ${mb} / ${tot} MB`
                                 :`downloading ${mb} MB`;
        setTimeout(poll,1000);
      }).catch(e=>fail(String(e)));
      poll();
    }).catch(e=>fail(String(e)));
}
drivego.onclick=driveImport;
driveurl.addEventListener("keydown",e=>{ if(e.key==="Enter") driveImport(); });

// export
exportBtn.onclick=async()=>{
  if(!state.id) return;
  err.textContent=""; exportBtn.disabled=true;
  const orig=exportBtn.textContent; exportBtn.innerHTML='<span class="spin"></span>Rendering…';
  try{
    const r=await fetch("/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(opts())});
    const j=await r.json();
    if(j.error){ err.textContent=j.error; }
    else{
      // j.url is unique per render, so the download link can point straight at
      // it — it used to reuse one URL and hand back the cached first export.
      result.src=j.url; result.load();
      dl.href=j.url; dl.download=j.name||"";
      resultwrap.hidden=false;
      resultwrap.scrollIntoView({behavior:"smooth"});
      sessCapture(j.name||"");   // add this export to the posting session (if one's running)
      exportmsg.hidden=false;
      exportmsg.innerHTML="✓ Exported <b>"+(j.name||"")+"</b> — download it below. "+
        "To make another from this same clip, just change the script (or settings) and hit Export again; no need to re-upload.";
    }
  }catch(e){ err.textContent=String(e); }
  exportBtn.disabled=false; exportBtn.textContent=orig;
};

// watch a fast draft (with speed) before the final export
previewBtn.onclick=async()=>{
  if(!state.id) return;
  err.textContent=""; previewBtn.disabled=true;
  const orig=previewBtn.textContent; previewBtn.innerHTML='<span class="spin"></span>Building preview…';
  try{
    const r=await fetch("/preview_video",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(opts())});
    const j=await r.json();
    if(j.error){ err.textContent=j.error; }
    else{
      frame.hidden=true; layersEl.style.display="none"; boxesEl.style.display="none";
      pvid.hidden=false;
      pvid.src=j.url; pvid.load();      // url is unique per render; no bust needed
      pvid.play().catch(()=>{});
      timebadge.textContent="▶ live preview (draft quality, looping)";
    }
  }catch(e){ err.textContent=String(e); }
  previewBtn.disabled=false; previewBtn.textContent=orig;
};
</script>
</body></html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"b-roll editor  ->  http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
