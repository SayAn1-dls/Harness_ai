from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    RECEIVED = "RECEIVED"
    ANALYZING = "ANALYZING"
    CONTEXT_BUILDING = "CONTEXT_BUILDING"
    RULE_RESOLUTION = "RULE_RESOLUTION"
    IMPACT_ANALYSIS = "IMPACT_ANALYSIS"
    PLANNING = "PLANNING"
    PLAN_VALIDATION = "PLAN_VALIDATION"
    READY_TO_EXECUTE = "READY_TO_EXECUTE"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    JUDGING = "JUDGING"
    FAILED = "FAILED"
    DIAGNOSING = "DIAGNOSING"
    CONTEXT_UPDATE = "CONTEXT_UPDATE"
    REPLANNING = "REPLANNING"
    VERIFIED = "VERIFIED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    PR_READY = "PR_READY"
    STOPPED = "STOPPED"
    ESCALATED = "ESCALATED"


SUCCESS_STATES = {TaskStatus.VERIFIED, TaskStatus.HUMAN_REVIEW, TaskStatus.PR_READY}
FAILURE_LOOP_STATES = {
    TaskStatus.FAILED,
    TaskStatus.DIAGNOSING,
    TaskStatus.CONTEXT_UPDATE,
    TaskStatus.REPLANNING,
    TaskStatus.IMPLEMENTING,
}


class Lane(str, Enum):
    A = "trivial"
    B = "normal"
    C = "complex"


class Severity(str, Enum):
    BLOCKER = "BLOCKER"
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class FindingCategory(str, Enum):
    CORRECTNESS = "CORRECTNESS"
    SECURITY = "SECURITY"
    PERFORMANCE = "PERFORMANCE"
    ARCHITECTURE = "ARCHITECTURE"
    MAINTAINABILITY = "MAINTAINABILITY"
    RELIABILITY = "RELIABILITY"
    COMPATIBILITY = "COMPATIBILITY"
    TESTING = "TESTING"
    REQUIREMENTS = "REQUIREMENTS"
    RULES = "RULES"
    ACCESSIBILITY = "ACCESSIBILITY"
    OBSERVABILITY = "OBSERVABILITY"


class EvidenceLevel(int, Enum):
    LLM_ONLY = 1
    DATAFLOW = 2
    RULE_VIOLATION = 3
    STATIC_ANALYZER = 4
    REPRODUCIBLE_TEST = 5


class FailureClass(str, Enum):
    CODE_BUG = "CODE_BUG"
    TEST_BUG = "TEST_BUG"
    ENVIRONMENT_BUG = "ENVIRONMENT_BUG"
    DEPENDENCY_BUG = "DEPENDENCY_BUG"
    WRONG_ASSUMPTION = "WRONG_ASSUMPTION"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    TOOL_FAILURE = "TOOL_FAILURE"
    UNKNOWN = "UNKNOWN"


class RecoveryAction(str, Enum):
    PATCH = "patch"
    RESEARCH = "research"
    REPLAN = "re-plan"
    ROLLBACK = "rollback"
    RETRY = "retry"
    ESCALATE = "escalate"


