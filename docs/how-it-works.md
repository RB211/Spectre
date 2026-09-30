# How Spectre works

Spectre is two Python modules. [`spectre.py`](../spectre.py) is the whole game:
renderer, world, enemies, audio, networking and menus. It depends only on
pygame. [`spectre_vr.py`](../spectre_vr.py) is loaded only by `--vr` and adds
OpenXR on top.

- [The renderer](#the-renderer)
- [Hidden lines](#hidden-lines)
- [Performance](#performance)
- [Sound](#sound)
- [LAN play](#lan-play)
- [VR](#vr)

## The renderer

`View` is the whole renderer. Each frame it takes world geometry through
these steps:

1. world → camera space;
2. clip to the near plane;
3. perspective divide;
4. clip to the screen;
5. draw a fogged line (Liang–Barsky clipping) or a filled face
   (Sutherland–Hodgman clipping).

Every object is first tested against the view with a bounding sphere, and
skipped if it's out of sight.

**Painter's algorithm.** The floor grid is drawn first. Then every solid,
fence run, tracer and explosion is sorted back to front and painted in that
order, so near things paint over far ones.

**Wires before solids.** A mast or aerial attached to an object is drawn
before the object itself. That way the half of the object standing in front of
the wire paints over it.

## Hidden lines

The original Spectre's look comes from **hidden-line removal**: solids are
filled with the background colour and only their front-facing edges are
drawn, so a building hides whatever stands behind it.

- **Convex lumps.** Models are built from convex lumps of faces (`Part`). Each
  face is wound outwards when the model is built, so a single dot product per
  face says whether it faces the camera.
- **Edges.** Each edge remembers the faces it borders. It's drawn once if
  either of those faces the camera, and not at all if neither does.
- **Touching, not overlapping.** Lumps are built to touch rather than overlap:
  two solids that pass through each other can't be sorted back to front.
- **Lump order.** When a model has more than one lump, they're ordered by the
  planes that separate them, not by distance from the camera. The lump on the
  camera's side of a separating plane is always painted last.
- **Sealed seams.** Backface culling only reasons about one convex lump at a
  time. Where two lumps meet (a cap on a roof, a turret on a deck, a barrel in
  its breech), the lower lump's top face can fill nothing and let the upper
  lump's underside show through. `Part.seal_against` finds every face whose
  corners all sit inside a neighbouring lump and never draws it. This is
  worked out when a model is built, so new models get it for free.

## Performance

On a busy level (34 buildings, 9 tanks) a frame takes about 2.1 ms at
1100×740 and 2.6 ms at 1600×1000. The worst case, with the camera buried
inside a building, is 11 ms.

Resolution barely matters: the cost is in the edges, not the pixels. That's
what makes VR possible without a GPU renderer (see [VR](#vr)).

## Sound

`Audio` builds every sound at startup, in about 150 ms, as raw PCM. There are
no sample files and no numpy.

- **Synthesis.** Each sound is layered oscillators over stepped envelopes,
  plus white noise through a one-pole low-pass filter. That filter is most of
  what separates a rumble from a hiss. The common sounds come in two pitch
  variants, so rapid fire doesn't sound mechanical.
- **Positional sound.** The mixer runs in stereo. `Game.play_at` places a
  sound in the world: quieter with distance, and panned to the side it came
  from. You can hear which flank is shooting at you.
- **Engine drone.** Six seamless loops, each built from exact harmonics of its
  own base frequency and cut to a whole number of cycles, so the end of the
  buffer runs into its start with no click. The loop in use follows the
  throttle, with hysteresis so it doesn't flutter at a boundary.

## LAN play

LAN play is up to four tanks over plain TCP on port 35700. Messages are one
JSON object per line. There are no accounts and no discovery service.

- **Levels are seeds.** The arena is never sent over the network: a level is a
  seed, and every machine grows the same buildings from it.
- **The host owns the world.** The host runs the enemies, shells, prizes and
  clock, and streams the world to everyone at 20 Hz.
- **Your tank is local.** Each player drives their own tank on their own
  machine and reports its position at 30 Hz, so the controls never feel the
  network. Other players' tanks are extrapolated between reports.

## VR

Mostly [`spectre_vr.py`](../spectre_vr.py).

### Drawing each eye in software

VR doesn't need a GPU renderer. The same software renderer draws the arena
once per eye, and each eye's picture goes to the OpenXR runtime as a texture.
An eye at Quest 3 resolution (about 1830×1920) takes under 2 ms to draw.
Uploading it takes under 1 ms, because OpenGL reads the surface's own pixels
through a memoryview with no copy. A full stereo frame, game update included,
takes about 6 ms, well inside the 11 ms a 90 Hz headset allows.

`View` gained two methods for this:

- **`set_eye`** aims the camera along any orientation at all, because a head
  rolls and nods.
- **`set_frustum`** takes an eye's lopsided field of view. Headset eyes see
  further to the outside than to the nose, so the centre of each eye's image
  isn't straight ahead.

The desktop picture is unchanged by either.

### The head in the tank

Your head moves relative to `Game.seat()`, the desktop camera without its
sway, recoil and jolt. A horizon that moves on its own is what makes people
feel sick.

The seat is measured from your head on the first tracked frame, not taken
from wherever the runtime put its origin. It's measured again on `F12`, and
when the Quest's own recenter moves the origin. pyopenxr's event loop drops
that recenter event, so the driver listens for it separately.

### Gauges and menus

A flat overlay can't simply be pasted over each eye's picture: because each
eye's field of view is lopsided, it comes out double. So everything that isn't
the arena hangs as a panel in the tank's space and goes through each eye's own
projection:

- **Menus** go on one sheet about a metre ahead and a little low, and the
  flight HUD is kept off it.
- **Gauges** hang in two rows that follow your gaze and stay within 35° either
  side of it: score, message and clock above; shields, radar and flags below.
- **The reticle** hangs 20 m out on the gun line and never turns, so it sits
  over whatever it's pointing at.

### Staying up

- **No freezing while waiting.** WiVRn doesn't finish the OpenXR handshake
  until a headset connects, so the handshake runs on a background thread and
  the game plays in the window meanwhile.
- **Surviving a lost session.** If the session is lost (WiVRn restarted, the
  link dropped), everything is released and the game goes back to waiting,
  exactly as at startup.
- **Forcing X11.** `--vr` asks SDL and PyOpenGL for X11/GLX under XWayland.
  pyopenxr only speaks the Xlib binding, and WiVRn refuses a Wayland/EGL
  context.
- **Vsync off.** `--vr` also turns off vsync on the window, so the desktop
  monitor's refresh rate can't hold the headset back.
- **Colour.** Gamma is decoded or passed through to match the swapchain
  format the runtime offers.

### Controllers

The Touch controllers act as a keyboard. Sticks, triggers and grips hold keys
down; buttons post key presses into pygame's event queue. That way every menu,
the in-game menu and the text boxes work in the hands unchanged. Firing and
taking hits make the controllers vibrate.
