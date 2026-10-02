"""Base look creator: turn an imported dyna2usd crash into a render-ready scene.

    blender -b --python blender/look.py -- --usd crash.usdc --look fe --field von_mises \
            --out crash_fe.blend --render fe.png --frame 40
    blender -b crash.blend --python blender/look.py -- --look studio --out crash_studio.blend
    blender -b --python blender/look.py                     # self-check

Looks (add one = write build_<name>(ctx, args) and register it in LOOKS):
  studio  photoreal product shot: cyclorama, 3 area lights, satin part colours,
          metal for rigid parts and beams, Cycles + AgX. --color-by <field>: satin
          colour-by-result (same JET9 bands + legend as fe).
  fe      LS-PrePost style (FE look guidelines): flat emissive banded JET9 fringe on
          one result field, headlight + AO shade, element lines, grey rigid parts,
          grey studio gradient, stepped legend.

Result colours go through field.py's "Dyna Field" modifier (attribute fe_value), so the
field can be switched later for every object from the scene props / field.set_field().

Shared base: imported USD lights removed, camera framed on the union of the model's
bounds over the whole animation, rigid parts detected from the data (edge lengths
unchanged between first and last frame = moves but never deforms).
Everything the script creates lives in the "dyna_look" collection; re-running
replaces it.
"""
import argparse
import math
import os
import sys
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dyna_import  # noqa: E402
import field  # noqa: E402

COLL = "dyna_look"
# FE look guidelines §2: 9 constant bands, legend shows the same 9 colours
JET9 = [(0, 0, 1), (0, .35, 1), (0, .75, 1), (0, 1, .65), (0, 1, 0),
        (.6, 1, 0), (1, 1, 0), (1, .5, 0), (1, 0, 0)]
PASTEL10 = [(.62, .74, .91), (.95, .71, .62), (.70, .87, .69), (.96, .86, .58), (.80, .70, .90),
            (.62, .88, .88), (.94, .70, .80), (.85, .85, .66), (.70, .78, .70), (.88, .78, .68)]
VIEWS = {"iso": (1, -1, .6), "iso2": (-1, -1, .6), "+x": (1, 0, .15), "-x": (-1, 0, .15),
         "+y": (0, 1, .15), "-y": (0, -1, .15), "top": (0, -.001, 1)}


# ---------------------------------------------------------------- pure helpers
nice_ceil = field.nice_ceil
ticks = field.legend_values


def deforms(p0, p1, edges, tol=1e-5):
    """True if any edge length changes by more than tol x median edge (rigid motion
    keeps every edge length, so translation/rotation alone reads as rigid). tol is
    float noise only: LS-DYNA rigid parts measure exactly 0, while elastic car parts
    that barely deform still sit at 1e-4..1e-2 and must keep their fringe."""
    if len(edges) == 0:
        return False
    l0 = np.linalg.norm(p0[edges[:, 0]] - p0[edges[:, 1]], axis=1)
    l1 = np.linalg.norm(p1[edges[:, 0]] - p1[edges[:, 1]], axis=1)
    return bool(np.abs(l1 - l0).max() > tol * max(np.median(l0), 1e-12))


def frame_distance(lo, hi, center, d, right, up, tan_x, tan_y):
    """Camera distance from center along d so all 8 bbox corners fit the frustum."""
    best = 0.0
    for c in [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]:
        v = c - center
        best = max(best, v.dot(d) + max(abs(v.dot(right)) / tan_x, abs(v.dot(up)) / tan_y))
    return best


def line_radius(elem, px):
    """Element-line radius (same units as inputs): 1/40 element, but at least half a
    pixel so it stays readable. None = lines would bury the fringe (< 4 px elements)."""
    return None if elem < 4 * px else max(elem / 40, 0.5 * px)


# ---------------------------------------------------------------- blender helpers
def look_collection(scene):
    c = bpy.data.collections.get(COLL)
    if c:
        for o in list(c.objects):
            bpy.data.objects.remove(o)
    else:
        c = bpy.data.collections.new(COLL)
        scene.collection.children.link(c)
    return c


def link(nt, a, b):
    nt.links.new(a, b)


def sock(sockets, name, typ):
    return next(s for s in sockets if s.name == name and s.type == typ)


def node(nt, typ, **props):
    n = nt.nodes.new(typ)
    for k, v in props.items():
        setattr(n, k, v)
    return n


def new_material(name):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    m.node_tree.nodes.clear()
    return m, m.node_tree


