"""Local skill discovery from SKILL.md files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from voidx.skills.schema import SkillDefinition, SkillMeta, SkillScope

SKILL_FILENAME = "SKILL.md"


def normalize_skill_name(name: str) -> str:
    return name.strip().lower()


class SkillParseError(ValueError):
    pass


class SkillRegistry:
    """Discovers bundled, global, and project skills.

    Search order is bundled first, then global, then project. Later sources
    override earlier sources with the same skill name.
    """

    def __init__(
        self,
        workspace: str,
        *,
        bundled_dir: Path | None = None,
        global_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.bundled_dir = bundled_dir or (Path(__file__).resolve().parent / "bundled" / "superpowers")
        self.global_dir = global_dir or (Path.home() / ".voidx" / "skills")
        self.project_dir = project_dir or (self.workspace / ".voidx" / "skills")

    def discover(self) -> list[SkillDefinition]:
        skills: dict[str, SkillDefinition] = {}
        for scope, root in (
            ("bundled", self.bundled_dir),
            ("global", self.global_dir),
            ("project", self.project_dir),
        ):
            for skill in self._discover_root(root, scope):
                skills[normalize_skill_name(skill.name)] = skill
        return sorted(skills.values(), key=lambda item: item.name)

    def get(self, name: str) -> SkillDefinition | None:
        target = normalize_skill_name(name)
        for skill in self.discover():
            if normalize_skill_name(skill.name) == target:
                return skill
        return None

    def _discover_root(self, root: Path, scope: SkillScope) -> list[SkillDefinition]:
        if not root.exists() or not root.is_dir():
            return []
        skills: list[SkillDefinition] = []
        for skill_file in sorted(root.glob(f"*/{SKILL_FILENAME}")):
            try:
                skills.append(parse_skill_file(skill_file, scope=scope))
            except SkillParseError:
                continue
        return skills


def parse_skill_file(path: Path, *, scope: SkillScope) -> SkillDefinition:
    text = path.read_text(encoding="utf-8", errors="replace")
    fields, body = _split_frontmatter(text)
    name = str(fields.get("name") or path.parent.name).strip()
    if not name:
        raise SkillParseError(f"Skill at {path} has no name")
    meta = SkillMeta(
        name=name,
        description=str(fields.get("description") or ""),
        enabled=_coerce_bool(fields.get("enabled"), default=True),
        triggers=[str(item).strip() for item in _coerce_list(fields.get("triggers")) if str(item).strip()],
        scope=scope,
    )
    return SkillDefinition(meta=meta, path=path.resolve(), body=body.strip())


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text

    end_index = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        raise SkillParseError("Unclosed frontmatter")

    frontmatter = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1:])
    return _parse_frontmatter(frontmatter), body


def _parse_frontmatter(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    current_key: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_key:
            current = result.setdefault(current_key, [])
            if isinstance(current, list):
                current.append(_parse_scalar(stripped[2:].strip()))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        value = value.strip()
        if not current_key:
            continue
        if value == "":
            result[current_key] = []
        else:
            result[current_key] = _parse_scalar(value)
    return result


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    lower = value.lower()
    if lower in {"true", "yes", "on"}:
        return True
    if lower in {"false", "no", "off"}:
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    return value


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"false", "no", "off", "0"}


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
