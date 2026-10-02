# Project: dyna2usd

## Goal
Build a library of rendering tools that take **LS-DYNA results** to **Blender** for
**studio / photoreal** renders, with OpenUSD as the interchange:

```
d3plot ──converter──► animated .usdc ──► Blender import ──► toolbox (look-dev, optimise, render deck) ──► frames / video
```

1. **Converter** (done, hardening) — d3plot → animated USD, exact geometry, erosion.
2. **Blender toolbox** (next) — scripts / addon that make a converted crash render-ready
   with minimal manual work.

## Decisions so far
- **lasso-python, not DPF.** Reads the d3plot directly, incl. per-state element-deletion
  flags. No DPF server, no Ansys licence, no version gate.
- **USD as the hand-off.** One `.usdc`, Z-up, mm→m baked as `scaleOp(1e-3)` on `/sim`
  so the whole model moves/scales as one in Blender. Parts named from LS-DYNA part
  titles, deterministic per-part colours, `UsdPreviewSurface` materials, studio lights.
- **Typed parent prims** (`/sim/beams`, `/sim/sph` as `Xform`): Blender's USD importer
  drops typeless prims, which would lose the 1e-3 scale.
- **Dropped:** web viewer (Needle WASM) and Gaussian-splat experiments — off-goal,
  removed from the repo.

## Benchmarks (2026-10-02, 16 cores, RTX A4000, Blender 5.1.2)

| | car (1.25M tris, 929 parts, 5.6k beams) | wiremesh (570k tris, 1.56M beams) |
|---|---|---|
| Convert (50 states) | 85 s → **18 s** (in-memory save) | 61 s → **40 s** (+ beam chains) |
| Output size | 0.35 GB | 1.95 → **1.01 GB** |
| Peak RAM (convert) | 5.9 GB | 14.6 GB (lasso loads all states) |
| Blender import | 0.8 s | 0.1 s |
| Blender eval / frame | 42 ms | 13 ms (native beams) |
| Viewport playback OpenGL → Vulkan | 4.2 → 5.3 fps | 11.1 → 19.0 fps |

Playback is limited by Blender redrawing deforming meshes, not by USD: plain spheres
with a Displace modifier (930 obj, 1.2M tris) play at the same ~5 fps. Merging parts
into one mesh made eval single-threaded (171 ms) — no gain, rejected.

## Roadmap

### Converter
- [x] Result fields as primvars (`--fields von_mises,plastic_strain,displacement`).
- [x] Beam results per wire node: `von_mises`, `plastic_strain`, `axial_force`,
      `axial_work` (real absorbed energy, cumulative F·dL).
- [ ] Bending/torsion work for beams (needs moments + rotations).
- [x] Blender: colour-by-result (`--color-by`), switchable field (Dyna Field GN modifier
      driven by scene props), fixed or percentile legend range, log scale.
- [x] Exterior-only faces for solids (shared hex faces dropped unless an owner erodes).
- [x] Part selection by id / name pattern: `--parts`, `--exclude` (replaced `--max-parts`).
- [ ] Proper `pip install` package + CLI entry point (`dyna2usd ...`).
- [ ] Round-trip tests on a small public d3plot.

### Blender toolbox
- [x] Import helper (`blender/dyna_import.py`): USD import, frame range from the stage.
- [ ] Material library: car paint, glass, rubber, metal, plastic — assign by part name
      rules (regex → material), config file per model.
- [x] Look creator (`blender/look.py --look studio|fe`): cyclorama + 3 area lights +
      satin/metal materials; FE look (JET9 fringe, headlight+AO shade, element lines,
      grey rigid parts, studio gradient, stepped legend). Data-driven: rigid parts,
      framing, legend max, line radius.
- [ ] FE look, film features: FE_Switch beauty↔FE wipe, BG_Mask, HUD scene (legend with
      DOF), fe_grey per-object fade, live value lines.
- [ ] Camera rigs: turntable, impact close-up; HDRI option for studio.
- [x] Beam radius: "Dyna Beam" node group + built-in Curve to Tube (off by default);
      scene curve display fixed so radius shows in EEVEE/viewport.
- [x] Wire energy proxy (`blender/wire_energy.py`, strain / velocity) for models without
      beam results.
- [ ] SPH point radius setup.
- [ ] Optimisation: decimate / hide internal parts, instance static parts, smooth
      shading + auto-smooth, mesh cache (Alembic / USD) streaming for big models.
- [ ] Render presets: Cycles photoreal vs EEVEE preview, denoise, sampling, output paths;
      headless batch render (`blender -b deck.blend -P render.py`).