def emission_out(nt, color_socket, strength=1.0):
    em = node(nt, "ShaderNodeEmission")
    em.inputs["Strength"].default_value = strength
    link(nt, color_socket, em.inputs["Color"])
    out = node(nt, "ShaderNodeOutputMaterial")
    link(nt, em.outputs[0], out.inputs["Surface"])
    return em


def math_node(nt, op, a, b=None, clamp=False):
    n = node(nt, "ShaderNodeMath", operation=op, use_clamp=clamp)
    for i, v in enumerate((a, b)):
        if v is None:
            continue
        if isinstance(v, (int, float)):
            n.inputs[i].default_value = v
        else:
            link(nt, v, n.inputs[i])
    return n.outputs[0]


def ramp(nt, fac, colors, interp="CONSTANT"):
    r = node(nt, "ShaderNodeValToRGB")
    cr = r.color_ramp
    cr.interpolation = interp
    while len(cr.elements) > 1:
        cr.elements.remove(cr.elements[-1])
    for i, c in enumerate(colors):
        e = cr.elements[0] if i == 0 else cr.elements.new(i / len(colors))
        e.position, e.color = i / len(colors), (*c, 1)
    link(nt, fac, r.inputs["Fac"])
    return r.outputs["Color"]


def scene_prop(nt, name):
    a = node(nt, "ShaderNodeAttribute", attribute_type="VIEW_LAYER", attribute_name=name)
    return a.outputs["Factor"]


def assign(ob, mat):
    ob.data.materials.clear()
    ob.data.materials.append(mat)


def frames(scene, step=5):
    f = list(range(scene.frame_start, scene.frame_end + 1, step))
    return f if f[-1] == scene.frame_end else f + [scene.frame_end]


def eval_points(ob, dg):
    """Evaluated local positions (n, 3) of a mesh/curves object."""
    data = ob.evaluated_get(dg).data
    pts = data.vertices if ob.type == "MESH" else data.points
    p = np.empty(len(pts) * 3, np.float32)
    data.attributes["position"].data.foreach_get("vector", p)
    return p.reshape(-1, 3)


def eval_attr(ob, dg, name):
    data = ob.evaluated_get(dg).data
    a = data.attributes.get(name)
    if a is None:
        return None
    v = np.empty(len(a.data), np.float32)
    a.data.foreach_get("value", v)
    return v


# ---------------------------------------------------------------- shared base
class Ctx:
    """Scene facts every look needs, gathered once by scanning the animation."""

    def __init__(self, scene, fit_frames=None):
        self.scene = scene
        self.meshes = [o for o in scene.objects if o.type == "MESH" and o.visible_get()]
        self.curves = [o for o in scene.objects if o.type == "CURVES" and o.visible_get()]
        geo = self.meshes + self.curves
        assert geo, "no imported geometry in scene: pass --usd or open an imported .blend"
        lo, hi = Vector((1e30,) * 3), Vector((-1e30,) * 3)
        f0, f1 = scene.frame_start, scene.frame_end
        scene.frame_set(f0)
        dg = bpy.context.evaluated_depsgraph_get()
        p0 = {o.name: eval_points(o, dg) for o in self.meshes}
        edges = {}
        for o in self.meshes:
            me = o.evaluated_get(dg).data
            e = np.empty(len(me.edges) * 2, np.int32)
            me.edges.foreach_get("vertices", e)
            edges[o.name] = e.reshape(-1, 2)
        scene.frame_set(f1)
        dg = bpy.context.evaluated_depsgraph_get()
        self.rigid = set()
        for o in self.meshes:
            p1 = eval_points(o, dg)
            if len(p1) == len(p0[o.name]) and not deforms(p0[o.name], p1, edges[o.name]):
                self.rigid.add(o.name)
        # frame on what deforms: rigid barriers/ground can be 10-100x the subject
        subject = [o for o in geo if o.name not in self.rigid] or geo
        for f in fit_frames or frames(scene):
            scene.frame_set(f)
            dg = bpy.context.evaluated_depsgraph_get()
            for o in subject:
                for c in o.evaluated_get(dg).bound_box:
                    w = o.matrix_world @ Vector(c)
                    lo, hi = Vector(map(min, lo, w)), Vector(map(max, hi, w))
        self.lo, self.hi = lo, hi
        self.center = (lo + hi) / 2
        self.diag = (hi - lo).length
        # median element edge (local units, mm) over deforming meshes
        ls = [np.linalg.norm(p0[n][e[:, 0]] - p0[n][e[:, 1]], axis=1)
              for n, e in edges.items() if n not in self.rigid and len(e)]
        self.elem = float(np.median(np.concatenate(ls))) if ls else 0.0
        self.local_scale = (self.meshes or self.curves)[0].matrix_world.to_scale()[0]
        scene.frame_set(f0)
        print(f"look: {len(self.meshes)} meshes ({len(self.rigid)} rigid), {len(self.curves)} beam "
              f"objects, size {self.diag:.2f} m, median element {self.elem:.2f} (local units)")


