"""Small code-native pixel sprite atlas used by world entities and item drops.

Every sprite is drawn on a low-resolution transparent canvas and enlarged with
nearest-neighbour scaling.  This keeps the silhouettes crisp and makes red hit
flashes safe: only opaque sprite pixels are tinted.
"""
from functools import lru_cache

import pygame


OUTLINE = (43, 31, 29)
SKIN = (218, 166, 119)


def _canvas(size=(24, 28)):
    return pygame.Surface(size, pygame.SRCALPHA)


def _rect(surface, color, rect):
    pygame.draw.rect(surface, color, rect)


def _tint_red(surface):
    tinted = surface.copy()
    red = pygame.Surface(tinted.get_size(), pygame.SRCALPHA)
    red.fill((255, 62, 52, 0))
    red.set_alpha(150)
    mask = pygame.mask.from_surface(tinted)
    overlay = mask.to_surface(setcolor=(255, 58, 48, 185), unsetcolor=(0, 0, 0, 0))
    tinted.blit(overlay, (0, 0))
    return tinted


def _finish(surface, flash=False, scale=2):
    if flash:
        surface = _tint_red(surface)
    return pygame.transform.scale(surface, (surface.get_width() * scale,
                                            surface.get_height() * scale))


def _humanoid(kind, facing, frame):
    s = _canvas()
    step = frame % 2
    palettes = {
        "pirate": ((118, 38, 38), (181, 57, 44), (31, 28, 31)),
        "bandit": ((91, 62, 43), (135, 86, 49), (66, 43, 32)),
        "thief": ((52, 55, 70), (82, 78, 93), (30, 30, 40)),
    }
    body, light, hood = palettes.get(kind, palettes["bandit"])
    # Grounded boots and separated legs read at the player's scale.
    _rect(s, OUTLINE, (7, 21, 4, 5)); _rect(s, OUTLINE, (14, 21, 4, 5))
    _rect(s, (49, 38, 34), (6 - step, 25, 6, 2)); _rect(s, (49, 38, 34), (14 + step, 25, 6, 2))
    _rect(s, OUTLINE, (5, 10, 15, 13)); _rect(s, body, (6, 11, 13, 11))
    _rect(s, light, (7, 12, 3, 8)); _rect(s, OUTLINE, (11, 11, 2, 11))
    _rect(s, OUTLINE, (3, 12 + step, 4, 9)); _rect(s, body, (4, 13 + step, 2, 7))
    _rect(s, OUTLINE, (18, 12 - step, 4, 9)); _rect(s, body, (19, 13 - step, 2, 7))
    _rect(s, OUTLINE, (7, 3, 11, 9)); _rect(s, SKIN, (8, 4, 9, 7))
    if facing == "up":
        _rect(s, hood, (8, 4, 9, 7))
    else:
        eye_x = 9 if facing != "right" else 15
        if facing == "down":
            _rect(s, OUTLINE, (9, 7, 2, 1)); _rect(s, OUTLINE, (14, 7, 2, 1))
        else:
            _rect(s, OUTLINE, (eye_x, 7, 2, 1))
    if kind == "pirate":
        pygame.draw.polygon(s, OUTLINE, [(5, 5), (20, 5), (17, 1), (12, 3), (8, 1)])
        _rect(s, (188, 154, 71), (7, 5, 11, 1)); _rect(s, (226, 222, 199), (11, 12, 3, 8))
    elif kind == "bandit":
        pygame.draw.polygon(s, hood, [(6, 5), (9, 1), (17, 2), (20, 6), (17, 11), (7, 11)])
        _rect(s, (152, 115, 63), (7, 15, 11, 2))
    else:
        pygame.draw.polygon(s, hood, [(6, 5), (10, 1), (17, 3), (20, 7), (17, 11), (7, 11)])
        _rect(s, (105, 91, 112), (8, 8, 9, 2))
    return s


