"""Draw the launcher icons with the game's own renderer: the player's tank
in hidden-line wireframe on the arena's floor, and the same tile wearing a
lit VR badge for the headset launcher.

    python tools/make_icon.py OUT_DIR

writes OUT_DIR/spectre.png and OUT_DIR/spectre-vr.png, 256 px square.
install.sh runs it; nothing else needs to.
"""

import math
import os
import sys

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pygame                                                # noqa: E402
import spectre                                               # noqa: E402

SIZE = 256
SS = 3                                   # drawn big, scaled down once


def tile(vr):
    n = SIZE * SS
    surf = pygame.Surface((n, n), pygame.SRCALPHA, 32)
    pygame.draw.rect(surf, spectre.COL_BG + (255,), (0, 0, n, n),
                     border_radius=n // 6)
    art = pygame.Surface((n, n), 0, 32)
    art.fill(spectre.COL_BG)
    view = spectre.View(art)
    yaw = math.radians(215)
    dist = 5.6
    view.set_camera(math.sin(yaw) * dist, 3.1, math.cos(yaw) * dist,
                    yaw + math.pi, -0.36)
    view.segments(spectre.floor_grid(0.0, 0.0), spectre.COL_GRID, width=SS)
    view.shape(spectre.PLAYER_SHAPE, 0.0, 0.0, math.radians(20),
               spectre.COL_HUD, y=0.0, width=2 * SS, glow=True)
    mask = pygame.Surface((n, n), pygame.SRCALPHA, 32)
    pygame.draw.rect(mask, (255, 255, 255, 255), (0, 0, n, n),
                     border_radius=n // 6)
    clipped = pygame.Surface((n, n), pygame.SRCALPHA, 32)
    clipped.blit(art, (0, 0))
    clipped.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MIN)
    surf.blit(clipped, (0, 0))
    pygame.draw.rect(surf, spectre.COL_HUD, (0, 0, n, n), SS * 3,
                     border_radius=n // 6)
    if vr:
        badge = pygame.Rect(0, 0, n * 0.46, n * 0.24)
        badge.bottomright = (n - n * 0.07, n - n * 0.07)
        pygame.draw.rect(surf, spectre.COL_FLAG, badge,
                         border_radius=badge.h // 2)
        big = pygame.font.SysFont("dejavusansmono,liberationmono,monospace",
                                  int(badge.h * 0.78), bold=True)
        text = big.render("VR", True, spectre.COL_BG)
        surf.blit(text, text.get_rect(center=badge.center))
    return pygame.transform.smoothscale(surf, (SIZE, SIZE))


def main(out):
    pygame.init()
    pygame.display.set_mode((1, 1))
    os.makedirs(out, exist_ok=True)
    pygame.image.save(tile(False), os.path.join(out, "spectre.png"))
    pygame.image.save(tile(True), os.path.join(out, "spectre-vr.png"))


if __name__ == "__main__":
    main(sys.argv[1])
