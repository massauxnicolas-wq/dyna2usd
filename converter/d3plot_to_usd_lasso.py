"""LS-DYNA d3plot -> animated USD, variant B: erosion from lasso-python.

Alternative to d3plot_to_usd.py (DPF). Reads geometry, per-part grouping,
deformation and — crucially — the solver's own per-state element deletion
flags directly from the d3plot with open-lasso-python. No DPF server needed,
no DPF version gate (unlike DPF's erosion_flag which needs 2024 R2+).

Erosion is gated by the header flags the user pointed at:
  header.has_element_deletion_data / has_node_deletion_data.
When present, ArrayType.element_<type>_is_alive[state, elem] == 0 marks an
eroded element -> its faces drop out of the time-sampled USD topology.

    py -3.11 d3plot_to_usd_lasso.py resultats\\d3plot -o yaris_lasso.usdc
    py -3.11 d3plot_to_usd_lasso.py resultats\\d3plot -o t.usdc --states 1:6 --max-parts 5
"""
import argparse
import colorsys
import fnmatch
import os
import random
import tempfile
import time
from collections import defaultdict

import numpy as np
from lasso.dyna import D3plot, ArrayType as AT
from pxr import Usd, UsdGeom, UsdLux, UsdShade, Sdf, Vt, Gf, Tf

# inlined from the old d3plot_to_usd.py (DPF variant, removed):
# LS-DYNA hex8 face table -- 6 quads as local node indices
_HEX = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1),
        (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]


def part_color(key):
    """Deterministic distinct-ish part colour, seeded by the part key so the same
    part keeps its colour across restart families (no flicker at the boundary)."""
    return Gf.Vec3f(*colorsys.hsv_to_rgb(random.Random(f"{key}").random(), 0.65, 0.9))


def make_material(stage, scope, key, rgb):
    """Classic sim-viewer plastic: UsdPreviewSurface, matte-ish, non-metal.
    Under {scope}/materials so restart families don't clobber each other's paths."""
    mat = UsdShade.Material.Define(stage, f"{scope}/materials/mat_{key}")
    sh = UsdShade.Shader.Define(stage, f"{scope}/materials/mat_{key}/pbr")
    sh.CreateIdAttr("UsdPreviewSurface")
    sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(rgb)
    sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.45)
    sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
    mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
    return mat


def studio_lights(stage):
    """Classic 3-point studio + dome environment (Z-up).
    # ponytail: intensities eyeballed for usdview/Blender, tune per renderer."""
    dome = UsdLux.DomeLight.Define(stage, "/sim/lights/dome")
    dome.CreateIntensityAttr(1000.0)
    # DistantLight emits along -Z; rotate to key/fill/rim positions
    for name, rot, inten in [("key",  (-50,  40, 0), 3000.0),
                             ("fill", (-35, -60, 0), 1000.0),
                             ("rim",  (-60, 180, 0), 2000.0)]:
        li = UsdLux.DistantLight.Define(stage, f"/sim/lights/{name}")
        li.CreateIntensityAttr(inten)
        UsdGeom.Xformable(li.GetPrim()).AddRotateXYZOp().Set(Gf.Vec3f(*rot))

# (node_indexes, part_indexes, is_alive, face-kind). Beams handled separately
# as linear BasisCurves under /sim/beams/ (no faces).
ELEM = [
    ("shell",  AT.element_shell_node_indexes,  AT.element_shell_part_indexes,  AT.element_shell_is_alive,  "poly"),
    ("solid",  AT.element_solid_node_indexes,  AT.element_solid_part_indexes,  AT.element_solid_is_alive,  "hex"),
    ("tshell", AT.element_tshell_node_indexes, AT.element_tshell_part_indexes, AT.element_tshell_is_alive, "hex"),
]


# result fields -> per-face (uniform) primvars, one array key per ELEM type (same order)
FIELDS = {
    "von_mises":      [AT.element_shell_stress, AT.element_solid_stress, AT.element_tshell_stress],
    "plastic_strain": [AT.element_shell_effective_plastic_strain,
                       AT.element_solid_effective_plastic_strain,
                       AT.element_tshell_effective_plastic_strain],
}
# beams carry the same names (so one Blender field switch covers shells, solids and
# wires) plus their own axial results; per segment, then averaged/maxed onto chain nodes
BEAM_FIELDS = ["von_mises", "plastic_strain", "axial_force", "axial_work"]
FIELD_CHOICES = list(FIELDS) + ["displacement", "axial_force", "axial_work"]


def von_mises(sig):
    """(..., 6) stress [xx, yy, zz, xy, yz, zx] -> von Mises equivalent stress."""
    xx, yy, zz, xy, yz, zx = np.moveaxis(sig, -1, 0)
    return np.sqrt(0.5 * ((xx - yy) ** 2 + (yy - zz) ** 2 + (zz - xx) ** 2)
                   + 3.0 * (xy ** 2 + yz ** 2 + zx ** 2))


def elem_field(arr, name, s):
    """Per-element scalar for state s, one array per ELEM type (None if absent).
    Through-thickness / integration points reduced by max (the usual fringe plot).
    ponytail: max over layers only; add mid/outer-surface choice if anyone asks."""
    out = []
    for key in FIELDS[name]:
        a = arr.get(key)
        if a is None:
            out.append(None)
            continue
        v = np.asarray(a[s], dtype=np.float32)
        if name == "von_mises":
            v = von_mises(v)
        while v.ndim > 1:
            v = v.max(axis=1)
        out.append(v)
    return out