def _quadruped(kind, facing, frame):
    s = _canvas((30, 22))
    flip = facing == "left"
    if kind == "wolf":
        fur, light, dark = (91, 96, 103), (139, 139, 132), (54, 57, 62)
    elif kind == "boar":
        fur, light, dark = (111, 70, 48), (151, 98, 59), (60, 43, 34)
    elif kind == "sheep":
        fur, light, dark = (218, 210, 184), (245, 237, 211), (73, 58, 48)
    else:  # cow
        fur, light, dark = (225, 211, 181), (248, 232, 198), (78, 52, 39)
    # Tail, body, legs and head use chunky 1px/2px clusters.
    pygame.draw.polygon(s, OUTLINE, [(4, 8), (0, 5), (1, 11), (5, 13)])
    pygame.draw.polygon(s, fur, [(5, 7), (2, 6), (3, 10), (6, 11)])
    _rect(s, OUTLINE, (5, 5, 18, 12)); _rect(s, fur, (6, 6, 16, 10))
    if kind == "sheep":
        for x, y in ((7, 6), (11, 5), (15, 6), (19, 5), (8, 10), (13, 9), (18, 11)):
            pygame.draw.circle(s, light, (x, y), 3)
    elif kind == "cow":
        _rect(s, dark, (8, 6, 5, 5)); _rect(s, dark, (17, 10, 5, 5)); _rect(s, light, (13, 6, 4, 4))
    else:
        _rect(s, light, (7, 6, 10, 3)); _rect(s, dark, (7, 14, 14, 2))
    leg_shift = frame % 2
    for x, shift in ((7, leg_shift), (12, 1 - leg_shift), (18, leg_shift), (22, 1 - leg_shift)):
        _rect(s, OUTLINE, (x, 15, 3, 6)); _rect(s, dark, (x + shift, 17, 2, 4))
    _rect(s, OUTLINE, (21, 6, 8, 10)); _rect(s, dark if kind == "sheep" else fur, (22, 7, 6, 8))
    if kind == "wolf":
        pygame.draw.polygon(s, OUTLINE, [(21, 7), (22, 1), (25, 6), (27, 1), (29, 8)])
        pygame.draw.polygon(s, dark, [(22, 6), (23, 3), (25, 7), (27, 3), (28, 8)])
        _rect(s, light, (24, 9, 4, 2)); _rect(s, (226, 174, 55), (26, 8, 1, 1))
    elif kind == "boar":
        pygame.draw.polygon(s, dark, [(21, 7), (23, 2), (25, 7)])
        _rect(s, light, (26, 12, 4, 2)); _rect(s, (232, 217, 171), (28, 13, 2, 2))
    elif kind == "sheep":
        _rect(s, OUTLINE, (23, 4, 2, 4)); _rect(s, OUTLINE, (27, 5, 2, 4)); _rect(s, (214, 173, 91), (27, 10, 1, 1))
    else:
        _rect(s, (238, 214, 151), (22, 4, 2, 4)); _rect(s, (238, 214, 151), (27, 4, 2, 4))
        _rect(s, light, (25, 12, 4, 3)); _rect(s, OUTLINE, (27, 9, 1, 1))
    return pygame.transform.flip(s, True, False) if flip else s


@lru_cache(maxsize=128)
def entity_sprite(kind, facing="right", frame=0, flash=False):
    if kind in ("wolf", "boar", "sheep", "cow"):
        return _finish(_quadruped(kind, facing, frame), flash)
    return _finish(_humanoid(kind, facing, frame), flash)


@lru_cache(maxsize=32)
def villager_sprite(role="villager", facing="down", frame=0):
    s = _canvas()
    colors = {
        "farmer": ((95, 124, 72), (139, 151, 82)),
        "merchant": ((116, 64, 105), (170, 100, 113)),
        "villager": ((82, 105, 126), (118, 145, 146)),
    }
    coat, light = colors.get(role, colors["villager"])
    step = frame % 2
    _rect(s, OUTLINE, (7, 21, 4, 6)); _rect(s, OUTLINE, (14, 21, 4, 6))
    _rect(s, (62, 43, 32), (6 - step, 25, 6, 2)); _rect(s, (62, 43, 32), (14 + step, 25, 6, 2))
    _rect(s, OUTLINE, (5, 10, 15, 13)); _rect(s, coat, (6, 11, 13, 11)); _rect(s, light, (7, 12, 3, 8))
    _rect(s, OUTLINE, (7, 3, 11, 9)); _rect(s, SKIN, (8, 4, 9, 7)); _rect(s, (84, 52, 35), (8, 3, 9, 3))
    if facing == "down":
        _rect(s, OUTLINE, (9, 7, 1, 1)); _rect(s, OUTLINE, (15, 7, 1, 1))
    elif facing == "left": _rect(s, OUTLINE, (9, 7, 1, 1))
    elif facing == "right": _rect(s, OUTLINE, (15, 7, 1, 1))
    else: _rect(s, (84, 52, 35), (8, 5, 9, 6))
    if role == "farmer":
        _rect(s, (163, 111, 52), (5, 2, 15, 3)); _rect(s, (196, 145, 66), (8, 0, 9, 3))
    elif role == "merchant":
        _rect(s, (220, 173, 69), (5, 12, 2, 9)); _rect(s, (220, 173, 69), (18, 12, 2, 9))
        _rect(s, (136, 77, 44), (18, 18, 5, 5))
    else:
        _rect(s, (173, 68, 47), (6, 11, 13, 2))
    return _finish(s)


