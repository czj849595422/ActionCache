#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""感知系统真实链路测试 demo (移植自 MCAgent src/perception)。

流程:
  1. 启动 Minecraft 服务器 (src/launcher/server.py), 登录一个 SkillAgent;
  2. 经 RCON 在 agent 位置召唤一只 NoAI 僵尸 (确定性验证敌对感知);
  3. BasePerception.perceive() 采集原始感知 (桥 /perception/raw);
  4. 应用 combat_snapshot 规则 → combat_verbalizer 渲染中文文本;
  5. perceive_snapshot() 输出跨模块快照;
  6. 清理: disconnect agent -> 优雅停止服务器。

用法:
  python script/demo_perception.py                    # 真实链路
  python script/demo_perception.py --check           # 仅离线渲染内置样例
  python script/demo_perception.py --keep-alive      # 完成后保持运行
"""

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.launcher.server import Server  # noqa: E402
from src.perception import BasePerception, PerceptionMode  # noqa: E402
from src.perception.rule import RuleContext, combat_snapshot  # noqa: E402
from src.perception.verbalizer import combat_verbalizer  # noqa: E402


SAMPLE_OBSERVATION = {
    "self": {
        "health": 20,
        "food": 18,
        "position": {"x": -5.5, "y": 8.0, "z": -7.5},
        "gameMode": "survival",
        "onGround": True,
        "heldItem": {"name": "stone_sword", "count": 1},
    },
    "players": [{"username": "perception_demo", "isSelf": True}],
    "entities": [
        {
            "id": 68,
            "type": "hostile",
            "name": "zombie",
            "position": {"x": -5.5, "y": 8.0, "z": -7.5},
            "distance": 0.0,
            "health": 20,
        }
    ],
    "items": [],
    "blocks": [{"name": "stone", "distance": 1.0, "material": "stone"}],
    "summary": {"hostile_count": 1, "nearest_hostile_distance": 0.0},
}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["global", "local"], default="global", help="感知模式")
    parser.add_argument("--radius", type=float, default=None, help="实体感知半径")
    parser.add_argument("--block-radius", type=int, default=8, help="方块扫描半径")
    parser.add_argument("--language", choices=["zh", "en"], default="zh", help="verbalizer 语言")
    parser.add_argument("--check", action="store_true", help="仅离线渲染内置样例, 不启动服务器")
    parser.add_argument("--keep-alive", action="store_true", help="结束后保持服务器与 agent 运行")
    return parser


def render_offline(language: str) -> str:
    text = combat_verbalizer(language=language).render(SAMPLE_OBSERVATION)
    print(f"== 离线渲染 ({language}) ==")
    print(text)
    return text


def main() -> int:
    args = build_parser().parse_args()

    if args.check:
        render_offline("zh")
        render_offline("en")
        print("--check 通过: 离线渲染正常, 未启动服务器")
        return 0

    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent = SkillAgent(
        username="perception_demo",
        host="localhost",
        port=25565,
        bridge_port=3211,
        log_dir=log_dir,
    )

    try:
        print("== 1/5 启动 Minecraft 服务器 ==")
        server.start()
        print(f"服务器已就绪 (pid={server.pid})")

        print("== 2/5 登录 SkillAgent ==")
        agent.connect()
        status = agent.get_status()
        position = status.get("position") or {}
        print(
            f"agent 已登录: {agent.username}, "
            f"pos=({position.get('x')}, {position.get('y')}, {position.get('z')})"
        )

        print("== 3/5 召唤 NoAI 僵尸并采集原始感知 ==")
        x = float(position.get("x", 0))
        y = float(position.get("y", 64))
        z = float(position.get("z", 0))
        summon = (
            f"summon minecraft:zombie {x:.2f} {y:.2f} {z:.2f} "
            "{NoAI:1b,PersistenceRequired:1b,Silent:1b,"
            "CustomName:'\"perception_zombie\"'}"
        )
        print(f"RCON: {summon}")
        server.send_command(summon, response_timeout=10)
        time.sleep(2)

        perception = BasePerception(
            agent=agent,
            mode=PerceptionMode.GLOBAL if args.mode == "global" else "local",
            radius=args.radius,
            block_radius=args.block_radius,
        )
        raw = perception.perceive()
        top_keys = sorted(raw.keys())
        counts = raw.get("metadata", {}).get("counts", {}) if isinstance(raw, dict) else {}
        print(f"原始感知顶层字段: {top_keys}")
        print(f"数量统计: {counts}")
        assert all(key in raw for key in ("self", "entities", "players", "items", "blocks", "metadata"))

        entity_names = [e.get("name") for e in raw.get("entities", []) if isinstance(e, dict)]
        print(f"实体列表: {entity_names}")
        assert any("zombie" in str(name or "").lower() for name in entity_names), "未检测到召唤的僵尸"

        print("== 4/5 应用规则并渲染文本 ==")
        context = RuleContext(
            mode=args.mode,
            sub_mode="radius",
            radius=args.radius,
            metadata={"raw_data": raw},
        )
        processed = combat_snapshot(k=5, radius=32).apply(raw, context)
        text = combat_verbalizer(language=args.language).render(processed)
        print(text)
        assert "【自身状态】" in text or "[Self Status]" in text
        assert "zombie" in text or "敌对实体" in text

        print("== 5/5 输出快照并保存结果 ==")
        snapshot = perception.perceive_snapshot()
        print(
            f"快照: id={snapshot.snapshot_id}, mode={snapshot.mode}, "
            f"agent={snapshot.agent_id}, 字段={sorted(snapshot.to_dict().keys())}"
        )

        tmp_dir = ROOT / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        (tmp_dir / "perception_demo_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (tmp_dir / "perception_demo_text.txt").write_text(text, encoding="utf-8")
        print("原始感知与渲染文本已保存到 .tmp/perception_demo_raw.json / perception_demo_text.txt")

        if args.keep_alive:
            print("--keep-alive: 保持运行, Ctrl+C 清理")
            while True:
                time.sleep(1)
        print("感知链路验证通过。")
        return 0
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C, 开始清理")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            agent.disconnect()
            server.stop()
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
