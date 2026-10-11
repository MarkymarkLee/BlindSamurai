"""Blind Samurai: fight monsters you can only feel and hear.

Angles throughout are in degrees, seen from above, measured clockwise from "north",
which is the direction of the camera. 0 = facing the camera, +90 = turned to your right.
"""


def wrap(deg: float) -> float:
    """Wrap an angle to (-180, 180]."""
    deg = (deg + 180.0) % 360.0 - 180.0
    return 180.0 if deg == -180.0 else deg


def lerp_angle(a: float, b: float, t: float) -> float:
    return wrap(a + wrap(b - a) * t)
