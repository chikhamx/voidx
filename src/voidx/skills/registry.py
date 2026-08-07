"""Local skill discovery from SKILL.md files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from voidx.platform.paths import voidx_global_skills_dir, voidx_workspace_skills_dir
from voidx.skills.schema import SkillDefinition, SkillMeta, SkillScope

SKILL_FILENAME = "SKILL.md"
DEFAULT_BUNDLED_DIR = Path(__file__).resolve().parent / "bundled"
SKILL_NAME_RE = re.compile(r"^(?=.{1,64}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


@dataclass
class _ParsedSkillCacheEntry:
    mtime_ns: int
    size: int
    skill: SkillDefinition


_PARSED_SKILL_CACHE: dict[tuple[str, SkillScope], _ParsedSkillCacheEntry] = {}


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
        self.bundled_dir = bundled_dir or DEFAULT_BUNDLED_DIR
        self.global_dir = global_dir or voidx_global_skills_dir()
        self.project_dir = project_dir or voidx_workspace_skills_dir(self.workspace)
        self._cache: list[SkillDefinition] | None = None
        self._cache_signature: tuple[tuple[str, str, int, int], ...] | None = None

    def discover(self) -> list[SkillDefinition]:
        signature = self._discover_signature()
        if self._cache is not None and self._cache_signature == signature:
            return self._cache
        skills: dict[str, SkillDefinition] = {}
        for scope, root in (
            ("bundled", self.bundled_dir),
            ("global", self.global_dir),
            ("project", self.project_dir),
        ):
            for skill in self._discover_root(root, scope):
                skills[normalize_skill_name(skill.name)] = skill
        self._cache = sorted(skills.values(), key=lambda item: item.name)
        self._cache_signature = signature
        return self._cache

    def invalidate(self) -> None:
        self._cache = None
        self._cache_signature = None

    def create_skill(
        self,
        name: str,
        description: str,
        body: str,
        *,
        scope: Literal["project", "global"] = "project",
    ) -> Path | None:
        """Create a SKILL.md file. Returns the path, or None if it already exists.

        Security: name is validated against SKILL_NAME_RE on the first line.
        This is the sole path-escape defense since sandbox skips the check
        (skill create has no file_path arg). Must not rely on tool-layer validation.
        """
        if not SKILL_NAME_RE.match(name):
            raise ValueError(
                f"Invalid skill name '{name}': must be 1-64 chars, lowercase "
                f"alphanumeric with hyphens, not starting/ending with a hyphen."
            )

        root = self.project_dir if scope == "project" else self.global_dir
        path = root / name / SKILL_FILENAME
        if path.exists():
            return None

        path.parent.mkdir(parents=True, exist_ok=True)
        frontmatter = (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "enabled: true\n"
            "---\n\n"
        )
        path.write_text(frontmatter + body, encoding="utf-8")
        self.invalidate()
        return path


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

    def _discover_signature(self) -> tuple[tuple[str, str, int, int], ...]:
        entries: list[tuple[str, str, int, int]] = []
        for scope, root in (
            ("bundled", self.bundled_dir),
            ("global", self.global_dir),
            ("project", self.project_dir),
        ):
            if not root.exists() or not root.is_dir():
                continue
            for skill_file in sorted(root.glob(f"*/{SKILL_FILENAME}")):
                try:
                    stat = skill_file.stat()
                except OSError:
                    continue
                entries.append((scope, str(skill_file.resolve()), stat.st_mtime_ns, stat.st_size))
        return tuple(entries)


def parse_skill_file(path: Path, *, scope: SkillScope) -> SkillDefinition:
    resolved = path.resolve()
    stat = resolved.stat()
    key = (str(resolved), scope)
    cached = _PARSED_SKILL_CACHE.get(key)
    if cached is not None and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
        return cached.skill.model_copy(deep=True)

    text = resolved.read_text(encoding="utf-8", errors="replace")
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
    skill = SkillDefinition(meta=meta, path=resolved, body=body.strip())
    _PARSED_SKILL_CACHE[key] = _ParsedSkillCacheEntry(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        skill=skill,
    )
    return skill.model_copy(deep=True)


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
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if stripped.startswith("- ") and result:
            last_key = next(reversed(result))
            current = result[last_key]
            if isinstance(current, list):
                current.append(_parse_scalar(stripped[2:].strip()))
            index += 1
            continue
        if ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            index += 1
            continue
        if value in ("", ">", "|"):
            collected, index = _collect_block(lines, index + 1, folded=(value == ">"))
            if value == "" and not collected:
                result[key] = []
            elif value == "":
                result[key] = collected[0] if len(collected) == 1 else collected
            else:
                result[key] = collected
        else:
            result[key] = _parse_scalar(value)
            index += 1
    return result


def _collect_block(lines: list[str], start: int, *, folded: bool = False) -> tuple[str, int]:
    parts: list[str] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.startswith(" ") and not line.startswith("\t") and line.strip():
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            break
        if not stripped:
            index += 1
            continue
        parts.append(stripped)
        index += 1
    text = " ".join(parts) if folded else "\n".join(parts)
    return text, index


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
