"""Import a dyna2usd .usdc into Blender and make the beams render-ready.

    blender --python blender/dyna_import.py -- crash.usdc [radius_mm]   # GUI
    blender -b --python blender/dyna_import.py                          # self-check

Beams arrive as Curves objects (one per LS-DYNA part, under the `beams` empty).
Each gets two modifiers after the USD cache:
  1. "Dyna Beam"     -- Radius (mm, solver units: /sim carries the 1e-3 scale) written
                        to the curve `radius` attribute. Cycles renders native curves
                        as round tubes of that radius, zero triangles.
  2. "Curve to Tube" -- Blender's built-in essentials modifier (Scale 1 = Radius),
                        OFF by default. Turn it on for real geometry (caps, UVs, custom
                        profile, booleans). Heavy: 1.5M beams -> ~19M tris.
Change all beams at once: select them, Alt+Enter on Radius.

The scene's curve display is set so radius is honoured everywhere: EEVEE/viewport
`Strip` (the default `Strand` draws fixed thin lines, ignoring radius) and Cycles
`3D Curves` (true round tubes, not flat ribbons).
"""
import os
import sys

import bpy

GROUP = "Dyna Beam"
TUBE = "Curve to Tube"


def beam_group():
    """Get or build the shared node group: Curves -> Set Curve Radius(Radius) -> out."""
    ng = bpy.data.node_groups.get(GROUP)
    if ng:
        return ng
    ng = bpy.data.node_groups.new(GROUP, "GeometryNodeTree")
    ng.is_modifier = True
    io = ng.interface
    io.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    r = io.new_socket("Radius", in_out="INPUT", socket_type="NodeSocketFloat")
    r.default_value, r.min_value, r.subtype = 0.5, 0.0, "DISTANCE"
    io.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi, go = ng.nodes.new("NodeGroupInput"), ng.nodes.new("NodeGroupOutput")
    rad = ng.nodes.new("GeometryNodeSetCurveRadius")
    ng.links.new(gi.outputs["Geometry"], rad.inputs["Curve"])
    ng.links.new(gi.outputs["Radius"], rad.inputs["Radius"])
    ng.links.new(rad.outputs["Curve"], go.inputs["Geometry"])
    rad.location, go.location = (220, 0), (440, 0)
    return ng


def tube_group():
    """Blender's essentials "Curve to Tube" group, appended once per file."""
    ng = bpy.data.node_groups.get(TUBE)
    if ng:
        return ng
    lib = os.path.join(bpy.utils.system_resource("DATAFILES"), "assets", "nodes",
                       "geometry_nodes_essentials.blend")
    with bpy.data.libraries.load(lib, link=False) as (src, dst):
        dst.node_groups = [TUBE]
    return bpy.data.node_groups[TUBE]


def set_input(mod, name, value):
    mod[mod.node_group.interface.items_tree[name].identifier] = value


def add_beam_modifiers(ob, radius=None):
    """Append radius + (disabled) tube modifiers after the USD cache modifier."""
    m = ob.modifiers.get(GROUP) or ob.modifiers.new(GROUP, "NODES")
    m.node_group = beam_group()
    if radius is not None:
        set_input(m, "Radius", radius)
    t = ob.modifiers.get(TUBE)
    if t is None:
        t = ob.modifiers.new(TUBE, "NODES")
        t.node_group = tube_group()
        set_input(t, "Scale", 1.0)                           # tube radius == Radius
        t.show_viewport = t.show_render = False
    return m, t


def curve_display(scene):
    scene.render.hair_type = "STRIP"                         # EEVEE/viewport honour radius
    scene.cycles_curves.shape = "THICK"                      # Cycles: round 3D curves


def import_dyna(path, radius=None):
    path = os.path.abspath(path)                             # importer resolves relative paths oddly
    bpy.ops.wm.usd_import(filepath=path)                     # sets frame range from stage
    scene = bpy.context.scene
    curve_display(scene)
    beams = [o for o in scene.objects if o.type == "CURVES"]
    for ob in beams:
        add_beam_modifiers(ob, radius)
    print(f"dyna2usd: imported {path}, {len(beams)} beam objects with '{GROUP}' + '{TUBE}' (off)")
    return beams


def _selfcheck():
    """2-segment curve: native radius follows Radius, and the tube matches it."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    cv = bpy.data.hair_curves.new("c")
    cv.add_curves([3])
    cv.set_types(type="POLY")                                # USD linear curves import as POLY
    cv.points.foreach_set("position", [0, 0, 0, 10, 0, 0, 20, 0, 0])
    ob = bpy.data.objects.new("beam", cv)
    bpy.context.scene.collection.objects.link(ob)
    m, t = add_beam_modifiers(ob, radius=2.0)
    dg = bpy.context.evaluated_depsgraph_get()
    gs = ob.evaluated_get(dg).evaluated_geometry()           # keep ref: data lives in it
    assert gs.mesh is None and gs.curves.attributes["radius"].data[1].value == 2.0
    t.show_viewport = True
    dg = bpy.context.evaluated_depsgraph_get()
    gs = ob.evaluated_get(dg).evaluated_geometry()
    ys = [v.co.y for v in gs.mesh.vertices]
    assert abs(max(ys) - 2.0) < 1e-4, max(ys)               # tube radius == Radius
    print("blender selfcheck ok")


if __name__ == "__main__":
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if args:
        import_dyna(args[0], float(args[1]) if len(args) > 1 else None)
    else:
        _selfcheck()