def beam_field(arr, name, s, work=None):
    """Per-beam scalar for state s, or None if the d3plot lacks it.
    von_mises: sqrt(axial^2 + 3 (shear_1^2 + shear_2^2)) per integration point, max.
    axial_work: precomputed cumulative F.dL (see axial_work()), signed."""
    if name == "axial_work":
        return None if work is None else work[s]
    if name == "axial_force":
        a = arr.get(AT.element_beam_axial_force)
        return None if a is None else np.asarray(a[s], np.float32)
    if name == "plastic_strain":
        a = arr.get(AT.element_beam_plastic_strain)
        return None if a is None else np.asarray(a[s], np.float32).reshape(len(a[s]), -1).max(1)
    ax, sh = arr.get(AT.element_beam_axial_stress), arr.get(AT.element_beam_shear_stress)
    if ax is None:
        return None
    ax = np.asarray(ax[s], np.float32).reshape(len(ax[s]), -1)            # (nb, n_ip)
    sh2 = 0.0 if sh is None else (np.asarray(sh[s], np.float32) ** 2).reshape(len(ax), ax.shape[1], -1).sum(-1)
    return np.sqrt(ax ** 2 + 3.0 * sh2).max(1)


def axial_work(force, coords, conn):
    """Cumulative axial work per beam, (n_states, n_beams): sum of
    0.5 (F_s + F_s-1) (L_s - L_s-1) over every state -- energy put into the element
    by stretching (recovered on elastic unloading, kept when it went plastic).
    ponytail: axial only; bending/torsion work needs beam moments + rotations."""
    L = np.linalg.norm(coords[:, conn[:, 0]] - coords[:, conn[:, 1]], axis=2).astype(np.float32)
    F = np.asarray(force, np.float32)
    dw = 0.5 * (F[1:] + F[:-1]) * (L[1:] - L[:-1])
    return np.concatenate([np.zeros((1, F.shape[1]), np.float32), np.cumsum(dw, axis=0)])


def seg_to_nodes(seg, v, how="max"):
    """Segment values -> chain node values from the 1-2 segments touching each node
    (max keeps hot spots; mean for signed fields like axial force)."""
    nxt = np.where(seg >= 0, v[np.maximum(seg, 0)], np.nan)  # segment after node i
    prv = np.r_[np.nan, nxt[:-1]]                            # segment before node i
    st = np.vstack([prv, nxt])
    return (np.nanmax(st, 0) if how == "max" else np.nanmean(st, 0)).astype(np.float32)


def part_selected(pid, name, include=(), exclude=()):
    """--parts / --exclude match: an int matches the part id, anything else is an
    fnmatch pattern on the part title (case-insensitive)."""
    def hit(pats):
        return any((p.isdigit() and int(p) == pid) or fnmatch.fnmatch(name.lower(), p.lower())
                   for p in pats)
    return (not include or hit(include)) and not hit(exclude)


def interior_faces(parts, ever_dead):
    """Solid/tshell faces shared by two elements are interior: drop them unless an
    owner erodes (then the face may become exterior mid-run; kept, shown with its
    own element). Returns {(pid, face_index)} to drop."""
    owners = defaultdict(list)
    for pid, p in parts.items():
        for i, (f, (et, e)) in enumerate(zip(p["faces"], p["src"])):
            if ELEM[et][4] == "hex":
                owners[tuple(sorted(f))].append((pid, i, et, e))
    drop = set()
    for own in owners.values():
        if len(own) > 1 and not any(ever_dead(et, e) for _, _, et, e in own):
            drop.update((pid, i) for pid, i, _, _ in own)
    return drop


def dedup(seq):
    """Order-preserving unique — collapses d3plot degenerate elements (tri stored
    as [a,b,c,c], tet as hex8 with repeats) to their real face."""
    out = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def element_faces(kind, conn):
    """Faces (lists of global node indices) for one element: shells 1 polygon,
    hex/tshell 6 quads (interior ones removed later by interior_faces)."""
    if kind == "poly":                       # shell: one polygon (tri or quad)
        f = dedup(conn.tolist())
        return [f] if len(f) >= 3 else []
    faces = []                               # hex/tshell: 6 quads, collapsed
    for fa in _HEX:
        f = dedup([int(conn[i]) for i in fa])
        if len(f) >= 3:
            faces.append(f)
    return faces


_USD_EXT = (".usd", ".usda", ".usdc")


def usd_out(path):
    """Force a valid USD extension (default .usdc). CreateNew rejects anything
    else -- and it fails only after the multi-minute d3plot load, so normalise
    the path up front. No/unknown ext -> .usdc; .usda/.usd kept as chosen."""
    root, ext = os.path.splitext(path)
    return path if ext.lower() in _USD_EXT else root + ".usdc"


def check_paths(inputs, out):
    """Fail fast BEFORE the multi-minute load: every input d3plot must exist, and the
    output dir must be creatable. Catches the classic 'forgot -o, so the output path got
    parsed as an extra d3plot family' mistake up front instead of after a 300s load."""
    missing = [p for p in inputs if not os.path.exists(p)]
    if missing:
        hint = ""
        if any(os.path.splitext(p)[1].lower() in _USD_EXT for p in missing):
            hint = ("\n  a .usd* path is listed as an INPUT -- did you forget -o before "
                    "the output? usage: ... <d3plot> -o out.usdc")
        raise SystemExit(f"input d3plot not found: {', '.join(missing)}{hint}")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)  # so CreateNew won't fail late