def base_scene(ctx, args):
    sc = ctx.scene
    for o in [o for o in sc.objects if o.type == "LIGHT"]:   # USD studio lights: replaced
        bpy.data.objects.remove(o)
    coll = look_collection(sc)
    cam_data = bpy.data.cameras.new("dyna_cam")
    cam_data.lens = args.lens
    cam = bpy.data.objects.new("dyna_cam", cam_data)
    coll.objects.link(cam)
    sc.camera = cam
    d = Vector(VIEWS[args.view]).normalized()
    sc.render.resolution_x, sc.render.resolution_y = args.res
    sc.render.resolution_percentage = 100
    fov = 2 * math.atan(cam_data.sensor_width / (2 * cam_data.lens))
    rot = (-d).to_track_quat("-Z", "Y")
    dist = frame_distance(ctx.lo, ctx.hi, ctx.center, d, rot @ Vector((1, 0, 0)),
                          rot @ Vector((0, 1, 0)), math.tan(fov / 2),
                          math.tan(fov / 2) * args.res[1] / args.res[0]) * args.margin
    cam.location = ctx.center + d * dist
    cam.rotation_euler = rot.to_euler()
    cam_data.clip_start, cam_data.clip_end = dist * 0.01, dist * 4
    ctx.cam, ctx.dist, ctx.coll = cam, dist, coll
    # world width of the frame at the model -> size of one pixel, in local units
    ctx.px = 2 * dist * math.tan(fov / 2) / args.res[0] / ctx.local_scale
    sc.render.engine = "CYCLES" if args.engine == "cycles" else "BLENDER_EEVEE"
    if args.engine == "cycles":
        sc.cycles.samples = args.samples
        sc.cycles.use_denoising = True
        use_gpu(sc)
    return coll


def use_gpu(sc):
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for kind in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
            try:
                prefs.compute_device_type = kind
            except TypeError:
                continue
            prefs.get_devices()
            if any(dv.type == kind for dv in prefs.devices):
                for dv in prefs.devices:
                    dv.use = dv.type == kind
                sc.cycles.device = "GPU"
                return
    except Exception as e:  # no GPU / no cycles addon: CPU render still works
        print(f"look: GPU not enabled ({e}); rendering on CPU")


