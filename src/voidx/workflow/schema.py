"""Structured workflow graph definitions."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class NodeGate(BaseModel):
    denied_tools: tuple[str, ...] = Field(default_factory=tuple)
    allowed_paths: tuple[str, ...] = Field(default_factory=tuple)
    description: str = ""
    required_before_transition: str = ""


class WorkflowStep(BaseModel):
    order: int
    action: str
    description: str = ""


class NodeIO(BaseModel):
    input: dict[str, str]
    output: dict[str, str]


class NodeSubworkflow(BaseModel):
    name: str
    description: str = ""
    steps: list[WorkflowStep]
    exit_condition: str

    @field_validator("name", "exit_condition")
    @classmethod
    def _require_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("node subworkflow fields cannot be empty")
        return normalized


class WorkflowNode(BaseModel):
    name: str
    goal: str
    description: str
    io: NodeIO
    persona: str
    gate: NodeGate = Field(default_factory=NodeGate)
    workflow: list[WorkflowStep] = Field(default_factory=list)
    subworkflow: NodeSubworkflow | None = None
    rules: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)

    @field_validator("name", "goal", "description", "persona")
    @classmethod
    def _normalize_text(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"workflow node {info.field_name} cannot be empty")
        if info.field_name == "name":
            return normalized.lower()
        return normalized


class Edge(BaseModel):
    source: str
    target: str
    condition: str
    label: str = ""
    description: str = ""

    @field_validator("source", "target", "condition")
    @classmethod
    def _normalize_token(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("workflow edge fields cannot be empty")
        return normalized


class TerminalExit(BaseModel):
    condition: str = "done"
    label: str = "end"
    description: str = "end the current workflow node without activating a successor"

    @field_validator("condition", "label", "description")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("workflow terminal exit fields cannot be empty")
        return normalized


class GoalEntry(BaseModel):
    goal_type: str
    nodes: list[str]
    reason: str

    @field_validator("goal_type")
    @classmethod
    def _normalize_goal_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("workflow goal type cannot be empty")
        return normalized

    @field_validator("nodes")
    @classmethod
    def _normalize_nodes(cls, values: list[str]) -> list[str]:
        return [item.strip().lower() for item in values if item.strip()]


class WorkflowDAG(BaseModel):
    name: str
    nodes: dict[str, WorkflowNode]
    edges: list[Edge] = Field(default_factory=list)
    goal_map: list[GoalEntry] = Field(default_factory=list)
    terminal_exit: TerminalExit = Field(default_factory=TerminalExit)

    @field_validator("nodes", mode="before")
    @classmethod
    def _coerce_nodes(cls, value):
        if isinstance(value, dict):
            return value
        return {node.name: node for node in value or []}

    @model_validator(mode="after")
    def _validate_references(self) -> "WorkflowDAG":
        names = set(self.nodes)
        for edge in self.edges:
            if edge.source not in names:
                raise ValueError(f"workflow edge references unknown source: {edge.source}")
            if edge.target not in names:
                raise ValueError(f"workflow edge references unknown target: {edge.target}")
        for entry in self.goal_map:
            missing = [node for node in entry.nodes if node not in names]
            if missing:
                raise ValueError(f"workflow goal {entry.goal_type} references unknown nodes: {missing}")
        return self

    def edges_from(self, name: str) -> list[Edge]:
        source = name.strip().lower()
        return [edge for edge in self.edges if edge.source == source]

    def edges_to(self, name: str) -> list[Edge]:
        target = name.strip().lower()
        return [edge for edge in self.edges if edge.target == target]

    def entry_nodes(self, goal_type: str) -> list[str]:
        entry = self.entry(goal_type)
        return list(entry.nodes) if entry else []

    def entry(self, goal_type: str) -> GoalEntry | None:
        normalized = goal_type.strip().lower()
        for entry in self.goal_map:
            if entry.goal_type == normalized:
                return entry
        return None

    def gate_for(self, name: str) -> NodeGate | None:
        node = self.nodes.get(name.strip().lower())
        return node.gate if node else None

    def all_denied_tools(self, active_nodes: list[str]) -> set[str]:
        denied: set[str] = set()
        for name in active_nodes:
            gate = self.gate_for(name)
            if gate:
                denied.update(gate.denied_tools)
        return denied

    def is_terminal_condition(self, condition: str) -> bool:
        return condition.strip().lower() == self.terminal_exit.condition

    def terminal_exit_summary(self) -> str:
        return f"{self.terminal_exit.condition} -> {self.terminal_exit.label}"
