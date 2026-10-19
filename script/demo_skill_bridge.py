#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skill bridge 冒烟验证（无需 Minecraft 服务器）。

验证链路：
  1. 启动 bridge_skill.js 子进程（每 agent 一个进程、独占端口）；
  2. GET /skill/list 确认 JS 侧能加载 src/skills 全部技能；
  3. Python 侧 SkillRegistry 读取同一份 src/skills 的 skill.json；
  4. postprocess 动态加载 src/skills/*/postprocess.py（验证导入路径适配）。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\demo_skill_bridge.py

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402


def main() -> int:
    agent = SkillAgent(
        username="skill_demo",
        bridge_port=3299,
        log_dir=ROOT / ".tmp" / "logs",
    )
    try:
        print("== 1. 启动 bridge 子进程 ==")
        agent.start_bridge()
        status = agent._get_bridge("/skill/list")
        skills = status.get("skills", {})
        print(f"bridge /skill/list: {len(skills)} 个技能, "
              f"capabilities={status.get('capabilities', {}).get('mineflayer-pathfinder')}")
        print("技能列表:", ", ".join(sorted(skills.keys())))

        print("\n== 2. Python SkillRegistry 读取 manifest ==")
        planner_info = agent.action_planner.list_skills()
        local = planner_info["local"]
        print(f"本地 manifest: {len(local)} 个技能")
        for name, meta in sorted(local.items()):
            print(f"  - {name}: {meta['commands']}")

        print("\n== 3. postprocess 动态加载 ==")
        from src.agents.action.result_postprocessor import postprocess_action_result

        result = postprocess_action_result(
            {
                "action": "inventory.inspect",
                "ok": True,
                "result": {"ok": True, "items": [], "empty_slots": 36},
            }
        )
        print("inventory.inspect 后处理结果:", json.dumps(result, ensure_ascii=False)[:120])

        print("\n冒烟验证通过。")
        return 0
    finally:
        print("\n== 清理: 停止 bridge 进程 ==")
        agent.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