# ---------------------------------------------------------------- studio look
def build_studio(ctx, args):
    sc = ctx.scene
    coll = ctx.coll
    # rigid parts much bigger than the subject (sim ground, long rails) are set
    # dressing: the cyclorama replaces them
    for o in ctx.meshes:
        if o.name in ctx.rigid and max(o.dimensions) > 2 * ctx.diag:
            o.hide_render = o.hide_viewport = True
            print(f"look studio: hid oversized rigid part {o.name}")
    # cyclorama: floor + curved back wall facing the camera, sized to cover the view
    floor_z, R = ctx.lo.z, ctx.diag * 1.5
    W, D, H = ctx.dist * 6, ctx.dist * 3, ctx.dist * 3
    prof = [(-D, 0)] + [(R * math.sin(t), R - R * math.cos(t))
                        for t in np.linspace(0, math.pi / 2, 16)] + [(R, H)]
    verts = [(x, y, z) for x in (-W / 2, W / 2) for (y, z) in prof]
    n = len(prof)
    faces = [(i, i + 1, n + i + 1, n + i) for i in range(n - 1)]
    me = bpy.data.meshes.new("dyna_cyc")
    me.from_pydata(verts, [], faces)
    me.shade_smooth() if hasattr(me, "shade_smooth") else None
    cyc = bpy.data.objects.new("dyna_cyc", me)
    coll.objects.link(cyc)
    az = math.atan2(ctx.cam.location.y - ctx.center.y, ctx.cam.location.x - ctx.center.x)
    back = ctx.center.xy - Vector((math.cos(az), math.sin(az))) * ctx.diag * 0.9
    cyc.matrix_world = (Matrix.Translation((back.x, back.y, floor_z))
                        @ Matrix.Rotation(az + math.pi / 2, 4, "Z"))
    m, nt = new_material("dyna_cyc")
    bs = node(nt, "ShaderNodeBsdfPrincipled")
    bs.inputs["Base Color"].default_value = (.45, .46, .48, 1)
    bs.inputs["Roughness"].default_value = .8
    link(nt, bs.outputs[0], node(nt, "ShaderNodeOutputMaterial").inputs["Surface"])
    assign(cyc, m)

    # 3 area lights: big soft key, fill, rim; power scales with distance^2
    # ponytail: powers tuned by eye on the car + wiremesh renders; scale with diag^2
    for name, az_off, el, power, size in [("key", 40, 45, 250, .9), ("fill", -70, 25, 60, 1.2),
                                          ("rim", 170, 55, 200, .6)]:
        ld = bpy.data.lights.new(f"dyna_{name}", "AREA")
        ld.shape, ld.size = "DISK", ctx.diag * size
        L = bpy.data.objects.new(f"dyna_{name}", ld)
        coll.objects.link(L)
        a, e = az + math.radians(az_off), math.radians(el)
        dirv = Vector((math.cos(a) * math.cos(e), math.sin(a) * math.cos(e), math.sin(e)))
        L.location = ctx.center + dirv * ctx.diag * 1.4
        L.rotation_euler = (-dirv).to_track_quat("-Z", "Y").to_euler()
        ld.energy = power * (ctx.diag * 1.4 / 5) ** 2
    world = bpy.data.worlds.new("dyna_world")
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs["Color"].default_value = (.05, .05, .055, 1)
    world.node_tree.nodes["Background"].inputs["Strength"].default_value = .4
    sc.world = world

    # materials: satin part colours (kept from the USD), brushed metal for rigid/beams
    steel, nt = new_material("dyna_steel")
    bs = node(nt, "ShaderNodeBsdfPrincipled")
    bs.inputs["Base Color"].default_value = (.55, .56, .58, 1)
    bs.inputs["Metallic"].default_value = 1
    bs.inputs["Roughness"].default_value = .32
    link(nt, bs.outputs[0], node(nt, "ShaderNodeOutputMaterial").inputs["Surface"])
    for o in ctx.meshes:
        if o.name in ctx.rigid:
            assign(o, steel)
            continue
        src = o.data.materials[0] if o.data.materials else None
        col = (.7, .7, .7, 1)
        if src and src.node_tree and "Principled BSDF" in src.node_tree.nodes:
            col = tuple(src.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value)
        m, nt = new_material(f"dyna_satin_{o.name}")
        bs = node(nt, "ShaderNodeBsdfPrincipled")
        bs.inputs["Base Color"].default_value = col
        bs.inputs["Roughness"].default_value = .38
        bs.inputs["Coat Weight"].default_value = .4
        bs.inputs["Coat Roughness"].default_value = .08
        link(nt, bs.outputs[0], node(nt, "ShaderNodeOutputMaterial").inputs["Surface"])
        assign(o, m)
    for o in ctx.curves:
        assign(o, steel)
    if args.color_by:
        objs, _, vmin, vmax = setup_field(ctx, args, args.color_by)
        m, nt = new_material("dyna_result")
        bs = node(nt, "ShaderNodeBsdfPrincipled")
        link(nt, ramp(nt, fe_value(nt), JET9), bs.inputs["Base Color"])
        bs.inputs["Roughness"].default_value = .38
        bs.inputs["Coat Weight"].default_value = .4
        bs.inputs["Coat Roughness"].default_value = .08
        link(nt, bs.outputs[0], node(nt, "ShaderNodeOutputMaterial").inputs["Surface"])
        for o in objs:
            assign(o, m)
        if args.legend:
            fe_legend(ctx, args, vmin, vmax, args.color_by)
    sc.view_settings.view_transform = "AgX"
    try:
        sc.view_settings.look = "AgX - Medium High Contrast"
    except TypeError:
        pass


