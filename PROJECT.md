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

## Roadmap

### Converter
- [ ] Result fields as primvars (von Mises, plastic strain, displacement) → colour-by-result
      renders in Blender.
- [ ] Exterior-only faces for solids (currently all 6 hex faces → interior duplicates).
- [ ] Part selection by id / name filter (replace the debug-only `--max-parts`).
- [ ] Proper `pip install` package + CLI entry point (`dyna2usd ...`).
- [ ] Round-trip tests on a small public d3plot.

### Blender toolbox
- [ ] Import helper: USD import with the right options, frame range & fps from the stage.
- [ ] Material library: car paint, glass, rubber, metal, plastic — assign by part name
      rules (regex → material), config file per model.
- [ ] Studio deck: cyclorama / infinite floor, HDRI + 3-point lights, camera rigs
      (turntable, impact close-up, side/top), one-click scene build.
- [ ] Beam thickness (geometry nodes / curve bevel) and SPH point radius setup.
- [ ] Optimisation: decimate / hide internal parts, instance static parts, smooth
      shading + auto-smooth, mesh cache (Alembic / USD) streaming for big models.
- [ ] Render presets: Cycles photoreal vs EEVEE preview, denoise, sampling, output paths;
      headless batch render (`blender -b deck.blend -P render.py`).
