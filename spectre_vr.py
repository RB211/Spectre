"""Spectre through a headset: OpenXR on top of the software renderer.

    .venv/bin/python spectre.py --vr        drive it in a headset
    .venv/bin/python spectre.py --vr-check  is the headset side all here?

The arena is still drawn the way the desk draws it -- hidden lines, filled
faces, painter's algorithm, all in pygame -- only twice, once from each eye,
and each picture is handed to the runtime as a texture.  Drawing is cheap
(an eye at headset resolution is under 2 ms; the work is in the edges, not
the pixels), so stereo costs a second pass and an upload, not a new
renderer.

The head rides in the tank: the tank's seat (Game.seat -- the desk camera
without its sway and recoil) is the anchor, and the head pose, measured
from where the player first sat, rides relative to it.  Look out the side
while the tank drives straight on.

The instruments cannot be pasted over each eye's picture: an eye's frustum
is lopsided, so the middle of its image is not straight ahead, and a flat
overlay comes out double.  Everything that is not the arena hangs as a pane
in the tank's space instead and goes through each eye's own matrix: menus
on one sheet, the gauges in two rows round the gaze, the reticle out on the
gun line.

Runtimes: WiVRn (a wireless Quest), Monado, or SteamVR -- whoever owns
active_runtime.json.  pyopenxr's ContextObject does the handshake, handed
SDL's live GLX context instead of opening a window of its own.
"""

import glob
import math
import os
import sys
import threading
import time
from ctypes import POINTER, byref, cast

import pygame
import xr
import xr.utils
import xr.utils.gl as xrgl
from OpenGL import GL
from OpenGL.GL import shaders

import spectre

# After the runtime moves its LOCAL origin (the Quest's own hold-the-Meta-
# button recenter), give the change this long to land before measuring the
# head again.
RECENTER_SETTLE = 0.3

# The errors that mean the session (or the whole runtime) is gone, not that
# one call went wrong; and how long to wait before asking a runtime that is
# not back yet -- a server mid-restart -- for a new instance.
LOST = (xr.exception.SessionLostError, xr.exception.InstanceLostError)
REJOIN_RETRY = 3.0

WAIT_HINT = ("start Quest Link / Air Link or SteamVR"
             if sys.platform == "win32"
             else "start the WiVRn app on the Quest")

# Where things hang, in the tank's space (meters, degrees).  The canvases
# are painted in pixels; DENSITY says how many pixels make a degree at the
# pane, so print keeps a size the visor can resolve.
HUD_DENSITY = 18.0       # instrument canvas, pixels to the degree
MENU_SPAN = 66.0         # the menu sheet, degrees edge to edge
MENU_DIST, MENU_PITCH = 1.2, -4.0
TOP_DIST, DASH_DIST = 1.0, 0.8
TOP_EDGE = 10.0          # the readouts' bottom edge, above the sight line
DASH_EDGE = -12.0        # the dash's top edge, below it
DASH_FLOOR = -36.0       # the dash must end above this
ROW_GAP = 4.0            # degrees of air between neighbouring panes
ROW_HALF = 35.0          # a row reaches no further than this either side
GLASS_DIST = 20.0        # the reticle, out along the gun line
RETICLE_DENSITY = 9.0    # and drawn larger than the desk's: pixels/degree
# (panes left to right, the one on the centre line) -- the readouts above
# the sight line with the message in the middle, the dash below with the
# radar in the middle.  Each pane faces the eye.
HUD_ROWS = ((("score", "message", "clock"), "message", TOP_DIST, TOP_EDGE),
            (("status", "radar", "tally"), "radar", DASH_DIST, DASH_EDGE))
PANE_NEAR, PANE_FAR = 0.05, 100.0


class _SDLContext(xr.utils.GraphicsContextProvider):
    """pygame owns the GL context and it lives on this thread for the whole
    run, so binding is a settled matter."""

    def make_current(self):
        pass

    def done_current(self):
        pass

    def destroy(self):
        pass


# The runtime offers its swapchain formats; prefer the ones where our 8-bit
# picture means what the compositor thinks it means.  An sRGB swapchain
# stores our gamma-encoded colours as they are; anything else is read as
# linear, and the compositor pass decodes for it (Compositor, `decode`).
_SRGB = (GL.GL_SRGB8_ALPHA8, GL.GL_SRGB8)
_FORMAT_PREFERENCE = _SRGB + (GL.GL_RGBA8, GL.GL_RGB10_A2, GL.GL_RGBA16F)


def _pick_format(runtime_formats):
    for want in _FORMAT_PREFERENCE:
        if want in runtime_formats:
            return want
    raise RuntimeError("no shared swapchain format with the runtime")


xrgl.OpenGLGraphics.select_color_swapchain_format = staticmethod(_pick_format)


def _quat_columns(q):
    """The rotation's three columns -- the pose's local axes expressed in
    reference space."""
    x, y, z, w = q.x, q.y, q.z, q.w
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)),
        (2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x)),
        (2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)),
    )


def _mat_mul(a, b):
    """Two row-major 4x4s, as flat lists."""
    return [sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
            for r in range(4) for c in range(4)]


