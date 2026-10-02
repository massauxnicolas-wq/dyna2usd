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
- [ ] Result fields as primvars (von Mises, plastic strain, displacement) → colour-by-result
      renders in Blender.
- [ ] Exterior-only faces for solids (currently all 6 hex faces → interior duplicates).
- [ ] Part selection by id / name filter (replace the debug-only `--max-parts`).
- [ ] Proper `pip install` package + CLI entry point (`dyna2usd ...`).
- [ ] Round-trip tests on a small public d3plot.

### Blender toolbox
- [x] Import helper (`blender/dyna_import.py`): USD import, frame range from the stage.
- [ ] Material library: car paint, glass, rubber, metal, plastic — assign by part name
      rules (regex → material), config file per model.
- [ ] Studio deck: cyclorama / infinite floor, HDRI + 3-point lights, camera rigs
      (turntable, impact close-up, side/top), one-click scene build.
- [x] Beam radius: "Dyna Beam" node group, native curves or tube mesh.
- [ ] SPH point radius setup.
- [ ] Optimisation: decimate / hide internal parts, instance static parts, smooth
      shading + auto-smooth, mesh cache (Alembic / USD) streaming for big models.
- [ ] Render presets: Cycles photoreal vs EEVEE preview, denoise, sampling, output paths;
      headless batch render (`blender -b deck.blend -P render.py`).
