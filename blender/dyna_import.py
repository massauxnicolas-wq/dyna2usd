"""Import a dyna2usd .usdc into Blender and make the beams render-ready.

    blender --python blender/dyna_import.py -- crash.usdc        # GUI
    blender -b --python blender/dyna_import.py                   # self-check

Beams arrive as Curves objects (one per LS-DYNA part, under the `beams` empty).
Each gets a "Dyna Beam" geometry-nodes modifier: Radius drives the curve radius
attribute. Default = native curves: Cycles renders them as round tubes at that
radius (verified identical to the mesh) for zero triangles. Tube Mesh = real
geometry (Curve to Mesh) for modifiers/booleans/export -- heavy: 1.5M beams ->
~19M tris. Units are the solver's (mm): /sim carries the 1e-3 scale,
so Radius 0.5 = 1 mm diameter wire. Change all beams at once: select them,
Alt+Enter (or Alt+drag) on the modifier's Radius.
"""
import sys

import bpy

GROUP = "Dyna Beam"


def beam_group():
    """Get or build the shared beam node group:
    Curves -> Set Curve Radius(Radius) -> [Tube Mesh? Curve to Mesh(circle, Scale=radius) + smooth] -> out."""
    ng = bpy.data.node_groups.get(GROUP)
    if ng:
        return ng
    ng = bpy.data.node_groups.new(GROUP, "GeometryNodeTree")
    ng.is_modifier = True
    io = ng.interface
    io.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    r = io.new_socket("Radius", in_out="INPUT", socket_type="NodeSocketFloat")
    r.default_value, r.min_value, r.subtype = 0.5, 0.0, "DISTANCE"
    tube = io.new_socket("Tube Mesh", in_out="INPUT", socket_type="NodeSocketBool")
    tube.default_value = False    # native curves: Cycles renders them as exact round tubes
    res = io.new_socket("Resolution", in_out="INPUT", socket_type="NodeSocketInt")
    res.default_value, res.min_value = 6, 3
    io.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")

    N, L = ng.nodes, ng.links
    gi, go = N.new("NodeGroupInput"), N.new("NodeGroupOutput")
    rad = N.new("GeometryNodeSetCurveRadius")
    circ = N.new("GeometryNodeCurvePrimitiveCircle")
    circ.inputs["Radius"].default_value = 1.0                # scaled by curve radius
    c2m = N.new("GeometryNodeCurveToMesh")
    rfield = N.new("GeometryNodeInputRadius")               # Blender 5: profile scale is
    smooth = N.new("GeometryNodeSetShadeSmooth")
    sw = N.new("GeometryNodeSwitch")
    sw.input_type = "GEOMETRY"
    L.new(gi.outputs["Geometry"], rad.inputs["Curve"])
    L.new(gi.outputs["Radius"], rad.inputs["Radius"])
    L.new(gi.outputs["Resolution"], circ.inputs["Resolution"])
    L.new(rad.outputs["Curve"], c2m.inputs["Curve"])
    L.new(circ.outputs["Curve"], c2m.inputs["Profile Curve"])
    L.new(rfield.outputs["Radius"], c2m.inputs["Scale"])     # explicit, not implied by radius
    L.new(c2m.outputs["Mesh"], smooth.inputs["Geometry"])
    L.new(gi.outputs["Tube Mesh"], sw.inputs["Switch"])
    L.new(rad.outputs["Curve"], sw.inputs["False"])          # native curves, radius set
    L.new(smooth.outputs["Geometry"], sw.inputs["True"])
    L.new(sw.outputs[0], go.inputs["Geometry"])
    for i, n in enumerate([gi, rad, circ, c2m, smooth, sw, go]):
        n.location = (i * 220, 0 if n is not circ else -220)
    rfield.location = (440, -380)
    return ng


def add_beam_modifier(ob, radius=None):
    """Append the beam modifier after the USD cache modifier (needs the animated curve)."""
    m = ob.modifiers.get(GROUP) or ob.modifiers.new(GROUP, "NODES")
    m.node_group = beam_group()
    if radius is not None:
        m[m.node_group.interface.items_tree["Radius"].identifier] = radius
    return m


def import_dyna(path, radius=None):
    bpy.ops.wm.usd_import(filepath=path)                     # sets frame range from stage
    beams = [o for o in bpy.context.scene.objects if o.type == "CURVES"]
    for ob in beams:
        add_beam_modifier(ob, radius)
    print(f"dyna2usd: imported {path}, {len(beams)} beam objects with '{GROUP}'")
    return beams


def _selfcheck():
    """Build a 2-segment curve, apply the group, check tube size follows Radius."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    cv = bpy.data.hair_curves.new("c")
    cv.add_curves([3])
    cv.set_types(type="POLY")                                # USD linear curves import as POLY
    cv.points.foreach_set("position", [0, 0, 0, 10, 0, 0, 20, 0, 0])
    ob = bpy.data.objects.new("beam", cv)
    bpy.context.scene.collection.objects.link(ob)
    m = add_beam_modifier(ob, radius=2.0)
    m[m.node_group.interface.items_tree["Tube Mesh"].identifier] = True
    dg = bpy.context.evaluated_depsgraph_get()
    gs = ob.evaluated_get(dg).evaluated_geometry()     # keep ref: mesh lives in it
    me = gs.mesh
    ys = [v.co.y for v in me.vertices]
    assert len(me.vertices) == 3 * 6, len(me.vertices)       # 3 points x 6-sided ring
    assert abs(max(ys) - 2.0) < 1e-4, max(ys)               # tube radius == Radius
    m[m.node_group.interface.items_tree["Tube Mesh"].identifier] = False
    ob.update_tag()
    dg = bpy.context.evaluated_depsgraph_get()
    gs = ob.evaluated_get(dg).evaluated_geometry()
    assert gs.mesh is None and gs.curves.attributes["radius"].data[1].value == 2.0   # native curves keep radius
    print("blender selfcheck ok")


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if args:
        import_dyna(args[0], float(args[1]) if len(args) > 1 else None)
    else:
        _selfcheck()
