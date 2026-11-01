from graphlib import TopologicalSorter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PolicyError(ValueError):
    """A deterministic execution policy rejected an operation."""


class ModelBudgetExceeded(PolicyError):
    """No additional logical model call is authorized for this revision."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Task(StrictModel):
    id: str
    description: str
    stage: Literal["design", "implementation", "validation", "documentation"]
    depends_on: list[str]


class Plan(StrictModel):
    objective: str
    architecture: str
    assumptions: list[str]
    questions: list[str]
    acceptance_criteria: list[str]
    risks: list[str]
    tasks: list[Task]

    @model_validator(mode="after")
    def validate_graph(self):
        graph = {task.id: set(task.depends_on) for task in self.tasks}
        if len(graph) != len(self.tasks):
            raise ValueError("Task IDs must be unique")
        if any(dep not in graph for deps in graph.values() for dep in deps):
            raise ValueError("Every dependency must name an existing task")
        list(TopologicalSorter(graph).static_order())
        if not any(task.stage == "implementation" for task in self.tasks):
            raise ValueError("At least one implementation task is required")
        return self


class FileChange(StrictModel):
    path: str
    content: str


class Patch(StrictModel):
    rationale: str
    files: list[FileChange]


class Review(StrictModel):
    passed: bool
    findings: list[str]


class RunCreate(StrictModel):
    requirement: str = Field(min_length=10, max_length=8000)
    scenario: Literal["greenfield", "brownfield", "ambiguous"] = "brownfield"
    mode: Literal["demo", "live"] = "demo"


class ResumeRequest(StrictModel):
    revision: int = Field(ge=1)
    answer: str = Field(min_length=1, max_length=4000)


class ApprovalRequest(StrictModel):
    revision: int = Field(ge=1)
    candidate_hash: str = Field(pattern="^[a-f0-9]{64}$")
    approved: bool
    reviewer: str = Field(min_length=1, max_length=100)


class ReplanRequest(StrictModel):
    revision: int = Field(ge=1)
    requirement: str = Field(min_length=10, max_length=8000)
