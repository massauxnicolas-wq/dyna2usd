"""Blender (5.x) -- fake "Energy Absorbed" colouring for LS-DYNA beam wires imported from USD.

    blender crash.blend --python blender/wire_energy.py -- --mode velocity [--out x.blend]
    (or Text Editor: select the wire CURVES objects, run; no selection = every beam object)

Proxy for when the model was converted without beam results. With
`--fields axial_work` the converter writes the REAL absorbed energy (cumulative F.dL per
beam, see converter) -> use look.py / field.py with field "axial_work" instead.

MODE "strain"   -- energy that STAYED (per wire segment, stored on its start point):
    strain  = L / L0 - 1                          L0 = segment length on the sim start frame
    eps_p   = max(0, |strain| - Yield Strain)     plastic part
    e_abs   = max over time of eps_p              plastic work density ~ sigma_y * eps_p
MODE "velocity" -- energy that FLOWED THROUGH (per node):
    v       = |P - P_prev|                        scene units per frame (depends on d3plot output interval)
    e_abs   = max over time of v^2                peak kinetic energy density ~ 1/2 m v^2
Hold Peak (modifier checkbox, default on): max-hold in a Simulation Zone -> colour stays after
springback / once the wave has passed. Off -> instantaneous value of the current frame.
Auto Max (checkbox, default off): Max Strain / Max Speed replaced by this frame's highest value
on the object -> red is always the hottest wire now. Floored at Ref Speed / Yield Strain so a
quiet frame shows blue instead of stretching noise to red. ponytail: per object, not across objects.
Output: point attribute "energy" in 0..1, log scale  log(1 + e/ref) / log(1 + max/ref),
read by material "Wire_Energy". Bake the sim (or play from frame 0) before rendering.

dyna2usd wires are polylines (beams sharing nodes joined by the converter), so strain is
per segment: each point measures the segment to the next point of its curve (the last
point uses the previous segment).
ponytail: stretch or node speed only; bending/kinks at joints are not seen. Eroding wire
parts change point count mid-run, which breaks the index-matched simulation state.
"""
import argparse
import sys

import bpy

MODE = "velocity"  # "strain" | "velocity"
MAT_NAME, MOD_NAME = "Wire_Energy", "WireEnergy"
GROUPS = {"strain": "WireEnergyProxy", "velocity": "WireVelocityProxy"}


def _new_group(name, inputs):
    ng = bpy.data.node_groups.get(name) or bpy.data.node_groups.new(name, "GeometryNodeTree")
    ng.is_modifier = True
    ng.nodes.clear()  # rebuild in place: existing modifiers keep pointing at it, script edits apply
    ng.interface.clear()
    ng.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    for label, default in inputs:
        s = ng.interface.new_socket(label, in_out="INPUT", socket_type="NodeSocketFloat")
        s.default_value, s.min_value = default, 1e-9
    ng.interface.new_socket("Hold Peak", in_out="INPUT", socket_type="NodeSocketBool").default_value = True
    ng.interface.new_socket("Auto Max", in_out="INPUT", socket_type="NodeSocketBool").default_value = False
    ng.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    N, L = ng.nodes, ng.links

    def node(t, x, y, **props):
        n = N.new(t)
        n.location = (x, y)
        for k, v in props.items():
            setattr(n, k, v)
        return n

    def math(op, a, b=None, x=0, y=0, t="ShaderNodeMath"):
        n = node(t, x, y, operation=op)
        for i, v in enumerate((a, b)):
            if v is None:
                continue
            if isinstance(v, bpy.types.NodeSocket):
                L.new(v, n.inputs[i])
            else:
                n.inputs[i].default_value = v
        return n.outputs[0] if t == "ShaderNodeMath" or op != "LENGTH" else n.outputs["Value"]

    def store(geo, name, value, x, y, domain, data_type="FLOAT"):
        n = node("GeometryNodeStoreNamedAttribute", x, y, data_type=data_type, domain=domain)
        L.new(geo, n.inputs["Geometry"])
        n.inputs["Name"].default_value = name
        L.new(value, n.inputs["Value"])
        return n.outputs["Geometry"]

    def sample_prev(state_geo, name, x, y, domain, data_type="FLOAT"):  # same-index value from state
        a = node("GeometryNodeInputNamedAttribute", x - 200, y, data_type=data_type)
        a.inputs["Name"].default_value = name
        s = node("GeometryNodeSampleIndex", x, y, data_type=data_type, domain=domain)
        L.new(state_geo, s.inputs["Geometry"])
        L.new(a.outputs["Attribute"], s.inputs["Value"])
        L.new(node("GeometryNodeInputIndex", x - 200, y - 120).outputs[0], s.inputs["Index"])
        return s.outputs["Value"]

    def zone(g0):  # simulation zone fed with first-frame geometry g0 -> (state, output node)
        sin = node("GeometryNodeSimulationInput", -800, 200)
        sout = node("GeometryNodeSimulationOutput", 1000, 200)
        sin.pair_with_output(sout)
        L.new(g0, sin.inputs["Geometry"])
        return sin.outputs["Geometry"], sout

    def finish(sout, ref, mx):  # e_abs -> log-normalized 0..1 point attribute "energy"
        e = node("GeometryNodeInputNamedAttribute", 1100, -100, data_type="FLOAT")
        e.inputs["Name"].default_value = "e_abs"
        stat = node("GeometryNodeAttributeStatistic", 900, -600, data_type="FLOAT", domain="POINT")
        L.new(sout.outputs["Geometry"], stat.inputs["Geometry"])
        L.new(e.outputs["Attribute"], stat.inputs["Attribute"])
        sw = node("GeometryNodeSwitch", 1050, -600, input_type="FLOAT")  # Auto Max ? frame max : knob
        L.new(gin.outputs["Auto Max"], sw.inputs["Switch"])
        L.new(mx, sw.inputs["False"])
        L.new(math("MAXIMUM", stat.outputs["Max"], ref, 900, -750), sw.inputs["True"])  # floor
        mx = sw.outputs[0]
        log1p = lambda v, yy: math("LOGARITHM", math("ADD", math("DIVIDE", v, ref, 1100, yy),
                                                     1.0, 1100, yy), 10.0, 1200, yy)
        norm = math("DIVIDE", log1p(e.outputs["Attribute"], -300), log1p(mx, -450), 1250, -100)
        clamp = node("ShaderNodeClamp", 1300, -100)
        L.new(norm, clamp.inputs["Value"])
        L.new(store(sout.outputs["Geometry"], "energy", clamp.outputs[0], 1400, 200, "POINT"),
              gout.inputs["Geometry"])

    def hold(cur, prev, x, y):  # Hold Peak ? max(cur, prev) : cur
        sw = node("GeometryNodeSwitch", x + 150, y, input_type="FLOAT")
        L.new(gin.outputs["Hold Peak"], sw.inputs["Switch"])
        L.new(cur, sw.inputs["False"])
        L.new(math("MAXIMUM", cur, prev, x, y), sw.inputs["True"])
        return sw.outputs[0]

    gin, gout = node("NodeGroupInput", -1400, 0), node("NodeGroupOutput", 1600, 0)
    zero = node("ShaderNodeValue", -1200, 0).outputs[0]
    return ng, gin, L, node, math, store, sample_prev, zone, finish, zero, hold


