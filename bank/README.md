# Overlay clip bank (split-screen top panel)

Drop video files in here. Every clip shows up in the **Top clip** picker when the
editor is in **Split screen** mode, and you cycle through them with `‹` / `›`.

```
┌──────────────────┐
│  a clip from     │  45%   ← from this folder
│  THIS folder     │
├──────────────────┤
│  black + text    │  10%
├──────────────────┤
│  your b-roll     │  45%   ← the clip you upload/import
└──────────────────┘
```

## Rules

- **`.mp4`, `.mov`, `.m4v`, `.webm`, `.mkv`.** The filename becomes the label
  (`subway_surfers.mp4` → "Subway Surfers"), so name them readably.
- **Keep them small — well under 100 MB.** This folder is committed to git so the
  clips exist in production, and GitHub hard-rejects any file over 100 MB. The
  repo's `.gitignore` ignores video everywhere *except* here (`!bank/**`), so a
  clip you drop in this folder will be tracked.
- **Length doesn't matter.** A short clip is looped to cover the b-roll's full
  duration, so a 6-second loop under a 3-minute clip is fine.
- **Aspect doesn't matter.** Each clip is scaled to cover the panel and
  centre-cropped, never letterboxed — but a vertical or squarish source keeps
  more of its subject than a wide one, since the panel is 1080×864.
- **Audio is ignored.** Split exports are silent by design.

## Compressing a clip for the bank

```sh
ffmpeg -i big_clip.mp4 -t 15 -an \
  -vf "scale=1080:864:force_original_aspect_ratio=increase,crop=1080:864" \
  -c:v libx264 -preset slow -crf 26 bank/my_clip.mp4
```

That pre-crops to the panel and strips audio — usually a few MB for 15 seconds,
and it saves the renderer a scale pass.
