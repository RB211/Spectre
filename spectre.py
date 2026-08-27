#!/usr/bin/python
"""Spectre -- a wireframe tank arena, cloned in pygame.

Drive a vector tank around a walled arena, run down the flags before the
clock does, and stay out of the sights of the tanks that come looking for
you.  Everything is drawn as line art, but the solids are solid: faces are
filled with the background and only forward-facing edges are stroked, so a
building hides what stands behind it -- Spectre's hidden-line trick, and the
reason its arenas read as places rather than diagrams.

    /usr/bin/python spectre.py        (pygame lives in the system python)

    W / up      drive              space   fire
    S / down    reverse            shift   turbo
    A D / left right   turn        tab     radar range
    p  pause    f  fullscreen      esc  menu (main menu / quit)

LAN play: one machine picks HOST A LAN GAME and reads out the address the
lobby shows; the others pick JOIN A LAN GAME and type it in (port 35700).
Everyone waits in the lobby until the host presses enter, then it is the
same arena for all of you -- shared flags, shared clock, and the enemy
tanks come for whoever is nearest.
"""

import json
import math
import os
import queue
import random
import socket
import struct
import sys
import threading

# Name the window before SDL loads, so a tiling compositor has something
# stable to hang a rule on (Hyprland: class:^(spectre)$).
for _hint in ("SDL_APP_ID", "SDL_VIDEO_WAYLAND_WMCLASS", "SDL_VIDEO_X11_WMCLASS"):
    os.environ.setdefault(_hint, "spectre")

try:
    import pygame
except ImportError:                                        # pragma: no cover
    sys.exit("spectre: this interpreter has no pygame.\n"
             "        pygame is installed for the system python here -- try:\n"
             "            /usr/bin/python spectre.py")

# ---------------------------------------------------------------- settings --

WIDTH, HEIGHT = 1100, 740
FPS = 60

FOV = math.radians(74)
NEAR, FAR = 0.30, 250.0
ARENA = 115.0           # half-width of the walled floor
EYE = 2.35              # camera height above the deck
GRID = 14.0             # floor grid spacing
GRID_R = 5              # grid cells drawn either side of the player
FILL_EDGE = 3000.0      # slack before a fill is clipped rather than handed on

P_ACCEL, P_BRAKE, P_TOP = 30.0, 46.0, 30.0
P_TURN_TAP = math.radians(30)    # a tap barely nudges: this is the aim
P_TURN_HELD = math.radians(132)  # holding it swings the whole tank round
P_TURN_RAMP = 0.85               # seconds spent winding from one to the other
TURBO = 1.7
SHIELD_MAX = 100.0
AMMO_START = 40
SHOT_SPEED = 115.0
SHOT_DELAY = 0.26
SHOT_DAMAGE = 34.0

COL_BG = (4, 6, 11)
COL_GRID = (24, 78, 60)
COL_WALL = (60, 190, 215)
COL_BUILD = (70, 215, 135)
COL_TOWER = (160, 215, 95)
COL_FLAG = (255, 205, 70)
COL_HUNTER = (255, 95, 95)
COL_SENTRY = (225, 130, 255)
COL_SHOT = (255, 245, 170)
COL_ESHOT = (255, 145, 110)
COL_AMMO = (95, 225, 255)
COL_SHIELD = (125, 165, 255)
COL_HUD = (110, 255, 175)
COL_WARN = (255, 115, 95)
COL_STAR = (38, 50, 76)
COL_WHITE = (235, 255, 245)

# LAN play: one machine hosts and owns the world, everyone else drives a
# tank in it.  Messages are one JSON object per line over TCP -- at LAN
# latencies the simplicity is worth far more than the bytes.
PORT = 35700
MAX_PLAYERS = 4
SNAP_DT = 1.0 / 20.0            # host world snapshots
POSE_DT = 1.0 / 30.0            # everyone's own-tank reports
# Tank colors for LAN play: one per player, and none of them a color the
# world already speaks -- walls are cyan, buildings green, towers olive,
# hunters salmon, sentries lavender, prizes amber / ice / periwinkle.
PLAYER_COLS = ((255, 145, 40),      # amber-orange
               (80, 130, 255),      # cobalt
               (255, 85, 170),      # rose
               (165, 90, 255))      # violet
SHOT_COLS = (COL_SHOT, COL_ESHOT, COL_SENTRY)

TAU = math.pi * 2

# The one thing worth remembering between sessions: what to call you.
SETTINGS_PATH = os.path.expanduser("~/.config/spectre/settings.json")


def default_name():
    return (os.environ.get("USER") or "player").upper()[:10]


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f)
    except OSError:
        pass                       # a name that lasts one session, then

# --------------------------------------------------------------- geometry --


def polyline(points, close=False):
    pts = list(points)
    if close:
        pts.append(pts[0])
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def ring(radius, y, n=8, phase=0.0):
    return [(radius * math.cos(phase + TAU * i / n), y,
             radius * math.sin(phase + TAU * i / n)) for i in range(n)]


def wrap(angle):
    """Fold an angle into -pi .. pi."""
    return (angle + math.pi) % TAU - math.pi


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def clip_rect(x0, y0, x1, y1, w, h):
    """Liang-Barsky: trim a screen line to the window, or drop it."""
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
        if p == 0.0:
            if q < 0.0:
                return None
        else:
            r = q / p
            if p < 0.0:
                if r > t1:
                    return None
                if r > t0:
                    t0 = r
            else:
                if r < t0:
                    return None
                if r < t1:
                    t1 = r
    return (x0 + dx * t0, y0 + dy * t0, x0 + dx * t1, y0 + dy * t1)


def clip_poly(pts, w, h):
    """Sutherland-Hodgman against the window, so a polygon that runs off to
    the horizon never reaches pygame as a coordinate a mile wide."""
    for axis, limit, keep_low in ((0, 0.0, False), (0, w, True),
                                  (1, 0.0, False), (1, h, True)):
        if len(pts) < 3:
            return []
        out = []
        n = len(pts)
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            av, bv = a[axis], b[axis]
            ina = av <= limit if keep_low else av >= limit
            inb = bv <= limit if keep_low else bv >= limit
            if ina:
                out.append(a)
            if ina != inb and bv != av:
                t = (limit - av) / (bv - av)
                out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        pts = out
    return pts


# ------------------------------------------------------------------ solids --
# Spectre's party trick was hidden-line removal: every object is a solid
# filled with the background, with only its facing edges stroked.  So models
# are built out of convex lumps of *faces* rather than loose edges -- fill a
# face black and everything behind it stops existing.


def newell(pts):
    """A face normal, no square roots: only its direction matters here."""
    nx = ny = nz = 0.0
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        nx += (a[1] - b[1]) * (a[2] + b[2])
        ny += (a[2] - b[2]) * (a[0] + b[0])
        nz += (a[0] - b[0]) * (a[1] + b[1])
    return nx, ny, nz


def centroid(pts):
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n,
            sum(p[2] for p in pts) / n)


class Part:
    """One convex lump.  Faces are wound outwards at construction time, so
    one dot product per face then says what you cannot possibly see."""

    __slots__ = ("verts", "faces", "normals", "centers", "edges", "decor",
                 "two_sided", "center", "sealed")

    def __init__(self, verts, faces, decor=(), two_sided=False):
        self.sealed = frozenset()
        self.verts = [(float(v[0]), float(v[1]), float(v[2])) for v in verts]
        self.decor = [tuple(d) for d in decor]
        self.two_sided = two_sided
        used = sorted({i for f in faces for i in f})
        self.center = centroid([self.verts[i] for i in used])

        self.faces, self.normals, self.centers = [], [], []
        for face in faces:
            pts = [self.verts[i] for i in face]
            n = newell(pts)
            length = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
            n = (n[0] / length, n[1] / length, n[2] / length)
            c = centroid(pts)
            if not two_sided:
                ox, oy, oz = (c[0] - self.center[0], c[1] - self.center[1],
                              c[2] - self.center[2])
                if n[0] * ox + n[1] * oy + n[2] * oz < 0.0:   # wound inwards
                    face = tuple(reversed(tuple(face)))
                    n = (-n[0], -n[1], -n[2])
            self.faces.append(tuple(face))
            self.normals.append(n)
            self.centers.append(c)

        # Every edge remembers the faces it borders, so it can be stroked
        # once if either of them faces the camera -- and skipped if neither.
        shared = {}
        for fi, face in enumerate(self.faces):
            for k in range(len(face)):
                a, b = face[k], face[(k + 1) % len(face)]
                shared.setdefault((a, b) if a < b else (b, a), []).append(fi)
        self.edges = [(a, b, tuple(fs)) for (a, b), fs in shared.items()]

    def contains(self, p, eps=1e-6):
        """Is this point inside (or resting on) the lump?  Convex only."""
        for (nx, ny, nz), c in zip(self.normals, self.centers):
            if nx * (p[0] - c[0]) + ny * (p[1] - c[1]) + nz * (p[2] - c[2]) > eps:
                return False
        return True

    def seal_against(self, others):
        """Find the faces buried in a neighbouring lump.  Backface culling
        only reasons about one convex lump at a time, so where two of them
        meet -- a turret on a deck, a cap on a roof -- the lower one's top is
        turned away and fills nothing, leaving the upper one's underside
        showing through.  A face with every corner inside its neighbour is
        never visible from anywhere, so it is simply never drawn."""
        buried = [i for i, face in enumerate(self.faces)
                  if any(all(o.contains(self.verts[k]) for k in face)
                         for o in others)]
        self.sealed = frozenset(buried)

    def scaled(self, s):
        clone = Part.__new__(Part)
        clone.verts = [(v[0] * s, v[1] * s, v[2] * s) for v in self.verts]
        clone.centers = [(c[0] * s, c[1] * s, c[2] * s) for c in self.centers]
        clone.center = tuple(c * s for c in self.center)
        clone.faces, clone.normals = self.faces, self.normals
        clone.edges, clone.decor = self.edges, self.decor
        clone.two_sided, clone.sealed = self.two_sided, self.sealed
        return clone


def split_plane(a, b):
    """A face plane of `a` with all of `b` on the far side, or None."""
    for (nx, ny, nz), c in zip(a.normals, a.centers):
        d = nx * c[0] + ny * c[1] + nz * c[2]
        if all(nx * v[0] + ny * v[1] + nz * v[2] >= d - 1e-6 for v in b.verts):
            return (nx, ny, nz), d
    return None


class Shape:
    """A drawable object: convex lumps, plus wires for the bits too thin to
    be worth making solid -- aerials, rings, guy lines."""

    __slots__ = ("parts", "wires", "radius", "splits")

    def __init__(self, parts=(), wires=(), scale=1.0):
        self.parts = [p.scaled(scale) for p in parts]   # always a fresh copy
        for part in self.parts:
            others = [q for q in self.parts if q is not part and not q.two_sided]
            if others:
                part.seal_against(others)

        # For every pair of lumps, keep a face plane that separates them.
        # Sorting lumps by centre distance is nearly right, but a long lump
        # beside a tall one lands in the wrong order at a grazing angle --
        # which side of the touching plane the eye is on never lies, and it
        # is one dot product per pair at draw time.  Convention: the second
        # lump of the pair lives on the plane's positive side.
        self.splits = []
        for i, a in enumerate(self.parts):
            for j in range(i + 1, len(self.parts)):
                plane = split_plane(a, self.parts[j])
                if plane is None:
                    plane = split_plane(self.parts[j], a)
                    if plane is not None:
                        (nx, ny, nz), d = plane
                        plane = (-nx, -ny, -nz), -d
                if plane is not None:
                    self.splits.append((i, j) + plane)

        self.wires = [tuple(tuple(c * scale for c in p) for p in seg)
                      for seg in wires]
        pts = [v for p in self.parts for v in p.verts]
        pts += [p for seg in self.wires for p in seg]
        self.radius = max((math.dist(p, (0.0, 0.0, 0.0)) for p in pts), default=1.0)


def prismoid(bottom, top):
    """A floor, a lid, and a wall per side: box, wedge, drum or nose cone."""
    n = len(bottom)
    verts = list(bottom) + list(top)
    faces = [tuple(range(n)), tuple(range(n, 2 * n))]
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, n + j, n + i))
    return Part(verts, faces)


def box_part(x0, x1, y0, y1, z0, z1):
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    z0, z1 = min(z0, z1), max(z0, z1)
    return prismoid([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
                    [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)])


