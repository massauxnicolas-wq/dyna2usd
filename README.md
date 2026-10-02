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
| `blender/look.py` — look creator: `studio`, `fe` (LS-PrePost style) | **working** |
| Blender toolbox (material library by part rules, camera rigs, batch render) | planned — see `PROJECT.md` |

## Quick start

Requires Python 3.11 with `open-lasso-python`, `usd-core` (`pxr`) and `numpy`.

```bat
pip install lasso-python usd-core numpy

:: full model, all states
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o crash.usdc

:: fast smoke test — states 1-6, first 5 parts
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o t.usdc --states 1:6 --parts "2000*" --exclude "HS*"

:: with result fields for colour-by-result renders
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o crash.usdc --fields von_mises,plastic_strain,displacement,axial_force,axial_work

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

### Switching the result field

Looks colour through a **Dyna Field** geometry-nodes modifier (`blender/field.py`): a menu
picks the field, Min/Max (and Log) normalise it into `fe_value`, which the materials read.
Every modifier is driven by scene props, so one change switches the whole model:

- Properties › Scene › Custom Properties: `fe_field` (0 von_mises, 1 plastic_strain,
  2 displacement, 3 axial_force, 4 axial_work), `fe_min`, `fe_max`, `fe_log`.
- Or, to also recompute the range and relabel the legend, in the Python console:
  `import field; field.set_field(C.scene, "plastic_strain")`

Studio colour-by-result: `look.py --look studio --color-by plastic_strain`.
`axial_work` defaults to a 3-decade log legend (`--scale linear|log` overrides): beam energy
is heavy-tailed (median 0.01, max 2.7e5 on the wiremesh), linear shows it all blue.

### Wire energy proxy

`blender/wire_energy.py --mode strain|velocity` colours wires by a geometry-only estimate
(plastic stretch or peak node speed, max-held in a simulation zone) when the model was
converted without beam results. With `--fields axial_work`, prefer the real energy.

### Looks

```bat
:: FE post-processor look: banded JET9 fringe, legend, grey rigid parts, element lines
blender -b --python blender\look.py -- --usd crash.usdc --look fe --field von_mises --out crash_fe.blend --render fe.png

:: photoreal studio: cyclorama, 3 area lights, satin part colours, metal rigid parts/beams
blender -b --python blender\look.py -- --usd crash.usdc --look studio --out crash_studio.blend --render studio.png

:: or apply a look to an already-imported .blend
blender -b crash.blend --python blender\look.py -- --look fe --field plastic_strain --out crash_fe.blend
```

Useful options: `--view iso|iso2|+x|-x|+y|-y|top`, `--fit all|frame` (whole animation vs
the rendered frame only), `--legend-max` / `--legend-pct` (default: 99.5th percentile over
the animation, rounded to a clean number), `--lines auto|on|off`, `--res`, `--samples`,
`--engine cycles|eevee`, `--radius` (beam mm). `--help` lists all. New looks: write
`build_<name>(ctx, args)` and add it to `LOOKS`.

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
