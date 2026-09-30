# Changelog

## v1.0.1 (2026-09-29)

- **A Windows installer.** Each release now carries
  `Spectre-<version>-windows-setup.exe`, built by GitHub Actions: a per-user
  install with **Spectre** and **Spectre VR** in the Start menu, and an
  uninstaller. There's also a portable zip.
- **VR on Windows is included but hasn't been tested yet.** It should work
  through Quest Link, Air Link or SteamVR.
- On Windows the game writes its log to `%LOCALAPPDATA%\Spectre`, since it has
  no console, and your default player name comes from your Windows login.

## v1.0.0 (2026-09-29)

The first public release.

### The game

- A wireframe tank arena with hidden-line removal: filled faces, painting
  from back to front, and seams sealed where solids meet.
- Collect every flag before the clock runs out. Each cleared level pays a time
  bonus and adds another enemy tank.
- Fast red **hunters** and slow, heavy purple **sentries**, which track you by
  sight and lead their shots.
- Ammunition and shield pods, which destroyed tanks drop too.
- Steering that winds up: tap for fine aim, hold to swing round.
- Cockpit and chase cameras, a radar with three ranges, and turbo.
- Synthesised sound, positioned in stereo, with an engine drone that follows
  the throttle.

### LAN co-op

- Up to four players on one network: host, join by address, and wait in a
  lobby.
- Levels grown from a shared seed; the host runs the world, and each player
  drives their own tank locally.
- Player names and colours, set under **SETTINGS**.

### VR

- `--vr` plays in any OpenXR headset whose runtime supports OpenGL. Tested with
  a Quest 3 over WiVRn.
- Your head is free inside the tank, with gauges that follow your gaze, a
  reticle on the gun line and menus on a single sheet.
- Touch controllers drive, fire and work every menu, and vibrate when you fire
  or take hits.
- Plays in the window until the headset connects, and carries on there if the
  link drops.
- `--vr-check` checks the headset side without starting a session.

### Install

- `install.sh` installs the game with its own Python environment and adds
  **Spectre** and **Spectre VR** to the app launcher. Spectre VR is also listed
  in the WiVRn app on the Quest.
- **QUIT** on the title menu.