# ---------------------------------------------------------------- FE look
def fe_material(ctx, args):
    """FE_Look: Emission(mix(part colours, JET9 fringe, fe_stress) x headlight shade)."""
    m, nt = new_material("FE_Look")
    fac = fe_value(nt)
    fringe = ramp(nt, fac, JET9)
    part = ramp(nt, node(nt, "ShaderNodeObjectInfo").outputs["Random"], PASTEL10)
    mix = node(nt, "ShaderNodeMix", data_type="RGBA")
    link(nt, scene_prop(nt, "fe_stress"), mix.inputs[0])
    link(nt, part, sock(mix.inputs, "A", "RGBA"))
    link(nt, fringe, sock(mix.inputs, "B", "RGBA"))
    shade = fe_shade(nt, ctx.diag * args.ao)
    col = node(nt, "ShaderNodeMix", data_type="RGBA", blend_type="MULTIPLY")
    col.inputs[0].default_value = 1
    link(nt, mix.outputs[2], sock(col.inputs, "A", "RGBA"))
    link(nt, shade, sock(col.inputs, "B", "RGBA"))
    emission_out(nt, col.outputs[2])
    return m


def fe_value(nt):
    """Normalised result (Dyna Field modifier), top edge kept inside the red band."""
    v = node(nt, "ShaderNodeAttribute", attribute_type="GEOMETRY", attribute_name="fe_value")
    return math_node(nt, "MINIMUM", v.outputs["Factor"], 0.9999)


def fe_shade(nt, ao_dist):
    """(0.6 max(N.I,0) + 0.22 + 0.18 (0.5 + 0.5 N_z)) x (0.45 AO + 0.55)."""
    g = node(nt, "ShaderNodeNewGeometry")
    ndi = node(nt, "ShaderNodeVectorMath", operation="DOT_PRODUCT")
    link(nt, g.outputs["Normal"], ndi.inputs[0])
    link(nt, g.outputs["Incoming"], ndi.inputs[1])
    head = math_node(nt, "MULTIPLY_ADD", math_node(nt, "MAXIMUM", ndi.outputs["Value"], 0.0), .6)
    head.node.inputs[2].default_value = .22
    nz = node(nt, "ShaderNodeSeparateXYZ")
    link(nt, g.outputs["Normal"], nz.inputs[0])
    top = math_node(nt, "MULTIPLY_ADD", nz.outputs["Z"], .09)
    top.node.inputs[2].default_value = .09                   # 0.18 (0.5 + 0.5 Nz)
    ao = node(nt, "ShaderNodeAmbientOcclusion", samples=8)
    ao.inputs["Distance"].default_value = ao_dist
    aot = math_node(nt, "MULTIPLY_ADD", ao.outputs["AO"], .45)
    aot.node.inputs[2].default_value = .55
    return math_node(nt, "MULTIPLY", math_node(nt, "ADD", head, top), aot)


def fe_rigid_material():
    """FE_Rigid_Look: lerp(black, (0.72, 0.73, 0.75), 0.55 max(N.I,0) + 0.3)."""
    m, nt = new_material("FE_Rigid_Look")
    g = node(nt, "ShaderNodeNewGeometry")
    ndi = node(nt, "ShaderNodeVectorMath", operation="DOT_PRODUCT")
    link(nt, g.outputs["Normal"], ndi.inputs[0])
    link(nt, g.outputs["Incoming"], ndi.inputs[1])
    t = math_node(nt, "MULTIPLY_ADD", math_node(nt, "MAXIMUM", ndi.outputs["Value"], 0.0), .55)
    t.node.inputs[2].default_value = .3
    mix = node(nt, "ShaderNodeMix", data_type="RGBA")
    link(nt, t, mix.inputs[0])
    sock(mix.inputs, "A", "RGBA").default_value = (0, 0, 0, 1)
    sock(mix.inputs, "B", "RGBA").default_value = (.72, .73, .75, 1)
    emission_out(nt, mix.outputs[2])
    return m


