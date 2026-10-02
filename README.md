# dyna2usd

LS-DYNA `d3plot` results → animated **OpenUSD** → **Blender** studio / photoreal renders.

A small library of tools to turn crash & impact simulations into render-ready scenes:
exact geometry, per-part grouping, per-vertex deformation over time, element erosion,
beams and SPH — then a Blender toolbox to light, shade, optimise and render them.

## Status

| Piece | State |
|-------|-------|
| `converter/d3plot_to_usd_lasso.py` — d3plot → animated USD | **working** |
| Blender toolbox (scene setup, materials, optimisation, render decks) | planned — see `PROJECT.md` |

## Quick start

Requires Python 3.11 with `open-lasso-python`, `usd-core` (`pxr`) and `numpy`.

```bat
pip install lasso-python usd-core numpy

:: full model, all states
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o crash.usdc

:: fast smoke test — states 1-6, first 5 parts
py -3.11 converter\d3plot_to_usd_lasso.py <path\d3plot> -o t.usdc --states 1:6 --max-parts 5

:: full-restart chain merged on one timeline
py -3.11 converter\d3plot_to_usd_lasso.py run\d3plot restart\d3plot -o full.usdc

:: self-check (no args)
py -3.11 converter\d3plot_to_usd_lasso.py
```

Then in Blender: **File → Import → Universal Scene Description** and play the timeline.

Full conversion doc (output layout, erosion, beams, SPH, restarts): [`converter/README.md`](converter/README.md).

## Layout

```
converter/   d3plot → animated USD (the core)
data/        local test results & outputs (not versioned)
PROJECT.md   goal, decisions, roadmap
```

## License

MIT — see `LICENSE`.
