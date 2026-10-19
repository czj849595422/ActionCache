#!/usr/bin/env python3
"""Unified recording lifecycle, safety, and aggregation tests."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.recording import (  # noqa: E402
    LLMCallSource,
    RecordingConfig,
    RecordingError,
    RecordingManager,
    resolve_recording_config,
)
from src.recording.table import ActionExecutionTable  # noqa: E402


class RecordingLifecycleTests(unittest.TestCase):
    def test_recording_root_precedence_and_legacy_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertWarns(DeprecationWarning):
                legacy = resolve_recording_config(
                    {"dir": "legacy", "save_llm_calls": False},
                    base_dir=base,
                )
            self.assertEqual(legacy.root_dir, base / "legacy")
            with self.assertWarns(DeprecationWarning):
                explicit = resolve_recording_config(
                    {"dir": "legacy"},
                    root_dir="explicit",
                    base_dir=base,
                )
            self.assertEqual(explicit.root_dir, base / "explicit")
            default = resolve_recording_config({}, base_dir=base)
            self.assertEqual(default.root_dir, base / "data")

    def test_run_episode_and_llm_artifact_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run("Parallelized", run_id="run-1")
            task_spec = {
                "task_id": "task-a",
                "category": "debug",
                "name": "Demo",
            }
            episode = run.start_episode(
                metadata={"task_id": "task-a", "task_spec": task_spec}
            )
            handle = episode.llm_calls.begin(
                request_id="request-1",
                mode="complete",
                source=LLMCallSource(
                    component="planning_worker",
                    operation="plan",
                    task_id="task-a",
                    agent_id="a0",
                ),
                request={"messages": [{"role": "user", "content": "hello"}]},
            )
            self.assertTrue(
                handle.path.name.endswith(".a0.plan.001.request-.json"),
            )
            self.assertRegex(
                handle.path.name,
                r"^\d{8}-\d{6}-\d{6}\.a0\.plan\.001\.request-\.json$",
            )
            started = json.loads(handle.path.read_text(encoding="utf-8"))
            self.assertEqual(started["status"], "started")
            self.assertIsNone(started["completed_at"])

            handle.finish(
                status="completed",
                response={"content": "ok"},
                usage={"prompt_tokens": 2, "completion_tokens": 3},
            )
            episode.event("test.event", value=1)
            episode.finalize(status="completed", result={"ok": True}, memory=[])
            run.finalize()

            call = json.loads(handle.path.read_text(encoding="utf-8"))
            self.assertEqual(call["source"]["agent_id"], "a0")
            self.assertEqual(call["usage"]["normalized"]["input_tokens"], 2)
            self.assertEqual(call["usage"]["normalized"]["output_tokens"], 3)
            self.assertIsNone(call["usage"]["normalized"]["total_tokens"])
            self.assertTrue((episode.path / "episode.json").is_file())
            self.assertTrue((episode.path / "result.json").is_file())
            self.assertTrue((episode.path / "memory.json").is_file())
            self.assertTrue((episode.path / "events.jsonl").is_file())
            self.assertTrue((episode.path / "task_spec.yaml").is_file())
            self.assertEqual(
                yaml.safe_load((episode.path / "task_spec.yaml").read_text(encoding="utf-8")),
                task_spec,
            )
            self.assertTrue((episode.path / "llm" / "summary.json").is_file())
            self.assertTrue((run.path / "run.json").is_file())
            self.assertTrue((run.path / "summary.json").is_file())

    def test_team_episode_child_has_episode_shape_without_incrementing_run_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run("pvp", task="pvp", run_id="run-pvp")
            episode = run.start_episode(metadata={"task_id": "pvp"})
            team = episode.team_episode(
                "team-basic_async",
                baseline="basic_async",
                metadata={"team_id": "alpha"},
            )
            self.assertEqual(run.episode_count, 1)
            handle = team.llm_calls.begin(
                request_id="team-request",
                mode="complete",
                source=LLMCallSource(agent_id="Aria", task_id="pvp"),
                request={"messages": [{"role": "user", "content": "go"}]},
            )
            handle.finish(status="completed", response={"content": "ok"})
            team.event("team.event", value=1)
            team.finalize(status="completed", result={"ok": True}, memory=[])
            episode.finalize(status="completed", result={"winner": "alpha"})
            run.finalize()

            team_path = episode.path / "team-basic_async"
            self.assertTrue((team_path / "episode.json").is_file())
            self.assertTrue((team_path / "events.jsonl").is_file())
            self.assertTrue((team_path / "result.json").is_file())
            self.assertTrue((team_path / "memory.json").is_file())
            self.assertTrue((team_path / "llm" / "summary.json").is_file())
            self.assertEqual(
                json.loads((run.path / "summary.json").read_text(encoding="utf-8"))["episode_count"],
                1,
            )

    def test_pvp_parent_summary_aggregates_child_team_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run(
                "PVP",
                task="basic_async_vs_direct_action",
                run_id="run-pvp-team-aggregate",
            )
            episode = run.start_episode(episode_id="1")
            team_a = episode.team_episode(
                "team-basic_async",
                baseline="basic_async",
                metadata={"team_id": "alpha", "method": "basic_async"},
            )
            team_b = episode.team_episode(
                "team-direct_action",
                baseline="direct_action",
                metadata={"team_id": "bravo", "method": "direct_action"},
            )

            def record_team(
                team,
                *,
                request_id: str,
                agent_id: str,
                started_at: float,
                planning_duration: float,
                execution_delay: float,
                execution_duration: float,
                prompt_tokens: int,
                completion_tokens: int,
                planned_count: int,
                executed_count: int,
            ) -> dict[str, object]:
                handle = team.llm_calls.begin(
                    request_id=request_id,
                    mode="complete",
                    source=LLMCallSource(
                        component="planning_worker",
                        operation="plan",
                        agent_id=agent_id,
                        task_id="pvp",
                    ),
                    request={"messages": [{"role": "user", "content": "go"}]},
                    started_at=started_at,
                )
                handle.finish(
                    status="completed",
                    usage={
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                    completed_at=started_at + planning_duration,
                )
                table = ActionExecutionTable(team.path / "action_executions.csv")
                table.write_row(
                    {
                        "planning_started_at": started_at,
                        "planning_duration_seconds": planning_duration,
                        "execution_started_at": started_at + execution_delay,
                        "execution_duration_seconds": execution_duration,
                    }
                )
                table.close()
                result = {
                    "outcome": {"status": "running", "reason": "running", "detail": {}},
                    "planned_count": planned_count,
                    "executed_count": executed_count,
                    "started_at": started_at,
                    "finished_at": started_at + 10.0,
                    "runner_error": None,
                    "worker_errors": [],
                    "cleanup_errors": [],
                }
                team.finalize(status="completed", result=result)
                return result

            team_a_result = record_team(
                team_a,
                request_id="alpha-call",
                agent_id="Aria",
                started_at=100.0,
                planning_duration=2.0,
                execution_delay=5.0,
                execution_duration=1.0,
                prompt_tokens=10,
                completion_tokens=5,
                planned_count=2,
                executed_count=1,
            )
            team_b_result = record_team(
                team_b,
                request_id="bravo-call",
                agent_id="Eli",
                started_at=120.0,
                planning_duration=4.0,
                execution_delay=7.0,
                execution_duration=3.0,
                prompt_tokens=20,
                completion_tokens=10,
                planned_count=3,
                executed_count=3,
            )
            episode.finalize(
                status="completed",
                result={
                    "outcome": {
                        "status": "success",
                        "reason": "team_eliminated",
                        "detail": {"winner": "alpha"},
                    },
                    "final_metrics": {"elapsed_seconds": 60.0},
                    "team_results": [
                        {
                            "team_id": "alpha",
                            "method": "basic_async",
                            "result": team_a_result,
                            "exit_code": 0,
                        },
                        {
                            "team_id": "bravo",
                            "method": "direct_action",
                            "result": team_b_result,
                            "exit_code": 0,
                        },
                    ],
                    "started_at": 100.0,
                    "finished_at": 160.0,
                    "runner_error": None,
                    "cleanup_errors": [],
                },
            )
            run.finalize()

            aggregate = json.loads(
                (run.path / "summary.json").read_text(encoding="utf-8")
            )["aggregate_metrics"]
            self.assertEqual(aggregate["totals"]["planned_count"], 5.0)
            self.assertEqual(aggregate["totals"]["executed_count"], 4.0)
            self.assertEqual(aggregate["totals"]["llm_calls"], 2.0)
            self.assertEqual(aggregate["totals"]["input_tokens"], 30.0)
            self.assertEqual(aggregate["totals"]["output_tokens"], 15.0)
            self.assertEqual(aggregate["totals"]["total_tokens"], 45.0)
            self.assertEqual(aggregate["all_episode_averages"]["llm_calls"], 2.0)
            self.assertEqual(aggregate["all_episode_averages"]["planned_count"], 5.0)
            self.assertEqual(
                aggregate["all_episode_averages"]["planning_duration_seconds"],
                3.0,
            )
            self.assertEqual(
                aggregate["all_episode_averages"][
                    "planning_to_execution_start_seconds"
                ],
                6.0,
            )
            self.assertEqual(
                aggregate["all_episode_averages"]["execution_duration_seconds"],
                2.0,
            )
            planning = aggregate["llm_component_token_averages"]["all_episodes"][
                "planning_worker"
            ]
            self.assertEqual(planning["call_count"], 2.0)
            self.assertEqual(planning["total_tokens"], 45.0)
            self.assertEqual(planning["total_token_share"], 1.0)

    def test_pvp_run_summary_aggregates_method_win_rates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run(
                "PVP",
                task="basic_async_vs_coap",
                run_id="run-pvp-metrics",
            )
            self.assertEqual(run.context.baseline, "PVP")
            self.assertEqual(run.context.task, "basic_async_vs_coap")
            self.assertEqual(
                run.path,
                (
                    Path(tmp) / "PVP" / "basic_async_vs_coap" / "run-pvp-metrics"
                ).resolve(),
            )

            def finish_match(
                episode_id: str,
                *,
                winner: str | None,
                outcome_status: str = "success",
            ) -> None:
                episode = run.start_episode(episode_id=episode_id)
                episode.finalize(
                    status=(
                        "completed"
                        if outcome_status == "success"
                        else "failed"
                    ),
                    result={
                        "outcome": {
                            "status": outcome_status,
                            "reason": (
                                "team_eliminated"
                                if winner is not None
                                else "timeout"
                            ),
                            "detail": (
                                {"winner": winner}
                                if winner is not None
                                else {}
                            ),
                        },
                        "final_metrics": {"elapsed_seconds": 20.0},
                        "team_results": [
                            {
                                "team_id": "alpha",
                                "method": "basic_async",
                                "exit_code": 0,
                            },
                            {
                                "team_id": "bravo",
                                "method": "coap",
                                "exit_code": 0,
                            },
                        ],
                        "started_at": 100.0,
                        "finished_at": 120.0,
                    },
                )

            finish_match("1", winner="alpha")
            finish_match("2", winner="bravo")
            finish_match("3", winner="alpha")
            finish_match("4", winner=None, outcome_status="timeout")
            run.finalize()

            summary = json.loads((run.path / "summary.json").read_text(encoding="utf-8"))
            pvp = summary["pvp_metrics"]
            self.assertEqual(pvp["match_count"], 4)
            self.assertEqual(pvp["valid_match_count"], 4)
            self.assertEqual(pvp["winner_count"], 3)
            self.assertEqual(pvp["no_winner_count"], 1)
            self.assertEqual(
                pvp["outcome_status_counts"],
                {"success": 3, "timeout": 1},
            )

            basic = pvp["method_results"]["basic_async"]
            self.assertEqual(basic["match_count"], 4)
            self.assertEqual(basic["appearance_count"], 4)
            self.assertEqual(basic["win_count"], 2)
            self.assertEqual(basic["loss_count"], 1)
            self.assertEqual(basic["no_winner_count"], 1)
            self.assertEqual(basic["win_rate"], 0.5)
            self.assertEqual(basic["loss_rate"], 0.25)
            self.assertEqual(basic["no_winner_rate"], 0.25)

            coap = pvp["method_results"]["coap"]
            self.assertEqual(coap["match_count"], 4)
            self.assertEqual(coap["appearance_count"], 4)
            self.assertEqual(coap["win_count"], 1)
            self.assertEqual(coap["loss_count"], 2)
            self.assertEqual(coap["no_winner_count"], 1)
            self.assertEqual(coap["win_rate"], 0.25)

            matchup = pvp["matchups"]["basic_async_vs_coap"]
            self.assertEqual(matchup["match_count"], 4)
            self.assertEqual(matchup["methods"], ["basic_async", "coap"])
            self.assertEqual(
                matchup["wins"],
                {"basic_async": 2, "coap": 1},
            )
            self.assertEqual(matchup["win_rates"]["basic_async"], 0.5)
            self.assertEqual(matchup["win_rates"]["coap"], 0.25)
            self.assertEqual(matchup["no_winner_count"], 1)

    def test_run_summary_aggregates_common_and_coap_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run("coap", task="monster_clearance", run_id="run-metrics")

            def record_call(
                episode,
                *,
                request_id: str,
                component: str,
                operation: str,
                agent_id: str,
                started_at: float,
                prompt_tokens: int,
                completion_tokens: int,
            ) -> None:
                handle = episode.llm_calls.begin(
                    request_id=request_id,
                    mode="complete",
                    source=LLMCallSource(
                        component=component,
                        operation=operation,
                        agent_id=agent_id,
                    ),
                    request={"messages": [{"role": "user", "content": "go"}]},
                    started_at=started_at,
                )
                handle.finish(
                    status="completed",
                    usage={
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                    completed_at=started_at + 1.0,
                )

            success = run.start_episode(episode_id="1")
            record_call(
                success,
                request_id="success-plan",
                component="coap_planning",
                operation="plan",
                agent_id="Alice",
                started_at=100.0,
                prompt_tokens=8,
                completion_tokens=4,
            )
            record_call(
                success,
                request_id="success-cache",
                component="coap_cache",
                operation="cache_action",
                agent_id="Alice",
                started_at=110.0,
                prompt_tokens=2,
                completion_tokens=1,
            )
            success_table = ActionExecutionTable(success.path / "action_executions.csv")
            success_table.write_row(
                {
                    "planning_started_at": 100.0,
                    "planning_duration_seconds": 1.0,
                    "execution_started_at": 103.0,
                    "execution_duration_seconds": 2.0,
                }
            )
            success_table.close()
            success.finalize(
                status="completed",
                result={
                    "outcome": {"status": "success", "reason": "done", "detail": {}},
                    "final_metrics": {
                        "elapsed_seconds": 10.0,
                        "eliminated_monsters": 20,
                        "schedule_complete": True,
                        "nested": {"ignored": 1},
                    },
                    "planned_count": 4,
                    "executed_count": 3,
                    "started_at": 100.0,
                    "finished_at": 120.0,
                    "runner_error": None,
                    "worker_errors": [],
                    "cleanup_errors": [],
                    "cache_stats": {
                        "entry_count": 5,
                        "hit_count": 2,
                        "attempt_count": 3,
                        "success_count": 3,
                        "buffer_size": 1,
                        "update_count": 1,
                    },
                },
            )

            timeout = run.start_episode(episode_id="2")
            record_call(
                timeout,
                request_id="timeout-plan",
                component="coap_planning",
                operation="plan",
                agent_id="Bob",
                started_at=200.0,
                prompt_tokens=16,
                completion_tokens=8,
            )
            record_call(
                timeout,
                request_id="timeout-cache",
                component="coap_cache",
                operation="cache_action",
                agent_id="Bob",
                started_at=210.0,
                prompt_tokens=4,
                completion_tokens=2,
            )
            timeout_table = ActionExecutionTable(timeout.path / "action_executions.csv")
            timeout_table.write_row(
                {
                    "planning_started_at": 200.0,
                    "planning_duration_seconds": 4.0,
                    "execution_started_at": 207.0,
                    "execution_duration_seconds": 8.0,
                }
            )
            timeout_table.close()
            timeout.finalize(
                status="failed",
                result={
                    "outcome": {"status": "timeout", "reason": "timeout", "detail": {}},
                    "final_metrics": {
                        "elapsed_seconds": 30.0,
                        "eliminated_monsters": 10,
                        "schedule_complete": False,
                    },
                    "planned_count": 6,
                    "executed_count": 5,
                    "started_at": 200.0,
                    "finished_at": 260.0,
                    "runner_error": "stopped",
                    "worker_errors": [["worker", "error"]],
                    "cleanup_errors": [["cleanup", "error"]],
                    "cache_stats": {
                        "entry_count": 8,
                        "hit_count": 5,
                        "attempt_count": 10,
                        "success_count": 4,
                        "buffer_size": 2,
                        "update_count": 3,
                    },
                },
            )

            run.finalize()

            success_llm = json.loads(
                (success.path / "llm" / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(success_llm["component_token_stats"]["coap_planning"]["call_count"], 1.0)
            self.assertEqual(success_llm["component_token_stats"]["coap_planning"]["total_tokens"], 12.0)
            self.assertEqual(success_llm["component_token_stats"]["coap_planning"]["total_token_share"], 0.8)
            self.assertEqual(success_llm["component_token_stats"]["coap_cache"]["call_count"], 1.0)
            self.assertEqual(success_llm["component_token_stats"]["coap_cache"]["total_tokens"], 3.0)
            self.assertEqual(success_llm["component_token_stats"]["coap_cache"]["total_token_share"], 0.2)

            timeout_llm = json.loads(
                (timeout.path / "llm" / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(timeout_llm["component_token_stats"]["coap_planning"]["total_tokens"], 24.0)
            self.assertEqual(timeout_llm["component_token_stats"]["coap_planning"]["total_token_share"], 0.8)
            self.assertEqual(timeout_llm["component_token_stats"]["coap_cache"]["total_tokens"], 6.0)
            self.assertEqual(timeout_llm["component_token_stats"]["coap_cache"]["total_token_share"], 0.2)

            summary = json.loads((run.path / "summary.json").read_text(encoding="utf-8"))
            aggregate = summary["aggregate_metrics"]
            self.assertEqual(aggregate["episode_count"], 2)
            self.assertEqual(aggregate["valid_episode_count"], 2)
            self.assertEqual(aggregate["successful_episode_count"], 1)
            self.assertEqual(aggregate["success_count"], 1)
            self.assertEqual(aggregate["timeout_count"], 1)
            self.assertEqual(aggregate["success_rate"], 0.5)
            self.assertEqual(aggregate["success_rate_stddev"], 0.5)
            self.assertEqual(aggregate["totals"]["planned_count"], 10.0)
            self.assertEqual(aggregate["totals"]["executed_count"], 8.0)
            self.assertEqual(aggregate["totals"]["llm_calls"], 4.0)
            self.assertEqual(aggregate["totals"]["input_tokens"], 30.0)
            self.assertEqual(aggregate["totals"]["output_tokens"], 15.0)
            self.assertEqual(aggregate["totals"]["total_tokens"], 45.0)
            self.assertEqual(aggregate["all_episode_averages"]["wall_seconds"], 40.0)
            self.assertEqual(aggregate["successful_episode_averages"]["wall_seconds"], 20.0)
            self.assertEqual(aggregate["all_episode_averages"]["planned_count"], 5.0)
            self.assertEqual(aggregate["successful_episode_averages"]["planned_count"], 4.0)
            self.assertEqual(aggregate["all_episode_averages"]["llm_calls"], 2.0)
            self.assertEqual(aggregate["successful_episode_averages"]["llm_calls"], 2.0)
            self.assertEqual(aggregate["all_episode_averages"]["planning_duration_seconds"], 2.5)
            self.assertEqual(aggregate["successful_episode_averages"]["planning_duration_seconds"], 1.0)
            self.assertEqual(aggregate["all_episode_averages"]["planning_to_execution_start_seconds"], 5.0)
            self.assertEqual(aggregate["successful_episode_averages"]["planning_to_execution_start_seconds"], 3.0)
            self.assertEqual(aggregate["successful_episode_stddevs"]["wall_seconds"], 0.0)
            self.assertEqual(aggregate["successful_episode_stddevs"]["task_elapsed_seconds"], 0.0)
            self.assertEqual(aggregate["successful_episode_stddevs"]["planning_duration_seconds"], 0.0)
            self.assertEqual(aggregate["successful_episode_stddevs"]["planning_to_execution_start_seconds"], 0.0)
            self.assertEqual(aggregate["successful_episode_stddevs"]["total_tokens"], 0.0)
            self.assertEqual(aggregate["task_metric_averages"]["all_episodes"]["elapsed_seconds"], 20.0)
            self.assertEqual(
                aggregate["task_metric_averages"]["successful_episodes"]["elapsed_seconds"],
                10.0,
            )
            self.assertEqual(aggregate["task_metric_averages"]["all_episodes"]["schedule_complete"], 0.5)
            component_averages = aggregate["llm_component_token_averages"]
            self.assertEqual(component_averages["all_episodes"]["coap_planning"]["call_count"], 1.0)
            self.assertEqual(component_averages["all_episodes"]["coap_planning"]["input_tokens"], 12.0)
            self.assertEqual(component_averages["all_episodes"]["coap_planning"]["output_tokens"], 6.0)
            self.assertEqual(component_averages["all_episodes"]["coap_planning"]["total_tokens"], 18.0)
            self.assertEqual(component_averages["all_episodes"]["coap_planning"]["total_token_share"], 0.8)
            self.assertEqual(component_averages["all_episodes"]["coap_cache"]["call_count"], 1.0)
            self.assertEqual(component_averages["all_episodes"]["coap_cache"]["input_tokens"], 3.0)
            self.assertEqual(component_averages["all_episodes"]["coap_cache"]["output_tokens"], 1.5)
            self.assertEqual(component_averages["all_episodes"]["coap_cache"]["total_tokens"], 4.5)
            self.assertEqual(component_averages["all_episodes"]["coap_cache"]["total_token_share"], 0.2)
            self.assertEqual(component_averages["successful_episodes"]["coap_planning"]["total_tokens"], 12.0)
            self.assertEqual(component_averages["successful_episodes"]["coap_planning"]["total_token_share"], 0.8)
            self.assertEqual(component_averages["successful_episodes"]["coap_cache"]["total_tokens"], 3.0)
            self.assertEqual(component_averages["successful_episodes"]["coap_cache"]["total_token_share"], 0.2)
            component_stddevs = aggregate["llm_component_token_stddevs"]
            self.assertEqual(component_stddevs["successful_episodes"]["coap_planning"]["total_tokens"], 0.0)
            self.assertEqual(component_stddevs["successful_episodes"]["coap_cache"]["total_tokens"], 0.0)
            self.assertEqual(aggregate["error_counts"]["runner_error_count"], 1)
            self.assertEqual(aggregate["error_counts"]["worker_error_count"], 1)
            self.assertEqual(aggregate["error_counts"]["cleanup_error_count"], 1)

            coap = summary["coap_metrics"]
            self.assertEqual(coap["final_entry_count"], 8.0)
            self.assertEqual(coap["final_hit_count"], 5.0)
            self.assertEqual(coap["final_attempt_count"], 10.0)
            self.assertEqual(coap["final_success_count"], 4.0)
            self.assertEqual(coap["cache_success_rate"], 0.4)
            self.assertEqual(coap["cache_hit_per_episode"], 3.5)
            self.assertEqual(coap["all_episode_averages"]["entry_count"], 6.5)
            self.assertEqual(coap["successful_episode_averages"]["entry_count"], 5.0)

    def test_successful_episode_stddevs_ignore_failed_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run("parallelized", task="monster_clearance", run_id="run-stddevs")

            def finish_episode(
                episode_id: str,
                *,
                outcome_status: str,
                started_at: float,
                finished_at: float,
                elapsed_seconds: float,
                planning_duration: float,
                planning_to_execution_start: float,
                total_tokens: int,
            ) -> None:
                episode = run.start_episode(episode_id=episode_id)
                handle = episode.llm_calls.begin(
                    request_id=f"call-{episode_id}",
                    mode="complete",
                    source=LLMCallSource(
                        component="planning_worker",
                        operation="plan",
                        agent_id="Alice",
                    ),
                    request={"messages": [{"role": "user", "content": "go"}]},
                    started_at=started_at,
                )
                handle.finish(
                    status="completed",
                    usage={
                        "prompt_tokens": total_tokens,
                        "completion_tokens": 0,
                        "total_tokens": total_tokens,
                    },
                    completed_at=started_at + 1.0,
                )
                table = ActionExecutionTable(episode.path / "action_executions.csv")
                table.write_row(
                    {
                        "planning_started_at": started_at,
                        "planning_duration_seconds": planning_duration,
                        "execution_started_at": (
                            started_at + planning_to_execution_start
                        ),
                        "execution_duration_seconds": 1.0,
                    }
                )
                table.close()
                episode.finalize(
                    status=(
                        "completed"
                        if outcome_status == "success"
                        else "failed"
                    ),
                    result={
                        "outcome": {"status": outcome_status, "reason": outcome_status, "detail": {}},
                        "final_metrics": {"elapsed_seconds": elapsed_seconds},
                        "planned_count": 1,
                        "executed_count": 1,
                        "started_at": started_at,
                        "finished_at": finished_at,
                    },
                )

            finish_episode(
                "1",
                outcome_status="success",
                started_at=100.0,
                finished_at=110.0,
                elapsed_seconds=10.0,
                planning_duration=2.0,
                planning_to_execution_start=4.0,
                total_tokens=10,
            )
            finish_episode(
                "2",
                outcome_status="success",
                started_at=200.0,
                finished_at=230.0,
                elapsed_seconds=30.0,
                planning_duration=6.0,
                planning_to_execution_start=8.0,
                total_tokens=30,
            )
            finish_episode(
                "3",
                outcome_status="timeout",
                started_at=300.0,
                finished_at=400.0,
                elapsed_seconds=100.0,
                planning_duration=100.0,
                planning_to_execution_start=100.0,
                total_tokens=100,
            )

            run.finalize()

            aggregate = json.loads(
                (run.path / "summary.json").read_text(encoding="utf-8")
            )["aggregate_metrics"]
            self.assertAlmostEqual(aggregate["success_rate"], 2.0 / 3.0)
            self.assertAlmostEqual(
                aggregate["success_rate_stddev"],
                (2.0 / 9.0) ** 0.5,
            )
            successful_stddevs = aggregate["successful_episode_stddevs"]
            self.assertEqual(successful_stddevs["wall_seconds"], 10.0)
            self.assertEqual(successful_stddevs["task_elapsed_seconds"], 10.0)
            self.assertEqual(successful_stddevs["planning_duration_seconds"], 2.0)
            self.assertEqual(successful_stddevs["planning_to_execution_start_seconds"], 2.0)
            self.assertEqual(successful_stddevs["total_tokens"], 10.0)
            self.assertEqual(
                aggregate["llm_component_token_stddevs"]["successful_episodes"]["planning_worker"]["total_tokens"],
                10.0,
            )

    def test_run_with_task_gets_task_level_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run(
                "parallelized",
                task="mining",
                run_id="run-9",
            )
            self.assertEqual(run.context.task, "mining")
            self.assertTrue(
                (Path(tmp) / "parallelized" / "mining" / "run-9").is_dir()
            )
            self.assertEqual(
                run.path,
                (
                    Path(tmp) / "parallelized" / "mining" / "run-9"
                ).resolve(),
            )

    def test_open_run_resumes_existing_run_with_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run(
                "parallelized",
                task="clear.task",
                run_id="run-resume",
            )
            for episode_id in ("1", "2"):
                episode = run.start_episode(
                    episode_id=episode_id,
                    metadata={"task_id": "task-a"},
                )
                episode.finalize(status="completed", result={"ok": True})
            run.finalize()
            started_at = json.loads(
                (run.path / "run.json").read_text(encoding="utf-8")
            )["started_at"]

            reopened = manager.open_run(
                "parallelized",
                task="clear.task",
                run_id="run-resume",
            )
            self.assertEqual(reopened.context.run_id, "run-resume")
            self.assertEqual(reopened.episode_count, 2)
            manifest = json.loads(
                (reopened.path / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["started_at"], started_at)
            self.assertEqual(manifest["status"], "started")
            self.assertEqual(
                [item["episode_id"] for item in manifest["episodes"]],
                ["1", "2"],
            )

            episode = reopened.start_episode(
                episode_id="3",
                metadata={"task_id": "task-b"},
            )
            episode.finalize(status="completed", result={"ok": True})
            reopened.finalize()

            summary = json.loads(
                (reopened.path / "summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["episode_id"] for item in summary["episodes"]],
                ["1", "2", "3"],
            )
            self.assertTrue((reopened.path / "1").is_dir())
            self.assertTrue((reopened.path / "2").is_dir())
            self.assertTrue((reopened.path / "3").is_dir())

    def test_resume_recomputes_aggregate_over_old_and_new_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            run = manager.start_run(
                "parallelized",
                task="clear.task",
                run_id="run-aggregate-resume",
                metadata={"config": {"max_run_seconds": 30}},
            )

            def finish(episode_id: str, planned: int, executed: int) -> None:
                episode = run.start_episode(episode_id=episode_id)
                episode.finalize(
                    status="completed",
                    result={
                        "outcome": {"status": "success"},
                        "planned_count": planned,
                        "executed_count": executed,
                    },
                )

            finish("1", 2, 1)
            finish("2", 3, 2)
            run.finalize()

            reopened = manager.open_run(
                "parallelized",
                task="clear.task",
                run_id="run-aggregate-resume",
            )
            self.assertEqual(reopened.config_values(), {"max_run_seconds": 30})
            finish = lambda episode_id, planned, executed: reopened.start_episode(
                episode_id=episode_id
            ).finalize(
                status="completed",
                result={
                    "outcome": {"status": "success"},
                    "planned_count": planned,
                    "executed_count": executed,
                },
            )
            finish("3", 5, 4)
            reopened.finalize()

            summary = json.loads(
                (reopened.path / "summary.json").read_text(encoding="utf-8")
            )
            aggregate = summary["aggregate_metrics"]
            self.assertEqual(aggregate["episode_count"], 3)
            self.assertEqual(aggregate["successful_episode_count"], 3)
            self.assertEqual(aggregate["totals"]["planned_count"], 10.0)
            self.assertEqual(aggregate["totals"]["executed_count"], 7.0)

    def test_open_run_rejects_missing_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            with self.assertRaises(RecordingError):
                manager.open_run(
                    "parallelized",
                    task="clear.task",
                    run_id="missing",
                )

    def test_invalid_and_duplicate_identifiers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = RecordingManager(RecordingConfig(root_dir=Path(tmp)))
            with self.assertRaises(RecordingError):
                manager.start_run("../outside")
            run = manager.start_run("parallelized", run_id="same")
            with self.assertRaises(RecordingError):
                manager.start_run("parallelized", run_id="same")
            run.start_episode(episode_id="episode-0001")
            with self.assertRaises(RecordingError):
                run.start_episode(episode_id="episode-0001")

    def test_redaction_and_missing_usage_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = RecordingConfig(
                root_dir=Path(tmp),
                redact_values=("literal-secret",),
            )
            run = RecordingManager(config).start_run("standalone")
            episode = run.start_episode()
            handle = episode.llm_calls.begin(
                request_id="redacted",
                mode="complete",
                source=LLMCallSource(),
                request={
                    "messages": [{"role": "user", "content": "literal-secret"}],
                    "access_token": "token-value",
                    "input_tokens": 7,
                },
            )
            handle.finish(status="completed", response={"content": "done"})
            payload = json.loads(handle.path.read_text(encoding="utf-8"))
            self.assertEqual(payload["request"]["access_token"], "***")
            self.assertEqual(payload["request"]["input_tokens"], 7)
            self.assertNotIn("literal-secret", handle.path.read_text(encoding="utf-8"))
            self.assertFalse(payload["usage"]["usage_available"])
            self.assertEqual(
                payload["usage"]["normalized"],
                {"input_tokens": None, "output_tokens": None, "total_tokens": None},
            )

    def test_concurrent_calls_have_one_file_each(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = RecordingManager(
                RecordingConfig(root_dir=Path(tmp))
            ).start_run("parallelized")
            episode = run.start_episode()

            def record(index: int) -> None:
                handle = episode.llm_calls.begin(
                    request_id=f"request-{index}",
                    mode="complete",
                    source=LLMCallSource(agent_id=f"a{index}"),
                    request={"messages": [{"role": "user", "content": str(index)}]},
                )
                handle.finish(status="completed", response={"content": str(index)})

            threads = [threading.Thread(target=record, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            summary = episode.llm_calls.summary()
            self.assertEqual(summary["total_calls"], 8)
            self.assertEqual(summary["status_counts"], {"completed": 8})
            self.assertEqual(len(list((episode.path / "llm" / "calls").glob("*.json"))), 8)


if __name__ == "__main__":
    unittest.main()