def block_part(hx, hz, height, storeys=(0.34, 0.67)):
    """A box with storey lines ruled across its walls.  Each line is pinned
    to the wall it belongs to, so the ones round the back stay round the back."""
    ground = [(-hx, 0.0, -hz), (hx, 0.0, -hz), (hx, 0.0, hz), (-hx, 0.0, hz)]
    verts = ground + [(x, height, z) for x, _, z in ground]
    faces = [(0, 1, 2, 3), (4, 5, 6, 7),
             (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    decor = []
    for fi in (2, 3, 4, 5):
        a, b = faces[fi][0], faces[fi][1]
        for frac in storeys:
            y = height * frac
            i = len(verts)
            verts.append((ground[a][0], y, ground[a][2]))
            verts.append((ground[b][0], y, ground[b][2]))
            decor.append((fi, i, i + 1))
    return Part(verts, faces, decor)


def pyramid_part(hx, hz, height):
    base = [(-hx, 0.0, -hz), (hx, 0.0, -hz), (hx, 0.0, hz), (-hx, 0.0, hz)]
    return Part(base + [(0.0, height, 0.0)],
                [(0, 1, 2, 3), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)])


def octa_part(r):
    verts = [(r, 0, 0), (-r, 0, 0), (0, r, 0), (0, -r, 0), (0, 0, r), (0, 0, -r)]
    faces = [(0, 2, 4), (2, 1, 4), (1, 3, 4), (3, 0, 4),
             (2, 0, 5), (1, 2, 5), (3, 1, 5), (0, 3, 5)]
    return Part(verts, faces)


def flat_part(pts):
    """A single sheet with no inside -- drawn from either side."""
    return Part(pts, [tuple(range(len(pts)))], two_sided=True)


# ----------------------------------------------------------------- models --
# Model space is +x right, +y up, +z forward.  Lumps are built to touch, not
# to overlap: two solids that interpenetrate cannot be ordered back to front.


def player_shape(scale=1.0):
    """The screensaver's tank, given faces to hide behind."""
    w, floor, deck = 0.62, 0.10, 0.52
    parts = [
        prismoid([(-w, floor, -1.10), (w, floor, -1.10),
                  (w, floor, 1.10), (-w, floor, 1.10)],
                 [(-w, deck, -0.95), (w, deck, -0.95),
                  (w, deck, 0.55), (-w, deck, 0.55)]),      # hull + glacis
        box_part(-0.34, 0.34, deck, 0.80, -0.45, 0.35),     # turret
        box_part(-0.07, 0.07, 0.61, 0.75, 0.35, 1.55),      # gun barrel
    ]
    for side in (-1, 1):
        parts.append(box_part(side * w, side * (w + 0.22), 0.0, 0.30, -1.15, 1.15))
    return Shape(parts, scale=scale)


def hunter_shape(scale=1.0):
    """Lean, nosed forward, all lance -- the tank that comes for you."""
    w = 0.58
    base = [(-w, 0.12, -1.00), (w, 0.12, -1.00), (w, 0.12, 0.50),
            (0.0, 0.12, 1.30), (-w, 0.12, 0.50)]
    # The deck is the footprint scaled down, so every wall is a flat
    # trapezoid: a twisted quad culls wrongly and tears at the silhouette.
    deck = [(x * 0.72, 0.60, z * 0.72) for x, _, z in base]
    parts = [
        prismoid(base, deck),
        box_part(-0.07, 0.07, 0.48, 0.62, 1.15, 1.95),      # lance
    ]
    for side in (-1, 1):
        parts.append(box_part(side * w, side * (w + 0.18), 0.0, 0.26, -1.05, 1.05))
    wires = [((0.0, 0.60, -0.72), (0.0, 1.45, -0.95))]      # aerial
    return Shape(parts, wires, scale)


def sentry_shape(scale=1.0):
    """Slow, hexagonal, and far too well armed."""
    lo = ring(0.95, 0.42, 6)
    hi = ring(0.58, 1.15, 6)
    parts = [prismoid(lo, hi),
             box_part(-0.09, 0.09, 0.72, 0.90, 0.60, 2.05)]
    wires = [((0.0, 1.15, 0.0), (0.0, 1.75, 0.0))]
    wires += polyline(ring(0.22, 1.75, 4), close=True)
    wires += [((p[0], 0.42, p[2]), (p[0] * 0.92, 0.0, p[2] * 0.92)) for p in lo]
    return Shape(parts, wires, scale)


def flag_shape(scale=1.0):
    """A pole, two pennants and a plinth: the thing you are here for."""
    parts = [
        box_part(-0.06, 0.06, 0.0, 3.30, -0.06, 0.06),
        prismoid(ring(0.75, 0.0, 6), ring(0.62, 0.20, 6)),
        flat_part([(0.07, 3.28, 0.0), (1.55, 2.85, 0.0), (0.07, 2.38, 0.0)]),
        flat_part([(0.0, 3.28, 0.07), (0.0, 2.85, 1.55), (0.0, 2.38, 0.07)]),
    ]
    return Shape(parts, polyline(ring(0.30, 0.95, 6), close=True), scale)


def ammo_shape(scale=1.0):
    return Shape([octa_part(1.0)], scale=scale)


def shield_shape(scale=1.0):
    return Shape([box_part(-0.62, 0.62, -0.62, 0.62, -0.62, 0.62)], scale=scale)


PLAYER_SHAPE = player_shape(1.95)
TITLE_SHAPE = player_shape(3.6)
HUNTER_SHAPE = hunter_shape(1.95)
SENTRY_SHAPE = sentry_shape(2.10)
FLAG_SHAPE = flag_shape(1.30)
AMMO_SHAPE = ammo_shape(1.45)
SHIELD_SHAPE = shield_shape(1.45)


# ------------------------------------------------------------------- view --

class View:
    """A wireframe camera: world segments in, glowing lines on the screen."""

    def __init__(self, surface):
        self.dim = 1.0
        self.set_surface(surface)
        self.set_camera(0.0, EYE, 0.0, 0.0)

    def set_surface(self, surface):
        self.surface = surface
        self.w, self.h = surface.get_size()
        self.cx, self.cy = self.w * 0.5, self.h * 0.5
        # FOV across the narrow axis: a window tiled tall and thin then widens
        # the view instead of squeezing it down to a letterbox slit.
        self.f = (min(self.w, self.h) * 0.5) / math.tan(FOV * 0.5)

    def set_camera(self, x, y, z, yaw, pitch=0.0):
        self.ex, self.ey, self.ez = x, y, z
        self.cyaw, self.syaw = math.cos(yaw), math.sin(yaw)
        self.cpit, self.spit = math.cos(pitch), math.sin(pitch)

    # -- transforms -------------------------------------------------------
    def to_cam(self, p):
        rx = p[0] - self.ex
        ry = p[1] - self.ey
        rz = p[2] - self.ez
        xc = rx * self.cyaw - rz * self.syaw
        zc = rx * self.syaw + rz * self.cyaw
        return (xc, ry * self.cpit - zc * self.spit, zc * self.cpit + ry * self.spit)

    def visible(self, x, y, z, radius):
        """Cheap bounding-sphere cull against the frustum."""
        cx, _, cz = self.to_cam((x, y, z))
        if cz < -radius or cz - radius > FAR:
            return False
        if cz > radius:
            r = radius * self.f / cz
            px = self.cx + cx / cz * self.f
            if px + r < 0.0 or px - r > self.w:
                return False
        return True

    def project(self, p):
        """One world point onto the screen: (x, y, depth), or None."""
        x, y, z = self.to_cam(p)
        if z < NEAR or z > FAR:
            return None
        return (self.cx + x / z * self.f, self.cy - y / z * self.f, z)

    # -- drawing ----------------------------------------------------------
    def segments(self, segs, color, width=1, glow=False):
        to_cam = self.to_cam
        self.cam_segments([(to_cam(a), to_cam(b)) for a, b in segs],
                          color, width, glow)

    def chunk(self, center, radius, segs, color, width=1, glow=False):
        if self.visible(center[0], center[1], center[2], radius):
            self.segments(segs, color, width, glow)

    def shape(self, shp, x, z, yaw, color, y=0.0, width=1, glow=False):
        """Draw a solid: fill the faces that look at us, stroke the edges
        bordering one, and let both hide whatever stands behind them."""
        radius = shp.radius
        if not self.visible(x, y + radius * 0.4, z, radius + 1.0):
            return
        c, s = math.cos(yaw), math.sin(yaw)
        ex, ey, ez = self.ex, self.ey, self.ez
        to_cam = self.to_cam

        parts = shp.parts
        if len(parts) > 1:                  # lumps still need ordering
            # The eye in model space, then farthest lump first -- but let
            # the split planes overrule the distances: the lump whose side
            # of the plane the eye is on must be painted after the other.
            mex = (ex - x) * c - (ez - z) * s
            mey = ey - y
            mez = (ex - x) * s + (ez - z) * c
            order = sorted(range(len(parts)), key=lambda i: -(
                (mex - parts[i].center[0]) ** 2
                + (mey - parts[i].center[1]) ** 2
                + (mez - parts[i].center[2]) ** 2))
            if shp.splits:
                before = {i: set() for i in order}   # lumps owed a head start
                for i, j, (nx, ny, nz), d in shp.splits:
                    if nx * mex + ny * mey + nz * mez > d:
                        before[j].add(i)
                    else:
                        before[i].add(j)
                done, laid = set(), []
                while len(laid) < len(parts):
                    pick = next((k for k in order
                                 if k not in done and before[k] <= done), None)
                    if pick is None:                 # tangled: trust distance
                        laid += [k for k in order if k not in done]
                        break
                    done.add(pick)
                    laid.append(pick)
                order = laid
            parts = [parts[i] for i in order]

        if shp.wires:
            # Wires first: a mast or an aerial is attached to the solid, so
            # the solid must be free to paint over the half behind it.
            self.cam_segments(
                [(to_cam((x + a[0] * c + a[2] * s, y + a[1],
                          z - a[0] * s + a[2] * c)),
                  to_cam((x + b[0] * c + b[2] * s, y + b[1],
                          z - b[0] * s + b[2] * c)))
                 for a, b in shp.wires], color, width, glow)

        for part in parts:
            verts = [to_cam((x + v[0] * c + v[2] * s, y + v[1],
                             z - v[0] * s + v[2] * c)) for v in part.verts]
            if part.two_sided:
                seen = [True] * len(part.faces)
            else:
                seen = []
                for (nx, ny, nz), fc in zip(part.normals, part.centers):
                    fx = x + fc[0] * c + fc[2] * s
                    fz = z - fc[0] * s + fc[2] * c
                    seen.append((nx * c + nz * s) * (ex - fx)
                                + ny * (ey - y - fc[1])
                                + (nz * c - nx * s) * (ez - fz) > 0.0)
            for i in part.sealed:
                seen[i] = False

            surf = self.surface
            for i, face in enumerate(part.faces):
                if seen[i]:
                    poly = self.project_face([verts[k] for k in face])
                    if poly is not None:
                        pygame.draw.polygon(surf, COL_BG, poly)

            segs = [(verts[a], verts[b]) for a, b, fs in part.edges
                    if seen[fs[0]] or (len(fs) > 1 and seen[fs[1]])]
            segs += [(verts[a], verts[b]) for fi, a, b in part.decor if seen[fi]]
            self.cam_segments(segs, color, width, glow)


    def project_face(self, pts):
        """Near-clip a polygon and project it; None if nothing is left."""
        out = []
        n = len(pts)
        for i in range(n):
            a = pts[i]
            b = pts[(i + 1) % n]
            az, bz = a[2], b[2]
            ina = az >= NEAR
            if ina:
                out.append(a)
            if ina != (bz >= NEAR):
                t = (NEAR - az) / (bz - az)
                out.append((a[0] + (b[0] - a[0]) * t,
                            a[1] + (b[1] - a[1]) * t, NEAR))
        if len(out) < 3:
            return None
        f, ox, oy, w, h = self.f, self.cx, self.cy, self.w, self.h
        scr = [(ox + q[0] / q[2] * f, oy - q[1] / q[2] * f) for q in out]
        xs = [q[0] for q in scr]
        ys = [q[1] for q in scr]
        if max(xs) < 0.0 or min(xs) > w or max(ys) < 0.0 or min(ys) > h:
            return None
        if (min(xs) < -FILL_EDGE or max(xs) > w + FILL_EDGE
                or min(ys) < -FILL_EDGE or max(ys) > h + FILL_EDGE):
            scr = clip_poly(scr, w, h)
            if len(scr) < 3:
                return None
        return scr

    def cam_segments(self, segs, color, width=1, glow=False):
        """Clip to the near plane, project, and stroke."""
        surf, draw = self.surface, pygame.draw.line
        f, ox, oy, w, h = self.f, self.cx, self.cy, self.w, self.h
        r0, g0, b0 = color
        dim = self.dim
        for a, b in segs:
            az, bz = a[2], b[2]
            if az < NEAR:
                if bz < NEAR:
                    continue
                t = (NEAR - az) / (bz - az)
                a = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, NEAR)
                az = NEAR
            elif bz < NEAR:
                t = (NEAR - bz) / (az - bz)
                b = (b[0] + (a[0] - b[0]) * t, b[1] + (a[1] - b[1]) * t, NEAR)
                bz = NEAR
            depth = (az + bz) * 0.5
            if depth > FAR:
                continue
            line = clip_rect(ox + a[0] / az * f, oy - a[1] / az * f,
                             ox + b[0] / bz * f, oy - b[1] / bz * f, w, h)
            if line is None:
                continue
            k = (1.0 - depth / FAR) ** 0.8 * dim
            if k <= 0.05:
                continue
            col = (int(r0 * k), int(g0 * k), int(b0 * k))
            x0, y0, x1, y1 = line
            if glow and depth < 80.0:
                draw(surf, (col[0] >> 2, col[1] >> 2, col[2] >> 2),
                     (x0, y0), (x1, y1), width + 3)
            draw(surf, col, (x0, y0), (x1, y1),
                 width if depth < 70.0 else 1)


# ------------------------------------------------------------- the arena --

class Building:
    """A block, a tower or a pyramid.  Built once around its own footprint
    and drawn where it stands -- solid, so it hides what it stands in front of."""

    def __init__(self, x, z, hx, hz, height, kind):
        self.x, self.z, self.hx, self.hz = x, z, hx, hz
        self.kind = kind
        if kind == "pyramid":
            self.shape = Shape([pyramid_part(hx, hz, height)])
            self.color = COL_TOWER
            self.h = height
        elif kind == "tower":
            cap = hx * 1.5
            self.shape = Shape(
                [block_part(hx, hz, height, (0.5,)),
                 box_part(-hx * .5, hx * .5, height, height + cap,
                          -hz * .5, hz * .5)],
                [((0.0, height + cap, 0.0), (0.0, height + cap * 1.9, 0.0))])
            self.color = COL_BUILD
            self.h = height + cap * 1.9
        else:
            self.shape = Shape([block_part(hx, hz, height)])
            self.color = COL_BUILD
            self.h = height
        self.center = (x, self.h * 0.5, z)
        self.radius = math.hypot(hx, hz) + self.h * 0.5

    def blocks(self, x, z, pad=0.0):
        return abs(x - self.x) <= self.hx + pad and abs(z - self.z) <= self.hz + pad

    def draw(self, view):
        view.shape(self.shape, self.x, self.z, 0.0, self.color)


