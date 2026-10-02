"""Switchable result field for dyna2usd scenes: one geometry-nodes modifier per object
("Dyna Field") picks a result attribute from a menu and writes it normalised to 0..1 as
`fe_value`, which every result material reads.

Switch every object at once from the scene (Properties > Scene > Custom Properties):
    fe_field  index into FIELDS (0 von_mises, 1 plastic_strain, 2 displacement,
              3 axial_force, 4 axial_work)
    fe_min, fe_max  legend range (values outside clamp to the end bands)
    fe_log    1 = log10 scale between fe_min and fe_max (heavy-tailed fields: energy)
The modifier inputs are driven by those props. To also recompute the range and relabel
the legend, run in Blender's Python console:
    import field; field.set_field(C.scene, "plastic_strain")
A model only carries the fields it was converted with (`--fields` in the converter);
a missing field reads 0 (bottom colour).
"""
import bpy
import numpy as np

GROUP = "Dyna Field"
FIELDS = ["von_mises", "plastic_strain", "displacement", "axial_force", "axial_work"]
TITLES = {"von_mises": "Von Mises stress [MPa]",
          "plastic_strain": "Effective plastic strain [-]",
          "displacement": "Displacement [mm]",
          "axial_force": "Beam axial force [kN]",
          "axial_work": "Beam absorbed energy [kN.mm = J]"}
SIGNED = {"axial_force"}                                     # legend symmetric about 0
LOG = {"axial_work"}               # default log scale: energy sits in a few wires (median
DECADES = 3                        # 0.01 vs p99.5 144 vs max 2.7e5 on the wiremesh)


def nice_ceil(v):
    """Round up to a clean number: 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8 x 10^n."""
    import math
    if v <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(v))
    return next(m * e for m in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if m * e >= v * (1 - 1e-9))


def field_group():
    """Menu Switch over Named Attributes -> (v - Min) / (Max - Min), clamped -> fe_value.
    Stored on face corners for meshes (face results stay per face, no averaging) and on
    points for curves / point clouds."""
    ng = bpy.data.node_groups.get(GROUP)
    if ng:
        return ng
    ng = bpy.data.node_groups.new(GROUP, "GeometryNodeTree")
    ng.is_modifier = True
    io = ng.interface
    io.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    io.new_socket("Field", in_out="INPUT", socket_type="NodeSocketMenu")
    for name, dv in (("Min", 0.0), ("Max", 1.0)):
        io.new_socket(name, in_out="INPUT", socket_type="NodeSocketFloat").default_value = dv
    io.new_socket("Log", in_out="INPUT", socket_type="NodeSocketBool")
    io.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    N, L = ng.nodes, ng.links
    gi, go = N.new("NodeGroupInput"), N.new("NodeGroupOutput")
    ms = N.new("GeometryNodeMenuSwitch")
    ms.data_type = "FLOAT"
    # rename the 2 default items instead of clearing: menu values are internal ids that
    # are never reused, so clearing would shift every field to id+2
    for i, f in enumerate(FIELDS):
        if i < len(ms.enum_items):
            ms.enum_items[i].name = f
        else:
            ms.enum_items.new(f)
    for i, f in enumerate(FIELDS):
        a = N.new("GeometryNodeInputNamedAttribute")
        a.data_type = "FLOAT"
        a.inputs["Name"].default_value = f
        a.location = (-200, -150 * i)
        L.new(a.outputs["Attribute"], ms.inputs[f])
    L.new(gi.outputs["Field"], ms.inputs["Menu"])
    mr = N.new("ShaderNodeMapRange")                         # clamped linear map Min..Max -> 0..1
    mr.clamp = True

    def maybe_log(sock, y):                                  # Log ? log10(max(x, 1e-30)) : x
        mx = N.new("ShaderNodeMath")
        mx.operation, mx.inputs[1].default_value = "MAXIMUM", 1e-30
        lg = N.new("ShaderNodeMath")
        lg.operation, lg.inputs[1].default_value = "LOGARITHM", 10.0
        sw = N.new("GeometryNodeSwitch")
        sw.input_type = "FLOAT"
        L.new(sock, mx.inputs[0]); L.new(mx.outputs[0], lg.inputs[0])
        L.new(gi.outputs["Log"], sw.inputs["Switch"])
        L.new(sock, sw.inputs["False"]); L.new(lg.outputs[0], sw.inputs["True"])
        for i, n in enumerate((mx, lg, sw)):
            n.location = (220 + i * 160, y)
        return sw.outputs[0]

    L.new(maybe_log(ms.outputs[0], -300), mr.inputs["Value"])
    L.new(maybe_log(gi.outputs["Min"], -500), mr.inputs["From Min"])
    L.new(maybe_log(gi.outputs["Max"], -700), mr.inputs["From Max"])
    sep = N.new("GeometryNodeSeparateComponents")
    L.new(gi.outputs["Geometry"], sep.inputs["Geometry"])
    join = N.new("GeometryNodeJoinGeometry")
    for comp, domain in (("Mesh", "CORNER"), ("Curve", "POINT"), ("Point Cloud", "POINT")):
        st = N.new("GeometryNodeStoreNamedAttribute")
        st.data_type, st.domain = "FLOAT", domain
        st.inputs["Name"].default_value = "fe_value"
        L.new(sep.outputs[comp], st.inputs["Geometry"])
        L.new(mr.outputs["Result"], st.inputs["Value"])
        L.new(st.outputs["Geometry"], join.inputs["Geometry"])
    for comp in ("Volume", "Instances"):
        L.new(sep.outputs[comp], join.inputs["Geometry"])
    L.new(join.outputs["Geometry"], go.inputs["Geometry"])
    for i, n in enumerate([gi, ms, mr, sep, join, go]):
        n.location = (i * 220 - 400, 200)
    return ng