class _SplitContext(xrgl.ContextObject):
    """ContextObject with its __enter__ cut in two.  WiVRn holds
    xrCreateInstance on a socket until a headset actually connects, so the
    instance half runs on a worker thread with nothing GL about it; the
    session half touches the GL context and the swapchains and stays on the
    main thread."""

    space_changed = False

    def enter_instance(self):
        """The blocking half: instance and system.  No GL, no SDL -- safe
        off the main thread."""
        self.instance = xr.create_instance(
            create_info=self._instance_create_info)
        self.system_id = xr.get_system(
            instance=self.instance,
            get_info=xr.SystemGetInfo(form_factor=self.form_factor))

    def poll_xr_events(self):
        """ContextObject's event pump, with an ear out for the one event it
        drops: the runtime moving the LOCAL origin under us.  The pump reads
        xr.poll_event through the module, so a spy there hears every event
        without re-implementing the pump."""
        real = xr.poll_event

        def spy(instance):
            buf = real(instance)
            if buf.type == \
                    xr.StructureType.EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING:
                self.space_changed = True
            return buf

        xr.poll_event = spy
        try:
            super().poll_xr_events()
        finally:
            xr.poll_event = real

    def enter_session(self):
        """The GL half, on the main thread with the context current: the
        graphics binding, the session, the reference space, the action set,
        the swapchains -- ContextObject.__enter__ from the graphics binding
        down.  Returns the swapchain's colour format."""
        from xr.utils.gl.context_object import SwapchainStruct
        self.graphics = xrgl.OpenGLGraphics(
            instance=self.instance,
            system=self.system_id,
            context_provider=self.context_provider)
        self.graphics_binding_pointer = self.graphics.graphics_binding.pointer
        self._session_create_info.next = self.graphics_binding_pointer
        self._session_create_info.system_id = self.system_id
        self.session = xr.create_session(
            instance=self.instance,
            create_info=self._session_create_info)
        self.space = xr.create_reference_space(
            session=self.session,
            create_info=self._reference_space_create_info)
        self.default_action_set = xr.create_action_set(
            instance=self.instance,
            create_info=xr.ActionSetCreateInfo(
                action_set_name="default_action_set",
                localized_action_set_name="Default Action Set",
                priority=0))
        self.action_sets.append(self.default_action_set)
        config_views = xr.enumerate_view_configuration_views(
            instance=self.instance,
            system_id=self.system_id,
            view_configuration_type=self.view_configuration_type)
        self.graphics.initialize_resources()
        color_format = self.graphics.select_color_swapchain_format(
            xr.enumerate_swapchain_formats(self.session))
        self.swapchains.clear()
        self.swapchain_image_buffers.clear()
        self.swapchain_image_ptr_buffers.clear()
        for vp in config_views:
            info = xr.SwapchainCreateInfo(
                array_size=1,
                format=color_format,
                width=vp.recommended_image_rect_width,
                height=vp.recommended_image_rect_height,
                mip_count=1,
                face_count=1,
                sample_count=vp.recommended_swapchain_sample_count,
                usage_flags=xr.SwapchainUsageFlags.SAMPLED_BIT
                | xr.SwapchainUsageFlags.COLOR_ATTACHMENT_BIT)
            swapchain = SwapchainStruct(
                xr.create_swapchain(session=self.session, create_info=info),
                info.width, info.height)
            self.swapchains.append(swapchain)
            images = xr.enumerate_swapchain_images(
                swapchain=swapchain.handle,
                element_type=self.graphics.swapchain_image_type)
            self.swapchain_image_buffers.append(images)
            ptrs = (POINTER(xr.SwapchainImageBaseHeader) * len(images))()
            for ix in range(len(images)):
                ptrs[ix] = cast(byref(images[ix]),
                                POINTER(xr.SwapchainImageBaseHeader))
            self.swapchain_image_ptr_buffers.append(ptrs)
        self.graphics.make_current()
        return color_format


# ------------------------------------------------------------ the hands --

# The Touch controllers.  Each action is read once a frame; the ones that
# are held (drive, fire, turbo) become keys held down (Game.held), and the
# ones that are pressed become key presses posted to pygame's queue -- so
# every menu, the esc menu and the text boxes work in the hands unchanged.
# (name, kind, bindings by interaction profile.)  The simple controller
# gets fire and the menu, which is enough to get round.
_L, _R = "/user/hand/left", "/user/hand/right"
_BOOL, _FLOAT, _VEC2 = (xr.ActionType.BOOLEAN_INPUT,
                        xr.ActionType.FLOAT_INPUT,
                        xr.ActionType.VECTOR2F_INPUT)
_TOUCH = "/interaction_profiles/oculus/touch_controller"
_SIMPLE = "/interaction_profiles/khr/simple_controller"
TOUCH_ACTIONS = (
    ("drive", _VEC2, {_TOUCH: [_L + "/input/thumbstick"]}),
    ("steer", _VEC2, {_TOUCH: [_R + "/input/thumbstick"]}),
    ("fire", _FLOAT, {_TOUCH: [_R + "/input/trigger/value",
                               _L + "/input/trigger/value"],
                      _SIMPLE: [_R + "/input/select/click"]}),
    ("turbo", _FLOAT, {_TOUCH: [_L + "/input/squeeze/value",
                                _R + "/input/squeeze/value"]}),
    ("a", _BOOL, {_TOUCH: [_R + "/input/a/click"]}),
    ("b", _BOOL, {_TOUCH: [_R + "/input/b/click"]}),
    ("x", _BOOL, {_TOUCH: [_L + "/input/x/click"]}),
    ("y", _BOOL, {_TOUCH: [_L + "/input/y/click"]}),
    ("lclick", _BOOL, {_TOUCH: [_L + "/input/thumbstick/click"]}),
    ("rclick", _BOOL, {_TOUCH: [_R + "/input/thumbstick/click"]}),
    ("menu", _BOOL, {_TOUCH: [_L + "/input/menu/click"],
                     _SIMPLE: [_L + "/input/menu/click"]}),
)
PULL = 0.5               # a trigger or grip this far in is held
STICK = 0.5              # a stick this far over is a key held
# Presses: the button, and the key it posts.  B backs out of a menu (esc)
# and the menu button raises the esc menu -- both are esc, which on the
# title would quit, so there they do nothing: quit from the menu instead.
PRESSES = (("a", pygame.K_RETURN), ("b", pygame.K_ESCAPE),
           ("menu", pygame.K_ESCAPE), ("x", pygame.K_TAB),
           ("y", pygame.K_v), ("rclick", pygame.K_p),
           ("lclick", pygame.K_F12))