def wall_chunks():
    """The perimeter fence, cut into cullable posts-and-rails runs."""
    chunks = []
    step, top = 11.5, 9.0
    posts = int(ARENA * 2 / step)
    for axis in (0, 1):
        for side in (-1, 1):
            for start in range(0, posts, 4):
                segs = []
                a = -ARENA + start * step
                b = min(ARENA, a + 4 * step)
                u = a
                while u < b - 1e-6:
                    nxt = min(b, u + step)
                    for p, q in ((u, nxt),):
                        if axis == 0:
                            segs += [((p, 0.0, side * ARENA), (p, top, side * ARENA))]
                            for y in (0.0, top * 0.5, top):
                                segs += [((p, y, side * ARENA), (q, y, side * ARENA))]
                        else:
                            segs += [((side * ARENA, 0.0, p), (side * ARENA, top, p))]
                            for y in (0.0, top * 0.5, top):
                                segs += [((side * ARENA, y, p), (side * ARENA, y, q))]
                    u = nxt
                mid = (a + b) * 0.5
                center = (mid, top * 0.5, side * ARENA) if axis == 0 else \
                         (side * ARENA, top * 0.5, mid)
                chunks.append((center, (b - a) * 0.5 + top, segs))
    return chunks


WALLS = wall_chunks()


def floor_grid(px, pz):
    """A patch of floor grid around the player, cell by cell so it can fade."""
    segs = []
    gx = math.floor(px / GRID)
    gz = math.floor(pz / GRID)
    lim = ARENA
    for i in range(-GRID_R, GRID_R + 1):
        x = (gx + i) * GRID
        if abs(x) > lim:
            continue
        for j in range(-GRID_R, GRID_R + 1):
            z0 = (gz + j) * GRID
            z1 = z0 + GRID
            if z1 < -lim or z0 > lim:
                continue
            z0, z1 = max(-lim, z0), min(lim, z1)
            segs.append(((x, 0.0, z0), (x, 0.0, z1)))
    for j in range(-GRID_R, GRID_R + 1):
        z = (gz + j) * GRID
        if abs(z) > lim:
            continue
        for i in range(-GRID_R, GRID_R + 1):
            x0 = (gx + i) * GRID
            x1 = x0 + GRID
            if x1 < -lim or x0 > lim:
                continue
            x0, x1 = max(-lim, x0), min(lim, x1)
            segs.append(((x0, 0.0, z), (x1, 0.0, z)))
    return segs


def push_out(x, z, r, buildings):
    """Slide a circle out of any box it has driven into, and off the walls."""
    hit = False
    for b in buildings:
        dx, dz = x - b.x, z - b.z
        ox = b.hx + r - abs(dx)
        oz = b.hz + r - abs(dz)
        if ox > 0.0 and oz > 0.0:
            hit = True
            if ox < oz:
                x = b.x + math.copysign(b.hx + r, dx if dx else 1.0)
            else:
                z = b.z + math.copysign(b.hz + r, dz if dz else 1.0)
    lim = ARENA - r
    if abs(x) > lim or abs(z) > lim:
        hit = True
        x, z = clamp(x, -lim, lim), clamp(z, -lim, lim)
    return x, z, hit


def line_blocked(x0, z0, x1, z1, buildings, step=3.5):
    """Walk a ray in short hops looking for something solid."""
    d = math.hypot(x1 - x0, z1 - z0)
    n = int(d / step)
    if n <= 0:
        return False
    dx, dz = (x1 - x0) / n, (z1 - z0) / n
    for i in range(1, n):
        px, pz = x0 + dx * i, z0 + dz * i
        for b in buildings:
            if b.blocks(px, pz, 0.4):
                return True
    return False


# ---------------------------------------------------------------- pieces --

class Shell:
    __slots__ = ("x", "y", "z", "dx", "dz", "speed", "life", "friendly",
                 "damage", "color", "owner")

    def __init__(self, x, y, z, dx, dz, friendly, damage, color,
                 speed=SHOT_SPEED, owner=-1):
        self.x, self.y, self.z = x, y, z
        self.dx, self.dz = dx, dz
        self.speed = speed
        self.life = 2.6
        self.friendly = friendly
        self.damage = damage
        self.color = color
        self.owner = owner

    def step(self, dt):
        self.x += self.dx * self.speed * dt
        self.z += self.dz * self.speed * dt
        self.life -= dt
        return self.life > 0.0 and abs(self.x) < ARENA and abs(self.z) < ARENA

    def draw(self, view):
        tail = 2.6
        view.segments([((self.x, self.y, self.z),
                        (self.x - self.dx * tail, self.y, self.z - self.dz * tail))],
                      self.color, width=2, glow=True)


class Burst:
    """An explosion: shards thrown out of a point and left to fall."""

    def __init__(self, x, y, z, size=1.0, color=COL_SHOT, count=16, life=0.85):
        self.x, self.y, self.z = x, y, z
        self.color, self.life, self.t = color, life, 0.0
        self.parts = []
        for _ in range(count):
            th = random.uniform(0.0, TAU)
            ph = random.uniform(-0.35, 1.15)
            self.parts.append(((math.cos(ph) * math.sin(th), math.sin(ph),
                                math.cos(ph) * math.cos(th)),
                               random.uniform(7.0, 21.0) * size))

    def step(self, dt):
        self.t += dt
        return self.t < self.life

    def draw(self, view):
        u = self.t / self.life
        drop = 11.0 * self.t * self.t
        segs = []
        for (dx, dy, dz), sp in self.parts:
            r0 = sp * self.t
            r1 = r0 + sp * 0.14 * (1.0 - u)
            segs.append(((self.x + dx * r0, self.y + dy * r0 - drop, self.z + dz * r0),
                         (self.x + dx * r1, self.y + dy * r1 - drop, self.z + dz * r1)))
        k = 1.0 - u
        col = (int(self.color[0] * k), int(self.color[1] * k), int(self.color[2] * k))
        view.segments(segs, col, width=2, glow=True)


class Prize:
    """A flag or a pod: something that hovers, spins and wants collecting."""

    def __init__(self, x, z, kind):
        self.x, self.z, self.kind = x, z, kind
        self.spin = random.uniform(0.0, TAU)
        self.t = random.uniform(0.0, TAU)
        if kind == "flag":
            self.shape, self.color = FLAG_SHAPE, COL_FLAG
            self.rate, self.lift = 1.3, 0.0
        elif kind == "ammo":
            self.shape, self.color = AMMO_SHAPE, COL_AMMO
            self.rate, self.lift = 2.4, 2.0
        else:
            self.shape, self.color = SHIELD_SHAPE, COL_SHIELD
            self.rate, self.lift = 1.8, 2.0
        self.radius = self.shape.radius

    def step(self, dt):
        self.spin = (self.spin + self.rate * dt) % TAU
        self.t += dt

    def draw(self, view):
        bob = self.lift + (0.35 * math.sin(self.t * 2.2) if self.lift else 0.0)
        view.shape(self.shape, self.x, self.z, self.spin, self.color,
                   y=bob, width=2, glow=True)


class Tank:
    def __init__(self, x, z, yaw, shape, color, radius=2.7):
        self.x, self.z, self.yaw = x, z, yaw
        self.shape, self.color, self.radius = shape, color, radius
        self.speed = 0.0
        self.hp = 100.0
        self.flash = 0.0
        self.cool = 0.0

    @property
    def forward(self):
        return math.sin(self.yaw), math.cos(self.yaw)

    def hurt(self, amount):
        self.hp -= amount
        self.flash = 0.14
        return self.hp <= 0.0

    def move(self, dt, buildings):
        dx, dz = self.forward
        self.x += dx * self.speed * dt
        self.z += dz * self.speed * dt
        self.x, self.z, hit = push_out(self.x, self.z, self.radius, buildings)
        if hit:
            self.speed *= 0.25
        return hit

    def draw(self, view, y=0.0):
        col = COL_WHITE if self.flash > 0.0 else self.color
        view.shape(self.shape, self.x, self.z, self.yaw, col, y=y,
                   width=2, glow=True)


HUNTER = dict(shape=HUNTER_SHAPE, color=COL_HUNTER, hp=58.0, top=21.0,
              turn=math.radians(100), reach=95.0, delay=1.45, damage=13.0,
              radius=2.6, shot=COL_ESHOT, speed=95.0, score=150, keep=26.0)
SENTRY = dict(shape=SENTRY_SHAPE, color=COL_SENTRY, hp=115.0, top=12.5,
              turn=math.radians(62), reach=125.0, delay=1.05, damage=20.0,
              radius=3.0, shot=COL_SENTRY, speed=80.0, score=275, keep=42.0)


class Enemy(Tank):
    """Turn towards the player, close to a comfortable range, shoot.
    Nudge around anything solid it walks into on the way."""

    def __init__(self, x, z, kind):
        spec = HUNTER if kind == "hunter" else SENTRY
        super().__init__(x, z, random.uniform(0, TAU), spec["shape"],
                         spec["color"], spec["radius"])
        self.kind = kind
        self.spec = spec
        self.hp = spec["hp"]
        self.cool = random.uniform(0.4, spec["delay"])
        self.dodge = random.choice((-1.0, 1.0))
        self.dodge_t = 0.0

    def think(self, dt, game, p):
        dx, dz = p.x - self.x, p.z - self.z
        dist = math.hypot(dx, dz) or 1e-6
        spec = self.spec
        want = math.atan2(dx, dz)

        # steer around whatever is directly ahead
        fx, fz = self.forward
        self.dodge_t -= dt
        probe = self.radius + 7.0
        if any(b.blocks(self.x + fx * probe, self.z + fz * probe, self.radius)
               for b in game.buildings):
            if self.dodge_t <= 0.0:
                self.dodge = random.choice((-1.0, 1.0))
                self.dodge_t = 1.2
            want += self.dodge * 1.1

        err = wrap(want - self.yaw)
        rate = spec["turn"] * dt
        self.yaw = wrap(self.yaw + clamp(err, -rate, rate))

        if dist > spec["reach"] * 0.8:
            throttle = 1.0
        elif dist > spec["keep"]:
            throttle = 0.62
        elif dist > spec["keep"] * 0.55:
            throttle = 0.0
        else:
            throttle = -0.55
        if abs(err) > 1.2:
            throttle = min(throttle, 0.35)
        target = spec["top"] * throttle
        self.speed += clamp(target - self.speed, -30.0 * dt, 22.0 * dt)
        self.move(dt, game.buildings)

        self.cool -= dt
        if (self.cool <= 0.0 and dist < spec["reach"] and abs(err) < 0.14
                and not line_blocked(self.x, self.z, p.x, p.z, game.buildings)):
            self.fire(game, p, dist)

    def fire(self, game, target, dist):
        spec = self.spec
        self.cool = spec["delay"] * random.uniform(0.85, 1.3)
        lead = dist / spec["speed"]
        tx, tz = target.forward
        aim = math.atan2(target.x + tx * target.speed * lead - self.x,
                         target.z + tz * target.speed * lead - self.z)
        dx, dz = math.sin(aim), math.cos(aim)
        game.shells.append(Shell(self.x + dx * 3.2, 1.4, self.z + dz * 3.2,
                                 dx, dz, False, spec["damage"], spec["shot"],
                                 spec["speed"]))
        game.play_at("efire", self.x, self.z)
        game.net_fx("efire", self.x, self.z)


class Player(Tank):
    def __init__(self):
        super().__init__(0.0, 0.0, 0.0, PLAYER_SHAPE, COL_HUD, 2.7)
        self.reset(0.0, 0.0, 0.0)
        self.shields = SHIELD_MAX
        self.ammo = AMMO_START
        self.lives = 3
        self.score = 0

    def reset(self, x, z, yaw):
        self.x, self.z, self.yaw = x, z, yaw
        self.speed = 0.0
        self.turbo = 1.0
        self.cool = 0.0
        self.flash = 0.0
        self.bob = 0.0
        self.turn_dir = 0
        self.turn_hold = 0.0
        self.kick = 0.0
        self.shake = 0.0

    def drive(self, dt, keys, buildings):
        fwd = keys[pygame.K_w] or keys[pygame.K_UP]
        back = keys[pygame.K_s] or keys[pygame.K_DOWN]
        left = keys[pygame.K_a] or keys[pygame.K_LEFT]
        right = keys[pygame.K_d] or keys[pygame.K_RIGHT]
        boost = (keys[pygame.K_LSHIFT] or keys[pygame.K_RSHIFT]) and self.turbo > 0.0

        top = P_TOP * (TURBO if boost and fwd else 1.0)
        if fwd:
            self.speed += P_ACCEL * dt
        elif back:
            self.speed -= P_BRAKE * dt
        else:
            self.speed -= math.copysign(min(abs(self.speed), 18.0 * dt), self.speed)
        self.speed = clamp(self.speed, -P_TOP * 0.45, top)

        if boost and fwd:
            self.turbo = max(0.0, self.turbo - dt * 0.32)
        else:
            self.turbo = min(1.0, self.turbo + dt * 0.14)

        # Steering winds up: tap for fine aim, hold to swing round.  The
        # ramp is squared, so it stays in the slow half for most of a second
        # and only then runs away with you.
        want = (1 if right else 0) - (1 if left else 0)
        if want != self.turn_dir:
            self.turn_dir, self.turn_hold = want, 0.0
        if want:
            self.turn_hold = min(P_TURN_RAMP, self.turn_hold + dt)
            wind = (self.turn_hold / P_TURN_RAMP) ** 2
            turn = P_TURN_TAP + (P_TURN_HELD - P_TURN_TAP) * wind
            if boost and fwd:
                turn *= 0.72
            self.yaw = wrap(self.yaw + want * turn * dt)

        bumped = self.move(dt, buildings)
        self.bob += abs(self.speed) * dt * 0.9
        self.kick *= math.exp(-9.0 * dt)
        self.shake *= math.exp(-5.0 * dt)
        return bumped


# ------------------------------------------------------------------ audio --