class JudgeDecision(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ITERATE = "ITERATE"
    ESCALATE = "ESCALATE"


class GitHubPermission(str, Enum):
    READ_REPO = "READ_REPO"
    READ_ISSUES = "READ_ISSUES"
    READ_PRS = "READ_PRS"
    CREATE_BRANCH = "CREATE_BRANCH"
    WRITE_BRANCH = "WRITE_BRANCH"
    CREATE_PR = "CREATE_PR"
    COMMENT_PR = "COMMENT_PR"
    MERGE_PR = "MERGE_PR"
    DELETE_BRANCH = "DELETE_BRANCH"


class Budget(BaseModel):
    tokens: int = 300_000
    tokens_used: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    runtime_seconds: int = 1800
    runtime_used_seconds: float = 0
    max_iterations: int = 5
    max_parallel_agents: int = 3
    max_agent_depth: int = 1
    max_search_calls: int = 60
    max_file_reads: int = 100
    max_shell_calls: int = 60
    max_full_test_runs: int = 20
    search_calls: int = 0
    file_reads: int = 0
    shell_calls: int = 0
    full_test_runs: int = 0

    def remaining_tokens(self) -> int:
        return max(0, self.tokens - self.tokens_used)

    def record_tokens(self, n: int) -> None:
        self.tokens_used += max(0, n)

    def exhausted(self) -> bool:
        return self.tokens_used >= self.tokens or self.runtime_used_seconds >= self.runtime_seconds


class AcceptanceCriterion(BaseModel):
    id: str
    text: str
    satisfied: bool = False
    evidence: list[str] = Field(default_factory=list)


class Rule(BaseModel):
    id: str
    scope: str = "**"
    severity: Severity = Severity.MEDIUM
    instruction: str
    source: str
    verification: str = ""
    conflicts_with: list[str] = Field(default_factory=list)


class Finding(BaseModel):
    finding_id: str
    category: FindingCategory
    severity: Severity
    confidence: float
    file: str | None = None
    line: int | None = None
    description: str
    evidence: list[str] = Field(default_factory=list)
    evidence_level: EvidenceLevel = EvidenceLevel.LLM_ONLY
    requirement: str | None = None
    rule: str | None = None
    reproduction: str | None = None
    suggested_fix: str | None = None
    agent: str
    iteration: int = 0

    def priority(self, impact: float = 1.0, requirement_relevance: float = 1.0) -> float:
        sev_w = {
            Severity.BLOCKER: 6,
            Severity.CRITICAL: 5,
            Severity.HIGH: 4,
            Severity.MEDIUM: 3,
            Severity.LOW: 2,
            Severity.INFO: 1,
        }[self.severity]
        return sev_w * self.confidence * impact * requirement_relevance * (self.evidence_level / 5)


class PlanStep(BaseModel):
    order: int
    action: str
    files: list[str] = Field(default_factory=list)
    verification: str = ""


class ImplementationPlan(BaseModel):
    version: int = 1
    steps: list[PlanStep] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    forbidden_files: list[str] = Field(default_factory=list)
    rollback: str = "git reset --hard HEAD on the task branch"


class ContextPacket(BaseModel):
    path: str
    reason: str
    excerpt: str
    token_estimate: int = 0


class ContextSnapshot(BaseModel):
    snapshot_id: str
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    score: float = 0
    files: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    packets: list[ContextPacket] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class IntakeResult(BaseModel):
    problem: str
    intent: str
    requirements: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    blocking_ambiguities: list[str] = Field(default_factory=list)
    risk: str = "low"
    score: float = 0


class ImpactReport(BaseModel):
    affected_files: list[str] = Field(default_factory=list)
    potentially_affected_files: list[str] = Field(default_factory=list)
    callers: dict[str, list[str]] = Field(default_factory=dict)
    callees: dict[str, list[str]] = Field(default_factory=dict)
    tests: list[str] = Field(default_factory=list)
    regression_risks: list[str] = Field(default_factory=list)
    score: float = 0


class VerificationResult(BaseModel):
    passed: bool = False
    commands: list[dict[str, Any]] = Field(default_factory=list)
    acceptance: list[AcceptanceCriterion] = Field(default_factory=list)
    coverage_notes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    score: float = 0


class JudgeResult(BaseModel):
    decision: JudgeDecision
    confidence: float
    blocking_findings: list[Finding] = Field(default_factory=list)
    non_blocking_findings: list[Finding] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    remaining_risk: list[str] = Field(default_factory=list)
    global_score: float = 0


class IterationRecord(BaseModel):
    iteration: int
    previous_state: str
    new_observations: list[str] = Field(default_factory=list)
    failed_assumptions: list[str] = Field(default_factory=list)
    new_context: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    remaining_requirements: list[str] = Field(default_factory=list)
    new_plan: list[str] = Field(default_factory=list)
    verification_delta: list[str] = Field(default_factory=list)
    failure_class: FailureClass | None = None
    recovery_action: RecoveryAction | None = None


class AgentInvocation(BaseModel):
    name: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    tokens: int = 0
    tool_calls: int = 0
    status: str = "running"
    artifact: str | None = None


class TaskState(BaseModel):
    task_id: str
    repository: str
    workspace: str
    base_commit: str = ""
    branch: str = ""
    objective: str
    issue_body: str = ""
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    risk: str = "low"
    lane: Lane = Lane.B
    context_snapshot: str | None = None
    applicable_rules: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    potentially_affected_files: list[str] = Field(default_factory=list)
    plan_version: int = 0
    iteration: int = 0
    history: list[IterationRecord] = Field(default_factory=list)
    agents: list[AgentInvocation] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    baseline: dict[str, Any] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)
    status: TaskStatus = TaskStatus.RECEIVED
    current_agent: str | None = None
    last_failure: str | None = None
    repeated_failure_count: int = 0
    human_approval: bool = False
    pr_url: str | None = None
    stop_reason: str | None = None
    global_score: float = 0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch(self) -> None:
        self.updated_at = utcnow()


class Event(BaseModel):
    ts: datetime = Field(default_factory=utcnow)
    event: str
    task: str
    agent: str | None = None
    result: str | None = None
    status: str | None = None
    artifacts: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class AgentContract(BaseModel):
    name: str
    purpose: str
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    permissions: dict[str, bool] = Field(default_factory=dict)
    model: str = "default"
    temperature: float = 0.1
    max_tokens: int = 4096
    max_tool_calls: int = 20
    timeout: int = 300
    dependencies: list[str] = Field(default_factory=list)
    success_conditions: list[str] = Field(default_factory=list)
    failure_conditions: list[str] = Field(default_factory=list)
    escalation_conditions: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    agent: str
    success: bool
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    tokens: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


class StopCondition(str, Enum):
    VERIFIED_SUCCESS = "verified_success"
    MAX_ITERATIONS = "max_iterations"
    BUDGET_EXCEEDED = "budget_exceeded"
    SAME_FAILURE_REPEATED = "same_failure_repeated"
    CONFIDENCE_DECREASING = "confidence_decreasing"
    REQUIRED_PERMISSION_MISSING = "required_permission_missing"
    UNSAFE_ACTION_REQUIRED = "unsafe_action_required"
    HUMAN_STOP = "human_stop"