class Hands:
    """The controllers as the keyboard: fills Game.held from the sticks,
    triggers and grips, and posts a key press for each button pressed --
    and for each flick of the left stick up or down, which walks a menu."""

    def __init__(self):
        self.was = {}

    def apply(self, state, game):
        drive = state.get("drive", (0.0, 0.0))
        steer = state.get("steer", (0.0, 0.0))
        held = set()
        if drive[1] > STICK:
            held.add(pygame.K_w)
        elif drive[1] < -STICK:
            held.add(pygame.K_s)
        turn = drive[0] if abs(drive[0]) >= abs(steer[0]) else steer[0]
        if turn < -STICK:
            held.add(pygame.K_a)
        elif turn > STICK:
            held.add(pygame.K_d)
        if state.get("fire", 0.0) > PULL:
            held.add(pygame.K_SPACE)
        if state.get("turbo", 0.0) > PULL:
            held.add(pygame.K_LSHIFT)
        game.held = held

        now = {name: state.get(name, 0.0) > PULL for name, _k in PRESSES}
        now["up"] = drive[1] > STICK
        now["down"] = drive[1] < -STICK
        keys = [k for name, k in PRESSES if now[name] and not self.was.get(name)]
        if now["up"] and not self.was.get("up"):
            keys.append(pygame.K_UP)
        if now["down"] and not self.was.get("down"):
            keys.append(pygame.K_DOWN)
        self.was = now
        for k in keys:
            if k == pygame.K_ESCAPE and game.state == "title":
                continue
            pygame.event.post(pygame.event.Event(
                pygame.KEYDOWN, key=k, mod=0, unicode="", scancode=0))

    def release(self, game):
        self.was = {}
        game.held = set()


# ----------------------------------------------------------- the driver --