def fe_lines_group(line_mat):
    """FE_Lines: element edges -> 4-sided tubes (Mesh to Curve -> Curve to Mesh), joined."""
    ng = bpy.data.node_groups.get("FE_Lines")
    if ng:
        bpy.data.node_groups.remove(ng)
    ng = bpy.data.node_groups.new("FE_Lines", "GeometryNodeTree")
    ng.is_modifier = True
    io = ng.interface
    io.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    r = io.new_socket("Radius", in_out="INPUT", socket_type="NodeSocketFloat")
    r.default_value, r.min_value = .2, 0
    io.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    N, L = ng.nodes, ng.links
    gi, go = N.new("NodeGroupInput"), N.new("NodeGroupOutput")
    m2c = N.new("GeometryNodeMeshToCurve")
    circ = N.new("GeometryNodeCurvePrimitiveCircle")
    circ.inputs["Resolution"].default_value = 4
    circ.inputs["Radius"].default_value = 1.0
    c2m = N.new("GeometryNodeCurveToMesh")
    sm = N.new("GeometryNodeSetMaterial")
    sm.inputs["Material"].default_value = line_mat
    join = N.new("GeometryNodeJoinGeometry")
    L.new(gi.outputs["Geometry"], m2c.inputs["Mesh"])
    L.new(m2c.outputs["Curve"], c2m.inputs["Curve"])
    L.new(circ.outputs["Curve"], c2m.inputs["Profile Curve"])
    L.new(gi.outputs["Radius"], c2m.inputs["Scale"])
    L.new(c2m.outputs["Mesh"], sm.inputs["Geometry"])
    L.new(gi.outputs["Geometry"], join.inputs["Geometry"])
    L.new(sm.outputs["Geometry"], join.inputs["Geometry"])
    L.new(join.outputs["Geometry"], go.inputs["Geometry"])
    for i, n in enumerate([gi, m2c, c2m, sm, join, go]):
        n.location = (i * 200, 0)
    circ.location = (200, -200)
    return ng


def fe_world(sc):
    """Grey studio gradient by window y: (0.30,0.33,0.37) bottom -> (0.80,0.83,0.87) top."""
    w = bpy.data.worlds.new("FE_Studio")
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    tc = node(nt, "ShaderNodeTexCoord")
    xyz = node(nt, "ShaderNodeSeparateXYZ")
    link(nt, tc.outputs["Window"], xyz.inputs[0])
    mix = node(nt, "ShaderNodeMix", data_type="RGBA")
    link(nt, xyz.outputs["Y"], mix.inputs[0])
    sock(mix.inputs, "A", "RGBA").default_value = (.30, .33, .37, 1)
    sock(mix.inputs, "B", "RGBA").default_value = (.80, .83, .87, 1)
    bg = node(nt, "ShaderNodeBackground")
    link(nt, mix.outputs[2], bg.inputs["Color"])
    link(nt, bg.outputs[0], node(nt, "ShaderNodeOutputWorld").inputs["Surface"])
    sc.world = w


def flat_emission(name, rgb, alpha=1.0):
    m, nt = new_material(name)
    em = node(nt, "ShaderNodeEmission")
    em.inputs["Color"].default_value = (*rgb, 1)
    out = node(nt, "ShaderNodeOutputMaterial")
    if alpha >= 1:
        link(nt, em.outputs[0], out.inputs["Surface"])
    else:
        mx = node(nt, "ShaderNodeMixShader")
        mx.inputs[0].default_value = alpha
        link(nt, node(nt, "ShaderNodeBsdfTransparent").outputs[0], mx.inputs[1])
        link(nt, em.outputs[0], mx.inputs[2])
        link(nt, mx.outputs[0], out.inputs["Surface"])
    return m


def fe_legend(ctx, args, vmin, vmax, fld):
    """Stepped JET9 bar + 10 edge values + title on a 60 % light panel, parented to
    the camera on the right side of frame.
    ponytail: in-scene, no DOF; a separate HUD scene (guidelines §6) when DOF is needed."""
    cam = ctx.cam
    d = cam.data.clip_start * 5
    half_w = d * math.tan(cam.data.angle / 2)
    half_h = half_w * args.res[1] / args.res[0]
    bar_h, bar_w = half_h * 1.1, half_h * .07
    x0, y0 = half_w * .80, -bar_h / 2
    ink = flat_emission("FE_Ink", (.03, .035, .04))
    txt_h = bar_h / 9 * .55

    def plane(name, x, y, w, h, mat, z=0.0):
        me = bpy.data.meshes.new(name)
        me.from_pydata([(x, y, z), (x + w, y, z), (x + w, y + h, z), (x, y + h, z)], [], [(0, 1, 2, 3)])
        ob = bpy.data.objects.new(name, me)
        ctx.coll.objects.link(ob)
        ob.parent = cam
        ob.location = (0, 0, -d)
        assign(ob, mat)
        return ob

    def text(name, body, x, y, size, align="LEFT"):  # returns the object
        cu = bpy.data.curves.new(name, "FONT")
        cu.body, cu.size, cu.align_x, cu.align_y = body, size, align, "CENTER"
        ob = bpy.data.objects.new(name, cu)
        ctx.coll.objects.link(ob)
        ob.parent = cam
        ob.location = (x, y, -d + 1e-4 * d)
        assign(ob, ink)
        return ob

    for i, c in enumerate(JET9):
        plane(f"FE_band_{i}", x0, y0 + bar_h * i / 9, bar_w, bar_h / 9, flat_emission(f"FE_jet_{i}", c))
    for i, val in enumerate(ticks(vmin, vmax, log=getattr(ctx, "log", False))):
        text(f"FE_tick_{i}", f"{val:.4g}", x0 + bar_w * 1.25, y0 + bar_h * i / 9, txt_h)
    title = text("FE_title", args.title or field.TITLES[fld], x0 + bar_w * 3,
                 y0 + bar_h + txt_h * 2.2, txt_h * 1.1, "RIGHT")
    bpy.context.view_layer.update()                          # text dimensions need an eval
    pad = txt_h
    left = min(x0 - pad * 7.5, x0 + bar_w * 3 - title.dimensions.x - pad)
    plane("FE_panel", left, y0 - pad * 1.5, half_w - left - pad * .1, bar_h + pad * 5,
          flat_emission("FE_Panel", (.82, .83, .85), alpha=.6), z=-1e-4 * d)
    cam.data.shift_x = .09                                   # model left of the legend panel


