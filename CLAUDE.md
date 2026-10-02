# CLAUDE.md — dyna2usd

LS-DYNA d3plot → animated OpenUSD → Blender studio/photoreal rendering toolbox.
Read `PROJECT.md` for goal, decisions, roadmap; `converter/README.md` for the
converter internals.

## Environment — READ FIRST
- **Always run Python with `py -3.11`.** Only 3.11 has `pxr` (OpenUSD), `lasso`,
  `numpy`. System `python` (3.13) has none and will fail.
- Blender scripts run inside Blender's own Python (`bpy`), not `py -3.11`.

## Layout
`converter/` (d3plot → animated USD, the core) · `data/` (14 GB test results, **never
commit**, gitignored).

## Commands
```
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o out.usdc
py -3.11 converter\d3plot_to_usd_lasso.py                 # self-check, must print "selfcheck ok"
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
