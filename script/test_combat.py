#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""战斗技能测试：撤退 + 掉落三档拾取 + 投掷物远程。

流程：
  1. 启动 Minecraft 服务器并登录 1 个 SkillAgent（和平模式、禁刷怪）；
  2. RCON 召唤猪/僵尸并发放铁剑、雪球；
  3. 验证：
     - combat.attack 击杀猪（动物）：collect_drops 传 optional 也被强制为 required，
       且成功收集猪肉（collected_count >= 1）；
     - combat.attack 击杀僵尸：collect_drops=optional，collect_mode=optional；
     - combat.fight 用 snowball（投掷物）：combat_type=throwable，shots_fired>=1，
       collect_drops=none 不拾取；
     - combat.retreat 省略 distance 时撤退至默认 12 格；
     - combat.retreat 途中目标消失时以 target_lost 成功结束；
  4. 清理：disconnect、停止服务器。
"""

# 运行：
#   & 'C:\Users\Chen Zijian\.conda\envs\mcagent\python.exe' script\test_combat.py

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.skill_agent import SkillAgent  # noqa: E402
from src.launcher.server import Server  # noqa: E402


def error_code(result) -> str | None:
    payload = result.result if isinstance(result.result, dict) else {}
    return payload.get("error_code") or getattr(result, "error", None)


def entity_by_name(perception, name):
    self_id = (perception.get("self") or {}).get("entity", {}).get("id")
    candidates = [
        e for e in perception.get("entities", [])
        if e and e.get("name") == name and e.get("id") != self_id and e.get("position")
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.get("distance") if e.get("distance") is not None else 1e9)


def spawn_near(server, agent, mob, *, no_ai=False):
    """清掉同类实体后在 bot 头顶召唤，并轮询感知直到可见。"""
    server.send_command(f"kill @e[type=minecraft:{mob}]")
    time.sleep(1)
    nbt = " {NoAI:1b}" if no_ai else ""
    server.send_command(f"execute at combat_test run summon minecraft:{mob} ~0 ~1 ~{nbt}")
    for _ in range(4):
        time.sleep(2)
        entity = entity_by_name(agent.get_raw_perception(), mob)
        if entity:
            return entity
    return None


def wait_terminal(planner, action_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = planner.status(action_id)
        if st.status in ("completed", "failed", "interrupted", "stopped"):
            return st
        time.sleep(1)
    raise TimeoutError(f"action {action_id} 未在 {timeout}s 内结束")


def final_result(st):
    """continuous 动作的 status 输出把最终结果嵌套在 result.result 里。"""
    payload = st.result if isinstance(st.result, dict) else {}
    nested = payload.get("result")
    return nested if isinstance(nested, dict) else payload


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mc-port", type=int, default=25565)
    parser.add_argument("--bridge-port", type=int, default=3257)
    parser.add_argument("--wait-server", type=int, default=180)
    parser.add_argument("--keep-alive", action="store_true", help="测试完成后保留进程(调试用)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    log_dir = ROOT / "script" / "logs"
    server = Server(server_dir=ROOT / "src" / "minecraft", log_dir=log_dir)
    agent: SkillAgent | None = None

    try:
        print("== 1/8 启动服务器并登录 agent ==")
        server.start()
        server.send_command("difficulty easy")
        server.send_command("gamerule doMobSpawning false")
        server.send_command("time set night")
        time.sleep(1)
        agent = SkillAgent(
            username="combat_test",
            host="localhost",
            port=args.mc_port,
            bridge_port=args.bridge_port,
            log_dir=log_dir,
        )
        agent.connect()
        planner = agent.action_planner
        server.send_command("give combat_test iron_sword 1")
        server.send_command("give combat_test snowball 16")
        time.sleep(2)
        equipped = planner.execute("inventory.equip", {"name": "iron_sword", "destination": "hand"})
        assert equipped.ok, f"装备铁剑失败: {error_code(equipped)}"
        print(f"agent 已登录并装备铁剑, bridge_port={agent.bridge_port}")

        print("== 2/8 撤退到默认 12 格 ==")
        retreat_target = spawn_near(server, agent, "zombie", no_ai=True)
        assert retreat_target, "未找到撤退测试僵尸"
        r = planner.start(
            "combat.retreat",
            {"target": {"entity_id": retreat_target["id"]}},
        )
        assert r.ok, f"retreat(default) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id, timeout=45)
        payload = final_result(st)
        retreat_distance = payload.get("distance")
        assert st.status == "completed" and payload.get("reason") == "distance_reached", (
            f"默认撤退未完成: status={st.status}, result={payload}"
        )
        assert payload.get("requested_distance") == 12, f"默认距离应为 12: {payload}"
        assert isinstance(retreat_distance, (int, float)) and retreat_distance >= 12, (
            f"撤退后距离不足 12 格: {payload}"
        )
        print(f"  默认撤退 OK: 最终距离 {retreat_distance:.1f} 格")

        print("== 3/8 撤退途中目标丢失 ==")
        lost_target = spawn_near(server, agent, "zombie", no_ai=True)
        assert lost_target, "未找到目标丢失测试僵尸"
        r = planner.start(
            "combat.retreat",
            {"target": {"entity_id": lost_target["id"]}, "distance": 30},
        )
        assert r.ok, f"retreat(target_lost) start 失败: {error_code(r)}"
        time.sleep(0.5)
        server.send_command("kill @e[type=minecraft:zombie,sort=nearest,limit=1]")
        st = wait_terminal(planner, r.action_id, timeout=15)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("reason") == "target_lost", (
            f"目标丢失语义错误: status={st.status}, result={payload}"
        )
        assert payload.get("requested_distance") == 30, f"显式距离未生效: {payload}"
        print("  目标丢失后成功结束 OK: reason=target_lost")

        print("== 4/8 击杀猪（动物 -> 强制 required 拾取） ==")
        pig = spawn_near(server, agent, "pig")
        assert pig, "未找到猪实体"
        r = planner.start(
            "combat.attack",
            {"target": {"entity_id": pig["id"]}, "collect_drops": "optional"},
        )
        assert r.ok, f"attack(pig) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("target_dead"), (
            f"猪未击杀: status={st.status}, result={payload}"
        )
        assert payload.get("collect_mode") == "required", (
            f"动物目标应强制 required, 实际 {payload.get('collect_mode')}"
        )
        time.sleep(1)
        inv = agent.get_raw_perception(block_radius=4).get("self", {}).get("inventory", [])
        porkchop = sum(i.get("count", 0) for i in inv if i.get("name") == "porkchop")
        assert porkchop >= 1, f"背包中应有猪肉: 当前 {porkchop}"
        print(f"  猪已击杀并强制 required 拾取 OK: collect_mode=required, 背包猪肉 {porkchop}")

        print("== 5/8 击杀僵尸（optional 拾取） ==")
        zombie = spawn_near(server, agent, "zombie")
        assert zombie, "未找到僵尸实体"
        r = planner.start(
            "combat.attack",
            {"target": {"entity_id": zombie["id"]}, "collect_drops": "optional"},
        )
        assert r.ok, f"attack(zombie) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id)
        payload = final_result(st)
        assert st.status == "completed" and payload.get("target_dead"), (
            f"僵尸未击杀: status={st.status}, result={payload}"
        )
        assert payload.get("collect_mode") == "optional", (
            f"僵尸目标应 optional, 实际 {payload.get('collect_mode')}"
        )
        print(f"  僵尸已击杀 OK: collect_mode=optional, 收集 {payload.get('collected_count', 0)} 个")

        print("== 6/8 投掷物战斗（snowball + none 拾取） ==")
        zombie2 = spawn_near(server, agent, "zombie")
        assert zombie2, "未找到第二个僵尸实体"
        r = planner.start(
            "combat.fight",
            {
                "weapon": "snowball",
                "target": {"entity_id": zombie2["id"]},
                "collect_drops": "none",
                "max_duration_ms": 30000,
            },
        )
        assert r.ok, f"fight(snowball) start 失败: {error_code(r)}"
        st = wait_terminal(planner, r.action_id, timeout=60)
        payload = final_result(st)
        assert payload.get("combat_type") == "throwable", (
            f"snowball 应识别为 throwable, 实际 {payload.get('combat_type')}"
        )
        assert payload.get("shots_fired", 0) >= 1, f"应至少投掷 1 次: {payload}"
        assert payload.get("collect_mode") is None, f"none 模式不应带 collect_mode: {payload}"
        print(
            f"  投掷物战斗 OK: combat_type=throwable, 投掷 {payload.get('shots_fired')} 次, "
            f"结束原因 {payload.get('reason')}"
        )

        print("== 7/8 远程射程检查（弓不贴脸） ==")
        server.send_command("give combat_test bow 1")
        server.send_command("give combat_test arrow 8")
        time.sleep(2)
        zombie3 = spawn_near(server, agent, "zombie")
        assert zombie3, "未找到第三个僵尸实体"
        server.send_command("execute at combat_test run tp @e[type=minecraft:zombie,sort=nearest,limit=1] ~12 ~0 ~0")
        time.sleep(1)
        assert zombie3, "未找到第三个僵尸实体"
        r = planner.start(
            "combat.fight",
            {
                "weapon": "bow",
                "target": {"entity_id": zombie3["id"]},
                "collect_drops": "none",
                "max_duration_ms": 20000,
            },
        )
        assert r.ok, f"fight(bow) start 失败: {error_code(r)}"
        min_distance = 1e9
        deadline = time.time() + 12
        while time.time() < deadline:
            st = planner.status(r.action_id)
            payload = final_result(st)
            d = payload.get("last_distance")
            if isinstance(d, (int, float)):
                min_distance = min(min_distance, d)
            if st.status in ("completed", "failed"):
                break
            time.sleep(0.5)
        planner.stop(r.action_id, reason="test_stop")
        assert min_distance > 5, f"远程不应贴脸（最小距离 {min_distance:.1f}）"
        print(f"  远程射程 OK: 最小交战距离 {min_distance:.1f} 格（未贴脸近战）")

        print("== 8/8 全部用例通过 ==")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[test] 失败: {exc}")
        return 1
    finally:
        if not args.keep_alive:
            if agent:
                try:
                    agent.disconnect()
                except Exception as exc:  # noqa: BLE001
                    print(f"[test] 清理 agent 失败: {exc}")
            server.stop()
            time.sleep(1)
        else:
            print(f"[test] --keep-alive: 保留进程 (server pid={server.pid})")


if __name__ == "__main__":
    sys.exit(main())