class VRDriver:
    """Owns the OpenXR session: paces the frames, reads the head and the
    hands, and turns each runtime view into an eye to draw."""

    def __init__(self):
        self.ctx = self._new_context()
        self._entered = False
        self._lost = False               # a view saw the session go
        self._state = "idle"             # pending -> ready | failed
        self._error = None
        self.decode = 1.0                # gamma for a linear swapchain
        # The seated head, measured once: its position and level heading in
        # LOCAL become the tank's eye point, whatever origin the runtime
        # happened to pick.  None until measured.
        self._origin = None
        self._recenter_at = 0.0          # measure on the first frame
        # Where the player is looking, off the seat: (yaw, pitch) in
        # radians, yaw positive to the right.  None until a seat stands.
        self.head = None
        self.hands = {}                  # action name -> its value
        self._actions = {}               # action name -> (xr.Action, kind)
        self._hand_paths = ()
        self._haptic = None

    @staticmethod
    def _new_context():
        """A fresh handshake: one per session, since a lost session (or a
        lost runtime -- WiVRn restarted) takes its instance with it and the
        next one starts from nothing."""
        return _SplitContext(
            context_provider=_SDLContext(),
            instance_create_info=xr.InstanceCreateInfo(
                enabled_extension_names=[xr.KHR_OPENGL_ENABLE_EXTENSION_NAME]),
            reference_space_create_info=xr.ReferenceSpaceCreateInfo(
                reference_space_type=xr.ReferenceSpaceType.LOCAL),
        )

    @property
    def eye_sizes(self):
        return [(s.width, s.height) for s in self.ctx.swapchains]

    def recenter(self):
        """Take wherever the head is now as the seat."""
        self._recenter_at = time.monotonic()

    def open(self):
        """Start the handshake without blocking the game: a daemon thread
        sits out WiVRn's wait for a headset while the desk plays on."""

        def wait_for_runtime():
            try:
                try:
                    self.ctx.enter_instance()
                except xr.exception.RuntimeUnavailableError:
                    # No active runtime registered -- but an installed
                    # manifest may be sitting right there.
                    manifest = find_manifest()
                    if manifest is None:
                        raise
                    os.environ["XR_RUNTIME_JSON"] = manifest
                    print("spectre: no active OpenXR runtime registered;"
                          " using %s" % manifest)
                    self.ctx.enter_instance()
                self._state = "ready"
            except Exception as exc:     # reported from frames()
                self._error = exc
                self._state = "failed"

        self._state = "pending"
        threading.Thread(target=wait_for_runtime, daemon=True,
                         name="spectre-xr-open").start()

    def close(self):
        if self._entered or self._state == "ready":
            self._teardown()
        # A thread still blocked in the runtime's recvmsg is a daemon:
        # process exit is what frees it.

    def _teardown(self):
        """Let go of everything the runtime gave us, one handle at a time --
        after a loss any of them may refuse, and one refusal must not strand
        the rest -- and hand the window its own framebuffer back."""
        ctx = self.ctx

        def quietly(fn, *args):
            try:
                fn(*args)
            except Exception:
                pass

        if getattr(ctx, "default_action_set", None) is not None:
            quietly(xr.destroy_action_set, ctx.default_action_set)
            ctx.default_action_set = None
        if getattr(ctx, "space", None) is not None:
            quietly(xr.destroy_space, ctx.space)
            ctx.space = None
        if ctx.session is not None:
            quietly(xr.destroy_session, ctx.session)
            ctx.session = None
        if ctx.graphics is not None:
            quietly(ctx.graphics.destroy)
            ctx.graphics = None
        if ctx.instance is not None:
            quietly(xr.destroy_instance, ctx.instance)
            ctx.instance = None
        quietly(GL.glBindFramebuffer, GL.GL_FRAMEBUFFER, 0)
        self._entered = False
        self._lost = False
        self._actions, self._hand_paths, self._haptic = {}, (), None
        self.hands = {}
        self.head = None
        self._origin, self._recenter_at = None, 0.0   # measure afresh

    def frames(self):
        """The runtime's frame pacing.  Yields a frame_state once per
        predicted display frame while the session runs -- and None while it
        does not (headset off, WiVRn waiting for a client), so the caller
        keeps the window alive and plays on the desk until the visor goes
        on.  ContextObject.frame_loop would sleep those stretches away with
        the event queue frozen, and the compositor calls that Application
        Not Responding.

        A session that ends -- lost (WiVRn restarted, the link dropped), or
        told to go by the runtime -- is let go, and the desk plays while the
        driver waits for the headset to come back, exactly as at startup.
        Only a runtime that was never there at all ends VR for the run."""
        first = True
        while True:
            told = False
            begun = time.monotonic()
            while self._state == "pending":
                if not told and time.monotonic() - begun > 2.0:
                    print("spectre: waiting for the headset -- %s;"
                          " playing on the desk until it joins." % WAIT_HINT)
                    told = True
                yield None
            if self._state == "failed":
                if first:
                    print("spectre: VR unavailable (%s); playing on the"
                          " desk." % self._error)
                    return
                # The runtime is not back yet (a server mid-restart): sit a
                # few seconds out on the desk and ask again.
                until = time.monotonic() + REJOIN_RETRY
                while time.monotonic() < until:
                    yield None
                self._restart()
                continue
            try:
                color_format = self.ctx.enter_session()
                self.decode = 1.0 if color_format in _SRGB else 2.2
            except Exception as exc:
                print("spectre: VR session failed (%s); playing on the"
                      " desk." % exc)
                if first:
                    self._teardown()
                    return
                self._restart()
                continue
            first = False
            self._entered = True
            print("spectre: headset session up.")
            try:
                why = yield from self._run()
            except LOST as exc:
                why = "lost (%s)" % exc
            print("spectre: headset session %s; playing on the desk until"
                  " it rejoins." % why)
            self._restart()

    def _restart(self):
        self._teardown()
        self.ctx = self._new_context()
        self.open()

    def _run(self):
        """One session, frame by frame, until it ends: returns why."""
        ctx = self.ctx
        try:
            self._make_actions()
        except Exception as exc:         # a runtime with no hands for us
            print("spectre: VR controllers unavailable (%s)." % exc)
            self._actions = {}
        xr.attach_session_action_sets(
            session=ctx.session,
            attach_info=xr.SessionActionSetsAttachInfo(
                count_action_sets=len(ctx.action_sets),
                action_sets=(xr.ActionSet * len(ctx.action_sets))(
                    *ctx.action_sets)))
        while True:
            ctx.poll_xr_events()
            if ctx.exit_render_loop:     # loss pending, or told to exit
                return "ended by the runtime"
            if not ctx.session_is_running or ctx.session_state not in (
                    xr.SessionState.READY,
                    xr.SessionState.SYNCHRONIZED,
                    xr.SessionState.VISIBLE,
                    xr.SessionState.FOCUSED):
                self.hands = {}          # hands down while not ours
                self.head = None
                yield None               # an idle beat: the desk's turn
                continue
            frame_state = xr.wait_frame(ctx.session)
            xr.begin_frame(ctx.session)
            self._read_actions()
            self._read_head(frame_state)
            ctx.render_layers = []
            ctx.graphics.make_current()
            yield frame_state
            if self._lost:               # a view saw it go mid-frame
                return "lost"
            xr.end_frame(
                ctx.session,
                frame_end_info=xr.FrameEndInfo(
                    display_time=frame_state.predicted_display_time,
                    environment_blend_mode=ctx.environment_blend_mode,
                    layers=ctx.render_layers))
            GL.glGetError()              # SteamVR-on-Linux housekeeping

    def eyes(self, frame_state, seat):
        """The frame's views, each yielded with its swapchain image bound:
        draw the eye inside.  `seat` is Game.seat()'s (x, y, z, yaw) -- the
        tank the head rides in.  Each eye is a dict: its world position and
        basis for View.set_eye, its frustum tangents for View.set_frustum,
        and `mvp`, the matrix from the tank's space (x right, y up, z
        forward, meters from the seat) to the eye's clip space, for the
        panes."""
        sx, sy, sz, yaw = seat
        cy, syw = math.cos(yaw), math.sin(yaw)
        # The seat's axes in the world, as the desk camera has them.
        right_a, fwd_a = (cy, 0.0, -syw), (syw, 0.0, cy)

        def to_world(v):
            return (right_a[0] * v[0] + fwd_a[0] * v[2],
                    v[1],
                    right_a[2] * v[0] + fwd_a[2] * v[2])

        if self.ctx.space_changed:
            self.ctx.space_changed = False
            self._recenter_at = time.monotonic() + RECENTER_SETTLE
        if self._recenter_at is not None \
                and time.monotonic() >= self._recenter_at:
            self._measure_seat(frame_state)

        try:
            for index, view in enumerate(self.ctx.view_loop(frame_state)):
                cols, p = self._seated(view.pose)
                # XR is right-handed with -z forward; the game's camera is
                # x right, y up, z forward.  A column of the pose's rotation
                # is that local axis in xr coordinates: flipping its z reads
                # it in the seat's camera coordinates, and the eye's forward
                # is the pose's -z.
                r = (cols[0][0], cols[0][1], -cols[0][2])
                u = (cols[1][0], cols[1][1], -cols[1][2])
                f = (-cols[2][0], -cols[2][1], cols[2][2])
                e = (p[0], p[1], -p[2])
                ew = to_world(e)
                fov = view.fov
                tl, tr = math.tan(fov.angle_left), math.tan(fov.angle_right)
                td, tu = math.tan(fov.angle_down), math.tan(fov.angle_up)
                view4 = [r[0], r[1], r[2], -(r[0] * e[0] + r[1] * e[1] + r[2] * e[2]),
                         u[0], u[1], u[2], -(u[0] * e[0] + u[1] * e[1] + u[2] * e[2]),
                         f[0], f[1], f[2], -(f[0] * e[0] + f[1] * e[1] + f[2] * e[2]),
                         0.0, 0.0, 0.0, 1.0]
                n, fa = PANE_NEAR, PANE_FAR
                proj4 = [2.0 / (tr - tl), 0.0, -(tr + tl) / (tr - tl), 0.0,
                         0.0, 2.0 / (tu - td), -(tu + td) / (tu - td), 0.0,
                         0.0, 0.0, (fa + n) / (fa - n), -2.0 * fa * n / (fa - n),
                         0.0, 0.0, 1.0, 0.0]
                yield {
                    "index": index,
                    "eye": (sx + ew[0], sy + ew[1], sz + ew[2]),
                    "basis": (to_world(r), to_world(u), to_world(f)),
                    "tans": (tl, tr, td, tu),
                    "mvp": _mat_mul(proj4, view4),
                }
        except LOST:                     # the session went mid-frame:
            self._lost = True            # frames() hears of it next

    def _make_actions(self):
        """The controllers' actions, with bindings suggested for the Touch
        (and the bare simple controller).  Runs between session creation
        and attach -- the only window OpenXR allows."""
        inst, aset = self.ctx.instance, self.ctx.default_action_set
        path = lambda s: xr.string_to_path(inst, s)
        self._hand_paths = (path(_L), path(_R))
        per_profile = {}
        for name, kind, binds in TOUCH_ACTIONS:
            action = xr.create_action(
                action_set=aset,
                create_info=xr.ActionCreateInfo(
                    action_name=name, action_type=kind,
                    localized_action_name=name))
            self._actions[name] = (action, kind)
            for profile, where in binds.items():
                per_profile.setdefault(profile, []).extend(
                    xr.ActionSuggestedBinding(action, path(w)) for w in where)
        self._haptic = xr.create_action(
            action_set=aset,
            create_info=xr.ActionCreateInfo(
                action_name="rumble",
                action_type=xr.ActionType.VIBRATION_OUTPUT,
                subaction_paths=list(self._hand_paths),
                localized_action_name="rumble"))
        per_profile[_TOUCH].extend(
            xr.ActionSuggestedBinding(self._haptic, path(h + "/output/haptic"))
            for h in (_L, _R))
        for profile, binds in per_profile.items():
            try:                         # one profile refused is no reason
                xr.suggest_interaction_profile_bindings(   # to lose the rest
                    instance=inst,
                    suggested_bindings=xr.InteractionProfileSuggestedBinding(
                        interaction_profile=path(profile),
                        suggested_bindings=binds))
            except xr.XrException as exc:
                print("spectre: %s bindings refused (%s)" % (profile, exc))

    def _read_actions(self):
        """Sync the hands and read every action into `hands`."""
        if not self._actions:
            return
        session = self.ctx.session
        try:
            xr.sync_actions(session, xr.ActionsSyncInfo(
                active_action_sets=[xr.ActiveActionSet(
                    self.ctx.default_action_set, xr.NULL_PATH)]))
        except xr.XrException:
            self.hands = {}
            return
        state = {}
        for name, (action, kind) in self._actions.items():
            info = xr.ActionStateGetInfo(action=action)
            try:
                if kind == _VEC2:
                    got = xr.get_action_state_vector2f(session, info)
                    v = (got.current_state.x, got.current_state.y)
                elif kind == _FLOAT:
                    v = xr.get_action_state_float(session, info).current_state
                else:
                    v = float(xr.get_action_state_boolean(
                        session, info).current_state)
            except xr.XrException:
                continue
            state[name] = v
        self.hands = state

    def buzz(self, strength, ms):
        """A thump in both palms: `strength` 0..1 for `ms`."""
        if self._haptic is None or not self._entered:
            return
        session = self.ctx.session
        for hand in self._hand_paths:
            info = xr.HapticActionInfo(action=self._haptic, subaction_path=hand)
            try:
                xr.apply_haptic_feedback(session, info, xr.HapticVibration(
                    duration=int(ms) * 1000000,
                    frequency=xr.FREQUENCY_UNSPECIFIED,
                    amplitude=float(min(1.0, strength))))
            except xr.XrException:
                pass

    def _locate(self, frame_state):
        try:
            return xr.locate_views(
                session=self.ctx.session,
                view_locate_info=xr.ViewLocateInfo(
                    view_configuration_type=self.ctx.view_configuration_type,
                    display_time=frame_state.predicted_display_time,
                    space=self.ctx.space))
        except xr.XrException:
            return None, ()

    def _read_head(self, frame_state):
        """The gaze off the seat, for the gauges to follow: the pose's
        forward (its -z) re-read in the seat's frame and taken apart into a
        heading and an elevation."""
        self.head = None
        if self._origin is None:
            return
        state, views = self._locate(frame_state)
        if not views or not state.view_state_flags \
                & xr.VIEW_STATE_ORIENTATION_VALID_BIT:
            return
        cols, _p = self._seated(views[0].pose)
        fx, fy, fz = -cols[2][0], -cols[2][1], -cols[2][2]   # xr: z back
        self.head = (math.atan2(fx, -fz), math.atan2(fy, math.hypot(fx, fz)))

    def _measure_seat(self, frame_state):
        """Measure the head -- the midpoint between the eyes, and its
        heading with pitch and roll left out -- and make it the seat.
        Nothing tracked yet (headset face down, poses invalid) and the
        measurement waits for a frame that has one."""
        state, views = self._locate(frame_state)
        need = xr.VIEW_STATE_POSITION_VALID_BIT \
            | xr.VIEW_STATE_ORIENTATION_VALID_BIT
        if not views or state.view_state_flags & need != need:
            return
        n = float(len(views))
        head = tuple(sum(getattr(v.pose.position, a) for v in views) / n
                     for a in "xyz")
        back = _quat_columns(views[0].pose.orientation)[2]   # xr +z, flat
        bx, bz = back[0], back[2]
        k = math.hypot(bx, bz)
        if k < 1e-3:                     # staring straight up or down:
            bx, bz, k = 0.0, 1.0, 1.0    # keep the runtime's heading
        self._origin = (head, (bx / k, bz / k))
        self._recenter_at = None

    def _seated(self, pose):
        """A LOCAL pose re-read relative to the measured seat: its
        rotation's columns and its position, both in the seat's frame (xr
        axes: x right, y up, z back)."""
        cols = _quat_columns(pose.orientation)
        p = pose.position
        if self._origin is None:
            return cols, (p.x, p.y, p.z)
        (hx, hy, hz), (bx, bz) = self._origin
        rx, rz = bz, -bx                 # right = up x back

        def seat(v):
            return (v[0] * rx + v[2] * rz, v[1], v[0] * bx + v[2] * bz)

        return (tuple(seat(c) for c in cols),
                seat((p.x - hx, p.y - hy, p.z - hz)))


