# `d3plot_to_usd_lasso.py` — LS-DYNA d3plot → animated OpenUSD

The **core of the project**. Reads an LS-DYNA `d3plot` result and writes a single
animated `.usdc` (or `.usda`) stage: exact geometry, per-part grouping, per-vertex
deformation over time, and element erosion (failed elements disappearing mid-crash).

Everything downstream — the Blender toolbox and renders — consumes
what this script produces. If the USD is wrong, everything after it is wrong. Test
this first.

---

## 1. Quick start

```bat
py -3.11 converter\d3plot_to_usd_lasso.py <path-to-d3plot> -o out.usdc
```

`<path-to-d3plot>` is the **first** state file of a result family (the file literally
named `d3plot`, sitting next to `d3plot01`, `d3plot02`, …). lasso globs the rest.

```bat
:: full model, all states
py -3.11 converter\d3plot_to_usd_lasso.py resultats\d3plot -o yaris.usdc

:: fast smoke test — first 5 states, first 5 parts
py -3.11 converter\d3plot_to_usd_lasso.py resultats\d3plot -o t.usdc --states 1:6 --parts "Hood*,2000*"

:: full-restart chain — base run then restart(s), in order, on one timeline
py -3.11 converter\d3plot_to_usd_lasso.py resultats\d3plot restart\d3plotac_base -o full.usdc
```

> **Always `py -3.11`.** Only the 3.11 interpreter has `pxr` (OpenUSD), `lasso`, and
> `numpy` installed. System `python` (3.13) has none and will fail on import.

### Full restarts (multiple families)

An LS-DYNA **full restart** writes a *new* d3plot family (its own base file + states),
which lasso loads as an independent d3plot — the same way PrePost superimposes the runs
into one longer animation (e.g. 140 + 198 = 338 states). Pass the families as **multiple
positional arguments, in restart order**. Each family is built under its own
`/sim/run{i}` group and occupies a consecutive slice of the shared timeline; its
**visibility is time-sampled** so only the active run shows at any frame — a hard,
seamless swap at each boundary (no color flicker, since part colours are seeded by part
id). A single family (one argument) is built flat under `/sim` exactly as before.

---

## 2. CLI

| Argument | Default | Meaning |
|----------|---------|---------|
| `d3plot` (positional) | — | One or more d3plot family base files, **in restart order**. One = flat under `/sim`; several = a restart chain merged onto one timeline (`/sim/run0`, `/sim/run1`, …). No args = run the self-check. |
| `-o`, `--out` | `out_lasso.usdc` | Output path. `.usdc` = binary (fast, compact); `.usda` = ASCII (readable, huge). Extension decides the format. **Overwritten if it exists.** |
| `--states` | all | 1-based **inclusive** slice `LO:HI`, e.g. `1:6` = states 1‑6. Open ends allowed (`:10`, `3:`). Applied **per family**; each family's slice is appended to the shared timeline. |
| `--parts` | all | Comma list of part ids or title patterns (`fnmatch`, case-insensitive) to keep, e.g. `2000*,Hood,1001`. Applies to meshes, beams and SPH. |
| `--exclude` | none | Same syntax; parts to drop (applied after `--parts`). E.g. `--exclude "HS*"` drops the barrier. |
| `--fps` | `24.0` | Sets both `timeCodesPerSecond` and `framesPerSecond` on the stage. One state → one frame → one time code. |
| `--fields` | none | Comma list of result primvars, time-sampled per frame. Shells/solids: `von_mises`, `plastic_strain` (per face, `uniform`, max over integration layers). All geometry: `displacement` (per vertex, magnitude from the first authored state). Beams (per wire node, from the 1-2 touching segments): `von_mises` (√(σ² + 3τ²), max over integration points), `plastic_strain`, `axial_force` (mean, signed) and `axial_work` = real absorbed energy, Σ ½(Fₛ+Fₛ₋₁)(Lₛ−Lₛ₋₁) over every state. Blender imports them as attributes; `blender/field.py` switches between them. Each beam field adds ~0.3 GB on the 1.5M-beam wiremesh. |