def part_names(arr, part_ids):
    """pid -> valid, unique USD prim name from LS-DYNA part titles.
    Falls back to part_<pid> when a title is missing/blank; disambiguates a
    sanitised-name collision with a _<pid> suffix."""
    titles = arr.get(AT.part_titles)
    tids = arr.get(AT.part_titles_ids)
    raw = {}
    if titles is not None and tids is not None:
        for pid, t in zip(np.asarray(tids), titles):
            t = t.decode("ascii", "ignore") if isinstance(t, (bytes, bytearray)) else str(t)
            t = t.strip()
            if t:
                raw[int(pid)] = t
    names, used = {}, set()
    for pid in map(int, part_ids):
        base = Tf.MakeValidIdentifier(raw[pid]) if pid in raw else f"part_{pid}"
        name = base if base not in used else f"{base}_{pid}"
        used.add(name)
        names[pid] = name
    return names


def sph_widths(srad, kind, s, sub):
    """USD point width = diameter = 2*radius, for the live particles `sub` at state s.
    Matches sph_radius's actual layout ('pp' per-particle-per-state, 'static'
    per-particle, 'state' uniform-per-state); None when radius is unusable."""
    if kind == "pp":
        w = srad[s, sub]
    elif kind == "static":
        w = srad[sub]
    elif kind == "state":
        w = np.full(len(sub), srad[s])
    else:
        return None
    return (2.0 * np.asarray(w)).astype(np.float32)


def beam_chains(pairs):
    """Join beam segments that share nodes into polylines (exact topological merge on
    node ids, no distance threshold). Walks from every end/junction node (degree != 2),
    then picks up leftover closed loops. Returns flat int arrays (nodes, seg):
    seg[i] = segment linking nodes[i] -> nodes[i+1], -1 at each chain's last node.
    ponytail: pure-Python walk, ~2 s per 1M segments; numba if it ever matters."""
    adj = defaultdict(list)
    for s, (a, b) in enumerate(pairs.tolist()):
        adj[a].append((b, s)); adj[b].append((a, s))
    used = bytearray(len(pairs))
    nodes, seg = [], []

    def walk(n, s):
        nodes.append(n)
        while True:
            used[s] = 1
            a, b = pairs[s]
            n = int(b) if a == n else int(a)
            seg.append(s); nodes.append(n)
            if len(adj[n]) != 2:
                break                                        # end or junction
            s = next((t for _, t in adj[n] if not used[t]), None)
            if s is None:
                break                                        # closed loop back at start
        seg.append(-1)

    for n, nb in adj.items():
        if len(nb) != 2:
            for _, s in nb:
                if not used[s]:
                    walk(n, s)
    for s in range(len(pairs)):                              # pure loops: no end nodes
        if not used[s]:
            walk(int(pairs[s][0]), s)
    return np.array(nodes, dtype=np.int64), np.array(seg, dtype=np.int64)


def live_curves(seg, alive=None):
    """Curves surviving erosion: alive[seg] False cuts the chain there. Returns
    (keep mask over chain nodes, per-curve vertex counts); 1-node leftovers dropped."""
    link = seg >= 0
    if alive is not None:
        link &= alive[np.maximum(seg, 0)]
    prev = np.r_[False, link[:-1]]
    keep = link | prev                                       # node touches a live link
    run = np.cumsum(keep & ~prev)[keep]                      # run id per kept node
    return keep, np.bincount(run)[1:].astype(np.int32)


def parse_states(spec, n):
    if not spec:
        return list(range(n))
    a, _, b = spec.partition(":")
    lo = (int(a) - 1) if a else 0
    hi = int(b) if b else n
    return list(range(max(0, lo), min(n, hi)))