@lru_cache(maxsize=32)
def item_sprite(item, scale=2):
    s = _canvas((12, 12))
    if item == "Çiğ Et":
        pygame.draw.polygon(s, OUTLINE, [(1, 5), (4, 2), (10, 3), (11, 8), (7, 11), (2, 9)])
        pygame.draw.polygon(s, (190, 64, 61), [(2, 5), (5, 3), (9, 4), (10, 8), (7, 10), (3, 8)])
        _rect(s, (244, 174, 144), (5, 4, 3, 2))
    elif item == "Pişmiş Et":
        pygame.draw.polygon(s, OUTLINE, [(1, 5), (4, 2), (10, 3), (11, 8), (7, 11), (2, 9)])
        pygame.draw.polygon(s, (132, 67, 38), [(2, 5), (5, 3), (9, 4), (10, 8), (7, 10), (3, 8)])
        _rect(s, (226, 150, 66), (4, 4, 5, 2))
    elif item == "Ekmek":
        pygame.draw.ellipse(s, OUTLINE, (1, 2, 10, 9)); pygame.draw.ellipse(s, (211, 145, 62), (2, 3, 8, 7))
        for x in (4, 7): pygame.draw.line(s, (245, 202, 112), (x, 4), (x - 1, 7), 1)
    elif item == "Odun":
        _rect(s, OUTLINE, (1, 3, 10, 7)); _rect(s, (139, 77, 39), (2, 4, 8, 5)); _rect(s, (220, 145, 66), (8, 5, 2, 3))
    elif item == "Tas":
        pygame.draw.polygon(s, OUTLINE, [(1, 9), (3, 3), (8, 1), (11, 5), (10, 10)])
        pygame.draw.polygon(s, (153, 151, 146), [(2, 8), (4, 4), (8, 2), (10, 5), (9, 9)])
        _rect(s, (206, 198, 184), (5, 4, 3, 2))
    elif item == "Tahil":
        pygame.draw.line(s, (112, 72, 34), (6, 11), (6, 2), 2)
        for x, y in ((3, 3), (8, 4), (3, 6), (8, 7)):
            pygame.draw.ellipse(s, (230, 183, 62), (x, y, 4, 3))
    elif item == "Ok":
        pygame.draw.line(s, OUTLINE, (1, 10), (10, 1), 3)
        pygame.draw.line(s, (181, 121, 61), (1, 10), (10, 1), 1)
        pygame.draw.polygon(s, (184, 190, 191), [(8, 1), (11, 0), (10, 4)])
        pygame.draw.polygon(s, (183, 73, 52), [(1, 10), (1, 6), (4, 9), (5, 11)])
    elif item == "Kadim Anahtar":
        pygame.draw.circle(s, OUTLINE, (4, 4), 4)
        pygame.draw.circle(s, (241, 188, 58), (4, 4), 2)
        pygame.draw.line(s, OUTLINE, (6, 6), (11, 11), 3)
        pygame.draw.line(s, (241, 188, 58), (6, 6), (11, 11), 1)
        _rect(s, (241, 188, 58), (8, 8, 4, 2))
    elif item == "Muhafız Özü":
        pygame.draw.polygon(s, OUTLINE, [(6, 0), (10, 3), (11, 8), (6, 12), (1, 8), (2, 3)])
        pygame.draw.polygon(s, (190, 61, 37), [(6, 2), (9, 4), (9, 8), (6, 10), (3, 8), (3, 4)])
        pygame.draw.polygon(s, (255, 169, 54), [(6, 3), (8, 6), (6, 9), (4, 6)])
    else:
        pygame.draw.polygon(s, (239, 189, 66), [(6, 0), (8, 4), (12, 6), (8, 8), (6, 12), (4, 8), (0, 6), (4, 4)])
    return pygame.transform.scale(s, (12 * scale, 12 * scale))