def find_manifest():
    """An installed runtime's manifest, when none is registered active:
    WiVRn first (the wireless Quest), then whatever else is there."""
    found = []
    for root in ("/usr/share/openxr/1", "/usr/local/share/openxr/1"):
        found += sorted(glob.glob(os.path.join(root, "openxr_*.json")))
    for path in found:
        if "wivrn" in path:
            return path
    return found[0] if found else None


# ------------------------------------------------------------ the panes --

def _pane(yaw, pitch, dist, half_w, half_h):
    """A pane's corners in the tank's space (x right, y up, z forward),
    top-left round to bottom-left, hung `dist` out along (yaw, pitch) in
    degrees and turned to face the eye."""
    a, b = math.radians(yaw), math.radians(pitch)
    f = (math.sin(a) * math.cos(b), math.sin(b), math.cos(a) * math.cos(b))
    r = (math.cos(a), 0.0, -math.sin(a))
    u = (f[1] * r[2] - f[2] * r[1], f[2] * r[0] - f[0] * r[2],
         f[0] * r[1] - f[1] * r[0])
    c = tuple(v * dist for v in f)
    return [tuple(c[i] + r[i] * half_w * sx + u[i] * half_h * sy
                  for i in range(3))
            for sx, sy in ((-1, 1), (1, 1), (1, -1), (-1, -1))]