def _segment_length(node, L, math, x, y):
    """Per point: length of the segment to the next point of its curve (last point:
    the previous segment). Works on the joined polylines the converter writes."""
    idx = node("GeometryNodeInputIndex", x - 600, y).outputs[0]
    nxt = node("GeometryNodeOffsetPointInCurve", x - 400, y)
    prv = node("GeometryNodeOffsetPointInCurve", x - 400, y - 160)
    for n, off in ((nxt, 1), (prv, -1)):
        L.new(idx, n.inputs["Point Index"])
        n.inputs["Offset"].default_value = off
    other = node("GeometryNodeSwitch", x - 200, y, input_type="INT")
    L.new(nxt.outputs["Is Valid Offset"], other.inputs["Switch"])
    L.new(prv.outputs["Point Index"], other.inputs["False"])
    L.new(nxt.outputs["Point Index"], other.inputs["True"])
    pos = node("GeometryNodeInputPosition", x - 400, y - 320).outputs[0]
    at = node("GeometryNodeFieldAtIndex", x, y - 200, data_type="FLOAT_VECTOR", domain="POINT")
    L.new(other.outputs[0], at.inputs["Index"])
    L.new(pos, at.inputs["Value"])
    vm = "ShaderNodeVectorMath"
    return math("LENGTH", math("SUBTRACT", pos, at.outputs[0], x + 150, y, vm), x=x + 300, y=y, t=vm)


def build_strain():
    ng, gi, L, node, math, store, sample_prev, zone, finish, zero, hold = _new_group(
        GROUPS["strain"], (("Yield Strain", 0.002), ("Max Strain", 0.5)))
    length = _segment_length(node, L, math, -1400, -250)
    g0 = store(store(gi.outputs["Geometry"], "rest_len", length, -1200, 200, "POINT"),
               "e_abs", zero, -1000, 200, "POINT")
    state, sout = zone(g0)
    rest = sample_prev(state, "rest_len", -400, -200, "POINT")
    prev = sample_prev(state, "e_abs", -400, -450, "POINT")
    strain = math("SUBTRACT", math("DIVIDE", length, rest, -200, -250), 1.0, 0, -250)
    eps_p = math("MAXIMUM", math("SUBTRACT", math("ABSOLUTE", strain, x=200, y=-250),
                                 gi.outputs["Yield Strain"], 400, -250), 0.0, 600, -250)
    g = store(gi.outputs["Geometry"], "rest_len", rest, 400, 200, "POINT")
    g = store(g, "e_abs", hold(eps_p, prev, 750, -300), 700, 200, "POINT")
    L.new(g, sout.inputs["Geometry"])
    finish(sout, gi.outputs["Yield Strain"], gi.outputs["Max Strain"])
    return ng