class Audio:
    """Every sound here is synthesised at start-up -- layered oscillators,
    filtered noise, honest envelopes -- and rendered straight to PCM.  No
    sample files and no numpy.  The mixer runs in stereo, so a shot tells you
    which side it came from, and there is a drone under it all that follows
    the engine."""

    RATE = 22050
    BANDS = 6

    def __init__(self, enabled=True):
        self.sounds = {}
        self.loops = []
        self.ok = False
        self.band = -1
        self.engine_on = False
        self.engine_channel = None
        if not enabled:
            return
        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(self.RATE, -16, 2, 512)
            self.rate, _, self.channels = pygame.mixer.get_init()
            pygame.mixer.set_num_channels(24)
            pygame.mixer.set_reserved(1)          # channel 0 is the engine's
            self.engine_channel = pygame.mixer.Channel(0)
            self._build()
        except (pygame.error, TypeError, IndexError):
            return
        self.ok = True

    # -- synthesis --------------------------------------------------------
    def _len(self, dur):
        return max(2, int(self.rate * dur))

    def _osc(self, dur, freq, shape="sine", decay=6.0, gain=1.0):
        """One oscillator under an exponential envelope, stepped rather than
        recomputed -- a multiply per sample instead of an exp()."""
        n = self._len(dur)
        env, fall = 1.0, math.exp(-decay / n)
        step = TAU / self.rate
        out, phase = [], 0.0
        for i in range(n):
            f = freq(i / n) if callable(freq) else freq
            phase += step * f
            if shape == "square":
                s = 1.0 if math.sin(phase) >= 0.0 else -1.0
            elif shape == "saw":
                s = (phase / math.pi) % 2.0 - 1.0
            elif shape == "tri":
                s = abs(((phase / math.pi) % 2.0) - 1.0) * 2.0 - 1.0
            else:
                s = math.sin(phase)
            out.append(s * env * gain)
            env *= fall
        return out

    def _noise(self, dur, decay=8.0, cutoff=0.3, gain=1.0):
        """White noise through a one-pole low pass: a low cutoff rumbles, a
        high one hisses, and that difference is most of an explosion."""
        n = self._len(dur)
        env, fall = 1.0, math.exp(-decay / n)
        out, y = [], 0.0
        for _ in range(n):
            y += cutoff * (random.uniform(-1.0, 1.0) - y)
            out.append(y * env * gain)
            env *= fall
        return out

    def _hush(self, dur):
        return [0.0] * self._len(dur)

    def _mix(self, *parts):
        out = [0.0] * max(len(p) for p in parts)
        for part in parts:
            for i, v in enumerate(part):
                out[i] += v
        return out

    def _seq(self, *parts):
        out = []
        for part in parts:
            out += part
        return out

    def _sound(self, samples, vol=0.5, fade=True):
        """Floats to a Sound.  A few milliseconds of taper at each end keep
        it from starting or stopping with a click -- except for loops, which
        are seamless by construction and must not be tapered at all."""
        n = len(samples)
        edge = max(1, int(self.rate * 0.003)) if fade else 0
        vals = []
        for i, s in enumerate(samples):
            g = vol
            if edge:
                g *= min(1.0, (i + 1) / edge, (n - i) / edge)
            v = int(clamp(s * g, -1.0, 1.0) * 32000)
            vals.append(v)
        if self.channels == 2:
            vals = [v for v in vals for _ in (0, 1)]
        return pygame.mixer.Sound(
            buffer=struct.pack("<%dh" % len(vals), *vals))

    # -- the sound set ----------------------------------------------------
    def _build(self):
        osc, noise, mix, seq = self._osc, self._noise, self._mix, self._seq

        # The player's gun: a click, a zap falling away, a thump underneath.
        self.sounds["fire"] = [self._sound(mix(
            osc(.20, lambda u: (840 - 580 * u) * b, "square", 12.0, .50),
            osc(.20, lambda u: (210 - 95 * u) * b, "sine", 9.0, .80),
            noise(.05, 45.0, .60, .45)), .32) for b in (1.0, 1.05)]

        # Theirs is duller and further down: you can tell it apart in a fight.
        self.sounds["efire"] = [self._sound(mix(
            osc(.24, lambda u: (430 - 250 * u) * b, "saw", 8.0, .42),
            osc(.24, lambda u: (150 - 68 * u) * b, "sine", 7.0, .60),
            noise(.06, 30.0, .35, .32)), .26) for b in (1.0, .94)]

        # Armour taking a hit: two detuned squares and a slap of noise.
        self.sounds["hit"] = [self._sound(mix(
            osc(.18, 780 * b, "square", 22.0, .32),
            osc(.18, 1170 * b, "square", 26.0, .20),
            osc(.18, lambda u: 300 - 120 * u, "tri", 14.0, .38),
            noise(.05, 50.0, .80, .50)), .30) for b in (1.0, 1.08)]

        # A shell into concrete: a ping that dives, and grit.
        self.sounds["ricochet"] = [self._sound(mix(
            osc(.20, lambda u: (1500 - 1150 * u) * b, "sine", 16.0, .32),
            noise(.13, 22.0, .50, .45)), .22) for b in (1.0, 1.12)]

        # A tank coming apart: crack, drop, rumble.
        self.sounds["boom"] = [self._sound(mix(
            osc(.65, lambda u: (165 - 118 * u) * b, "sine", 4.2, .85),
            noise(.65, 3.4, .10, .85),
            noise(.09, 26.0, .75, .55)), .48) for b in (1.0, .90)]

        # Your tank coming apart: the same, but it keeps going.
        self.sounds["doom"] = [self._sound(mix(
            osc(.95, lambda u: 130 - 95 * u, "sine", 3.0, .95),
            osc(.95, lambda u: 62 - 30 * u, "tri", 2.6, .50),
            noise(.95, 2.4, .07, .95),
            noise(.12, 20.0, .60, .65)), .55)]

        self.sounds["flag"] = [self._sound(seq(
            osc(.10, 784, "tri", 7.0, .80),
            osc(.10, 988, "tri", 7.0, .80),
            osc(.22, 1319, "tri", 5.0, .90)), .30)]

        self.sounds["pod"] = [self._sound(seq(
            osc(.09, 660, "tri", 8.0, .80),
            osc(.20, 990, "tri", 6.0, .90)), .28)]

        self.sounds["warn"] = [self._sound(seq(
            osc(.15, 320, "square", 5.0, .70),
            self._hush(.06),
            osc(.15, 320, "square", 5.0, .70)), .26)]

        self.sounds["clear"] = [self._sound(seq(
            osc(.12, 523, "tri", 6.0, .80),
            osc(.12, 659, "tri", 6.0, .80),
            osc(.12, 784, "tri", 6.0, .80),
            osc(.32, 1047, "tri", 3.5, .95)), .32)]

        self.loops = [self._engine_loop(44.0 + 12.0 * i) for i in range(self.BANDS)]

    def _engine_loop(self, f0, cycles=5, harmonics=6):
        """A drone built from exact harmonics of f0, exactly `cycles` long --
        so the end of the buffer runs into the start of it without a seam."""
        n = max(64, int(round(cycles * self.rate / f0)))
        f = cycles * self.rate / n                  # the frequency that fits
        shape = random.Random(int(f0))              # same texture every band
        phases = [shape.uniform(0.0, TAU) for _ in range(harmonics)]
        out = []
        for i in range(n):
            t = i / self.rate
            out.append(sum(math.sin(TAU * f * h * t + phases[h - 1]) / h ** 1.25
                           for h in range(1, harmonics + 1)) * 0.30)
        return self._sound(out, 1.0, fade=False)

    # -- playing ----------------------------------------------------------
    def play(self, name, pan=0.0, volume=1.0):
        """pan is -1 hard left to +1 hard right."""
        if not self.ok or volume <= 0.02:
            return
        bank = self.sounds.get(name)
        if not bank:
            return
        channel = random.choice(bank).play()
        if channel is None:
            return
        if self.channels < 2:                     # nowhere to pan it to
            channel.set_volume(volume)
            return
        pan = clamp(pan, -1.0, 1.0)
        channel.set_volume(volume * (1.0 - max(0.0, pan)),
                           volume * (1.0 + min(0.0, pan)))

    def engine(self, level):
        """Keep the drone matched to how hard the tank is working."""
        if not self.ok or not self.loops:
            return
        level = clamp(level, 0.0, 1.0)
        n = len(self.loops)
        band = min(n - 1, int(level * n))
        if not self.engine_on:
            self._band(band)
        elif band > self.band and level > (self.band + 1) / n + 0.03:
            self._band(band)                # hysteresis, or it flutters
        elif band < self.band and level < self.band / n - 0.03:
            self._band(band)
        self.engine_channel.set_volume(0.09 + 0.20 * level)

    def _band(self, band):
        self.band = band
        self.engine_channel.play(self.loops[band], loops=-1)
        self.engine_on = True

    def engine_stop(self):
        if self.ok and self.engine_on:
            self.engine_channel.stop()
            self.engine_on = False
            self.band = -1


# ---------------------------------------------------------------- network --
# One machine hosts and owns the world -- the enemies, the shells, the
# prizes, the clock.  Every player, host included, drives their own tank
# locally (so the controls never feel the wire) and reports where it is;
# the host aims the enemies at whoever is nearest and streams the world
# back.  Arenas are never sent at all: a level is a seed, and every
# machine grows the same buildings from it.