def _span(px, dist, mpp):
    """A pane's extent in degrees, `px` pixels across at `dist` meters."""
    return 2.0 * math.degrees(math.atan(px * mpp * 0.5 / dist))


def _hang_row(panes, center, dist, edge, k):
    """One row of (name, rect) panes, measured and spread: each pane's
    angular size from its pixels (shrunk by k), laid left to right with
    ROW_GAP between neighbours, the `center` pane on the centre line, and
    all of it shrunk as far as it takes to keep within ROW_HALF.  A row
    below the eye (edge < 0) hangs from its top edge, one above sits on its
    bottom edge.  Returns [(name, rect, yaw, pitch, half_w, half_h, dist)]
    and the row's lowest edge, in degrees."""
    mpp = dist * math.tan(math.radians(1.0)) / HUD_DENSITY

    def pitch_of(r, k):
        tall = _span(r.h, dist, mpp * k)
        return edge - tall * 0.5 if edge < 0 else edge + tall * 0.5

    def lay(k):
        # Off the level a degree of heading is less than a degree across,
        # so widths and gaps are spread by 1/cos(pitch), or a low row's
        # neighbours would close up.
        x, spots = 0.0, []
        for _name, r in panes:
            stretch = 1.0 / math.cos(math.radians(pitch_of(r, k)))
            across = _span(r.w, dist, mpp * k) * stretch
            spots.append((x, across))
            x += across + ROW_GAP * stretch
        names = [n for n, _r in panes]
        if center in names:
            left, across = spots[names.index(center)]
            mid = left + across * 0.5
        else:
            mid = (spots[-1][0] + spots[-1][1]) * 0.5
        return [(left - mid, across) for left, across in spots]

    spots = lay(k)
    while k > 0.2 and max(max(-l, l + a) for l, a in spots) > ROW_HALF:
        k *= 0.97
        spots = lay(k)
    out, low = [], edge
    for (name, r), (left, across) in zip(panes, spots):
        pitch = pitch_of(r, k)
        low = min(low, pitch - _span(r.h, dist, mpp * k) * 0.5)
        out.append((name, r, left + across * 0.5, pitch,
                    r.w * mpp * k * 0.5, r.h * mpp * k * 0.5, dist))
    return out, low