def build_velocity():
    ng, gi, L, node, math, store, sample_prev, zone, finish, zero, hold = _new_group(
        GROUPS["velocity"], (("Ref Speed", 20.0), ("Max Speed", 90.0)))
    pos = node("GeometryNodeInputPosition", -1400, -250).outputs[0]
    g0 = store(store(gi.outputs["Geometry"], "prev_pos", pos, -1200, 200, "POINT", "FLOAT_VECTOR"),
               "e_abs", zero, -1000, 200, "POINT")
    state, sout = zone(g0)
    prev_pos = sample_prev(state, "prev_pos", -400, -200, "POINT", "FLOAT_VECTOR")
    prev = sample_prev(state, "e_abs", -400, -450, "POINT")
    vm = "ShaderNodeVectorMath"
    speed = math("LENGTH", math("SUBTRACT", pos, prev_pos, 0, -250, vm), x=200, y=-250, t=vm)
    ke = math("MULTIPLY", speed, speed, 400, -250)
    g = store(gi.outputs["Geometry"], "prev_pos", pos, 400, 200, "POINT", "FLOAT_VECTOR")
    g = store(g, "e_abs", hold(ke, prev, 550, -300), 700, 200, "POINT")
    L.new(g, sout.inputs["Geometry"])
    sq = lambda s, y: math("MULTIPLY", s, s, 1000, y)  # ref/max given as speeds, compared as v^2
    finish(sout, sq(gi.outputs["Ref Speed"], -550), sq(gi.outputs["Max Speed"], -650))
    return ng


def build_material():
    mat = bpy.data.materials.get(MAT_NAME)
    if mat:
        return mat
    mat = bpy.data.materials.new(MAT_NAME)
    mat.use_nodes = True
    N, L = mat.node_tree.nodes, mat.node_tree.links
    bsdf = N["Principled BSDF"]
    attr = N.new("ShaderNodeAttribute")
    attr.attribute_name, attr.location = "energy", (-700, 300)
    ramp = N.new("ShaderNodeValToRGB")
    ramp.location = (-450, 300)
    els = ramp.color_ramp.elements
    for pos, rgb in ((0.0, (0.02, 0.05, 0.6)), (0.35, (0.0, 0.7, 0.25)),
                     (0.65, (1.0, 0.85, 0.0)), (1.0, (1.0, 0.0, 0.0))):
        el = els.new(pos) if pos not in (0.0, 1.0) else els[0 if pos == 0.0 else -1]
        el.color = (*rgb, 1.0)
    L.new(attr.outputs["Factor"], ramp.inputs["Fac"])
    L.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    L.new(ramp.outputs["Color"], bsdf.inputs["Emission Color"])
    L.new(attr.outputs["Factor"], bsdf.inputs["Emission Strength"])  # hot wires glow a bit
    bsdf.inputs["Metallic"].default_value = 0.3
    bsdf.inputs["Roughness"].default_value = 0.45
    return mat


def apply(objs, mode=MODE):
    ng = {"strain": build_strain, "velocity": build_velocity}[mode]()
    for o in bpy.data.objects:  # re-sync modifier inputs of every user after the interface rebuild
        for m in o.modifiers:
            if m.type == "NODES" and m.node_group == ng:
                m.node_group = ng
    mat = build_material()
    for o in objs:
        if o.type != "CURVES":
            continue
        for m in [m for m in o.modifiers if m.type == "NODES" and m.node_group
                  and m.node_group.name in GROUPS.values()]:
            o.modifiers.remove(m)  # one proxy at a time: both write "energy"
        o.modifiers.new(MOD_NAME, "NODES").node_group = ng  # appended after the USD cache
        o.data.materials.clear()
        o.data.materials.append(mat)
        print(f"wire energy ({mode}) ->", o.name)


def targets():
    """Selected wire objects, else every Curves object with a USD cache modifier."""
    sel = [o for o in bpy.context.selected_objects if o.type == "CURVES"]
    return sel or [o for o in bpy.context.scene.objects if o.type == "CURVES"
                   and any(m.type == "MESH_SEQUENCE_CACHE" for m in o.modifiers)]


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    ap = argparse.ArgumentParser(prog="wire_energy.py")
    ap.add_argument("--mode", choices=list(GROUPS), default=MODE)
    ap.add_argument("--out", help="save the .blend here")
    a = ap.parse_args(argv)
    apply(targets(), a.mode)
    if a.out:
        bpy.ops.wm.save_as_mainfile(filepath=a.out)