def build_family(stage, path, scope, frame0, states_spec=None, include=(), exclude=(),
                 fields=()):
    """Load one d3plot family and build all its geometry under `scope`, authoring
    every time sample at frame0+frame. Returns the number of frames authored.
    A full-restart chain calls this once per family with its own scope + cumulative
    frame0; convert() then gates each family's visibility to its window."""
    t = time.perf_counter()
    d3 = D3plot(path)
    print(f"[t] '{path}' loaded in {time.perf_counter() - t:.1f}s")
    t = time.perf_counter()
    hdr = d3.header
    arr = d3.arrays
    # lasso's node_displacement holds CURRENT ABSOLUTE coordinates per state
    # (verified: node_displacement[0] == node_coordinates), NOT a delta -- adding
    # node_coordinates would double-count position and make rotating parts pulse.
    coord_states = np.asarray(arr[AT.node_displacement], dtype=np.float64)  # (n_states,n_nodes,3)
    n_states = coord_states.shape[0]
    states = parse_states(states_spec, n_states)
    part_ids = np.asarray(arr.get(AT.part_ids, np.arange(hdr.n_parts)))
    pnames = part_names(arr, part_ids)   # pid -> real part title (valid USD prim name)

    has_del = bool(getattr(hdr, "has_element_deletion_data", False))
    has_ndel = bool(getattr(hdr, "has_node_deletion_data", False))
    n_beams = 0 if arr.get(AT.element_beam_node_indexes) is None else len(arr[AT.element_beam_node_indexes])
    n_sph = 0 if arr.get(AT.sph_node_indexes) is None else len(arr[AT.sph_node_indexes])
    print("--- d3plot summary " + "-" * 33)
    for label, n in [("states", n_states), ("parts", hdr.n_parts),
                     ("shells", hdr.n_shells), ("solids", hdr.n_solids),
                     ("tshells", hdr.n_thick_shells), ("beams", n_beams), ("sph", n_sph)]:
        print(f"  {label:<8}: {n:>12,}")
    print(f"  deletion: elements={'yes' if has_del else 'no'}  nodes={'yes' if has_ndel else 'no'}")
    print("-" * 52)
    if not has_del:
        print("  -> no element deletion flags in file: nothing to erode (all elements shown)")

    # gather faces per part id; each face carries (etype_idx, elem_index) for erosion
    parts = defaultdict(lambda: {"faces": [], "src": []})   # pid -> faces + (et,idx)
    for et, (name, ck, pk, ak, kind) in enumerate(ELEM):
        conns = arr.get(ck)
        pidx = arr.get(pk)
        if conns is None or pidx is None:
            continue
        conns = np.asarray(conns)
        pidx = np.asarray(pidx)
        for e in range(conns.shape[0]):
            pid = int(part_ids[pidx[e]])
            for f in element_faces(kind, conns[e]):
                parts[pid]["faces"].append(f)
                parts[pid]["src"].append((et, e))
    pids = [pid for pid in parts if part_selected(pid, pnames[pid], include, exclude)]

    # per-type alive arrays; None if absent or shape can't be mapped 1:1
    alive = []
    for et, (name, ck, pk, ak, kind) in enumerate(ELEM):
        a = arr.get(ak)
        c = arr.get(ck)
        if has_del and a is not None and c is not None and np.asarray(a).shape[1] == np.asarray(c).shape[0]:
            alive.append(np.asarray(a) != 0)             # (n_states, n_elem) bool
        else:
            if has_del and a is not None:
                print(f"  note: {name} is_alive shape {np.asarray(a).shape} != n_{name} "
                      f"{0 if c is None else np.asarray(c).shape[0]}; {name} erosion skipped")
            alive.append(None)

    drop = interior_faces({pid: parts[pid] for pid in pids},
                          lambda et, e: alive[et] is not None and not alive[et][:, e].all())
    for pid in pids:
        p = parts[pid]
        keep = [i for i in range(len(p["faces"])) if (pid, i) not in drop]
        p["faces"] = [p["faces"][i] for i in keep]
        p["src"] = [p["src"][i] for i in keep]
    pids = [pid for pid in pids if parts[pid]["faces"]]
    if drop:
        print(f"dropped {len(drop)} interior solid faces")

    # typed family group; convert() gates its visibility to this family's window
    UsdGeom.Xform.Define(stage, scope)
    elem_fields = [f for f in fields if f in FIELDS and any(arr.get(k) is not None for k in FIELDS[f])]
    for f in fields:
        if f in FIELDS and f not in elem_fields:
            print(f"  note: field {f} not in this d3plot; skipped")
    built = []
    for pid in pids:
        p = parts[pid]
        used = sorted({n for f in p["faces"] for n in f})        # global node ids
        g2l = {g: i for i, g in enumerate(used)}
        counts = np.array([len(f) for f in p["faces"]], dtype=np.int32)
        flat = np.array([g2l[n] for f in p["faces"] for n in f], dtype=np.int32)
        src = p["src"]
        # does this part ever erode?
        erodes = any(alive[et] is not None and not alive[et][:, e].all() for et, e in src)

        m = UsdGeom.Mesh.Define(stage, f"{scope}/{pnames[pid]}")   # real part title as prim name
        m.CreateDoubleSidedAttr(True)
        # explicit: unset means catmullClark -> usdview/Karma/Omniverse would subdivide
        m.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        rgb = part_color(pid)
        m.CreateDisplayColorAttr([rgb])                      # viewport fallback
        UsdShade.MaterialBindingAPI.Apply(m.GetPrim()).Bind(make_material(stage, scope, pid, rgb))
        if not erodes:
            m.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(counts))
            m.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(flat))
        else:
            m.CreateFaceVertexCountsAttr(); m.CreateFaceVertexIndicesAttr()
        et_arr = np.array([et for et, _ in src])
        e_arr = np.array([e for _, e in src])
        pv = UsdGeom.PrimvarsAPI(m)
        prim_fields = {f: pv.CreatePrimvar(f, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.uniform)
                       for f in elem_fields}
        if "displacement" in fields:
            prim_fields["displacement"] = pv.CreatePrimvar(
                "displacement", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
        built.append(dict(mesh=m, used=np.array(used), faces=p["faces"], src=src,
                          counts=counts, erodes=erodes, fields=prim_fields,
                          types=[(et, et_arr == et, e_arr[et_arr == et]) for et in np.unique(et_arr)]))
    print(f"built {len(built)} part meshes")

    # beams -> linear BasisCurves, one prim per part under /sim/beams/part_<pid>.
    # Segments sharing nodes are joined into polylines (beam_chains): no duplicated
    # points, continuous wires. widths is a constant primvar (1 mm pre-scale) meant
    # to be overridden by the radius input of the Blender beam node group.
    beams_built = []
    beam_alive = None
    beam_fields, work = [], None
    bconn = arr.get(AT.element_beam_node_indexes)
    bpidx = arr.get(AT.element_beam_part_indexes)
    if bconn is not None and bpidx is not None and len(bconn):
        bconn = np.asarray(bconn)
        bpidx = np.asarray(bpidx)
        ba = arr.get(AT.element_beam_is_alive)
        if has_del and ba is not None and np.asarray(ba).shape[1] == bconn.shape[0]:
            beam_alive = np.asarray(ba) != 0                 # (n_states, n_beams)
        elif has_del and ba is not None:
            print(f"  note: beam is_alive shape {np.asarray(ba).shape} != n_beams "
                  f"{bconn.shape[0]}; beam erosion skipped")
        # typed ancestor: Define() would otherwise create /sim/beams typeless and
        # Blender's importer drops typeless prims -> children lose /sim's 1e-3 scale
        UsdGeom.Xform.Define(stage, f"{scope}/beams")
        bgroups = defaultdict(lambda: {"pairs": [], "idx": []})
        n_curves = 0
        work = None
        if "axial_work" in fields and arr.get(AT.element_beam_axial_force) is not None:
            work = axial_work(arr[AT.element_beam_axial_force], coord_states, bconn[:, :2])
        beam_fields = [f for f in fields if f in BEAM_FIELDS and beam_field(arr, f, 0, work) is not None]
        for e in range(bconn.shape[0]):
            pid = int(part_ids[bpidx[e]])
            if not part_selected(pid, pnames[pid], include, exclude):
                continue
            n1, n2 = int(bconn[e, 0]), int(bconn[e, 1])      # cols 2+ = orientation nodes
            if n1 != n2:
                bgroups[pid]["pairs"].append((n1, n2))
                bgroups[pid]["idx"].append(e)
        for pid, g in bgroups.items():
            pairs = np.array(g["pairs"], dtype=np.int64)     # (n_segs, 2) global node ids
            idx = np.array(g["idx"])
            erodes = beam_alive is not None and not beam_alive[:, idx].all()
            c = UsdGeom.BasisCurves.Define(stage, f"{scope}/beams/{pnames[pid]}")
            c.CreateTypeAttr(UsdGeom.Tokens.linear)
            c.CreateWidthsAttr(Vt.FloatArray([1.0]))
            c.SetWidthsInterpolation(UsdGeom.Tokens.constant)
            rgb = part_color(pid)
            c.CreateDisplayColorAttr([rgb])
            UsdShade.MaterialBindingAPI.Apply(c.GetPrim()).Bind(
                make_material(stage, scope, f"beam_{pid}", rgb))
            nodes, seg = beam_chains(pairs)
            keep, counts = live_curves(seg)
            # default counts + points always authored: importers that read
            # default-time topology (Blender curves) need them; per-frame time
            # samples override points (and counts, if eroding) during playback
            c.CreateCurveVertexCountsAttr(Vt.IntArray.FromNumpy(counts))
            c.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(
                coord_states[states[0]][nodes].astype(np.float32)))
            pv = UsdGeom.PrimvarsAPI(c)
            bf = {f: pv.CreatePrimvar(f, Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
                  for f in beam_fields + (["displacement"] if "displacement" in fields else [])}
            beams_built.append(dict(curve=c, nodes=nodes, seg=seg, idx=idx, erodes=erodes, fields=bf))
            n_curves += len(counts)
        print(f"built {len(beams_built)} beam curve prims ({bconn.shape[0]} beams "
              f"-> {n_curves} curves)")

    # SPH particles -> UsdGeom.Points, one prim per part under /sim/sph/part_<pid>.
    # widths = 2*sph_radius: USD width is a DIAMETER, and Blender imports width/2 as
    # the point-cloud radius attribute -> Blender radius == sph_radius, still editable.
    sph_built = []
    sph_alive = None
    srad = rad_kind = None          # referenced in the frame loop even if no SPH
    snodes = arr.get(AT.sph_node_indexes)
    smat = arr.get(AT.sph_node_material_index)
    if snodes is not None and smat is not None and len(snodes):
        snodes = np.asarray(snodes)
        smat = np.asarray(smat)
        # sph_radius layout is not reliably (n_states, n_sph) -- some files store it
        # per-state (n_states,) or static per-particle (n_sph,). Detect which.
        srad = arr.get(AT.sph_radius)
        srad = np.asarray(srad) if srad is not None else None
        n_st = coord_states.shape[0]
        if srad is None:
            rad_kind = None
        elif srad.ndim == 2 and srad.shape[1] == snodes.shape[0]:
            rad_kind = "pp"        # per-particle per-state (documented ideal)
        elif srad.ndim == 1 and srad.shape[0] == snodes.shape[0]:
            rad_kind = "static"    # per-particle, constant in time
        elif srad.ndim == 1 and srad.shape[0] == n_st:
            rad_kind = "state"     # one radius per state, uniform over particles
            print(f"  note: sph_radius is per-state uniform {srad.shape}; one radius "
                  f"applied to all particles (verify it's a real radius)")
        else:
            rad_kind = None
            print(f"  note: sph_radius shape {srad.shape} unrecognised; widths skipped "
                  f"(set radius in Blender)")
        # this file carries sph_deletion (bool, True=deleted), not sph_is_alive
        sd = arr.get(AT.sph_deletion)
        if sd is None and arr.get(AT.sph_is_alive) is not None:
            sd = ~(np.asarray(arr.get(AT.sph_is_alive)) != 0)
        sd = np.asarray(sd) if sd is not None else None
        if sd is not None and sd.ndim == 2 and sd.shape[1] == snodes.shape[0]:
            sph_alive = ~sd.astype(bool)                     # (n_states, n_sph)
        elif sd is not None:
            print(f"  note: sph deletion shape {sd.shape} != (n_states, n_sph="
                  f"{snodes.shape[0]}); sph erosion skipped")
        UsdGeom.Xform.Define(stage, f"{scope}/sph")          # typed ancestor (see beams)
        sgroups = defaultdict(list)
        for e in range(snodes.shape[0]):
            # ponytail: material index treated as 0-based part index (like element
            # part_indexes); if sph parts look wrong this mapping is the suspect
            sgroups[int(part_ids[smat[e]])].append(e)
        for pid, ids in sgroups.items():
            if not part_selected(pid, pnames[pid], include, exclude):
                continue
            ids = np.array(ids)
            nodes = snodes[ids]                              # global node ids
            erodes = sph_alive is not None and not sph_alive[:, ids].all()
            pt = UsdGeom.Points.Define(stage, f"{scope}/sph/{pnames[pid]}")
            rgb = part_color(pid)
            pt.CreateDisplayColorAttr([rgb])
            pt.SetWidthsInterpolation(UsdGeom.Tokens.vertex)
            UsdShade.MaterialBindingAPI.Apply(pt.GetPrim()).Bind(
                make_material(stage, scope, f"sph_{pid}", rgb))
            # default points so importers reading default-time geometry see particles
            pt.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(
                coord_states[states[0]][nodes].astype(np.float32)))
            pt.CreateWidthsAttr()
            sph_built.append(dict(pt=pt, nodes=nodes, ids=ids, erodes=erodes))
        print(f"built {len(sph_built)} sph point prims ({snodes.shape[0]} particles)")

    print(f"[t] geometry processed in {time.perf_counter() - t:.1f}s")

    for frame, s in enumerate(states):
        t = time.perf_counter()
        tc = Usd.TimeCode(frame0 + frame)                    # offset onto shared timeline
        pts_all = coord_states[s].astype(np.float32)
        disp_all = None
        if "displacement" in fields:
            disp_all = np.linalg.norm(coord_states[s] - coord_states[states[0]], axis=1).astype(np.float32)
        efv = {f: elem_field(arr, f, s) for f in elem_fields}
        for b in built:
            pts = pts_all[b["used"]]
            b["mesh"].GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(pts), tc)
            ext = np.array([pts.min(0), pts.max(0)], dtype=np.float32)
            b["mesh"].GetExtentAttr().Set(Vt.Vec3fArray.FromNumpy(ext), tc)
            keep = None
            if b["erodes"]:
                keep = np.array([alive[et] is None or alive[et][s, e] for et, e in b["src"]], bool)
                counts, flat = [], []
                g2l = {g: i for i, g in enumerate(b["used"])}
                for f, k in zip(b["faces"], keep):
                    if k:
                        counts.append(len(f)); flat.extend(g2l[n] for n in f)
                assert sum(counts) == len(flat), "topology mismatch"
                b["mesh"].GetFaceVertexCountsAttr().Set(Vt.IntArray.FromNumpy(np.array(counts, np.int32)), tc)
                b["mesh"].GetFaceVertexIndicesAttr().Set(Vt.IntArray.FromNumpy(np.array(flat, np.int32)), tc)
            for f, pvar in b["fields"].items():
                if f == "displacement":
                    val = disp_all[b["used"]]
                else:
                    val = np.zeros(len(b["src"]), np.float32)        # per face, from its element
                    for et, mask, eidx in b["types"]:
                        if efv[f][et] is not None:
                            val[mask] = efv[f][et][eidx]
                    if keep is not None:
                        val = val[keep]                              # faces alive this frame
                pvar.Set(Vt.FloatArray.FromNumpy(val), tc)
        bfv = {f: beam_field(arr, f, s, work) for f in beam_fields}
        for b in beams_built:
            nodes, keep = b["nodes"], None
            if b["erodes"]:
                keep, counts = live_curves(b["seg"], beam_alive[s, b["idx"]])
                nodes = nodes[keep]
                b["curve"].GetCurveVertexCountsAttr().Set(Vt.IntArray.FromNumpy(counts), tc)
            # BasisCurves has no index array: points listed per curve, consumed by counts
            pts = pts_all[nodes]
            b["curve"].GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(pts), tc)
            for f, pvar in b["fields"].items():
                if f == "displacement":
                    val = disp_all[nodes]
                else:
                    val = seg_to_nodes(b["seg"], bfv[f][b["idx"]],
                                       "mean" if f in ("axial_force", "axial_work") else "max")
                    val = val if keep is None else val[keep]
                pvar.Set(Vt.FloatArray.FromNumpy(val), tc)
            if len(pts):
                ext = np.array([pts.min(0), pts.max(0)], dtype=np.float32)
                b["curve"].GetExtentAttr().Set(Vt.Vec3fArray.FromNumpy(ext), tc)
        for b in sph_built:
            nodes, sub = b["nodes"], b["ids"]
            if b["erodes"]:
                live = sph_alive[s, b["ids"]]
                nodes, sub = nodes[live], sub[live]
            pts = pts_all[nodes]
            b["pt"].GetPointsAttr().Set(Vt.Vec3fArray.FromNumpy(pts), tc)
            w = sph_widths(srad, rad_kind, s, sub)
            if w is not None:
                b["pt"].GetWidthsAttr().Set(Vt.FloatArray.FromNumpy(w), tc)
            if len(pts):
                ext = np.array([pts.min(0), pts.max(0)], dtype=np.float32)
                b["pt"].GetExtentAttr().Set(Vt.Vec3fArray.FromNumpy(ext), tc)
        print(f"  frame {frame0 + frame} (state {s}) done in {time.perf_counter() - t:.2f}s")
    return len(states)