def hud_quads(game, look):
    """The frame's panes, as (texture, corners, st) with corners in the
    tank's space.  In a menu the sheet hangs alone, a little low, fixed in
    the tank.  In play the instrument canvas is cut into panes in two rows
    that ride the gaze -- `look`, the head's (yaw, pitch) off the seat in
    radians -- up, down and round, so the middle stays clear and the
    gauges are a flick of the eyes away; and the reticle hangs out on the
    gun line, never turning, so it sits over what it marks."""
    if game.state not in ("play", "dead"):
        w, h = game.screen.get_size()
        half_w = MENU_DIST * math.tan(math.radians(MENU_SPAN * 0.5))
        return [("sheet", _pane(0.0, MENU_PITCH, MENU_DIST,
                                half_w, half_w * h / w), (0.0, 0.0, 1.0, 1.0))]
    canvas = game.vr_canvas
    cw, ch = canvas.get_size()
    got = dict(game.vr_pieces)

    def st(r):
        return (r.x / cw, r.y / ch, r.right / cw, r.bottom / ch)

    quads = []
    if "reticle" in got:
        r = got["reticle"]
        mpp = GLASS_DIST * math.tan(math.radians(1.0)) / RETICLE_DENSITY
        quads.append(("hud", _pane(0.0, 0.0, GLASS_DIST, r.w * mpp * 0.5,
                                   r.h * mpp * 0.5), st(r)))
    # The dash must end inside the eye's easy reach: lay it out, and if it
    # drops past DASH_FLOOR lay it out again a size smaller.
    k = 1.0
    while True:
        hung, low = [], 0.0
        for names, center, dist, edge in HUD_ROWS:
            row = [(n, got[n]) for n in names if n in got]
            if row:
                panes, bottom = _hang_row(row, center, dist, edge, k)
                hung += panes
                low = min(low, bottom)
        if low >= DASH_FLOOR or k < 0.3:
            break
        k *= 0.95
    yaw, pitch = look or (0.0, 0.0)
    cyw, syw = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)

    def aim(v):                          # nod the frame, then turn it
        x, y, z = v
        y, z = y * cp + z * sp, -y * sp + z * cp
        return (x * cyw + z * syw, y, -x * syw + z * cyw)

    for name, r, pyaw, ppitch, hw, hh, dist in hung:
        corners = [aim(c) for c in _pane(pyaw, ppitch, dist, hw, hh)]
        quads.append(("hud", corners, st(r)))
    return quads


# ------------------------------------------------------- the compositor --

_VERTEX = """
#version 120
uniform mat4 mvp;
varying vec2 uv;
void main() {
    uv = gl_MultiTexCoord0.xy;
    gl_Position = mvp * gl_Vertex;
}
"""
_FRAGMENT = """
#version 120
uniform sampler2D tex;
uniform float decode;    // 1: store as painted; 2.2: linearise first
uniform float opaque;    // 1: ignore the texture's alpha
varying vec2 uv;
void main() {
    vec4 c = texture2D(tex, uv);
    gl_FragColor = vec4(pow(c.rgb, vec3(decode)), mix(c.a, 1.0, opaque));
}
"""
_IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
_FULL = [(-1.0, 1.0, 0.0), (1.0, 1.0, 0.0), (1.0, -1.0, 0.0), (-1.0, -1.0, 0.0)]


