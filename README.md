# dyna2usd

LS-DYNA `d3plot` results → animated **OpenUSD** → **Blender** studio / photoreal renders.

A small library of tools to turn crash & impact simulations into render-ready scenes:
exact geometry, per-part grouping, per-vertex deformation over time, element erosion,
beams and SPH — then a Blender toolbox to light, shade, optimise and render them.

## Status

| Piece | State |
|-------|-------|
| `converter/d3plot_to_usd_lasso.py` — d3plot → animated USD | **working** |
| `blender/dyna_import.py` — import + beam radius node group | **working** |
| Blender toolbox (materials, studio deck, render presets) | planned — see `PROJECT.md` |

## Quick start

Requires Python 3.11 with `open-lasso-python`, `usd-core` (`pxr`) and `numpy`.

```bat
pip install lasso-python usd-core numpy

:: full model, all states
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o crash.usdc

:: fast smoke test — states 1-6, first 5 parts
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o t.usdc --states 1:6 --max-parts 5

:: with result fields for colour-by-result renders
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o crash.usdc --fields von_mises,plastic_strain,displacement

:: full-restart chain merged on one timeline
py -3.11 converter\d3plot_to_usd_lasso.py run\d3plot restart\d3plot -o full.usdc

:: self-check (no args)
py -3.11 converter\d3plot_to_usd_lasso.py
```

Then into Blender 5.1+:

```bat
blender --python blender\dyna_import.py -- crash.usdc [radius_mm]
```

Imports the stage (frame range from the file) and sets up the beams:

- **Dyna Beam** modifier — `Radius` (mm) drives the curve radius. Native curves render
  as round tubes in Cycles and EEVEE at zero triangle cost. Change all beams at once:
  select them, Alt+Enter on Radius.
- **Curve to Tube** (Blender's built-in essentials modifier, Scale = Radius) — off by
  default; enable it for real tube geometry (caps, UVs, custom profile).
- Scene curve display set so radius shows everywhere: EEVEE/viewport `Strip`
  (Blender's default `Strand` draws fixed thin lines and ignores radius), Cycles
  `3D Curves`.
- `--fields` results arrive as attributes (`von_mises`, `plastic_strain` per face,
  `displacement` per point) for an Attribute node → Color Ramp material.
- Motion blur works out of the box in Cycles (Blender samples the USD between frames).

**Playback tip:** use the Vulkan backend (Preferences → System → Backend), measured
+25–70 % viewport fps on deforming crash meshes.

Full conversion doc (output layout, erosion, beams, SPH, restarts): [`converter/README.md`](converter/README.md).

## Layout

```
converter/   d3plot → animated USD (the core)
blender/     Blender-side scripts (import, beams)
data/        local test results & outputs (not versioned)
PROJECT.md   goal, decisions, roadmap
```

## License

MIT — see `LICENSE`.