def result_objects(ctx, fld):
    """(objects carrying the field and deforming, the rest) at the first frame."""
    dg = bpy.context.evaluated_depsgraph_get()
    has, rest = [], []
    for o in ctx.meshes + ctx.curves:
        ok = o.name not in ctx.rigid and o.evaluated_get(dg).data.attributes.get(fld) is not None
        (has if ok else rest).append(o)
    return has, rest


def setup_field(ctx, args, fld):
    """Dyna Field modifiers on result objects + legend range -> (objs, rest, vmin, vmax)."""
    objs, rest = result_objects(ctx, fld)
    assert objs, (f"no deforming object carries '{fld}': re-convert with --fields {fld} "
                  "(converter/d3plot_to_usd_lasso.py)")
    for o in objs:
        field.add_field_modifier(o, ctx.scene)
    vmin = vmax = None
    log = fld in field.LOG if args.scale == "auto" else args.scale == "log"
    if args.legend_max:
        vmax = args.legend_max
        vmin = vmax / 10 ** field.DECADES if log else (-vmax if fld in field.SIGNED else 0.0)
    vmin, vmax = field.set_field(ctx.scene, fld, vmin, vmax, args.legend_pct, objs, log)
    ctx.log = log
    return objs, rest, vmin, vmax


def build_fe(ctx, args):
    sc = ctx.scene
    objs, rest, vmin, vmax = setup_field(ctx, args, args.field)
    sc["fe_stress"] = 1.0                                    # 0 = part colours, key it
    fe = fe_material(ctx, args)
    rigid = fe_rigid_material()
    line_mat = flat_emission("FE_Line", (.02, .02, .025))
    radius = {"off": None, "auto": line_radius(ctx.elem, ctx.px),
              "on": max(ctx.elem / 40, .5 * ctx.px)}[args.lines]
    lines = fe_lines_group(line_mat) if radius else None
    for o in rest:
        assign(o, rigid)
    for o in objs:
        assign(o, fe)
        if lines and o.type == "MESH":
            mod = o.modifiers.get("FE_Lines") or o.modifiers.new("FE_Lines", "NODES")
            mod.node_group = lines
            dyna_import.set_input(mod, "Radius", radius)
            mod.show_viewport = False                        # render-only: ~4x the triangles
    fe_world(sc)
    sc.view_settings.view_transform = "Standard"             # emission colours exact, no tonemap
    sc.view_settings.look = "None"
    if args.legend:
        fe_legend(ctx, args, vmin, vmax, args.field)
    print(f"look fe: field {args.field}, legend {vmin:g}..{vmax:g}, element lines "
          f"{'radius %.3g (local units)' % radius if radius else 'off (elements < 4 px or --lines off)'}")


LOOKS = {"studio": build_studio, "fe": build_fe}


