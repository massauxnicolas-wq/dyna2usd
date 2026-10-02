# Project: dyna2usd

## Goal
Build a library of rendering tools that take **LS-DYNA results** to **Blender** for
**studio / photoreal / FE-style** renders, with OpenUSD as the interchange:

```
d3plot ──converter──► animated .usdc (+ result fields) ──► Blender import ──► look (studio | fe) ──► frames / video
```

1. **Converter** (working, hardening) — d3plot → animated USD: exact geometry, erosion,
   beams, SPH, result fields.
2. **Blender toolbox** (in progress) — import, looks, switchable fields; next: material
   library, camera rigs, batch render.

## Decisions so far
- **lasso-python, not DPF.** Reads the d3plot directly, incl. per-state element-deletion
  flags. No DPF server, no Ansys licence, no version gate.
- **USD as the hand-off.** One `.usdc`, Z-up, mm→m baked as `scaleOp(1e-3)` on `/sim`
  so the whole model moves/scales as one in Blender. Parts named from LS-DYNA part
  titles, deterministic per-part colours, `UsdPreviewSurface` materials.
- **Typed parent prims** (`/sim/beams`, `/sim/sph` as `Xform`): Blender's USD importer
  drops typeless prims, which would lose the 1e-3 scale.
- **Author in memory, Export once:** `CreateNew`+`Save` was 40× slower, same file.
- **Beams as polylines** joined on shared node ids (exact, no distance threshold):
  halves the wiremesh file and frame writing.
- **Exterior solid faces only; `subdivisionScheme = none`** (unset = catmullClark in
  other USD tools).
- **No `velocities`:** tested, Cycles motion blur is identical without them.
- **Merging parts into one mesh: rejected** — Blender evaluates it single-threaded
  (171 ms vs 42 ms per frame), no playback gain.
- **Fields go through one GN modifier** (`fe_value`), driven by scene props, so any look
  can switch field without rebuilding materials.
- **Rigid = edge lengths unchanged to 1e-5** (LS-DYNA rigid parts are exactly 0; elastic
  parts must keep their fringe). Legend max = clean-rounded 99.5th percentile; log scale
  for heavy-tailed energy.
- **Dropped:** web viewer (Needle WASM) and Gaussian-splat experiments — off-goal,
  removed from the repo.

## Benchmarks (2026-10-02, 16 cores, RTX A4000, Blender 5.1.2)

| | car (1.25M tris, 929 parts, 5.6k beams) | wiremesh (570k tris, 1.56M beams) |
|---|---|---|
| Convert (50 states), no fields | 85 s → **18 s** (in-memory save) | 61 s → **40 s** (+ beam chains) |
| Convert, all 5 fields | 34 s | 83 s |
| Output size, no fields | 0.35 GB | 1.95 → **1.01 GB** |
| Output size, all 5 fields | 0.60 GB | 2.36 GB |
| Peak RAM (convert, no fields) | 5.9 GB | 14.6 GB (lasso loads all states) |
| Blender import | 0.8 s | 0.1 s |
| Blender eval / frame | 42 ms | 13 ms (native beams) |
| Viewport playback OpenGL → Vulkan | 4.2 → 5.3 fps | 11.1 → 19.0 fps |
| look.py build + 960×540 still (Cycles, GPU) | ~10 s | ~8 s |
| 4K still, 128 samples (wire energy) | — | 29 s |

Playback is limited by Blender redrawing deforming meshes, not by USD: plain spheres
with a Displace modifier (930 obj, 1.2M tris) play at the same ~5 fps. The first GPU
render on a machine takes minutes (kernel compile), later ones seconds.

## Roadmap

### Converter
- [x] Result fields as primvars: `von_mises`, `plastic_strain`, `displacement`.
- [x] Beam results per wire node: `von_mises`, `plastic_strain`, `axial_force`,
      `axial_work` (real absorbed energy, cumulative F·dL).
- [x] Exterior-only faces for solids (shared hex faces dropped unless an owner erodes).
- [x] Part selection by id / name pattern: `--parts`, `--exclude` (replaced `--max-parts`).
- [ ] Bending/torsion work for beams (needs moments + rotations).
- [ ] Mid-surface / outer-fibre choice for shell results (now max over layers).
- [ ] Proper `pip install` package + CLI entry point (`dyna2usd ...`).
- [ ] Round-trip tests on a small public d3plot.

### Blender toolbox
- [x] Import helper (`blender/dyna_import.py`): USD import, frame range from the stage.
- [x] Beam radius: "Dyna Beam" node group + built-in Curve to Tube (off by default);
      scene curve display fixed so radius shows in EEVEE/viewport.
- [x] Look creator (`blender/look.py --look studio|fe`): cyclorama + 3 area lights +
      satin/metal materials; FE look (JET9 fringe, headlight+AO shade, element lines,
      grey rigid parts, studio gradient, stepped legend). Data-driven: rigid parts,
      framing, legend range, line radius.
- [x] Colour-by-result (`--color-by`), switchable field (Dyna Field GN modifier driven by
      scene props), fixed or percentile legend range, log scale.
- [x] Wire energy proxy (`blender/wire_energy.py`, strain / velocity) for models without
      beam results.
- [ ] Legend placement that never covers the model (frame the subject in the free area).
- [ ] FE look, film features: FE_Switch beauty↔FE wipe, BG_Mask, HUD scene (legend with
      DOF), fe_grey per-object fade, live value lines.
- [ ] Material library: car paint, glass, rubber, metal, plastic — assign by part name
      rules (regex → material), config file per model.
- [ ] Camera rigs: turntable, impact close-up; HDRI option for studio.
- [ ] SPH point radius setup.
- [ ] Optimisation: decimate / hide internal parts, instance static parts.
- [ ] Render presets + headless batch render of animations (`blender -b deck.blend -P render.py`).
