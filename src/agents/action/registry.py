"""
Skill manifest registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Iterable


@dataclass
class SkillCommandSpec:
    """Manifest metadata for one skill command."""

    skill: str
    command: str
    modes: set[str] = field(default_factory=set)
    locks: set[str] = field(default_factory=set)
    switch_group: str | None = None
    switchable: bool = False
    mutable_params: set[str] = field(default_factory=set)
    update_policy: str = "merge"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_manifest(cls, skill: str, command: str, data: dict[str, Any]) -> "SkillCommandSpec":
        return cls(
            skill=skill,
            command=command,
            modes=set(data.get("mode", [])),
            locks=set(data.get("locks", [])),
            switch_group=data.get("switch_group"),
            switchable=bool(data.get("switchable", False)),
            mutable_params=set(data.get("mutable_params", [])),
            update_policy=data.get("update_policy", "merge"),
            raw=data,
        )


@dataclass
class SkillManifest:
    """Loaded skill manifest."""

    name: str
    version: str
    runtime: str
    entry: str
    path: Path
    plugins: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, SkillCommandSpec] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "SkillManifest":
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data["name"]
        commands = {
            command_name: SkillCommandSpec.from_manifest(name, command_name, command_data)
            for command_name, command_data in data.get("commands", {}).items()
        }
        return cls(
            name=name,
            version=data.get("version", "0.0.0"),
            runtime=data.get("runtime", "javascript"),
            entry=data.get("entry", "skill.js"),
            path=path.parent,
            plugins=data.get("plugins", {}),
            commands=commands,
            raw=data,
        )

    def command_spec(self, command: str) -> SkillCommandSpec:
        try:
            return self.commands[command]
        except KeyError as exc:
            raise KeyError(f"Skill {self.name!r} has no command {command!r}.") from exc


class SkillRegistry:
    """Load and resolve skill manifests from <repo>/src/skills."""

    def __init__(
        self,
        skills_dir: Path | str | None = None,
        *,
        allowed_actions: Iterable[str] | None = None,
    ):
        if skills_dir is None:
            skills_dir = Path(__file__).resolve().parents[2] / "skills"
        self.skills_dir = Path(skills_dir)
        self.allowed_actions = (
            None
            if allowed_actions is None
            else frozenset(str(action).strip() for action in allowed_actions)
        )
        self.manifests: dict[str, SkillManifest] = {}
        self.reload()

    def reload(self) -> None:
        self.manifests.clear()
        if not self.skills_dir.exists():
            return
        for manifest_path in sorted(self.skills_dir.rglob("skill.json")):
            manifest = SkillManifest.load(manifest_path)
            if manifest.name in self.manifests:
                previous = self.manifests[manifest.name].path
                raise ValueError(
                    f"Duplicate skill name {manifest.name!r}: {previous} and {manifest.path}"
                )
            self.manifests[manifest.name] = manifest

    def resolve(
        self,
        action: str,
        *,
        enforce_allowlist: bool = True,
    ) -> tuple[SkillManifest, SkillCommandSpec]:
        skill_name, command_name = self.parse_action(action)
        canonical = f"{skill_name}.{command_name}"
        if enforce_allowlist and not self.is_allowed(canonical):
            raise PermissionError(f"Action is not allowed in this task: {canonical}")
        try:
            manifest = self.manifests[skill_name]
        except KeyError as exc:
            raise KeyError(f"Unknown skill: {skill_name!r}.") from exc
        return manifest, manifest.command_spec(command_name)

    def is_allowed(self, action: str) -> bool:
        return self.allowed_actions is None or action in self.allowed_actions

    def parse_action(self, action: str) -> tuple[str, str]:
        if "." in action:
            skill_name, command_name = action.split(".", 1)
            return skill_name, command_name
        if action in self.manifests and action in self.manifests[action].commands:
            return action, action
        raise KeyError(
            f"Action {action!r} must be '<skill>.<command>' or a skill with a same-name command."
        )

    def list_skills(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, manifest in sorted(self.manifests.items()):
            commands = sorted(
                command
                for command in manifest.commands
                if self.is_allowed(f"{name}.{command}")
            )
            if not commands:
                continue
            result[name] = {
                "version": manifest.version,
                "runtime": manifest.runtime,
                "entry": manifest.entry,
                "plugins": manifest.plugins,
                "commands": commands,
            }
        return result