class Compositor:
    """pygame surfaces up to textures, and textures down onto an eye (or
    the window) as quads.  A surface's top row goes up as the texture's
    first row, so t runs down the picture, as y does on the surface."""

    def __init__(self):
        self.program = shaders.compileProgram(
            shaders.compileShader(_VERTEX, GL.GL_VERTEX_SHADER),
            shaders.compileShader(_FRAGMENT, GL.GL_FRAGMENT_SHADER))
        self.loc = {n: GL.glGetUniformLocation(self.program, n)
                    for n in ("mvp", "tex", "decode", "opaque")}
        self.textures = {}               # name -> (id, w, h)

    def upload(self, name, surface):
        w, h = surface.get_size()
        tex, tw, th = self.textures.get(name, (None, 0, 0))
        if tex is None:
            tex = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            for p in (GL.GL_TEXTURE_MIN_FILTER, GL.GL_TEXTURE_MAG_FILTER):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, p, GL.GL_LINEAR)
            for p in (GL.GL_TEXTURE_WRAP_S, GL.GL_TEXTURE_WRAP_T):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, p, GL.GL_CLAMP_TO_EDGE)
        GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
        if (tw, th) != (w, h):
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, w, h, 0,
                            GL.GL_BGRA, GL.GL_UNSIGNED_BYTE, None)
            self.textures[name] = (tex, w, h)
        # Straight from the surface's own pixels: no copy on the way.
        fmt = GL.GL_BGRA if surface.get_masks()[0] == 0xFF0000 else GL.GL_RGBA
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 4)
        GL.glPixelStorei(GL.GL_UNPACK_ROW_LENGTH, surface.get_pitch() // 4)
        with memoryview(surface.get_view("1")) as pixels:
            GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, w, h, fmt,
                               GL.GL_UNSIGNED_BYTE, pixels)
        GL.glPixelStorei(GL.GL_UNPACK_ROW_LENGTH, 0)

    def begin(self, decode):
        GL.glUseProgram(self.program)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDisable(GL.GL_FRAMEBUFFER_SRGB)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glUniform1i(self.loc["tex"], 0)
        GL.glUniform1f(self.loc["decode"], decode)
        # The picture's alpha blends the panes in and never reaches the
        # swapchain's own alpha, which stays opaque.
        GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA,
                               GL.GL_ZERO, GL.GL_ONE)

    def quad(self, name, corners, st=(0.0, 0.0, 1.0, 1.0), mvp=_IDENTITY,
             opaque=False):
        entry = self.textures.get(name)
        if entry is None:
            return
        GL.glBindTexture(GL.GL_TEXTURE_2D, entry[0])
        GL.glUniformMatrix4fv(self.loc["mvp"], 1, GL.GL_TRUE, mvp)
        GL.glUniform1f(self.loc["opaque"], 1.0 if opaque else 0.0)
        if opaque:
            GL.glDisable(GL.GL_BLEND)
        else:
            GL.glEnable(GL.GL_BLEND)
        s0, t0, s1, t1 = st
        GL.glBegin(GL.GL_QUADS)
        for (s, t), c in zip(((s0, t0), (s1, t0), (s1, t1), (s0, t1)), corners):
            GL.glTexCoord2f(s, t)
            GL.glVertex3f(*c)
        GL.glEnd()

    def end(self):
        GL.glDisable(GL.GL_BLEND)
        GL.glUseProgram(0)

    def window(self, size, aspect, clear=True):
        """Aim at the window, letterboxed to `aspect`, cleared to black."""
        W, H = size
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        GL.glViewport(0, 0, W, H)
        if clear:
            GL.glClearColor(0.0, 0.0, 0.0, 1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        if W / max(1, H) > aspect:
            w = int(H * aspect)
            GL.glViewport((W - w) // 2, 0, w, H)
        else:
            h = int(W / aspect)
            GL.glViewport(0, (H - h) // 2, W, h)


# -------------------------------------------------------------- the loop --

def run(game, clock, handle_events, frames=None):
    """The game with a headset: OpenXR paces the loop, each eye draws the
    arena, the window mirrors the left eye.  With no session yet -- or a
    session lost -- the desk plays on in the window at the desk's pace; and
    if no runtime ever answers, the rest of the run is on the desk."""
    sheet = game.screen                  # the game's own surface: the desk's
    aspect = sheet.get_width() / sheet.get_height()   # frame, or the menus
    comp = Compositor()
    hands = Hands()
    eye_surfaces = {}
    driver = VRDriver()
    driver.open()                        # non-blocking: a thread waits out
                                         # the runtime, the desk plays on

    def desk(dt):
        """One frame on the desk: the game draws its own picture on the
        sheet, and the window shows it."""
        game.headset, game.rumble = False, None
        game.screen = sheet
        game.view.set_surface(sheet)
        running = handle_events()
        game.update(dt)
        game.draw()
        comp.upload("sheet", sheet)
        comp.window(pygame.display.get_window_size(), aspect)
        comp.begin(1.0)
        comp.quad("sheet", _FULL, opaque=True)
        comp.end()
        pygame.display.flip()
        return running and not game.quit

    def headset(frame_state, dt):
        """One frame in the headset."""
        hands.apply(driver.hands, game)
        if game.vr_recenter:
            game.vr_recenter = False
            driver.recenter()
        game.headset, game.rumble = True, driver.buzz
        game.screen = sheet
        game.view.set_surface(sheet)
        running = handle_events()
        game.update(dt)
        sheet.fill((0, 0, 0, 0))
        game.draw()                      # menus on the sheet, gauges on
        menu = game.state not in ("play", "dead")   # the canvas
        if menu:
            comp.upload("sheet", sheet)
        else:
            comp.upload("hud", game.vr_canvas)
        quads = hud_quads(game, driver.head)

        view, first = game.view, None
        for eye in driver.eyes(frame_state, game.seat()):
            size = driver.eye_sizes[eye["index"]]
            surf = eye_surfaces.get(eye["index"])
            if surf is None or surf.get_size() != size:
                surf = eye_surfaces[eye["index"]] = \
                    pygame.Surface(size, 0, 32)
            game.screen = surf
            view.set_surface(surf)
            view.set_frustum(*eye["tans"])
            view.set_eye(eye["eye"], *eye["basis"])
            game.draw_world(aim=False)
            name = "eye%d" % eye["index"]
            comp.upload(name, surf)
            comp.begin(driver.decode)
            comp.quad(name, _FULL, opaque=True)
            for tex, corners, st in quads:
                comp.quad(tex, corners, st, eye["mvp"])
            comp.end()
            if first is None:
                first = name, size
        game.screen = sheet
        view.set_surface(sheet)

        # The desk sees the left eye, cropped to the window, and the menu
        # sheet over it.  The gauges are panes for a head, not a picture.
        if first is not None:
            name, (ew, eh) = first
            W, H = pygame.display.get_window_size()
            comp.window((W, H), W / max(1, H))
            comp.begin(1.0)
            k = (ew / eh) / (W / max(1, H))
            st = ((0.0, 0.5 - 0.5 * k, 1.0, 0.5 + 0.5 * k) if k < 1.0 else
                  (0.5 - 0.5 / k, 0.0, 0.5 + 0.5 / k, 1.0))
            comp.quad(name, _FULL, st, opaque=True)
            if menu:
                comp.window((W, H), aspect, clear=False)
                comp.quad("sheet", _FULL)
            comp.end()
            pygame.display.flip()
        return running and not game.quit

    running = True
    try:
        for frame_state in driver.frames():
            if frame_state is None:
                hands.release(game)
                dt = min(0.05, clock.tick(spectre.FPS) / 1000.0)
                running = desk(dt)
            else:
                dt = min(0.05, clock.tick(0) / 1000.0)
                running = headset(frame_state, dt)
            if frames is not None:
                frames -= 1
                running = running and frames > 0
            if not running:
                break
    finally:
        driver.close()
        hands.release(game)
    # No runtime ever answered: the rest of the run is on the desk.
    while running:
        running = desk(min(0.05, clock.tick(spectre.FPS) / 1000.0))
        if frames is not None:
            frames -= 1
            running = running and frames > 0


def check():
    """--vr-check: is the headset side all here?  Reports the OpenXR loader
    and whether a runtime answers, without opening a session (a runtime
    answering here never waits on a headset)."""
    from xr.library import openxr_loader_library
    print("spectre: OpenXR loader: %s" % openxr_loader_library._name)
    try:
        try:
            props = xr.enumerate_instance_extension_properties()
        except xr.exception.RuntimeUnavailableError:
            manifest = find_manifest()   # as --vr would
            if manifest is None:
                raise
            os.environ["XR_RUNTIME_JSON"] = manifest
            print("spectre: no active OpenXR runtime registered; trying %s"
                  % manifest)
            props = xr.enumerate_instance_extension_properties()
    except Exception as exc:
        print("spectre: no OpenXR runtime answered (%s) -- %s, and make it"
              " the active OpenXR runtime." % (exc, WAIT_HINT))
        return 1
    names = {p.extension_name.decode() if isinstance(p.extension_name, bytes)
             else p.extension_name for p in props}
    gl = xr.KHR_OPENGL_ENABLE_EXTENSION_NAME in names
    print("spectre: an OpenXR runtime answered, offering %d extensions;"
          " OpenGL %s." % (len(names), "supported" if gl else
                           "NOT supported -- the game cannot draw into it"))
    return 0 if gl else 1