def _ident(ng, name):
    return ng.interface.items_tree[name].identifier


def add_field_modifier(ob, scene):
    """Append "Dyna Field" (after the USD cache / beam modifiers), inputs driven by
    the scene props fe_field / fe_min / fe_max."""
    ng = field_group()
    # props must exist before the drivers: a driver created on a missing prop is
    # flagged invalid and stays dead until the file is reloaded
    for prop, dv in (("fe_field", 0), ("fe_min", 0.0), ("fe_max", 1.0), ("fe_log", 0)):
        if prop not in scene:
            scene[prop] = dv
    m = ob.modifiers.get(GROUP) or ob.modifiers.new(GROUP, "NODES")
    m.node_group = ng
    # a menu input stores the item index; plain "v" keeps the drivers simple
    # expressions, which Blender runs even with Python auto-run disabled
    for sock, prop in (("Field", "fe_field"), ("Min", "fe_min"), ("Max", "fe_max"), ("Log", "fe_log")):
        path = f'["{_ident(ng, sock)}"]'
        m.driver_remove(path)
        dr = m.driver_add(path).driver
        dr.type = "SCRIPTED"
        var = dr.variables.new()
        var.name, var.type = "v", "SINGLE_PROP"
        var.targets[0].id_type, var.targets[0].id = "SCENE", scene
        var.targets[0].data_path = f'["{prop}"]'
        dr.expression = "v"
    return m


def field_range(scene, objs, field, pct=99.5, log=False):
    """(min, max) legend range over the animation: pct-th percentile of |value| rounded
    to a clean number; symmetric for signed fields, from 0 otherwise, DECADES below the
    max on a log scale."""
    vals = []
    for f in range(scene.frame_start, scene.frame_end + 1, 2):
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        for o in objs:
            a = o.evaluated_get(dg).data.attributes.get(field)
            if a is not None and len(a.data):
                v = np.empty(len(a.data), np.float32)
                a.data.foreach_get("value", v)
                vals.append(v)
    scene.frame_set(scene.frame_start)
    assert vals, (f"no '{field}' attribute on the model: re-convert with --fields {field} "
                  "(converter/d3plot_to_usd_lasso.py)")
    v = np.concatenate(vals)
    top = nice_ceil(float(np.percentile(np.abs(v) if field in SIGNED else v, pct)))
    lo = top / 10 ** DECADES if log else (-top if field in SIGNED else 0.0)
    print(f"field {field}: max {v.max():.4g}, min {v.min():.4g}, legend {lo:g}..{top:g}"
          f"{' (log)' if log else ''}")
    return lo, top


def legend_values(vmin, vmax, n=9, log=False):
    """n+1 legend values at the band edges (geometric on a log scale)."""
    if log:
        return [vmin * (vmax / vmin) ** (i / n) for i in range(n + 1)]
    return [vmin + (vmax - vmin) * i / n for i in range(n + 1)]


def set_field(scene, field, vmin=None, vmax=None, pct=99.5, objs=None, log=None):
    """Switch every Dyna Field modifier to `field`, set the legend range (computed if not
    given) and scale (log default for LOG fields) and relabel the legend text objects
    if the look built one."""
    objs = objs or [o for o in scene.objects if GROUP in o.modifiers]
    log = field in LOG if log is None else log
    scene["fe_field"] = FIELDS.index(field)
    if vmin is None or vmax is None:
        vmin, vmax = field_range(scene, objs, field, pct, log)
    scene["fe_min"], scene["fe_max"], scene["fe_log"] = float(vmin), float(vmax), int(log)
    for i, val in enumerate(legend_values(vmin, vmax, log=log)):
        t = bpy.data.objects.get(f"FE_tick_{i}")
        if t:
            t.data.body = f"{val:.4g}"
    t = bpy.data.objects.get("FE_title")
    if t:
        t.data.body = TITLES[field]
    scene.update_tag()                                       # Python prop edits don't tag
    bpy.context.view_layer.update()                          # drivers re-read the props
    return vmin, vmax


def _selfcheck():
    """Mesh with two face values: switching fields through the scene props changes fe_value."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    me = bpy.data.meshes.new("m")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (2, 0, 0), (2, 1, 0)], [],
                   [(0, 1, 2, 3), (1, 4, 5, 2)])
    for name, vals in (("von_mises", [100.0, 300.0]), ("plastic_strain", [0.0, 0.5])):
        a = me.attributes.new(name, "FLOAT", "FACE")
        a.data.foreach_set("value", vals)
    ob = bpy.data.objects.new("o", me)
    sc.collection.objects.link(ob)
    add_field_modifier(ob, sc)

    def fe():
        dg = bpy.context.evaluated_depsgraph_get()
        a = ob.evaluated_get(dg).data.attributes["fe_value"]
        assert a.domain == "CORNER"
        v = np.empty(len(a.data), np.float32)
        a.data.foreach_get("value", v)
        return sorted(set(np.round(v, 4)))

    set_field(sc, "von_mises", 0, 400)
    assert fe() == [0.25, 0.75], fe()
    set_field(sc, "plastic_strain", 0, 1)
    assert fe() == [0.0, 0.5], fe()
    assert set_field(sc, "von_mises")[1] == 300                # p99.5 of 100/300 -> clean 300
    set_field(sc, "von_mises", 1, 1000, log=True)            # log: 100 -> 2/3, 300 -> 0.826
    assert fe() == [0.6667, 0.8257], fe()
    assert np.allclose(legend_values(0.1, 100, 3, log=True), [0.1, 1, 10, 100])
    assert nice_ceil(1967) == 2000 and nice_ceil(444.4) == 500 and legend_values(-5, 5)[0] == -5
    print("field selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