def lan_ip():
    """The address the LAN sees, found by aiming a datagram and reading
    the return address off the envelope.  Nothing is actually sent."""
    for probe in ("8.8.8.8", "10.255.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 53))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    try:                                # no route anywhere: ask the resolver
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


class Peer:
    """One connected socket: a reader thread feeding a queue, and a lock
    around sends so whole lines leave in one piece."""

    def __init__(self, sock):
        self.sock = sock
        self.pid = -1
        self.name = ""
        self.alive = True
        self.inbox = queue.Queue()
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        try:
            while True:
                data = self.sock.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        try:
                            self.inbox.put(json.loads(line))
                        except ValueError:
                            pass
        except OSError:
            pass
        self.alive = False
        self.inbox.put(None)                     # the hang-up marker

    def poll(self):
        """Everything that has arrived; ends with None if the line died."""
        out = []
        while True:
            try:
                out.append(self.inbox.get_nowait())
            except queue.Empty:
                return out

    def send(self, msg):
        if not self.alive:
            return
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        try:
            with self.lock:
                self.sock.sendall(data)
        except OSError:
            self.alive = False

    def close(self):
        self.alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class HostNet:
    """Listens for tanks, hands each one a player id, fans messages out."""

    def __init__(self, port=PORT):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", port))
        self.sock.listen(4)
        self.ip = lan_ip()
        self.peers = {}                          # pid -> Peer
        self.joins = queue.Queue()
        threading.Thread(target=self._accept, daemon=True).start()

    def free_pid(self):
        """The lowest seat not taken -- 0 is the host's -- so a leaver's
        color and spawn corner go back in the pool.  None if full up."""
        taken = set(self.peers)
        return next((p for p in range(1, MAX_PLAYERS) if p not in taken), None)

    def _accept(self):
        while True:
            try:
                sock, _ = self.sock.accept()
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.joins.put(Peer(sock))

    def admit(self):
        """Fresh sockets: connected, but not yet introduced."""
        out = []
        while True:
            try:
                out.append(self.joins.get_nowait())
            except queue.Empty:
                return out

    def broadcast(self, msg, skip=None):
        for pid, peer in self.peers.items():
            if pid != skip:
                peer.send(msg)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
        for peer in self.peers.values():
            peer.close()


def client_connect(addr, port=PORT, timeout=4.0):
    sock = socket.create_connection((addr, port), timeout=timeout)
    sock.settimeout(None)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return Peer(sock)


class RemotePlayer:
    """Someone else's tank: a pose that arrives over the wire, rolled
    forward between reports so it moves like a tank, not a strobe."""

    def __init__(self, pid, name):
        self.pid, self.name = pid, name
        self.color = PLAYER_COLS[pid % len(PLAYER_COLS)]
        self.x = self.z = self.yaw = self.speed = 0.0
        self.alive = True
        self.score = 0
        self.radius = 2.7
        self.stale = 0.0                         # since the last pose

    def pose(self, x, z, yaw, speed, alive, score):
        self.x, self.z, self.yaw, self.speed = x, z, yaw, speed
        self.alive, self.score = alive, score
        self.stale = 0.0

    @property
    def forward(self):
        return math.sin(self.yaw), math.cos(self.yaw)

    def step(self, dt):
        self.stale += dt
        if self.alive and self.stale < 0.5:      # dead reckoning
            self.x += math.sin(self.yaw) * self.speed * dt
            self.z += math.cos(self.yaw) * self.speed * dt

    def draw(self, view):
        if self.alive:
            view.shape(PLAYER_SHAPE, self.x, self.z, self.yaw, self.color,
                       width=2, glow=True)


# ------------------------------------------------------------------- game --

class Game:
    def __init__(self, screen, audio):
        self.screen = screen
        self.view = View(screen)
        self.audio = audio
        self.big = font(34)
        self.mid = font(20)
        self.small = font(14)
        self.tiny = font(11)
        self.tag = font(42)              # call signs over tanks
        self.tag_far = font(33)
        self.stars = []
        for _ in range(120):
            v = (random.uniform(-1, 1), random.uniform(.02, 1), random.uniform(-1, 1))
            n = math.dist(v, (0, 0, 0)) or 1.0
            self.stars.append(tuple(c / n * 900.0 for c in v))
        self.radar_range = 90.0
        self.chase = False
        self.best = 0
        self.player = Player()
        self.buildings, self.prizes, self.enemies = [], [], []
        self.shells, self.bursts = [], []
        self.state = "title"
        self.state_t = 0.0
        self.msg, self.msg_t = "", 0.0
        self.level = 1
        self.time_left = 0.0
        self.flags_taken = 0
        self.flags_needed = 0

        # -- LAN state.  role is None (solo), "host" or "client".
        self.role = None
        self.net = None                  # HostNet, or the host's Peer
        self.my_pid = 0
        self.my_name = (str(load_settings().get("name", ""))[:10].upper()
                        or default_name())
        self.name_text = ""
        self.remotes = {}                # pid -> RemotePlayer
        self.pending = []                # host: sockets awaiting their hello
        self.roster = []                 # lobby display: [pid, name] rows
        self.menu_sel = 0
        self.esc_prev = ("title", 0.0)   # where esc came from, to go back to
        self.esc_sel = 0
        self.quit = False                # the menu asks main() to stop
        self.join_text = ""
        self.join_err = ""
        self.snap_t = 0.0
        self.pose_t = 0.0
        self.level_seed = 0
        self.prize_seq = 0

    # -- building a level -------------------------------------------------
    # A level is grown from a seed, so in LAN play the seed is the level:
    # the host names a number and every machine builds the same arena.

    def free_spot(self, rng, clear=7.0, away_from_start=22.0):
        for _ in range(400):
            x = rng.uniform(-ARENA + 12, ARENA - 12)
            z = rng.uniform(-ARENA + 12, ARENA - 12)
            if math.hypot(x, z) < away_from_start:
                continue
            if any(b.blocks(x, z, clear) for b in self.buildings):
                continue
            return x, z
        return rng.uniform(-40, 40), rng.uniform(-40, 40)

    def spawn_pose(self):
        """Everyone gets their own patch of the cleared starting ground."""
        spots = ((0.0, 0.0), (13.0, 0.0), (-13.0, 0.0), (0.0, -13.0))
        x, z = spots[self.my_pid % len(spots)]
        return x, z, 0.0

    def build_level(self, seed=None):
        if seed is None:
            seed = random.randrange(1 << 30)
        self.level_seed = seed
        rng = random.Random(seed)
        n = self.level
        self.buildings = []
        want = min(34, 16 + n * 2)
        tries = 0
        while len(self.buildings) < want and tries < 900:
            tries += 1
            x = rng.uniform(-ARENA + 14, ARENA - 14)
            z = rng.uniform(-ARENA + 14, ARENA - 14)
            if math.hypot(x, z) < 26.0:                 # keep the spawn clear
                continue
            hx = rng.uniform(3.0, 9.0)
            hz = rng.uniform(3.0, 9.0)
            kind = rng.choices(("block", "tower", "pyramid"),
                               (0.6, 0.25, 0.15))[0]
            height = rng.uniform(7.0, 24.0) if kind != "pyramid" else \
                rng.uniform(6.0, 14.0)
            if any(abs(x - b.x) < hx + b.hx + 9.0 and abs(z - b.z) < hz + b.hz + 9.0
                   for b in self.buildings):
                continue
            self.buildings.append(Building(x, z, hx, hz, height, kind))

        self.flags_needed = min(9, 3 + n)
        self.flags_taken = 0
        self.prizes = [Prize(*self.free_spot(rng), "flag")
                       for _ in range(self.flags_needed)]
        for _ in range(2 + n // 3):
            self.prizes.append(Prize(*self.free_spot(rng), "ammo"))
        for _ in range(1 + n // 4):
            self.prizes.append(Prize(*self.free_spot(rng), "shield"))
        for i, prize in enumerate(self.prizes):
            prize.pid = i                    # so the wire can name them
        self.prize_seq = len(self.prizes)

        self.enemies = []
        if self.role != "client":            # the host owns the enemies
            for i in range(min(9, 1 + n)):
                kind = "sentry" if (n >= 2 and i % 3 == 2) else "hunter"
                while True:
                    x, z = self.free_spot(rng, clear=6.0, away_from_start=60.0)
                    if math.hypot(x, z) > 55.0:
                        break
                self.enemies.append(Enemy(x, z, kind))

        self.shells, self.bursts = [], []
        self.time_left = 100.0 + 12.0 * min(n, 6)
        self.warned = False
        self.player.reset(*self.spawn_pose())

    def new_game(self, seed=None, level=1):
        self.player = Player()
        self.player.color = PLAYER_COLS[self.my_pid % len(PLAYER_COLS)]
        self.level = level
        self.build_level(seed)
        for r in self.remotes.values():
            r.pose(*((0.0,) * 4), True, 0)
        self.snap_t = self.pose_t = 0.0
        self.state, self.state_t = "play", 0.0
        self.say("LEVEL %d  --  COLLECT %d FLAGS"
                 % (level, self.flags_needed), 3.0)

    def play_at(self, name, x, z, gain=1.0, reach=145.0):
        """Put a sound where it happened: quieter with distance, and over on
        the side it came from."""
        p = self.player
        dx, dz = x - p.x, z - p.z
        dist = math.hypot(dx, dz)
        volume = gain * max(0.0, 1.0 - dist / reach) ** 1.6
        if volume <= 0.02:
            return
        right = 0.0 if dist < 0.5 else \
            (dx * math.cos(p.yaw) - dz * math.sin(p.yaw)) / dist
        self.audio.play(name, pan=right * 0.85, volume=volume)

    def say(self, text, secs=2.2):
        self.msg, self.msg_t = text, secs

    # -- input ------------------------------------------------------------
    def key(self, event):
        k = event.key
        if self.state == "join_ip":
            self.key_join(event)
            return
        if self.state == "settings":
            self.key_settings(event)
            return
        if self.state == "escmenu":
            if k in (pygame.K_UP, pygame.K_w, pygame.K_DOWN, pygame.K_s):
                self.esc_sel = 1 - self.esc_sel
            elif k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
                self.escmenu_act()
            return
        if k == pygame.K_TAB:
            self.radar_range = {90.0: 150.0, 150.0: 55.0}.get(self.radar_range, 90.0)
        elif k == pygame.K_p and self.role is None and self.state in ("play", "paused"):
            self.state = "paused" if self.state == "play" else "play"
        elif self.state == "title" and k in (pygame.K_UP, pygame.K_w):
            self.menu_sel = (self.menu_sel - 1) % 4
        elif self.state == "title" and k in (pygame.K_DOWN, pygame.K_s):
            self.menu_sel = (self.menu_sel + 1) % 4
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER, pygame.K_SPACE):
            if self.state == "title":
                if self.menu_sel == 0:
                    self.new_game()
                elif self.menu_sel == 1:
                    self.start_host()
                elif self.menu_sel == 2:
                    self.join_err = ""
                    self.state, self.state_t = "join_ip", 0.0
                else:
                    self.name_text = self.my_name
                    self.state, self.state_t = "settings", 0.0
            elif (self.state == "lobby" and self.role == "host"
                  and k != pygame.K_SPACE):
                self.net_start_level(1)
            elif self.state == "over":
                if self.role == "host":
                    self.net.broadcast({"t": "tolobby"})
                    self.to_lobby()
                elif self.role is None:
                    self.state, self.state_t = "title", 0.0
            elif self.state == "clear" and self.state_t > 0.8:
                if self.role == "host":
                    self.net_start_level(self.level + 1)
                elif self.role is None:
                    self.next_level()

    def key_join(self, event):
        """A line editor just big enough for an address."""
        k = event.key
        if k == pygame.K_ESCAPE:
            self.state, self.state_t = "title", 0.0
        elif k == pygame.K_BACKSPACE:
            self.join_text = self.join_text[:-1]
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.connect_join()
        else:
            ch = event.unicode
            if ch and (ch.isalnum() or ch in ".-:_") and len(self.join_text) < 40:
                self.join_text += ch

    def key_settings(self, event):
        k = event.key
        if k == pygame.K_ESCAPE:
            self.state, self.state_t = "title", 0.0
        elif k == pygame.K_BACKSPACE:
            self.name_text = self.name_text[:-1]
        elif k in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self.my_name = self.name_text.strip()[:10] or default_name()
            save_settings({"name": self.my_name})
            self.state, self.state_t = "title", 0.0
        else:
            ch = event.unicode
            if ch and (ch.isalnum() or ch in "-_ ") and len(self.name_text) < 10:
                self.name_text += ch.upper()

    def fire(self):
        p = self.player
        if p.cool > 0.0 or p.ammo <= 0:
            if p.ammo <= 0 and p.cool <= 0.0:
                p.cool = 0.4
                self.audio.play("warn")
                self.say("OUT OF AMMUNITION", 1.2)
            return
        p.cool = SHOT_DELAY
        p.ammo -= 1
        dx, dz = p.forward
        x, z = p.x + dx * 3.6, p.z + dz * 3.6
        self.shells.append(Shell(x, 1.45, z, dx, dz, True, SHOT_DAMAGE,
                                 COL_SHOT, owner=self.my_pid))
        if self.role == "client":       # ours is cosmetic; the host's counts
            self.net.send({"t": "fire", "x": round(x, 2), "z": round(z, 2),
                           "dx": round(dx, 4), "dz": round(dz, 4)})
        else:
            self.net_fx("fire", x, z)
        p.kick = 0.045
        self.audio.play("fire")

    # -- the frame --------------------------------------------------------
    def update(self, dt):
        self.state_t += dt
        self.msg_t = max(0.0, self.msg_t - dt)
        if self.net:
            self.net_pump(dt)
            if self.net is None:            # the pump may have hung up
                return
        if self.state != "play":
            self.audio.engine_stop()
        if self.state in ("title", "paused", "join_ip", "lobby"):
            return
        if self.state == "play":
            self.update_play(dt)
        elif self.state == "escmenu":
            if self.role is None:
                return                      # solo: the world waits for you
            if self.role == "host" and self.esc_prev[0] in ("play", "dead"):
                self.update_play(dt)        # the others are still fighting
            else:
                self.step_ambient(dt)
        elif self.state in ("dead", "clear", "over"):
            if self.role == "host" and self.state == "dead":
                self.update_play(dt)        # the world must not die with us
            else:
                self.step_ambient(dt)
            if self.state == "dead" and self.state_t > 2.6:
                self.respawn()

    def step_ambient(self, dt):
        """Let what is already in flight land, without simulating anyone."""
        for lst in (self.shells, self.bursts):
            lst[:] = [o for o in lst if o.step(dt)]
        for e in self.enemies:
            e.flash = max(0.0, e.flash - dt)
        for r in self.remotes.values():
            r.step(dt)

    def update_play(self, dt):
        p = self.player
        alive = self.state == "play"        # a dead host still runs the world
        if alive:
            keys = pygame.key.get_pressed()
            if keys[pygame.K_SPACE]:
                self.fire()
            p.cool = max(0.0, p.cool - dt)
            p.flash = max(0.0, p.flash - dt)
            was = abs(p.speed)
            if p.drive(dt, keys, self.buildings) and was > 16.0:
                self.damage_player(2.5, shake=0.35)      # you felt that

        for r in self.remotes.values():
            r.step(dt)

        if self.role == "client":
            # These tanks are the host's: roll them forward between
            # snapshots, and leave the thinking to the machine that owns them.
            for e in self.enemies:
                e.flash = max(0.0, e.flash - dt)
                e.x += math.sin(e.yaw) * e.speed * dt
                e.z += math.cos(e.yaw) * e.speed * dt
        else:
            targets = self.tanks_alive()
            for e in self.enemies:
                e.flash = max(0.0, e.flash - dt)
                if targets:
                    near = min(targets, key=lambda t:
                               (t.x - e.x) ** 2 + (t.z - e.z) ** 2)
                    e.think(dt, self, near)

        if alive:
            for e in self.enemies:                       # shunted apart
                dx, dz = p.x - e.x, p.z - e.z
                d = math.hypot(dx, dz)
                if d < e.radius + p.radius:
                    push = (e.radius + p.radius - d) * 0.5 + 0.01
                    nx, nz = dx / (d or 1.0), dz / (d or 1.0)
                    if self.role != "client":
                        e.x -= nx * push
                        e.z -= nz * push
                    p.x += nx * push
                    p.z += nz * push
                    self.damage_player(24.0 * dt, shake=0.2)

        for prize in self.prizes:
            prize.step(dt)
        self.bursts[:] = [b for b in self.bursts if b.step(dt)]
        if self.role == "client":
            self.update_shells_client(dt)
        else:
            self.update_shells(dt)
            self.collect(dt)

        if alive:
            self.audio.engine(abs(p.speed) / P_TOP)

        self.time_left -= dt
        if self.time_left < 20.0 and not self.warned:
            self.warned = True
            self.audio.play("warn")
            self.say("TWENTY SECONDS", 2.0)
        if self.time_left <= 0.0:
            self.time_left = 0.0
            if self.role == "host":
                self.net.broadcast({"t": "over", "why": "OUT OF TIME"})
                self.best = max(self.best, p.score)
                self.state, self.state_t = "over", 0.0
                self.say("OUT OF TIME", 3.0)
            elif self.role is None:
                self.kill_player("OUT OF TIME")
            # a client only coasts here: the word comes from the host

    def tanks_alive(self):
        """Every tank the enemies might care about, host's own included."""
        out = [self.player] if self.state == "play" else []
        out += [r for r in self.remotes.values() if r.alive]
        return out

    def update_shells(self, dt):
        p = self.player
        live = []
        for s in self.shells:
            if not s.step(dt):
                self.bursts.append(Burst(s.x, s.y, s.z, 0.35, s.color, 7, 0.35))
                continue
            if any(b.blocks(s.x, s.z, 0.3) and s.y < b.h for b in self.buildings):
                self.bursts.append(Burst(s.x, s.y, s.z, 0.5, s.color, 9, 0.4))
                self.play_at("ricochet", s.x, s.z, 0.8)
                continue
            if s.friendly:
                for e in self.enemies:
                    if math.hypot(e.x - s.x, e.z - s.z) < e.radius + 0.8:
                        if e.hurt(s.damage):
                            self.kill_enemy(e, s.owner)
                        else:
                            self.play_at("hit", e.x, e.z)
                            self.bursts.append(Burst(s.x, 1.4, s.z, 0.4, s.color, 8, 0.35))
                        break
                else:
                    live.append(s)
                    continue
                continue
            if (self.state == "play"
                    and math.hypot(p.x - s.x, p.z - s.z) < p.radius + 0.8):
                self.bursts.append(Burst(s.x, 1.4, s.z, 0.5, COL_WARN, 10, 0.4))
                self.damage_player(s.damage, shake=0.9)
                continue
            if any(r.alive and math.hypot(r.x - s.x, r.z - s.z) < r.radius + 0.8
                   for r in self.remotes.values()):
                # the hit player settles their own damage; we stop the shell
                self.bursts.append(Burst(s.x, 1.4, s.z, 0.5, COL_WARN, 10, 0.4))
                continue
            live.append(s)
        self.shells = live

    def update_shells_client(self, dt):
        """Shells here are the host's word made visible: our own fly
        locally for feel, the rest arrive by snapshot.  Sparks and sounds
        are drawn where they seem to land; the damage that matters to us --
        an enemy shell into our own hull -- is judged here, because only
        this machine knows exactly where our tank is."""
        p = self.player
        live = []
        for s in self.shells:
            if not s.step(dt):
                self.bursts.append(Burst(s.x, s.y, s.z, 0.35, s.color, 7, 0.35))
                continue
            if any(b.blocks(s.x, s.z, 0.3) and s.y < b.h for b in self.buildings):
                self.bursts.append(Burst(s.x, s.y, s.z, 0.5, s.color, 9, 0.4))
                self.play_at("ricochet", s.x, s.z, 0.8)
                continue
            if s.friendly:
                if any(math.hypot(e.x - s.x, e.z - s.z) < e.radius + 0.8
                       for e in self.enemies):
                    self.play_at("hit", s.x, s.z)
                    self.bursts.append(Burst(s.x, 1.4, s.z, 0.4, s.color, 8, 0.35))
                    continue
            elif (self.state == "play"
                  and math.hypot(p.x - s.x, p.z - s.z) < p.radius + 0.8):
                self.bursts.append(Burst(s.x, 1.4, s.z, 0.5, COL_WARN, 10, 0.4))
                self.damage_player(s.damage, shake=0.9)
                continue
            live.append(s)
        self.shells = live

    def collect(self, dt):
        """Pickups, judged where the world lives.  The host reads every
        tank against every prize -- its own precisely, the others from
        their latest reports, which at LAN latencies is close enough to
        drive over a flag with."""
        p = self.player
        takers = [(self.my_pid, p)] if self.state == "play" else []
        takers += [(pid, r) for pid, r in self.remotes.items() if r.alive]
        keep = []
        for prize in self.prizes:
            by = next((pid for pid, t in takers
                       if math.hypot(prize.x - t.x, prize.z - t.z)
                       <= t.radius + 2.6), None)
            if by is None:
                keep.append(prize)
                continue
            if prize.kind == "flag":
                self.flags_taken += 1
            if self.role == "host":
                self.net.broadcast({"t": "prize", "id": prize.pid, "by": by,
                                    "ft": self.flags_taken})
            left = self.flags_needed - self.flags_taken
            if by == self.my_pid:
                if prize.kind == "flag":
                    p.score += 250
                    p.ammo += 5
                    self.audio.play("flag")
                    self.say("FLAG SECURED  --  %d TO GO" % left if left else
                             "ALL FLAGS SECURED", 1.6)
                elif prize.kind == "ammo":
                    p.ammo += 20
                    self.audio.play("pod")
                    self.say("AMMUNITION +20", 1.2)
                else:
                    p.shields = min(SHIELD_MAX, p.shields + 35.0)
                    self.audio.play("pod")
                    self.say("SHIELDS RESTORED", 1.2)
            elif prize.kind == "flag":
                self.audio.play("flag")
                self.say("%s TOOK A FLAG  --  %d TO GO" % (self.who(by), left)
                         if left else "ALL FLAGS SECURED", 1.6)
            self.bursts.append(Burst(prize.x, 1.6, prize.z, 0.35, prize.color, 10, 0.5))
        self.prizes = keep
        if (self.flags_taken >= self.flags_needed and self.role != "client"
                and self.state in ("play", "dead", "escmenu")):
            self.finish_level()

    def who(self, pid):
        if pid == self.my_pid:
            return self.my_name
        r = self.remotes.get(pid)
        return r.name if r else "A TANK"

    def damage_player(self, amount, shake=0.5):
        p = self.player
        if self.state != "play":
            return
        p.shields -= amount
        p.flash = 0.12
        p.shake = min(1.4, p.shake + shake)
        if amount > 3.0:
            self.audio.play("hit")
        if p.shields <= 0.0:
            p.shields = 0.0
            self.kill_player("TANK DESTROYED")

    def kill_enemy(self, enemy, owner=0):
        self.enemies.remove(enemy)
        if owner == self.my_pid:
            self.player.score += enemy.spec["score"]
        elif self.role == "host" and owner in self.net.peers:
            self.net.peers[owner].send({"t": "score", "v": enemy.spec["score"]})
        self.bursts.append(Burst(enemy.x, 1.5, enemy.z, 1.25, enemy.color, 22, 1.0))
        self.play_at("boom", enemy.x, enemy.z)
        self.net_fx("boom", enemy.x, enemy.z, enemy.color)
        if random.random() < 0.45:
            prize = Prize(enemy.x, enemy.z,
                          "ammo" if random.random() < 0.6 else "shield")
            prize.pid = self.prize_seq
            self.prize_seq += 1
            self.prizes.append(prize)
            if self.role == "host":
                self.net.broadcast({"t": "pspawn", "id": prize.pid,
                                    "k": prize.kind, "x": round(prize.x, 1),
                                    "z": round(prize.z, 1)})

    def kill_player(self, why):
        p = self.player
        self.bursts.append(Burst(p.x, 1.6, p.z, 1.6, COL_WARN, 28, 1.3))
        self.audio.play("doom")
        self.audio.engine_stop()
        if self.role is None:
            p.lives -= 1              # on the LAN a tank is only ever mislaid
        elif self.role == "host":
            self.net_fx("die", p.x, p.z)
        self.say(why, 2.4)
        self.state, self.state_t = "dead", 0.0

    def respawn(self):
        p = self.player
        if self.role is None and p.lives <= 0:
            self.best = max(self.best, p.score)
            self.state, self.state_t = "over", 0.0
            return
        p.reset(*self.spawn_pose())
        p.shields = SHIELD_MAX
        p.ammo = max(p.ammo, 12)
        if self.role is None:
            self.time_left = max(self.time_left, 35.0)
            self.shells = []
        for e in self.enemies:                       # give the player a moment
            if math.hypot(e.x - p.x, e.z - p.z) < 45.0 and self.role != "client":
                a = math.atan2(e.x, e.z)
                e.x, e.z = math.sin(a) * 70.0, math.cos(a) * 70.0
        self.state, self.state_t = "play", 0.0
        if self.role is None:
            self.say("%d TANK%s LEFT" % (p.lives, "" if p.lives == 1 else "S"), 2.0)
        else:
            self.say("BACK IN THE FIGHT", 2.0)

    def finish_level(self):
        self.bonus = int(self.time_left) * 10 + 500
        self.player.score += self.bonus
        if self.role == "host":
            self.net.broadcast({"t": "clear", "b": self.bonus})
        self.state, self.state_t = "clear", 0.0
        self.audio.play("clear")

    def next_level(self, seed=None, level=None):
        self.level = level if level is not None else self.level + 1
        self.build_level(seed)
        self.player.shields = min(SHIELD_MAX, self.player.shields + 30.0)
        self.state, self.state_t = "play", 0.0
        self.say("LEVEL %d  --  COLLECT %d FLAGS" % (self.level, self.flags_needed), 3.0)

    # -- LAN plumbing -----------------------------------------------------
    def start_host(self):
        try:
            self.net = HostNet()
        except OSError as err:
            self.say("CANNOT HOST -- %s"
                     % (err.strerror or str(err)).upper(), 3.0)
            return
        self.role, self.my_pid = "host", 0
        self.remotes, self.pending = {}, []
        self.roster = [[0, self.my_name]]
        self.state, self.state_t = "lobby", 0.0

    def connect_join(self):
        addr, port = self.join_text.strip() or "127.0.0.1", PORT
        if ":" in addr:
            addr, _, tail = addr.rpartition(":")
            try:
                port = int(tail)
            except ValueError:
                port = PORT
        try:
            self.net = client_connect(addr, port)
        except OSError:
            self.net = None
            self.join_err = "NO ANSWER FROM %s" % addr.upper()
            return
        self.role = "client"
        self.net.send({"t": "hello", "name": self.my_name, "v": 1})
        self.remotes, self.roster = {}, []
        self.state, self.state_t = "lobby", 0.0

    def to_lobby(self):
        self.state, self.state_t = "lobby", 0.0

    def leave_net(self, why=""):
        if self.net:
            self.net.close()
        self.net, self.role, self.my_pid = None, None, 0
        self.remotes, self.pending, self.roster = {}, [], []
        self.state, self.state_t = "title", 0.0
        if why:
            self.say(why, 3.5)

    def escape(self):
        """Esc from somewhere networked: back out one step, quietly."""
        if self.state == "join_ip":
            self.state, self.state_t = "title", 0.0
        elif self.net:
            if self.role == "host":
                self.net.broadcast({"t": "bye"})
            self.leave_net()

    def toggle_escmenu(self):
        """Esc mid-game: raise the menu, or fold it away again.  Solo, the
        world holds its breath underneath; on the LAN it plays on."""
        if self.state == "escmenu":
            self.state, self.state_t = self.esc_prev
        else:
            self.esc_prev = (self.state, self.state_t)
            self.esc_sel = 0
            self.state, self.state_t = "escmenu", 0.0

    def escmenu_act(self):
        if self.esc_sel == 0:                # back to the main menu
            if self.net:
                self.escape()
            else:
                self.best = max(self.best, self.player.score)
                self.state, self.state_t = "title", 0.0
        else:                                # quit
            self.quit = True

    def is_dead(self):
        """Dead even while the esc menu is hiding the state that says so."""
        return (self.state == "dead"
                or (self.state == "escmenu" and self.esc_prev[0] == "dead"))

    def net_fx(self, kind, x, z, color=None, skip=None):
        """Tell the clients something flashed or banged.  Host only; solo
        and client calls fall straight through."""
        if self.role != "host":
            return
        msg = {"t": "fx", "k": kind, "x": round(x, 1), "z": round(z, 1)}
        if color:
            msg["c"] = list(color)
        self.net.broadcast(msg, skip=skip)

    def net_start_level(self, level):
        seed = random.randrange(1 << 30)
        self.net.broadcast({"t": "start", "seed": seed, "level": level})
        if level <= 1:
            self.new_game(seed, 1)
        else:
            self.next_level(seed, level)

    def net_pump(self, dt):
        if self.role == "host":
            self.host_pump(dt)
        elif self.role == "client":
            self.client_pump(dt)

    # -- the host's half --------------------------------------------------
    def host_pump(self, dt):
        net = self.net
        for peer in net.admit():                     # newcomers knock
            if self.state == "lobby" and len(net.peers) < MAX_PLAYERS - 1:
                self.pending.append(peer)
            else:
                peer.send({"t": "no", "why": "GAME IN PROGRESS"
                           if self.state != "lobby" else "GAME IS FULL"})
                peer.close()
        for peer in self.pending[:]:                 # then introduce themselves
            for msg in peer.poll():
                if msg is None:
                    self.pending.remove(peer)
                    break
                if isinstance(msg, dict) and msg.get("t") == "hello":
                    self.pending.remove(peer)
                    pid = net.free_pid()
                    if pid is None:              # filled up while they knocked
                        peer.send({"t": "no", "why": "GAME IS FULL"})
                        peer.close()
                        break
                    peer.pid = pid
                    peer.name = (str(msg.get("name", ""))[:10].upper()
                                 or "TANK %d" % peer.pid)
                    net.peers[peer.pid] = peer
                    self.remotes[peer.pid] = RemotePlayer(peer.pid, peer.name)
                    peer.send({"t": "you", "id": peer.pid})
                    self.send_roster()
                    self.say("%s JOINED" % peer.name, 2.0)
                    break
        for pid, peer in list(net.peers.items()):
            for msg in peer.poll():
                if msg is None:
                    del net.peers[pid]
                    gone = self.remotes.pop(pid, None)
                    self.send_roster()
                    self.say("%s LEFT" % (gone.name if gone else "A TANK"), 2.5)
                    break
                if isinstance(msg, dict):
                    self.host_msg(pid, msg)
        if self.state in ("play", "dead", "clear", "escmenu"):
            self.snap_t += dt
            if self.snap_t >= SNAP_DT:
                self.snap_t = 0.0
                self.send_snapshot()

    def send_roster(self):
        self.roster = ([[0, self.my_name]]
                       + [[p, r.name] for p, r in sorted(self.remotes.items())])
        self.net.broadcast({"t": "roster", "pl": self.roster})

    def host_msg(self, pid, msg):
        t = msg.get("t")
        r = self.remotes.get(pid)
        if r is None:
            return
        if t == "p":
            was = r.alive
            r.pose(msg["x"], msg["z"], msg["yaw"], msg["sp"],
                   bool(msg["al"]), msg["sc"])
            if was and not r.alive:              # they were just blown apart
                self.bursts.append(Burst(r.x, 1.6, r.z, 1.6, COL_WARN, 28, 1.3))
                self.play_at("boom", r.x, r.z)
                self.net_fx("die", r.x, r.z, skip=pid)
        elif t == "fire":
            self.shells.append(Shell(msg["x"], 1.45, msg["z"], msg["dx"],
                                     msg["dz"], True, SHOT_DAMAGE, COL_SHOT,
                                     owner=pid))
            self.play_at("fire", msg["x"], msg["z"])
            self.net_fx("fire", msg["x"], msg["z"], skip=pid)

    def send_snapshot(self):
        p = self.player
        pl = [[0, round(p.x, 2), round(p.z, 2), round(p.yaw, 3),
               round(p.speed, 2), int(not self.is_dead()), p.score]]
        pl += [[pid, round(r.x, 2), round(r.z, 2), round(r.yaw, 3),
                round(r.speed, 2), int(r.alive), r.score]
               for pid, r in self.remotes.items()]
        en = [[0 if e.kind == "hunter" else 1, round(e.x, 2), round(e.z, 2),
               round(e.yaw, 3), round(e.speed, 2), int(e.flash > 0.0)]
              for e in self.enemies]
        sh = [[s.owner, round(s.x, 2), round(s.y, 2), round(s.z, 2),
               round(s.dx, 3), round(s.dz, 3), s.speed, s.damage,
               SHOT_COLS.index(s.color) if s.color in SHOT_COLS else 0]
              for s in self.shells]
        self.net.broadcast({"t": "s", "tl": round(self.time_left, 1),
                            "ft": self.flags_taken, "pl": pl, "en": en,
                            "sh": sh})

    # -- the client's half ------------------------------------------------
    def client_pump(self, dt):
        for msg in self.net.poll():
            if msg is None:
                self.leave_net("CONNECTION LOST")
                return
            if isinstance(msg, dict):
                self.client_msg(msg)
                if self.net is None:             # told to go home
                    return
        if self.state in ("play", "dead", "clear", "escmenu"):
            self.pose_t += dt
            if self.pose_t >= POSE_DT:
                self.pose_t = 0.0
                p = self.player
                self.net.send({"t": "p", "x": round(p.x, 2),
                               "z": round(p.z, 2), "yaw": round(p.yaw, 3),
                               "sp": round(p.speed, 2),
                               "al": int(not self.is_dead()),
                               "sc": p.score})

    def client_msg(self, msg):
        t = msg.get("t")
        if t == "s":
            self.apply_snapshot(msg)
        elif t == "fx":
            self.apply_fx(msg)
        elif t == "you":
            self.my_pid = msg["id"]
        elif t == "roster":
            self.roster = msg["pl"]
            names = {pid: name for pid, name in self.roster}
            for pid in [q for q in self.remotes if q not in names]:
                gone = self.remotes.pop(pid)
                if self.state != "lobby":
                    self.say("%s LEFT" % gone.name, 2.5)
            for pid, name in names.items():
                if pid != self.my_pid and pid not in self.remotes:
                    self.remotes[pid] = RemotePlayer(pid, name)
        elif t == "start":
            if msg["level"] <= 1:
                self.new_game(msg["seed"], 1)
            else:
                self.next_level(msg["seed"], msg["level"])
        elif t == "prize":
            self.prize_event(msg)
        elif t == "pspawn":
            prize = Prize(msg["x"], msg["z"], msg["k"])
            prize.pid = msg["id"]
            self.prizes.append(prize)
        elif t == "score":
            self.player.score += msg["v"]
        elif t == "clear":
            self.bonus = msg["b"]
            self.player.score += self.bonus
            self.state, self.state_t = "clear", 0.0
            self.audio.play("clear")
        elif t == "over":
            self.best = max(self.best, self.player.score)
            self.state, self.state_t = "over", 0.0
            self.say(msg.get("why", ""), 3.0)
        elif t == "tolobby":
            self.to_lobby()
        elif t == "no":
            self.leave_net(msg.get("why", "REFUSED"))
        elif t == "bye":
            self.leave_net("HOST LEFT")

    def apply_snapshot(self, msg):
        self.time_left = msg["tl"]
        self.flags_taken = msg["ft"]
        for pid, x, z, yaw, sp, al, sc in msg["pl"]:
            r = self.remotes.get(pid)
            if r:
                r.pose(x, z, yaw, sp, bool(al), sc)
        ghosts = []
        for k, x, z, yaw, sp, fl in msg["en"]:
            spec = HUNTER if k == 0 else SENTRY
            g = Tank(x, z, yaw, spec["shape"], spec["color"], spec["radius"])
            g.kind = "hunter" if k == 0 else "sentry"
            g.speed = sp
            g.flash = 0.08 if fl else 0.0
            ghosts.append(g)
        self.enemies = ghosts
        mine = [s for s in self.shells
                if s.friendly and s.owner == self.my_pid]
        for owner, x, y, z, dx, dz, sp, dmg, ci in msg["sh"]:
            if owner != self.my_pid:             # ours already fly locally
                mine.append(Shell(x, y, z, dx, dz, owner >= 0, dmg,
                                  SHOT_COLS[ci % len(SHOT_COLS)], sp, owner))
        self.shells = mine

    def apply_fx(self, msg):
        k, x, z = msg["k"], msg["x"], msg["z"]
        col = tuple(msg["c"]) if "c" in msg else COL_SHOT
        if k in ("fire", "efire"):
            self.play_at(k, x, z)
        elif k == "boom":
            self.bursts.append(Burst(x, 1.5, z, 1.25, col, 22, 1.0))
            self.play_at("boom", x, z)
        elif k == "die":
            self.bursts.append(Burst(x, 1.6, z, 1.6, COL_WARN, 28, 1.3))
            self.play_at("boom", x, z)

    def prize_event(self, msg):
        prize = next((q for q in self.prizes
                      if getattr(q, "pid", -1) == msg["id"]), None)
        if prize is None:
            return
        self.prizes.remove(prize)
        self.bursts.append(Burst(prize.x, 1.6, prize.z, 0.35,
                                 prize.color, 10, 0.5))
        self.flags_taken = msg["ft"]
        by, mine = msg["by"], msg["by"] == self.my_pid
        left = self.flags_needed - self.flags_taken
        if prize.kind == "flag":
            self.audio.play("flag")
            if mine:
                self.player.score += 250
                self.player.ammo += 5
            head = "FLAG SECURED" if mine else "%s TOOK A FLAG" % self.who(by)
            self.say("%s  --  %d TO GO" % (head, left) if left else
                     "ALL FLAGS SECURED", 1.6)
        elif mine and prize.kind == "ammo":
            self.player.ammo += 20
            self.audio.play("pod")
            self.say("AMMUNITION +20", 1.2)
        elif mine:
            self.player.shields = min(SHIELD_MAX, self.player.shields + 35.0)
            self.audio.play("pod")
            self.say("SHIELDS RESTORED", 1.2)

    # -- drawing ----------------------------------------------------------
    def draw(self):
        if self.state == "title":
            self.draw_title()
            return
        if self.state in ("join_ip", "lobby"):
            self.draw_net_screen()
            return
        if self.state == "settings":
            self.draw_settings()
            return
        self.draw_world()
        self.draw_hud()
        if self.state == "escmenu":
            self.draw_escmenu()
        elif self.state == "paused":
            self.panel(["PAUSED"], ["p  resume     esc  menu"])
        elif self.state == "clear":
            self.panel(["LEVEL %d CLEAR" % self.level,
                        "BONUS %d" % self.bonus,
                        "SCORE %d" % self.player.score],
                       ["the host calls the next level" if self.role == "client"
                        else "enter  next level"])
        elif self.state == "over":
            if self.role == "host":
                hint = "enter  back to lobby"
            elif self.role == "client":
                hint = "the host calls the lobby     esc  menu"
            else:
                hint = "enter  back to title"
            self.panel(["GAME OVER",
                        "SCORE %d" % self.player.score,
                        "BEST %d" % self.best], [hint])

    def camera(self):
        p = self.player
        sway = math.sin(p.bob * 2.0) * 0.07 * min(1.0, abs(p.speed) / 12.0)
        jolt = p.shake * math.sin(p.shake * 60.0 + p.bob * 9.0) * 0.05
        if self.chase:
            # Solids hide what is behind them, and that now includes the
            # camera: pull the boom in rather than film the inside of a wall.
            dx, dz = p.forward
            boom = 13.0
            while boom > 3.0:
                cx, cz = p.x - dx * boom, p.z - dz * boom
                if (abs(cx) < ARENA - 1.0 and abs(cz) < ARENA - 1.0
                        and not any(b.blocks(cx, cz, 1.5) for b in self.buildings)):
                    break
                boom -= 2.0
            self.view.set_camera(p.x - dx * boom, 3.0 + 3.4 * boom / 13.0 + sway,
                                 p.z - dz * boom, p.yaw, -0.19 - p.kick + jolt)
        else:
            self.view.set_camera(p.x, EYE + sway, p.z, p.yaw + jolt * 0.5,
                                 -p.kick + jolt)

    def draw_world(self):
        view, surf = self.view, self.screen
        surf.fill(COL_BG)
        self.camera()
        view.dim = 1.0

        f, cx, cy, w, h = view.f, view.cx, view.cy, view.w, view.h
        for star in self.stars:                       # a sky to fall short of
            x, y, z = view.to_cam(star)
            if z <= 1.0:
                continue
            px, py = cx + x / z * f, cy - y / z * f
            if 0.0 <= px < w and 0.0 <= py < h:
                surf.set_at((int(px), int(py)), COL_STAR)

        p = self.player
        view.segments(floor_grid(p.x, p.z), COL_GRID)   # the floor is under all

        # Everything else back to front, so a near solid paints over what it
        # hides -- fences and tracers included, or they would shine through.
        ex, ez = view.ex, view.ez
        queue = []
        for center, radius, segs in WALLS:
            queue.append(((center[0] - ex) ** 2 + (center[2] - ez) ** 2,
                          (center, radius, segs)))
        others = [r for r in self.remotes.values() if r.alive]
        for thing in self.buildings + self.prizes + self.enemies + others:
            queue.append(((thing.x - ex) ** 2 + (thing.z - ez) ** 2, thing))
        for thing in self.shells + self.bursts:
            queue.append(((thing.x - ex) ** 2 + (thing.z - ez) ** 2, thing))
        if self.chase and self.state != "dead":
            queue.append(((p.x - ex) ** 2 + (p.z - ez) ** 2, p))
        queue.sort(key=lambda item: -item[0])

        for _, thing in queue:
            if type(thing) is tuple:
                view.chunk(thing[0], thing[1], thing[2], COL_WALL)
            else:
                thing.draw(view)

        for r in self.remotes.values():          # call signs over the tanks
            if not r.alive:
                continue
            q = view.project((r.x, 3.6, r.z))
            if q is None:
                continue
            px, py, depth = q
            if not (0.0 <= px < w and 0.0 <= py < h):
                continue
            k = (1.0 - depth / FAR) ** 0.8       # fade with the tank
            if k <= 0.15:
                continue
            self.text(r.name, px, py,
                      self.tag if depth < 45.0 else self.tag_far,
                      tuple(int(c * k) for c in r.color), anchor="midbottom")

    # -- head-up display --------------------------------------------------
    def text(self, s, x, y, fnt, color=COL_HUD, anchor="topleft"):
        img = fnt.render(s, True, color)
        self.screen.blit(img, img.get_rect(**{anchor: (int(x), int(y))}))

    def bar(self, x, y, w, h, frac, color):
        surf = self.screen
        pygame.draw.rect(surf, (color[0] // 4, color[1] // 4, color[2] // 4),
                         (x, y, w, h), 1)
        fill = int((w - 4) * clamp(frac, 0.0, 1.0))
        if fill > 0:
            pygame.draw.rect(surf, color, (x + 2, y + 2, fill, h - 4))

    def draw_hud(self):
        surf, view, p = self.screen, self.view, self.player
        w, h = view.w, view.h

        if p.flash > 0.0 or p.shake > 0.25:            # taking fire
            k = int(clamp(max(p.flash * 6.0, p.shake * 0.7), 0.0, 1.0) * 120)
            pygame.draw.rect(surf, (k, k // 5, k // 6), (0, 0, w, h), 6)

        self.crosshair()

        self.text("SCORE %07d" % p.score, 18, 14, self.mid)
        self.text("LEVEL %d" % self.level, w / 2, 14, self.mid, anchor="midtop")
        low = self.time_left < 20.0
        clock = "%01d:%02d" % (int(self.time_left) // 60, int(self.time_left) % 60)
        self.text("TIME " + clock, w - 18, 14, self.mid,
                  COL_WARN if low and int(self.time_left * 3) % 2 else COL_HUD,
                  anchor="topright")

        base = h - 118
        self.text("SHIELDS", 18, base, self.small)
        frac = p.shields / SHIELD_MAX
        col = COL_HUD if frac > 0.55 else (255, 210, 90) if frac > 0.25 else COL_WARN
        self.bar(90, base - 2, 190, 15, frac, col)
        self.text("TURBO", 18, base + 24, self.small)
        self.bar(90, base + 22, 120, 11, p.turbo, COL_SHIELD)
        self.text("AMMO %3d" % p.ammo, 18, base + 46, self.mid,
                  COL_HUD if p.ammo > 5 else COL_WARN)

        self.text("FLAGS %d/%d" % (self.flags_taken, self.flags_needed),
                  w - 18, base, self.mid, COL_FLAG, anchor="topright")
        if self.role is None:
            self.text("TANKS", w - 18, base + 30, self.small, anchor="topright")
            for i in range(max(0, p.lives)):
                x = w - 26 - i * 20
                pygame.draw.polygon(surf, COL_HUD, ((x, base + 56), (x - 7, base + 68),
                                                    (x + 7, base + 68)), 1)
        else:                                  # the squadron, not the lives
            rows = [(self.my_pid, self.my_name, p.score)]
            rows += [(r.pid, r.name, r.score) for r in self.remotes.values()]
            y = base + 28
            for pid, name, score in sorted(rows):
                self.text("%-10s %6d" % (name, score), w - 18, y, self.small,
                          PLAYER_COLS[pid % len(PLAYER_COLS)], anchor="topright")
                y += 18
        self.text("v view    tab radar    p pause", w - 18, h - 20, self.tiny,
                  (48, 96, 78), anchor="bottomright")

        self.draw_radar()

        if self.msg_t > 0.0:
            k = clamp(self.msg_t * 2.0, 0.0, 1.0)
            col = tuple(int(c * k) for c in COL_HUD)
            self.text(self.msg, w / 2, h * 0.30, self.mid, col, anchor="center")

    def crosshair(self):
        surf, view, p = self.screen, self.view, self.player
        cx, cy = int(view.cx), int(view.cy)
        locked = False
        for e in self.enemies:
            err = wrap(math.atan2(e.x - p.x, e.z - p.z) - p.yaw)
            if abs(err) < 0.06 and math.hypot(e.x - p.x, e.z - p.z) < 140.0:
                locked = not line_blocked(p.x, p.z, e.x, e.z, self.buildings)
                if locked:
                    break
        col = COL_WARN if locked else (85, 205, 155)
        for sx in (-1, 1):
            pygame.draw.line(surf, col, (cx + sx * 9, cy), (cx + sx * 22, cy), 2)
            pygame.draw.line(surf, col, (cx, cy + sx * 9), (cx, cy + sx * 22), 2)
        surf.set_at((cx, cy), col)
        if locked:
            pygame.draw.rect(surf, col, (cx - 16, cy - 16, 32, 32), 1)

    def draw_radar(self):
        surf, view, p = self.screen, self.view, self.player
        cx, cy = int(view.w / 2), int(view.h - 122)
        r = 108
        pygame.draw.circle(surf, (16, 66, 50), (cx, cy), r, 1)
        pygame.draw.circle(surf, (12, 46, 36), (cx, cy), r * 2 // 3, 1)
        pygame.draw.line(surf, (12, 46, 36), (cx - r, cy), (cx + r, cy))
        pygame.draw.line(surf, (12, 46, 36), (cx, cy - r), (cx, cy + r))
        sweep = (self.state_t * 1.6) % TAU
        pygame.draw.line(surf, (22, 96, 70), (cx, cy),
                         (cx + math.sin(sweep) * r, cy - math.cos(sweep) * r))

        scale = r / self.radar_range
        cyaw, syaw = math.cos(p.yaw), math.sin(p.yaw)

        def blip(x, z):
            dx, dz = x - p.x, z - p.z
            if math.hypot(dx, dz) > self.radar_range:
                return None
            return (cx + (dx * cyaw - dz * syaw) * scale,
                    cy - (dx * syaw + dz * cyaw) * scale)

        for b in self.buildings:
            q = blip(b.x, b.z)
            if q:
                surf.set_at((int(q[0]), int(q[1])), (30, 78, 62))
        for prize in self.prizes:
            q = blip(prize.x, prize.z)
            if q:
                pygame.draw.circle(surf, prize.color, (int(q[0]), int(q[1])),
                                   3 if prize.kind == "flag" else 2)
        for e in self.enemies:
            q = blip(e.x, e.z)
            if q:
                x, y = int(q[0]), int(q[1])
                pygame.draw.polygon(surf, e.color,
                                    ((x, y - 4), (x - 4, y + 3), (x + 4, y + 3)), 1)
        for mate in self.remotes.values():
            q = blip(mate.x, mate.z) if mate.alive else None
            if q:
                x, y = int(q[0]), int(q[1])
                pygame.draw.polygon(surf, mate.color,
                                    ((x, y - 4), (x - 4, y + 3), (x + 4, y + 3)), 1)
        pygame.draw.polygon(surf, COL_WHITE,
                            ((cx, cy - 5), (cx - 4, cy + 4), (cx + 4, cy + 4)), 1)
        self.text("%dm" % int(self.radar_range), cx + r - 2, cy + r - 12,
                  self.tiny, (48, 110, 88), anchor="topright")

    def draw_escmenu(self):
        surf, view = self.screen, self.view
        w, h = view.w, view.h
        box = pygame.Rect(0, 0, 460, 190)
        box.center = (w // 2, int(h * 0.42))
        shade = pygame.Surface(box.size, pygame.SRCALPHA)
        shade.fill((4, 8, 12, 205))
        surf.blit(shade, box.topleft)
        pygame.draw.rect(surf, COL_HUD, box, 1)
        items = ("BACK TO THE MAIN MENU", "QUIT SPECTRE")
        y = box.top + 40
        for i, item in enumerate(items):
            on = i == self.esc_sel
            label = ("> %s <" % item) \
                if on and int(self.state_t * 2.5) % 2 == 0 else item
            self.text(label, box.centerx, y, self.mid,
                      COL_WHITE if on else (80, 190, 145), anchor="midtop")
            y += 44
        self.text("esc  back to the game", box.centerx, box.bottom - 32,
                  self.small, (70, 170, 130), anchor="midtop")

    def panel(self, lines, hints=()):
        surf, view = self.screen, self.view
        w, h = view.w, view.h
        box = pygame.Rect(0, 0, 460, 90 + 44 * len(lines))
        box.center = (w // 2, int(h * 0.42))
        shade = pygame.Surface(box.size, pygame.SRCALPHA)
        shade.fill((4, 8, 12, 205))
        surf.blit(shade, box.topleft)
        pygame.draw.rect(surf, COL_HUD, box, 1)
        y = box.top + 30
        for i, line in enumerate(lines):
            self.text(line, box.centerx, y, self.big if i == 0 else self.mid,
                      COL_HUD if i == 0 else COL_WHITE, anchor="midtop")
            y += 44
        for hint in hints:
            self.text(hint, box.centerx, box.bottom - 26, self.small,
                      (70, 170, 130), anchor="midtop")

    def draw_title(self):
        view, surf = self.view, self.screen
        surf.fill(COL_BG)
        t = self.state_t
        orbit = t * 0.30
        dist = 21.0
        view.dim = clamp(t * 0.8, 0.0, 1.0)
        view.set_camera(math.sin(orbit) * dist, 6.2 + math.sin(t * 0.5) * 1.2,
                        math.cos(orbit) * dist, orbit + math.pi, -0.22)
        f, cx, cy, w, h = view.f, view.cx, view.cy, view.w, view.h
        for star in self.stars:
            x, y, z = view.to_cam(star)
            if z > 1.0:
                px, py = cx + x / z * f, cy - y / z * f
                if 0.0 <= px < w and 0.0 <= py < h:
                    surf.set_at((int(px), int(py)), COL_STAR)
        view.segments(floor_grid(0.0, 0.0), COL_GRID)
        # Far to near, like the arena: when the orbit swings the flag round
        # the back, the tank must be free to paint over it.
        props = ((TITLE_SHAPE, 0.0, 0.0, t * 0.55, COL_HUD, 0.2),
                 (FLAG_SHAPE, 13.0, -7.0, -t * 1.1, COL_FLAG, 0.0))
        for shp, x, z, yaw, col, y in sorted(
                props, key=lambda p: -((p[1] - view.ex) ** 2
                                       + (p[2] - view.ez) ** 2)):
            view.shape(shp, x, z, yaw, col, y=y, width=2, glow=True)
        view.dim = 1.0

        self.text("S P E C T R E", w / 2, h * 0.14, font(58), COL_HUD, anchor="midtop")
        self.text("wireframe tank arena", w / 2, h * 0.14 + 74, self.mid,
                  (70, 170, 130), anchor="midtop")
        items = ("ONE PLAYER", "HOST A LAN GAME", "JOIN A LAN GAME",
                 "SETTINGS")
        y = h - 276
        for i, item in enumerate(items):
            on = i == self.menu_sel
            label = ("> %s <" % item) if on and int(t * 2.5) % 2 == 0 else item
            self.text(label, w / 2, y, self.mid,
                      COL_WHITE if on else (80, 190, 145), anchor="midtop")
            y += 32
        rows = ["W S  drive    A D  turn    space  fire    shift  turbo",
                "v  view    tab  radar    p  pause    esc  quit"]
        y = h - 128
        for row in rows:
            self.text(row, w / 2, y, self.small, (70, 160, 125), anchor="midtop")
            y += 24
        if self.best:
            self.text("BEST %d" % self.best, w / 2, h - 70, self.mid,
                      COL_FLAG, anchor="midtop")
        if self.msg_t > 0.0:                    # a word from the network
            self.text(self.msg, w / 2, h - 40, self.small, COL_WARN,
                      anchor="midtop")

    def draw_net_screen(self):
        """The address book and the lobby: flat screens, same wire art."""
        view, surf = self.view, self.screen
        surf.fill(COL_BG)
        w, h = view.w, view.h
        t = self.state_t
        self.text("S P E C T R E", w / 2, h * 0.10, font(44), COL_HUD,
                  anchor="midtop")
        if self.state == "join_ip":
            self.text("JOIN A LAN GAME", w / 2, h * 0.30, self.big,
                      COL_WHITE, anchor="midtop")
            self.text("HOST ADDRESS", w / 2, h * 0.30 + 70, self.small,
                      (80, 190, 145), anchor="midtop")
            box = pygame.Rect(0, 0, 420, 40)
            box.center = (w // 2, int(h * 0.30) + 118)
            pygame.draw.rect(surf, COL_HUD, box, 1)
            entry = self.join_text + ("_" if int(t * 2.5) % 2 else "")
            self.text(entry or " ", box.centerx, box.centery, self.mid,
                      COL_FLAG, anchor="center")
            if self.join_err:
                self.text(self.join_err, w / 2, box.bottom + 24, self.small,
                          COL_WARN, anchor="midtop")
            self.text("enter  connect     esc  back", w / 2, h - 60,
                      self.small, (70, 170, 130), anchor="midtop")
            return
        # the lobby
        self.text("LAN LOBBY", w / 2, h * 0.26, self.big, COL_WHITE,
                  anchor="midtop")
        if self.role == "host":
            self.text("PLAYERS JOIN THIS ADDRESS", w / 2, h * 0.26 + 52,
                      self.small, (80, 190, 145), anchor="midtop")
            self.text(self.net.ip, w / 2, h * 0.26 + 74, self.big,
                      COL_FLAG, anchor="midtop")
            self.text("port %d" % PORT, w / 2, h * 0.26 + 118, self.small,
                      (70, 160, 125), anchor="midtop")
            y = h * 0.26 + 158
        else:
            y = h * 0.26 + 110
        for pid, name in self.roster or [[self.my_pid, self.my_name]]:
            col = PLAYER_COLS[pid % len(PLAYER_COLS)]
            tag = "  (you)" if pid == self.my_pid else ""
            pygame.draw.polygon(surf, col,
                                ((w / 2 - 120, y + 14), (w / 2 - 127, y + 26),
                                 (w / 2 - 113, y + 26)), 1)
            self.text("%s%s" % (name, tag), w / 2 - 95, y + 8, self.mid, col)
            y += 40
        if self.role == "host":
            hint = "enter  launch     esc  close the lobby"
            if len(self.roster) < 2 and int(t * 1.5) % 2:
                self.text("WAITING FOR TANKS TO JOIN", w / 2, y + 18,
                          self.small, (80, 190, 145), anchor="midtop")
        else:
            hint = "waiting for the host to launch     esc  leave"
        self.text(hint, w / 2, h - 60, self.small, (70, 170, 130),
                  anchor="midtop")

    def draw_settings(self):
        view, surf = self.view, self.screen
        surf.fill(COL_BG)
        w, h = view.w, view.h
        t = self.state_t
        self.text("S P E C T R E", w / 2, h * 0.10, font(44), COL_HUD,
                  anchor="midtop")
        self.text("SETTINGS", w / 2, h * 0.30, self.big, COL_WHITE,
                  anchor="midtop")
        self.text("PLAYER NAME", w / 2, h * 0.30 + 70, self.small,
                  (80, 190, 145), anchor="midtop")
        box = pygame.Rect(0, 0, 300, 40)
        box.center = (w // 2, int(h * 0.30) + 118)
        pygame.draw.rect(surf, COL_HUD, box, 1)
        entry = self.name_text + ("_" if int(t * 2.5) % 2 else "")
        self.text(entry or " ", box.centerx, box.centery, self.mid,
                  COL_FLAG, anchor="center")
        self.text("this is the name the lobby and the other tanks see",
                  w / 2, box.bottom + 24, self.small, (70, 160, 125),
                  anchor="midtop")
        self.text("enter  save     esc  back", w / 2, h - 60,
                  self.small, (70, 170, 130), anchor="midtop")


def font(size):
    return pygame.font.SysFont(
        "dejavusansmono,liberationmono,couriernew,monospace", size)


# ------------------------------------------------------------------- main --

def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    mute = "--mute" in argv
    frames = None
    if "--frames" in argv:                       # a smoke test, not a feature
        frames = int(argv[argv.index("--frames") + 1])

    if not mute:
        try:
            pygame.mixer.pre_init(Audio.RATE, -16, 2, 512)
        except pygame.error:
            pass
    pygame.init()
    pygame.display.set_caption("Spectre")
    flags = pygame.RESIZABLE | (pygame.FULLSCREEN if "--fullscreen" in argv else 0)
    screen = pygame.display.set_mode((WIDTH, HEIGHT), flags)
    pygame.mouse.set_visible(False)

    game = Game(screen, Audio(enabled=not mute))
    clock = pygame.time.Clock()
    running = True

    while running:
        dt = min(0.05, clock.tick(FPS) / 1000.0)

        # Whoever owns the window decides how big it is -- a tiling compositor
        # very much included.  Read the size back, never argue with it: calling
        # set_mode() in reply to a resize starts a fight the window manager
        # always wins, and the window flickers for as long as it lasts.
        surface = pygame.display.get_surface()
        if surface is not None and surface.get_size() != (game.view.w, game.view.h):
            game.screen = surface
            game.view.set_surface(surface)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if game.state in ("join_ip", "settings"):
                    game.key(event)              # typing: letters are letters,
                                                 # esc backs out
                elif event.key in (pygame.K_ESCAPE, pygame.K_q):
                    if game.state in ("play", "paused", "dead", "clear",
                                      "over", "escmenu"):
                        game.toggle_escmenu()    # raise the menu, or lower it
                    elif game.net or game.state == "lobby":
                        game.escape()            # leave the LAN, keep the app
                    else:
                        running = False          # esc on the title quits
                elif event.key in (pygame.K_f, pygame.K_F11):
                    try:
                        pygame.display.toggle_fullscreen()
                    except pygame.error:
                        pass
                elif event.key == pygame.K_v:
                    game.chase = not game.chase
                else:
                    game.key(event)

        game.update(dt)
        game.draw()
        pygame.display.flip()
        if game.quit:                            # the esc menu said so
            running = False

        if frames is not None:
            frames -= 1
            if frames <= 0:
                running = False

    if game.net:                                 # hang up before leaving
        if game.role == "host":
            game.net.broadcast({"t": "bye"})
        game.net.close()
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
