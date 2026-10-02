# CLAUDE.md — dyna2usd

LS-DYNA d3plot → animated OpenUSD → Blender studio/photoreal rendering toolbox.
Read `PROJECT.md` for goal, decisions, roadmap; `converter/README.md` for the
converter internals.

## Environment — READ FIRST
- **Always run Python with `py -3.11`.** Only 3.11 has `pxr` (OpenUSD), `lasso`,
  `numpy`. System `python` (3.13) has none and will fail.
- Blender scripts run inside Blender's own Python (`bpy`), not `py -3.11`.

## Layout
`converter/` (d3plot → animated USD, the core) · `blender/` (bpy scripts, Blender 5.1) · `data/` (14 GB test results, **never
commit**, gitignored).

## Commands
```
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o out.usdc
py -3.11 converter\d3plot_to_usd_lasso.py                 # self-check, must print "selfcheck ok"
"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe" -b --factory-startup --python blender\dyna_import.py   # "blender selfcheck ok"
```
Test data: `data\d3plot_car\d3plot`, `data\d3plot_wiremesh\d3plot`.

## Rules — do NOT break
- `node_displacement` in lasso = **absolute coords per state**, not a delta. Never add
  `node_coordinates` back.
- Keep `/sim/beams`, `/sim/sph` explicitly typed `Xform` — Blender drops typeless
  prims and they'd lose the 1e-3 scale.
- mm→m stays a `scaleOp(1e-3)` on `/sim`; `metersPerUnit` stays 1.0.
- Part colours seeded by part key — must stay stable across runs/restart families.
- Erosion: skip (with a printed note) on shape mismatch, never guess the mapping.
- Run the self-check after any converter change.
- Blender curves: scene `render.hair_type` must be `STRIP` — the default `STRAND`
  makes EEVEE/viewport ignore radius (Cycles still honours it). Tubes come from the
  essentials "Curve to Tube" group with Scale 1 (its default 0.1 shrinks them).
- Don't author USD `velocities`: tested, Cycles blur is identical without them
  (Blender samples the cache between time samples).
- `blender/look.py`: rigid = edge lengths unchanged to 1e-5 (LS-DYNA rigid parts are
  exactly 0; elastic car parts are 1e-4..1e-2 and must keep their fringe). Framing uses
  deforming parts only (sim grounds/rails can be 50-100 m). FE legend max = clean-rounded
  99.5th percentile: the true max (one hot element) turns the whole fringe blue.
- Check every look change by rendering a 960x540 still and looking at it.
- Viewport playback is bound by Blender redrawing deforming meshes (~200 ns/tri), not
  by USD reading. Merging parts into one mesh was tested and is slower (single-thread
  eval) — don't retry it.
