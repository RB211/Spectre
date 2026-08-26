# Spectre

A clone of *Spectre* (Velocity, 1991) — the wireframe tank arena — in pygame.
It draws in line art, but with real **hidden-line removal**: solids are filled
with the background and only their facing edges are stroked, so a building
hides whatever stands behind it. Self-contained — one file, one dependency.

## Run it

pygame is installed for the **system** python here, not mise's:

    /usr/bin/python spectre.py

    --fullscreen   start full screen
    --mute         no sound

## Play

| key | |
|---|---|
| `W` `S` / `up` `down` | drive, reverse |
| `A` `D` / `left` `right` | turn — taps are fine, holding winds up |
| `space` | fire |
| `shift` | turbo (drains, refills when you let off) |
| `v` | first-person / chase camera |
| `tab` | radar range: 55m / 90m / 150m |
| `p` `f` | pause, fullscreen |
| `esc` | in-game menu — back to the main menu, or quit (on the title it quits) |

Collect every flag in the arena before the clock runs out. Enemy tanks —
fast red **hunters** and slow, hard-hitting purple **sentries** — hunt you
by sight and lead their shots, so keep buildings between you and them.
Octahedral pods are ammunition, cubes are shields; kills drop them too.
Clearing a level pays a time bonus and adds another tank to the arena.

**Steering winds up.** A tap turns you about half a degree, for lining up a
shot; hold the key and the rate climbs over about 0.85 s to a full 132°/s
swing. Fine aim and fast turns off the same key, no modifier.

## LAN play

Co-op over the local network, up to four tanks, straight TCP on port 35700 —
no accounts, no discovery service, just an address.

- One machine picks **HOST A LAN GAME**; the lobby shows the address to read
  out. The others pick **JOIN A LAN GAME** and type it in (`host:port` works
  if you have moved the port). Everyone waits in the lobby until the host
  presses `enter`.
- The arena is never transmitted — a level is a **seed**, and every machine
  grows the same buildings from it. The host owns the world (enemies, shells,
  prizes, the clock) and streams it at 20 Hz; each player drives their own
  tank locally and reports its pose at 30 Hz, so the controls never feel the
  wire. Remote tanks are dead-reckoned between reports.
- Flags and the clock are shared; scores are per player, and the enemy tanks
  come for whoever is nearest. No friendly fire. Destroyed tanks respawn
  after a moment — the mission only fails when the clock runs out, which
  sends everyone back to the lobby for another go.
- Every other tank flies its player's name over the turret, in that player's
  color, fading with distance like everything else. Set yours under
  **SETTINGS** on the title screen (it defaults to your login); it is kept in
  `~/.config/spectre/settings.json`.

## Hyprland / Omarchy

The window announces itself as class `spectre` (via SDL's app-id hints, set
before pygame imports), and never argues with the compositor about its own
size — it reads the size back each frame and re-aims the camera. A rule in
`~/.config/hypr/hyprland.lua` floats it instead of tiling it:

    o.window("spectre", { float = true, center = true, size = { 1280, 800 } })

Delete that line and `hyprctl reload` if you would rather have it tiled; the
game copes either way, including in a tall, narrow tile.

## How it works

- `View` — the whole renderer: world → camera → near-plane clip → perspective
  divide → screen clip → fogged line (Liang-Barsky) or filled face
  (Sutherland-Hodgman). Bounding-sphere culling per object.
- **Hidden lines.** Models are convex lumps of faces (`Part`), wound outwards
  at construction so one dot product per face says what you cannot see. Each
  edge remembers the faces it borders and is stroked once if either faces you,
  never if neither does. Lumps are built to touch, not overlap — two solids
  that interpenetrate cannot be ordered back to front.
- **Sealed seams.** Backface culling reasons about one convex lump at a time,
  so where two of them meet — a cap on a roof, a turret on a deck, a barrel in
  a breech — the lower one's top face is turned away and fills nothing, leaving
  the upper one's underside showing through. `Part.seal_against` finds any face
  whose every corner sits inside a neighbouring lump and never draws it. It is
  computed at construction, not hand-listed, so new models get it for free.
- **Wires go down before solids**, so a mast or an aerial attached to an object
  is painted over by the half of the object standing in front of it.
- **Painter's algorithm.** The floor grid goes down first, then every solid,
  fence run, tracer and explosion sorted back to front each frame.
- Busy level (34 buildings, 9 tanks): ~2.1 ms/frame at 1100x740, ~2.6 ms at
  1600x1000; worst case, camera buried inside a building, 11 ms.
- `Audio` synthesises every sound at startup (~150 ms) and renders it to raw
  PCM — no sample files, no numpy. Layered oscillators over stepped
  envelopes, plus white noise through a one-pole low pass, which is most of
  what separates a rumble from a hiss. Two pitch variants of the common
  sounds so repeated fire does not machine-gun.
- **Positional sound.** The mixer runs in stereo and `Game.play_at` places a
  sound in the world — attenuated by distance, panned to the side it came
  from. You can hear which flank is shooting at you.
- **Engine drone.** Six seamless loops, each built from exact harmonics of its
  own base frequency and cut to a whole number of cycles, so the buffer's end
  runs into its start with no seam. The band follows the throttle, with
  hysteresis so it does not flutter at a boundary.
