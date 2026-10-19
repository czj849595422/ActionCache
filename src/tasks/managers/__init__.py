"""内置任务管理器。"""

from .boss_combat import BossCombatManager
from .mining import MiningManager
from .monster_clearance import MonsterClearanceManager
from .pvp import PVPManager
from .trade import TradeTaskManager

__all__ = [
    "BossCombatManager",
    "MiningManager",
    "MonsterClearanceManager",
    "PVPManager",
    "TradeTaskManager",
]