def convert(inputs, out, states_spec=None, include=(), exclude=(), fps=24.0,
            fields=()):
    """Build one USD stage from one or more d3plot families (restart chain in order).
    Each family occupies a slice of the shared timeline and, when there is more than
    one, is gated to that slice by time-sampled visibility -- so a full restart plays
    as one continuous animation with a hard, seamless swap at each boundary."""
    out = usd_out(out)
    if isinstance(inputs, str):
        inputs = [inputs]
    check_paths(inputs, out)          # fail fast on bad in/out paths before CreateNew + load
    # author in memory, write once at the end: CreateNew+Save was 40x slower on
    # many-prim stages (car: 66 s vs 1.6 s save, identical output)
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)                # 1e-3 baked on /sim below
    stage.SetStartTimeCode(0)
    stage.SetTimeCodesPerSecond(fps); stage.SetFramesPerSecond(fps)
    root = UsdGeom.Xform.Define(stage, "/sim")
    stage.SetDefaultPrim(root.GetPrim())
    # single manipulable root: grab /sim in Blender to move/scale the whole model.
    # 1e-3 scale (mm -> m) baked as a transform op; families sit under /sim and inherit.
    root.AddScaleOp().Set(Gf.Vec3f(1e-3, 1e-3, 1e-3))
    studio_lights(stage)

    multi = len(inputs) > 1
    frame0 = 0
    for i, path in enumerate(inputs):
        scope = f"/sim/run{i}" if multi else "/sim"          # single family stays flat
        n = build_family(stage, path, scope, frame0, states_spec, include, exclude,
                         fields)
        if multi:
            # object "life": show this family only during [frame0, frame0+n). visibility
            # is a held token, so authoring invisible->inherited->invisible is enough.
            vis = UsdGeom.Imageable(stage.GetPrimAtPath(scope)).GetVisibilityAttr()
            if frame0 > 0:
                vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(0))
            vis.Set(UsdGeom.Tokens.inherited, Usd.TimeCode(frame0))
            vis.Set(UsdGeom.Tokens.invisible, Usd.TimeCode(frame0 + n))
        frame0 += n
    stage.SetEndTimeCode(max(0, frame0 - 1))

    t = time.perf_counter()
    stage.Export(out)
    print(f"[t] saved in {time.perf_counter() - t:.1f}s -> wrote {out} "
          f"({frame0} frames from {len(inputs)} d3plot famil{'ies' if multi else 'y'})")