---

## 3. Why lasso (not DPF)

There are two converters in this project's history. This one (`_lasso`) is the kept
path. It reads geometry, part grouping, deformation, and — the reason it exists —
the solver's **own per-state element-deletion flags** directly from the d3plot via
[`open-lasso-python`](https://github.com/open-lasso-python/lasso-python).

- **No DPF server**, no Ansys licensing, no version gate. The DPF variant needed
  DPF **2024 R2+** for its `erosion_flag`; this reads the raw header flags instead.
- Erosion is gated on `header.has_element_deletion_data` / `has_node_deletion_data`.
  When present, `element_<type>_is_alive[state, elem] == 0` marks an eroded element
  and its faces drop out of that frame's topology.

---

## 4. What the output looks like

A single stage, Z-up, seconds-based timeline:

```
/sim                        Xform  ← the one handle. Move/scale the whole model here.
│                                     Carries AddScaleOp(1e-3) = mm → m.
├── /sim/<PartTitle>        Mesh   ← one per part. Shells + solids + tshells.
│                                     points (+ extent) time-sampled every frame.
│                                     Eroding parts also time-sample topology.
│                                     subdivisionScheme = none (unset = catmullClark:
│                                     usdview/Karma/Omniverse would subdivide).
│                                     primvars:von_mises|plastic_strain|displacement
│                                     with --fields.
├── /sim/beams              Xform  ← typed on purpose (see §6, "beams").
│   └── /sim/beams/<PartTitle>  BasisCurves (linear)  ← beams joined into polylines.
├── /sim/sph                Xform  ← typed on purpose (see §6, "beams").
│   └── /sim/sph/<PartTitle>   Points  ← SPH particles. points + widths time-sampled.
├── /sim/materials/mat_<pid>   Material  ← UsdPreviewSurface, matte plastic.
└── /sim/lights/{dome,key,fill,rim}  ← DomeLight + 3 DistantLights (studio setup).
```

**Restart chains** nest one level deeper — each family gets a group whose `visibility`
is time-sampled to its window (materials move under the group too):

```
/sim
├── /sim/run0        Xform  ← visible frames [0, n0)     (contains <PartTitle> / beams / sph / materials)
└── /sim/run1        Xform  ← visible frames [n0, n0+n1)  …one per family
```

Stage metadata authored: `upAxis = Z`, `metersPerUnit = 1.0`, `startTimeCode = 0`,
`endTimeCode = total_frames-1` (sum over families), `timeCodesPerSecond =
framesPerSecond = fps`, `defaultPrim = /sim`.

**Part names:** prims are named from the LS-DYNA **part titles** (`part_titles`),
sanitised to valid USD identifiers (`Tf.MakeValidIdentifier`). A missing/blank title
falls back to `part_<pid>`; a sanitisation collision gets a `_<pid>` suffix.

**Scale note:** `metersPerUnit` is left at `1.0` and the mm→m conversion is baked as a
`scaleOp(1e-3)` on `/sim` instead — so grabbing `/sim` in Blender scales the whole
model as one component, and importers that ignore `metersPerUnit` still get the right
size.

---

## 5. The pipeline, step by step

`convert()` runs in five phases; each prints a `[t]` timing line so you can see where
a large model spends its time.

### 5.1 Load & read arrays
`D3plot(path)` memory-maps the family. Positions come from
`ArrayType.node_displacement`, which in lasso holds **current absolute coordinates
per state** — verified `node_displacement[0] == node_coordinates`. It is **not** a
delta; adding `node_coordinates` back would double-count and make rotating parts
visibly pulse. Shape: `(n_states, n_nodes, 3)`.

### 5.2 Build faces per part
Three face-producing element types are processed:

| Element | Face rule |
|---------|-----------|
| **shell** (`poly`) | one polygon — the element's own connectivity, deduped. |
| **solid** (`hex`) | six quad faces from the hex8 face table `_HEX`, then interior ones removed. |
| **tshell** (`hex`) | same six-quad hex treatment. |

After `--parts` / `--exclude` selection, `interior_faces` drops every hex face shared by
two elements (same node set) — unless one of its owners ever erodes, since the face then
becomes exterior mid-run.

Each face is a list of **global** node indices, filed under its part id, and paired
with its `(element-type, element-index)` origin so erosion can find it later.

**Degenerate elements** (a tri stored as `[a,b,c,c]`, a tet stored as a hex8 with
repeated corners) are collapsed by `dedup()` — an order-preserving unique — and any
face left with fewer than 3 distinct nodes is dropped.

### 5.3 Erosion arrays
For each element type, if the file has deletion data **and** the `is_alive` array's
element axis matches the connectivity count 1:1, an `(n_states, n_elem)` boolean
`alive` mask is kept. If shapes don't line up, that type's erosion is **skipped with
a printed note** rather than guessed — a mis-mapped mask would delete the wrong
elements. A part "erodes" only if at least one of its elements ever dies.

### 5.4 Author prims (topology at default time)
Per part: remap the part's used global node ids to a **local** 0..k index (`g2l`),
build `faceVertexCounts` + `faceVertexIndices`, define the `Mesh`, give it a seeded
random color (`Random(42)` → same part, same color across runs), a matte
`UsdPreviewSurface` material, and `doubleSided = true`.

- **Non-eroding parts** author topology **once** at default time — it never changes,
  so only `points` need time samples. Smaller file, faster playback.
- **Eroding parts** leave topology empty at default time and author it **per frame**
  (§5.5), because which faces exist changes as elements die.

### 5.5 Time samples (the animation)
For each selected state → frame:

- **Meshes:** `points` = that state's coordinates for the part's used nodes, plus a
  per-frame `extent` (local bbox). Eroding parts additionally rebuild
  `faceVertexCounts`/`faceVertexIndices` from the live faces only (with an assert that
  counts and indices stay consistent).
- **Beams:** segments sharing a node are joined once into polylines (`beam_chains`:
  walk from every end/junction node, then closed loops) — an exact topological merge
  on node ids, no distance threshold. Each frame lists the chain nodes' positions.
  Eroding parts re-cut the chains per frame (`live_curves`): a dead segment splits its
  curve, 1-point leftovers drop. Wiremesh: 1.56M beams → 10k curves, file 1.95 → 1.01
  GB, frame writing 32 → 15 s.
- **Fields** (`--fields`): meshes get `uniform` (per face) primvars from their element
  (`von_mises`, `plastic_strain`, filtered by the same erosion mask as the faces) and
  `vertex` `displacement`. Beams compute per segment, then put the value on chain nodes
  with `seg_to_nodes` (max of the 1-2 touching segments; mean for the signed
  `axial_force` / `axial_work`). `axial_work` is precomputed once over **all** states:
  Σ ½(Fₛ+Fₛ₋₁)(Lₛ−Lₛ₋₁) per beam (`axial_work()`), so a `--states` slice still shows
  the energy absorbed since t = 0.

### 5.6 Save
The stage is authored **in memory** (`Usd.Stage.CreateInMemory()`) and written once
with `stage.Export(out)`. `CreateNew` + `Save()` produced the identical file but was
~40× slower on many-prim stages (car: 66 s → 1.6 s).

---

## 6. Design decisions & known shortcuts (the `ponytail:` markers)

These are deliberate, marked in-code, and worth knowing before you trust or extend
the output:

- **Exterior faces only for solids** (`interior_faces`): a hex face shared by two
  elements is dropped (car: 105k faces), unless one owner erodes, then it is kept so the
  face appears when the neighbour dies (shown with its own element's erosion).
- **Beams are typed under `/sim/beams`** via an explicit `Xform.Define`. A plain
  `Define()` would create the parent typeless, and Blender's importer **drops typeless
  prims** — the curves would then lose `/sim`'s 1e-3 scale and come in 1000× too big.
- **Beam widths** are a constant `1.0` (= 1 mm pre-scale) primvar, overridden in
  Blender by the "Dyna Beam" node group's Radius (`blender/dyna_import.py`).
- **Result fields reduce integration points by max** (`elem_field`, `beam_field`) — the
  usual fringe-plot choice; mid-surface / outer-fibre selection is the upgrade path.
  Solid faces take their element's value. Beam von Mises = √(σ_axial² + 3(τ₁² + τ₂²))
  per integration point. `axial_work` is axial only (bending/torsion work would need
  beam moments and rotations); elastic unloading returns work, so it can dip.
- **No velocities authored, on purpose.** Tested: Cycles motion blur on the imported
  cache comes from Blender sampling the USD between time samples, and is identical
  with or without a `velocities` attribute — authoring them only doubled the data.
- **Light intensities** are eyeballed for usdview/Blender and may need tuning per
  renderer.
- **Beam orientation nodes ignored** — only the two end nodes (cols 0,1) of each beam
  are used; the third orientation node is dropped (curves don't need it).
- **SPH particles → `UsdGeom.Points`**, grouped by `sph_node_material_index` (treated
  as a 0-based part index like element part indexes — the suspect if SPH parts look
  wrong). Per-particle `widths = 2 × sph_radius`: USD width is a **diameter**, and
  Blender imports width/2 as the point-cloud **radius** attribute, so Blender's radius
  equals the solver's `sph_radius` and stays editable. Positions come from node coords
  like everything else. Erosion uses `sph_deletion` (bool, **True = deleted** →
  alive = `~deletion`) since this file has no `sph_is_alive`; particles drop out per
  frame when deleted.

---

## 7. Console output — what to check

A healthy run prints, in order:

```
[t] '<path>' loaded in …s
--- d3plot summary ---------------------------------
  states  :           50
  parts   :        1,725
  shells / solids / tshells / beams / sph : counts
  deletion: elements=yes  nodes=no
----------------------------------------------------
dropped <n> interior solid faces
built <n> part meshes
built <n> beam curve prims (<n> beams -> <n> curves)
[t] geometry processed in …s
  frame 0 (state 0) done in …s
  …
[t] saved in …s -> wrote <out> (<n> frames from 1 d3plot family)
```

Watch for:
- `note: field <name> not in this d3plot; skipped` → that result wasn't written by the
  solver (check `*DATABASE_EXTENT_BINARY`).
- `deletion: elements=no` → **no erosion possible**; if you expected failed
  elements to vanish, the flag wasn't written to the d3plot (a solver output setting).
- `note: <type> is_alive shape … != n_<type> …; erosion skipped` → a shape mismatch;
  that element type won't erode. Investigate before trusting the animation.

---

## 8. Requirements

- **Python 3.11** launched as `py -3.11`.
- `lasso` (open-lasso-python), `pxr` (OpenUSD), `numpy` — all installed under 3.11.
- Input: an LS-DYNA `d3plot` result family. Erosion additionally needs the solver to
  have written element-deletion data.

---

## 9. Testing further (next steps)

The converter is the thing to harden before building on it. Suggested checks:

1. **Round-trip a small slice** (`--states 1:3 --parts "2000*" -o t.usda`) and open the
   ASCII `.usda` — eyeball prim structure, time samples, scale op.
2. **Open in usdview / Blender** — confirm animation plays, model is ~metres, parts
   are colored and lit.
3. **Erosion case** — run a result known to have failed elements; scrub the timeline
   and confirm faces actually disappear (and reappear-count stays sane).
4. **Frame count** — `endTimeCode` should equal `len(states)-1`; play in Blender
   and confirm no missing/duplicated frames.
5. **Vertex sanity** — `points` count per part should be constant across frames
   (deformation moves vertices, it doesn't add them); topology only changes on
   eroding parts.

See the repo-root `README.md` for how this fits the Blender pipeline, and
`../PROJECT.md` for the full project history and rationale.