def parse(argv):
    ap = argparse.ArgumentParser(prog="look.py")
    ap.add_argument("--usd", help="import this dyna2usd .usdc first (else use the open .blend)")
    ap.add_argument("--look", choices=LOOKS, default="studio")
    ap.add_argument("--field", choices=field.FIELDS, default="von_mises", help="fe: fringe field")
    ap.add_argument("--color-by", choices=field.FIELDS, help="studio: colour-by-result field")
    ap.add_argument("--legend-max", type=float,
                    help="fixed legend max (signed fields: symmetric -max..max)")
    ap.add_argument("--legend-pct", type=float, default=99.5,
                    help="fe: legend max = this percentile over the animation, rounded up "
                         "(100 = true max; values above show red)")
    ap.add_argument("--scale", choices=["auto", "linear", "log"], default="auto",
                    help=f"legend scale (auto: log for {sorted(field.LOG)}, {field.DECADES} decades)")
    ap.add_argument("--title", help="fe: legend title (default: field name + units)")
    ap.add_argument("--no-legend", dest="legend", action="store_false")
    ap.add_argument("--lines", choices=["auto", "on", "off"], default="auto",
                    help="fe: element lines (auto = only if elements are >= 4 px on screen)")
    ap.add_argument("--ao", type=float, default=.01, help="fe: AO distance as a fraction of model size")
    ap.add_argument("--view", choices=VIEWS, default="iso")
    ap.add_argument("--lens", type=float, help="mm (default 50 studio, 70 fe)")
    ap.add_argument("--fit", choices=["all", "frame"], default="all",
                    help="frame the camera on the whole animation (moving models stay in shot) "
                         "or only on --frame (tight stills)")
    ap.add_argument("--margin", type=float, default=1.05, help="framing breathing room")
    ap.add_argument("--res", type=int, nargs=2, default=[1920, 1080])
    ap.add_argument("--engine", choices=["cycles", "eevee"], default="cycles")
    ap.add_argument("--samples", type=int, default=128)
    ap.add_argument("--radius", type=float, help="beam radius, mm")
    ap.add_argument("--out", help="save the .blend here")
    ap.add_argument("--render", help="render a still here")
    ap.add_argument("--frame", type=int, help="frame for --render (default: last)")
    a = ap.parse_args(argv)
    a.lens = a.lens or (70 if a.look == "fe" else 50)
    return a


def main(argv):
    args = parse(argv)
    t = time.perf_counter()
    if args.usd:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        dyna_import.import_dyna(args.usd, args.radius)
    sc = bpy.context.scene
    still = sc.frame_end if args.frame is None else args.frame
    ctx = Ctx(sc, [still] if args.fit == "frame" else None)
    print(f"[t] import + scan {time.perf_counter() - t:.1f}s"); t = time.perf_counter()
    base_scene(ctx, args)
    LOOKS[args.look](ctx, args)
    print(f"[t] look built {time.perf_counter() - t:.1f}s"); t = time.perf_counter()
    sc = ctx.scene
    if args.out:
        bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(args.out))
    if args.render:
        sc.frame_set(still)
        sc.render.filepath = os.path.abspath(args.render)
        bpy.ops.render.render(write_still=True)
        print(f"[t] render {time.perf_counter() - t:.1f}s -> {sc.render.filepath}")


def _selfcheck():
    assert nice_ceil(1.45e3) == 1500 and nice_ceil(0.806) == 1 and nice_ceil(212) == 250
    assert ticks(-50, 40)[0] == -50 and ticks(-50, 40)[-1] == 40
    assert nice_ceil(410) == 500 and nice_ceil(500) == 500 and nice_ceil(0.031) == 0.04
    dd = frame_distance(Vector((-1, -1, -1)), Vector((1, 1, 1)), Vector(), Vector((0, 0, 1)),
                        Vector((1, 0, 0)), Vector((0, 1, 0)), 1.0, 0.5)
    assert abs(dd - 3.0) < 1e-9                    # near face z=1 + |y|=1 / tan 0.5
    t = ticks(0, 900)
    assert len(t) == 10 and t[0] == 0 and t[-1] == 900 and t[1] == 100
    sq = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float)
    e = np.array([[0, 1], [1, 2], [2, 3], [3, 0]])
    rot = sq @ np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]]) + 5     # rotate + translate
    assert not deforms(sq, rot, e)                                   # rigid motion
    bent = sq.copy(); bent[2] = [1.5, 1.2, 0]
    assert deforms(sq, bent, e)
    bent[2] = [1, 1.00005, 0]
    assert deforms(sq, bent, e)                                      # elastic 5e-5 keeps fringe
    assert line_radius(5, 2.5) is None                              # 2 px elements: off
    assert line_radius(40, 2) == 1.0 and line_radius(10, 2.4) == 1.2
    print("look selfcheck ok")


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if argv:
        main(argv)
    else:
        _selfcheck()