def _selfcheck():
    assert usd_out("model.usdc") == "model.usdc"
    assert usd_out("model.usda") == "model.usda"      # ascii kept
    assert usd_out("model.usd") == "model.usd"
    assert usd_out("model.USDC") == "model.USDC"       # case-insensitive
    assert usd_out("model") == "model.usdc"            # no ext (the bug)
    assert usd_out("model.txt") == "model.usdc"        # wrong ext
    assert usd_out("a.b.v2") == "a.b.usdc"             # dotted stem

    nm = part_names({AT.part_titles_ids: [1, 2, 3], AT.part_titles: [b"Hood", b"A B!", b"  "]},
                    [1, 2, 3, 9])
    assert nm[1] == "Hood"                             # clean title kept
    assert nm[2] == Tf.MakeValidIdentifier("A B!")     # sanitised to valid id
    assert nm[3] == "part_3" and nm[9] == "part_9"     # blank / missing -> fallback
    col = part_names({AT.part_titles_ids: [1, 2], AT.part_titles: [b"A!", b"A@"]}, [1, 2])
    assert col[1] != col[2]                            # sanitise collision disambiguated

    pp = np.arange(6.0).reshape(2, 3)                  # (n_states=2, n_sph=3)
    assert list(sph_widths(pp, "pp", 1, np.array([0, 2]))) == [6.0, 10.0]      # 2*(3,5)
    assert list(sph_widths(np.array([1., 2., 3.]), "static", 0, np.array([2, 1]))) == [6.0, 4.0]
    assert list(sph_widths(np.array([0.5, 1.5]), "state", 1, np.array([0, 0, 0]))) == [3.0, 3.0, 3.0]
    assert sph_widths(None, None, 0, np.array([0])) is None

    # beams: Y junction (0-1-2, 1-3) + closed triangle loop (4-5-6) + reversed pair
    pr = np.array([[0, 1], [2, 1], [1, 3], [4, 5], [5, 6], [6, 4], [7, 8], [8, 9]])
    nodes, seg = beam_chains(pr)
    assert (seg >= 0).sum() == len(pr) and sorted(seg[seg >= 0]) == list(range(len(pr)))
    for i in np.flatnonzero(seg >= 0):                       # every link is a real segment
        assert {nodes[i], nodes[i + 1]} == set(pr[seg[i]])
    keep, counts = live_curves(seg)
    assert keep.all() and counts.sum() == len(nodes) and (counts >= 2).all()
    assert len(counts) == 5           # 3 branches at the junction + 1 loop + 7-8-9 chain
    dead = np.ones(len(pr), bool); dead[7] = False           # erode 8-9 off the 7-8-9 chain
    keep, counts = live_curves(seg, dead)
    assert len(counts) == 5 and counts.sum() == keep.sum()   # 7-8-9 -> 7-8 only
    dead[6] = False                                          # 7-8-9 fully gone
    keep, counts = live_curves(seg, dead)
    assert len(counts) == 4 and (counts >= 2).all()

    assert abs(von_mises(np.array([100., 0, 0, 0, 0, 0])) - 100) < 1e-9          # uniaxial
    assert abs(von_mises(np.array([0., 0, 0, 50, 0, 0])) - 50 * 3 ** 0.5) < 1e-9  # pure shear
    assert abs(von_mises(np.array([7., 7, 7, 0, 0, 0]))) < 1e-9                   # hydrostatic
    st = {AT.element_shell_stress: np.zeros((2, 3, 2, 6)), AT.element_shell_effective_plastic_strain:
          np.array([[[0.1, 0.3], [0, 0], [0.2, 0.1]]] * 2)}
    st[AT.element_shell_stress][1, 0, 1, 0] = 100.0                  # state 1, elem 0, layer 1
    vm = elem_field(st, "von_mises", 1)
    assert vm[1] is None and list(vm[0]) == [100, 0, 0]              # max over layers, no solids
    assert np.allclose(elem_field(st, "plastic_strain", 0)[0], [0.3, 0, 0.2])

    assert part_selected(5, "Hood_L") and part_selected(5, "x", ["5"]) and not part_selected(6, "x", ["5"])
    assert part_selected(7, "Hood_L", ["hood*"]) and not part_selected(7, "Hood_L", [], ["HOOD*"])
    sg = np.array([0, 1, -1, 2, -1])                         # chains a-b-c, d-e
    nv = seg_to_nodes(sg, np.array([1., 5., -2.]))
    assert list(nv) == [1, 5, 5, -2, -2]                     # max of touching segments
    assert list(seg_to_nodes(sg, np.array([1., 5., -2.]), "mean")) == [1, 3, 5, -2, -2]
    co = np.array([[[0, 0, 0], [10, 0, 0]], [[0, 0, 0], [11, 0, 0]], [[0, 0, 0], [10.5, 0, 0]]], float)
    w = axial_work(np.array([[0.], [2.], [0.]]), co, np.array([[0, 1]]))
    assert np.allclose(w[:, 0], [0, 1.0, 0.5])               # +0.5*(0+2)*1, then +0.5*(2+0)*(-0.5): unloading returns work
    hexf = {1: {"faces": [[0, 1, 2, 3], [4, 5, 6, 7]], "src": [(1, 0), (1, 0)]},
            2: {"faces": [[3, 2, 1, 0], [8, 9, 10, 11]], "src": [(1, 1), (1, 1)]}}
    assert interior_faces(hexf, lambda et, e: False) == {(1, 0), (2, 0)}
    assert interior_faces(hexf, lambda et, e: e == 1) == set()   # eroding owner: keep

    with tempfile.TemporaryDirectory() as d:
        try:
            check_paths([os.path.join(d, "nope", "d3plot")], os.path.join(d, "o.usdc")); assert False
        except SystemExit:
            pass                                   # missing input -> raises before any load
        nested = os.path.join(d, "made", "here", "o.usdc")
        check_paths([d], nested)                    # existing input (the dir), out dir auto-created
        assert os.path.isdir(os.path.dirname(nested))
    print("selfcheck ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("d3plot", nargs="*",
                    help="one or more d3plot families in restart order "
                         "(e.g. d3plot d3plotac_base); omit to run selfcheck")
    ap.add_argument("-o", "--out", default="out_lasso.usdc")
    ap.add_argument("--states", help="1-based inclusive slice, e.g. 1:6 (applied per family)")
    ap.add_argument("--parts", default="",
                    help="comma list of part ids or title patterns to keep, e.g. 2000*,Hood,1001")
    ap.add_argument("--exclude", default="", help="comma list of part ids or title patterns to drop")
    ap.add_argument("--fps", type=float, default=24.0)
    ap.add_argument("--fields", default="",
                    help=f"comma list of result primvars: {','.join(FIELD_CHOICES)}")
    a = ap.parse_args()
    if not a.d3plot:
        _selfcheck()
    else:
        fields = [f for f in a.fields.split(",") if f]
        bad = set(fields) - set(FIELD_CHOICES)
        if bad:
            ap.error(f"unknown --fields {sorted(bad)}; choose from {FIELD_CHOICES}")
        split = lambda v: [x.strip() for x in v.split(",") if x.strip()]
        convert(a.d3plot, a.out, a.states, split(a.parts), split(a.exclude), a.fps, fields)
