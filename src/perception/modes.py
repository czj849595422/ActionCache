"""
Perception mode constants.
"""


class PerceptionMode:
    GLOBAL = "global"
    LOCAL = "local"

    ALL = {GLOBAL, LOCAL}


class LocalPerceptionMode:
    RADIUS = "radius"
    VIEW = "view"

    ALL = {RADIUS, VIEW}
