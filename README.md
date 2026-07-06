# b-roll text overlay

Burn text onto a video in the **TikTok Sans** font. You write your text in a
markdown file; the tool centers it on your b-roll and writes a new video —
the "tech stack list" / centered-block look.

## Requirements

- **ffmpeg** (installed ✅)
- **Python 3** (installed ✅) — run with `py`
- **Flask** (installed ✅) — only needed for the web UI

## Editor (easiest)

```
py app.py
```

Open **http://localhost:5000**. Drag in a b-roll clip once — then everything is
live: as you change any control, a preview **frame** re-renders instantly. Click
**▶ Preview (watch it)** to render a fast draft and watch the clip in motion
(with speed applied) before committing. Hit **Export video** for the final
full-quality render (saved to `output/`).

Controls:
- **Text** — type/paste, or load one of the scripts (dropdown)
- **Layout** — centered block, or per-line captions
- **Font** — TikTok Sans, Poppins, Bebas Neue, Anton
- **Position** — center / bottom / top (auto by layout)
- **Speed** — 0.25×–3× (slows/speeds the b-roll; audio pitch-corrected)
- **Font size** — Auto (auto-fits the block) or a fixed size
- **Line spacing** — tighter/looser
- **Outline** — thickness + color
- **Text color**, **UPPERCASE**, **Shadow**
- **Preview scrubber** — pick which frame of the clip to preview on

**The 100 scripts:** the dropdown above the text box loads any script in
`texts.md`. Pick `#5 — No-code app` and its on-screen text fills the box; its
CAPTION + HASHTAGS show underneath for pasting into the post. Edit `texts.md`
(same `### N — Title` + fenced-block format) and the dropdown updates on refresh.

## Command line

## Quick start

1. Edit `captions.md` — put whatever lines you want (see the example inside).
2. Run it on your b-roll:

   ```
   py overlay.py path\to\your_broll.mp4
   ```

3. Result lands in `output\your_broll_captioned.mp4`.

## Two modes

### `block` (default) — centered text block
The whole file (minus `#` comment lines) is centered on the video for the full
duration. This is the reference look.

- Each line in the file = one line on screen.
- **Blank lines = vertical spacing** on screen (great for separating a list
  from a closing line).
- **Auto-fit:** the font shrinks automatically so the whole block always fits.
  Add more lines and it scales down; use fewer and it scales up.

### `captions` — one caption per line
```
py overlay.py clip.mp4 --mode captions
```
- Each line is its own caption at the bottom.
- One untimed line → shows the whole video.
- Several untimed lines → split evenly across the length.
- Add timestamps to control timing exactly:
  ```
  [0:00-0:03] first hook
  [0:03-0:07] the payoff
  ```

## Options

```
py overlay.py INPUT.mp4 [options]

  -t, --text FILE      text file (default: captions.md)
  -o, --output FILE    output path (default: output/<name>_captioned.mp4)
  --mode block|captions          (default: block)
  --position center|bottom|top   (default: center for block, bottom for captions)
  --size N             font size px (default: auto — with auto-fit in block mode)
  --color COLOR        text color (default: white)
  --outline COLOR      outline color (default: black)
  --border N           outline thickness px (default: auto)
  --spacing F          line-spacing factor of font size (default: -0.34; more
                       negative = tighter lines)
  --wrap N             max characters per line before wrapping
  --upper              force UPPERCASE
  --shadow-alpha F     shadow opacity 0-1 (default 0.55)
  --shadow-off N       shadow offset px (default 3)
  --no-shadow          turn the shadow off
  --font FILE          use a different .ttf
```

### Examples

```
# default centered list from captions.md
py overlay.py clip.mp4

# a different text file, custom output name
py overlay.py clip.mp4 -t stack.md -o output/final.mp4

# tighter lines, bigger shadow
py overlay.py clip.mp4 --spacing -0.4 --shadow-off 5

# bottom captions instead of a centered block
py overlay.py clip.mp4 --mode captions
```

## Fonts

- `fonts/TikTokSans.ttf` — official TikTok Sans variable font (Copyright TikTok
  Inc., open source).
- `fonts/TikTokSans-Bold.ttf` — bold static instance baked from it; this is what
  the tool renders with (ffmpeg can't set the weight axis on a variable font).

## Notes

- Works with any resolution / aspect ratio; sizes and positions scale to the video.
- Audio is copied through untouched; only the video is re-encoded (H.264, CRF 18).
- `expansion=none` is set internally so characters like `$`, `%`, `{` render
  literally instead of being interpreted by ffmpeg.
```
