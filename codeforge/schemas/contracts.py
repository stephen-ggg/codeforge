"""
schemas/contracts.py — Runtime source of truth for all codeforge data shapes.

Translated from the TypeScript interfaces in agent-contracts_v6.md.
The TypeScript interfaces are the design document; these Pydantic v2 models are the
implementation. Both must stay in sync — a contract change requires updating both.

This module imports nothing from within codeforge. It is a leaf node.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar, Union
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Pydantic v2 generic support
# ---------------------------------------------------------------------------
# In Pydantic v2, Generic models are declared with Generic[T] directly on BaseModel.
# We use a simple T typevar throughout.

T = TypeVar("T")


# ===========================================================================
# Part 1 — Shared types
# ===========================================================================

# ---------------------------------------------------------------------------
# Literal string union types (replicate TS `type` aliases)
# ---------------------------------------------------------------------------

AgentId = Literal[
    "requirements_analyst",
    "architecture_designer",
    "coder",
    "code_reviewer",
    "security_reviewer",
    "test_designer",
    "test_analyst",
    "orchestrator",
]

# Used in log events and access control — includes mechanical components
# that consume artifacts but are not LLM agents.
LogActor = Literal[
    "requirements_analyst",
    "architecture_designer",
    "coder",
    "code_reviewer",
    "security_reviewer",
    "test_designer",
    "test_analyst",
    "orchestrator",
    "commit_writer",
    "test_runner",              # mechanical; consumes code_artifact and test_suite
]

ArtifactType = Literal[
    "requirements_doc",
    "architecture_doc",
    "interface_manifest",
    "code_artifact",
    "module_interfaces",
    "review_report",
    "security_report",
    "test_suite",
    "test_results",
    "test_analysis",
]

ProjectStateDocument = Literal[
    "requirements_history",
    "architecture",
    "decisions_log",
    "feature_registry",
    "assumptions_log",
    "tech_stack",
    "test_coverage_map",
]

StateWriteTarget = Literal[
    "requirements_history",
    "architecture",
    "decisions_log",
    "feature_registry",
    "assumptions_log",
    "tech_stack",
    "test_coverage_map",
    "codeforge_repo",
    "source_repo",
]

WriteSource = Literal["agent_output", "human_decision", "codeforge_success"]

HumanInteractionKind = Literal[
    "clarification_questions",
    "requirements_confirm",
    "tech_decision_confirm",
    "escalation_notify",
    "escalation_response",
]

EscalationReason = Literal[
    "max_retries_exceeded",
    "global_ceiling_exceeded",
    "malformed_output",
    "output_truncated",
    "block_flag",
    "low_confidence",
    "human_required",
    "commit_failure",
    "schema_version_mismatch",
]

ReentryState = Literal[
    "requirements_clarification",
    "architecture",
    "coding",
    "code_review",
    "test_design",
    "test_execution",
    "commit",
]

GateRule = Literal[
    "schema_valid",
    "confidence_threshold",
    "block_flag_present",
    "warn_flag_present",
    "verdict_has_findings",
    "ac_coverage_must",
    "arch_criteria_coverage",
    "coverage_map_valid",
    "unique_test_paths",
    "requirements_txt_present",
    "package_json_dev_script",
    "module_interfaces_no_bodies",
    "schema_version_match",
    "global_ceiling",
    "locked_tech_decision",
    "spec_gap_has_description",
    "test_bug_context_clean",
    "code_bug_context_clean",
    "clarification_answers_complete",
]

RoutingDecision = Literal[
    "invoke_agent",
    "retry_same_agent",
    "re_prompt_same_agent",
    "await_human",
    "escalate",
    "succeed",
    "terminal",
]

HandoffInvocationType = Literal["first", "retry", "reentry", "re_prompt"]

EventType = Literal["handoff", "gate", "routing", "state_write", "human_interaction", "tool_call"]

CodeforgeStatus = Literal[
    "running",
    "awaiting_human",
    "succeeded",
    "failed_escalated",
    "failed_terminal",
]


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------

class Assumption(BaseModel):
    """An assumption made by an agent, optionally recorded to assumptions_log."""
    id: str = Field(..., description="e.g. 'ASSUME-001' — stable across runs for dedup")
    description: str
    impact: Literal["low", "medium", "high"]
    record: bool = Field(..., description="true → orchestrator appends to assumptions_log")


class Flag(BaseModel):
    """A flag raised by an agent. 'block' halts the pipeline immediately."""
    id: str
    description: str
    severity: Literal["warn", "block"]
    suggested_action: str | None = None


class AgentOutput(BaseModel, Generic[T]):
    """
    Universal agent output envelope. Every agent output — no exceptions.
    """
    output: T
    assumptions_made: list[Assumption]
    confidence: float = Field(..., ge=0.0, le=1.0)
    unresolved_flags: list[Flag]


# ---------------------------------------------------------------------------
# Artifact metadata (stamped by orchestrator, never by agent)
# ---------------------------------------------------------------------------

class ArtifactMeta(BaseModel):
    artifact_id: str                        # UUID
    artifact_type: ArtifactType
    produced_by: AgentId
    run_id: str
    codeforge_version: str
    schema_version: str                     # semver
    created_at: str                         # ISO 8601
    content_hash: str                       # SHA-256 of serialised output field
    # LogActor (not AgentId) — test_runner and commit_writer are mechanical consumers
    allowed_consumers: list[LogActor]
    forbidden_consumers: list[LogActor]


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class AccessEvent(BaseModel):
    artifact_id: str
    requesting_agent: LogActor              # LogActor — mechanical components also make access requests
    decision: Literal["allow", "deny"]
    reason_code: str
    assembly_id: str
    timestamp: str                          # ISO 8601


# ---------------------------------------------------------------------------
# Retry counters
# ---------------------------------------------------------------------------

class RetryCounters(BaseModel):
    code_review_loop: int = 0
    security_review_loop: int = 0
    test_loop: int = 0
    coder_validation: int = 0
    architecture_validation: int = 0
    infrastructure: int = 0
    environment_repair: int = 0   # auto-recovery: re-invoke test_designer to fix test infra/deps
    dependency_repair: int = 0    # auto-recovery: re-invoke coder to fix runtime requirements.txt
    low_confidence_reprompt: int = 0  # one-shot re-prompt of an agent before low-confidence escalate
    malformed_output: int = 0
    codeforge_state_commit: int = 0
    source_code_commit: int = 0


# ---------------------------------------------------------------------------
# Re-prompt context (discriminated union)
# ---------------------------------------------------------------------------

class ValidationError(BaseModel):
    """Details of a single structural validation failure."""
    field_path: str
    error_type: Literal[
        "missing_required",
        "wrong_type",
        "invalid_enum_value",
        "truncated",
        "empty_response",
    ]
    expected: str | None = None
    received: str | None = None             # sanitised — never raw malformed content


class MalformedOutputRePrompt(BaseModel):
    """Structural-stage re-prompt: structural validation failure."""
    reason: Literal["malformed_output"] = "malformed_output"
    original_input_ref: str                 # assembly_id
    validation_errors: list[ValidationError]
    attempt_number: int
    max_attempts: int                       # from config: malformed_output_retries


class ContractViolationRePrompt(BaseModel):
    """
    Contract-stage re-prompt: contract rule failure.
    Exactly one payload field is populated per the Part 4 mapping.
    """
    reason: Literal["contract_violation"] = "contract_violation"
    rule: GateRule
    original_input_ref: str
    detail: str                             # deterministic, template-generated
    attempt_number: int
    max_attempts: int

    # Rule-specific payloads — exactly one populated
    uncovered_ac_ids: list[str] | None = None
    unaddressed_ac_ids: list[str] | None = None
    mismatched_criterion_ids: list[str] | None = None
    findings_missing_for_verdict: str | None = None
    missing_spec_gap_for: list[str] | None = None
    missing_requirements_txt: bool | None = None
    duplicate_paths: list[str] | None = None
    leaking_signatures: list[str] | None = None


class LowConfidenceRePrompt(BaseModel):
    """Policy-stage re-prompt: agent confidence below threshold — one nudge before escalating."""
    reason: Literal["low_confidence"] = "low_confidence"
    prior_confidence: float
    threshold: float
    attempt_number: int
    max_attempts: int                       # from config: low_confidence_reprompt


# Union type for re-prompt context
RePromptContext = Union[MalformedOutputRePrompt, ContractViolationRePrompt, LowConfidenceRePrompt]


# ---------------------------------------------------------------------------
# Escalation and human resolution
# ---------------------------------------------------------------------------

class ReentryDirective(BaseModel):
    reentry_state: ReentryState
    counter_resets: list[str]               # keys from RetryCounters
    reset_global_ceiling: bool = False


class EscalationResolution(BaseModel):
    outcome: Literal["approved", "rejected", "modified"]
    reentry_directive: ReentryDirective | None = None
    change_summary: str | None = None       # required when outcome == "modified"
    human_notes: str


class EscalationEvent(BaseModel):
    escalation_id: str
    triggered_at: str                       # ISO 8601
    resolved_at: str | None = None
    reason: EscalationReason
    agent_output_ref: str                   # artifact_id that triggered this
    resolved: bool
    resolution: EscalationResolution | None = None
    suggested_reentry_state: ReentryState | None = None  # phase that was running when escalation fired


# ===========================================================================
# Part 2 — Agent input/output contracts
# ===========================================================================

# ---------------------------------------------------------------------------
# 2.1 Requirements Analyst
# ---------------------------------------------------------------------------

class ClarificationQuestion(BaseModel):
    id: str                                 # stable — used to match answer
    question: str
    why_blocking: str
    options: list[str] | None = None


class ClarificationAnswer(BaseModel):
    question_id: str
    answer: str
    selected_option: str | None = None


class ClarificationExchange(BaseModel):
    round: int                              # 1-based
    questions: list[ClarificationQuestion]
    answers: list[ClarificationAnswer]      # one per question — orchestrator enforces


class AcceptanceCriterion(BaseModel):
    id: str                                 # e.g. "AC-001"
    description: str
    testable: bool
    priority: Literal["must", "should", "could"]


class DataContract(BaseModel):
    entity: str
    fields: list[dict[str, Any]]            # [{name, type, constraints}]
    relationships: list[str]


class RequirementsDoc(BaseModel):
    run_id: str
    run_mode: Literal["new_project", "continuation"]
    feature_title: str
    feature_description: str
    scope: dict[str, list[str]]             # {in_scope, explicitly_out_of_scope}
    acceptance_criteria: list[AcceptanceCriterion]
    data_contracts: list[DataContract]
    changes_from_prior: dict[str, Any] | None = None
    human_confirmed_decisions: list[str]


class TechDecision(BaseModel):
    id: str
    domain: str
    decision: str
    rationale: str
    locked: bool
    record: bool
    supersedes: str | None = None


class InterfaceSpec(BaseModel):
    name: str
    kind: Literal["http_endpoint", "function", "event", "queue_message", "db_schema"]
    owner_module: str
    contract: dict[str, Any]
    stability: Literal["stable", "experimental", "deprecated"]
    successor: str | None = None
    removal_run: str | None = None

    @model_validator(mode="after")
    def _function_contract_requires_module_and_symbol(self) -> "InterfaceSpec":
        """A `function` interface must locate its symbol via `module` + `symbol`.

        A single dotted path (e.g. `src.arithmetic.add`) is ambiguous: it could mean
        module `src.arithmetic` with symbol `add`, or a module `src.arithmetic.add` —
        and the coder and test_designer can read it differently, so the code the coder
        writes and the import the test_designer emits silently disagree. Splitting the
        location into the two fields removes the ambiguity, and rejecting a malformed
        contract here makes it fail at architecture validation rather than as an opaque
        pytest collection error several agent calls later. Other interface kinds keep
        their free-form contract.
        """
        if self.kind != "function":
            return self
        for field in ("module", "symbol"):
            value = self.contract.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"function interface '{self.name}' must define a non-empty "
                    f"contract.{field} (e.g. module='src.arithmetic', symbol='add'); "
                    f"got {value!r}"
                )
        return self


class RequirementsSummary(BaseModel):
    """
    Orchestrator-produced deterministic projection from JSON state documents.
    Not an LLM call.
    """
    completed_runs: list[dict[str, Any]]    # {run_id, feature_title, status, key_decisions}
    active_interfaces: list[InterfaceSpec]
    locked_tech_decisions: list[TechDecision]
    open_assumptions: list[Assumption]


class RequirementsAnalystInput(BaseModel):
    run_mode: Literal["new_project", "continuation"]
    human_brief: str
    project_state: dict[str, Any] | None = None  # typed fields per spec 2.1
    clarification_history: list[ClarificationExchange] = Field(default_factory=list)
    confirm_rejection: dict[str, str] | None = None  # {rejected_doc_ref, rejection_feedback}


class RequirementsNeedsClarification(BaseModel):
    status: Literal["needs_clarification"] = "needs_clarification"
    questions: list[ClarificationQuestion]


class RequirementsComplete(BaseModel):
    status: Literal["complete"] = "complete"
    requirements_doc: RequirementsDoc


# Both wrapped in AgentOutput[T]
RequirementsAnalystOutput = Union[
    AgentOutput[RequirementsNeedsClarification],
    AgentOutput[RequirementsComplete],
]


# ---------------------------------------------------------------------------
# 2.2 Architecture Designer
# ---------------------------------------------------------------------------

class ModuleSpec(BaseModel):
    name: str
    responsibility: str
    dependencies: list[str]
    exposes: list[str]
    consumes: list[str]


class DataFlowSpec(BaseModel):
    name: str
    from_: str = Field(..., alias="from")
    to: str
    via: str
    data_description: str

    model_config = {"populate_by_name": True}


class CriteriaCoverageEntry(BaseModel):
    criterion_id: str
    module_names: list[str]
    notes: str | None = None


class ArchitectureDoc(BaseModel):
    run_mode: Literal["new_project", "continuation"]
    modules: list[ModuleSpec]
    interfaces: list[InterfaceSpec]
    data_flow: list[DataFlowSpec]
    tech_decisions: list[TechDecision]
    criteria_coverage: list[CriteriaCoverageEntry]
    diff: dict[str, Any] | None = None


class SpecGapDescription(BaseModel):
    criterion_id: str
    test_case_id: str
    gap_description: str
    affected_interfaces: list[str]
    affected_data_contracts: list[str]


class ArchitectureDesignerInput(BaseModel):
    run_mode: Literal["new_project", "continuation"]
    requirements_doc: RequirementsDoc
    current_architecture_md: str | None = None
    tech_stack_md: str | None = None
    feature_registry_md: str | None = None
    spec_gap_context: SpecGapDescription | None = None


ArchitectureDesignerOutput = AgentOutput[ArchitectureDoc]


# ---------------------------------------------------------------------------
# 2.3 Coder
# ---------------------------------------------------------------------------

class ReviewFinding(BaseModel):
    id: str
    file: str
    line_range: tuple[int, int] | None = None
    category: Literal["correctness", "clarity", "spec_adherence", "interface_compliance"]
    severity: Literal["info", "warn", "error"]
    description: str
    suggested_fix: str | None = None


class SecurityFinding(BaseModel):
    id: str
    file: str
    line_range: tuple[int, int] | None = None
    category: Literal[
        "injection",
        "authentication",
        "authorisation",
        "secrets_exposure",
        "dependency_vulnerability",
        "input_validation",
        "data_exposure",
        "other",
    ]
    severity: Literal["info", "warn", "critical"]
    cwe: str | None = None
    description: str
    recommended_fix: str


class CodeBugFinding(BaseModel):
    """
    Whitelist projection of FailureAnalysis + TestResult for the test_code_bug path.
    assertion text, stack_trace, error_message, evidence, and test code are NEVER included.
    """
    id: str
    criterion_id: str
    failure_summary: str                    # behavioural terms only
    failed_assertions: list[dict[str, str]] # [{expected, actual}] — no assertion source text


class CoderRetryContext(BaseModel):
    """
    Orchestrator-constructed projection. Firewall by projection — whitelist only.
    Gate: code_bug_context_clean.
    """
    retry_number: int
    max_retries: int
    trigger: Literal["code_review_fail", "security_review_fail", "test_code_bug"]
    review_findings: list[ReviewFinding] = Field(default_factory=list)
    security_findings: list[SecurityFinding] = Field(default_factory=list)
    code_bug_findings: list[CodeBugFinding] = Field(default_factory=list)


class CodeFixContext(BaseModel):
    """
    Populated on fail_code_bug re-entry from test phase.
    Contains only the AC ids implicated in the test failures. No test content.
    """
    flagged_criterion_ids: list[str]


class Edit(BaseModel):
    """A single surgical edit against an existing file.

    old_string must match exactly once in the target file. Used for
    change_type == "modified" in continuation runs so the coder patches existing
    files instead of rewriting them whole (which clobbers unrelated code).
    """
    old_string: str
    new_string: str

    @model_validator(mode="after")
    def old_string_non_empty(self) -> "Edit":
        # An empty old_string matches everywhere — str.replace would corrupt the
        # file. Reject it structurally so a bad edit never reaches apply time.
        if self.old_string == "":
            raise ValueError("Edit.old_string must be non-empty")
        if self.old_string == self.new_string:
            raise ValueError("Edit is a no-op (old_string == new_string)")
        return self


class ModuleImport(BaseModel):
    specifier: str
    named: list[str]
    default: str | None = None


class ModuleExport(BaseModel):
    name: str
    kind: Literal["function", "class", "interface", "type", "const", "enum"]
    signature: str


class ModuleFile(BaseModel):
    path: str
    imports: list[ModuleImport]
    exports: list[ModuleExport]
    env_vars_read: list[str]
    fs_path_patterns: list[str]


class ModuleInterfaces(BaseModel):
    files: list[ModuleFile]


class CodeFile(BaseModel):
    path: str
    content: str                            # full file body for new files (and the empty string when only `edits` apply)
    language: str
    change_type: Literal["new", "modified", "deleted"]
    change_reason: str | None = None
    edits: list[Edit] = Field(default_factory=list)  # surgical edits for change_type == "modified"

    @model_validator(mode="after")
    def edits_only_on_modified(self) -> "CodeFile":
        if self.edits and self.change_type != "modified":
            raise ValueError("edits are only valid when change_type == 'modified'")
        return self


class CoderInput(BaseModel):
    run_mode: Literal["new_project", "continuation"]
    requirements_doc: RequirementsDoc
    architecture_doc: ArchitectureDoc
    tech_stack_md: str | None = None
    existing_interfaces: list[InterfaceSpec] = Field(default_factory=list)
    retry_context: CoderRetryContext | None = None
    code_fix_context: CodeFixContext | None = None

    @model_validator(mode="after")
    def retry_and_fix_mutually_exclusive(self) -> "CoderInput":
        if self.retry_context is not None and self.code_fix_context is not None:
            raise ValueError("retry_context and code_fix_context are mutually exclusive")
        return self


class CodeArtifact(BaseModel):
    files: list[CodeFile]
    module_interfaces: ModuleInterfaces
    change_summary: str
    criteria_addressed: list[str]           # AC ids
    interface_changes: list[dict[str, Any]] # [{interface_name, change_type, breaking, description}]


CoderOutput = AgentOutput[CodeArtifact]


# ---------------------------------------------------------------------------
# 2.4 Code Reviewer
# ---------------------------------------------------------------------------

class ReviewReport(BaseModel):
    verdict: Literal["pass", "pass_with_notes", "fail"]
    summary: str
    findings: list[ReviewFinding]
    criteria_coverage: list[dict[str, Any]] # [{criterion_id, addressed, notes}]


class CodeReviewerInput(BaseModel):
    requirements_doc: RequirementsDoc
    architecture_doc: ArchitectureDoc
    decisions_log_md: str
    code_artifact: CodeArtifact


CodeReviewerOutput = AgentOutput[ReviewReport]


# ---------------------------------------------------------------------------
# 2.5 Security Reviewer
# ---------------------------------------------------------------------------

class SecurityChecklistItem(BaseModel):
    category: str
    assessed: bool
    result: Literal["clean", "finding_raised", "not_applicable"]
    notes: str


class SecurityReport(BaseModel):
    verdict: Literal["pass", "pass_with_notes", "fail"]
    summary: str
    findings: list[SecurityFinding]
    checklist: list[SecurityChecklistItem]


class SecurityReviewerInput(BaseModel):
    tech_stack_md: str
    requirements_doc: RequirementsDoc
    code_artifact: CodeArtifact


SecurityReviewerOutput = AgentOutput[SecurityReport]


# ---------------------------------------------------------------------------
# 2.6 Test Designer
# ---------------------------------------------------------------------------

class InterfaceManifest(BaseModel):
    """
    Orchestrator projection: InterfaceSpec[] from architecture_doc +
    DataContract[] from requirements_doc. No module rationale, no implementation detail.
    """
    interfaces: list[InterfaceSpec]
    data_contracts: list[DataContract]
    acceptance_criteria: list[AcceptanceCriterion]


class TestCase(BaseModel):
    id: str
    title: str
    criterion_ids: list[str]
    type: Literal["unit", "integration", "contract", "e2e"]
    description: str
    code: list[CodeFile]
    explicitly_not_testing: list[str]


class TestSuite(BaseModel):
    test_cases: list[TestCase]
    test_infrastructure: list[CodeFile]     # fixtures, mocks, helpers — not test logic
    coverage_map: list[dict[str, Any]]      # [{criterion_id, test_case_ids}]


class TestDesignerRetryContext(BaseModel):
    """
    Built by whitelist projection — gate: test_bug_context_clean.
    Only test_case, root_cause_hypothesis, recommended_action are copied.
    No TestResult content ever included.
    """
    retry_number: int
    max_retries: int
    failed_test_cases: list[dict[str, Any]] # [{test_case, root_cause_hypothesis, recommended_action}]


class TestDesignerInput(BaseModel):
    requirements_doc: RequirementsDoc
    interface_manifest: InterfaceManifest
    test_coverage_map_md: str
    feature_registry_md: str
    retry_context: TestDesignerRetryContext | None = None
    code_fix_context: CodeFixContext | None = None

    @model_validator(mode="after")
    def retry_and_fix_mutually_exclusive(self) -> "TestDesignerInput":
        if self.retry_context is not None and self.code_fix_context is not None:
            raise ValueError("retry_context and code_fix_context are mutually exclusive")
        return self


TestDesignerOutput = AgentOutput[TestSuite]


# ---------------------------------------------------------------------------
# 2.7 Test Runner (mechanical — no LLM)
# ---------------------------------------------------------------------------

class TestRunnerInput(BaseModel):
    test_suite: TestSuite
    code_artifact: CodeArtifact
    run_config: dict[str, Any]              # {timeout_seconds, environment_vars, sandbox_image}
    run_mode: Literal["new_project", "continuation"] = "new_project"
    source_root: str | None = None         # existing source repo root; staged before deltas in continuation


class FailedAssertion(BaseModel):
    assertion: str                          # test source text — NEVER copied into coder context
    expected: str
    actual: str


class TestResult(BaseModel):
    test_case_id: str
    status: Literal["pass", "fail", "error", "skipped"]
    duration_ms: float
    error_message: str | None = None
    stack_trace: str | None = None
    failed_assertions: list[FailedAssertion] | None = None


# Deterministic classification of an overall_status="error" — which sandbox step failed.
# Drives auto-recovery routing (which agent owns the fix) far more reliably than the
# test_analyst's free-text root_cause_hypothesis.
TestRunnerErrorPhase = Literal[
    "missing_requirements_txt",     # code_artifact had no dependency manifest      → coder
    "runtime_dep_install_failed",   # runtime dependency install failed             → coder
    "build_failed",                 # compile/type-check gate failed (e.g. tsc)     → coder
    "test_dep_install_failed",      # test-only dependency install failed           → test_designer
    "no_results_report",            # runner produced no JUnit XML report           → test_designer
    "results_parse_error",          # results.xml was not valid XML                 → (transient)
    "pytest_exit_error",            # test command exited with a non-0/1 code       → test_designer
]


class TestRunnerResults(BaseModel):
    run_id: str
    started_at: str                         # ISO 8601
    completed_at: str
    overall_status: Literal["pass", "fail", "error"]
    test_results: list[TestResult]
    environment_info: dict[str, str]        # {sandbox_image, runtime_version}
    stdout_tail: str
    stderr_tail: str
    error_phase: TestRunnerErrorPhase | None = None  # set only when overall_status == "error"


# ---------------------------------------------------------------------------
# 2.8 Test Analyst
# ---------------------------------------------------------------------------

class FailureAnalysis(BaseModel):
    test_case_id: str
    root_cause_hypothesis: Literal["code_bug", "test_bug", "spec_gap", "environment", "ambiguous"]
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: str
    recommended_action: str                 # for code_bug: behavioural terms only
    spec_gap: SpecGapDescription | None = None  # populated only when root_cause == spec_gap


class TestAnalysis(BaseModel):
    verdict: Literal[
        "pass",
        "fail_code_bug",
        "fail_test_bug",
        "fail_spec_gap",
        "fail_ambiguous",
        "error",
    ]
    summary: str
    failure_analyses: list[FailureAnalysis]
    coverage_update: list[dict[str, Any]]   # [{criterion_id, test_case_ids, status, notes}]


class TestAnalystInput(BaseModel):
    requirements_doc: RequirementsDoc
    test_suite: TestSuite
    test_runner_results: TestRunnerResults
    test_coverage_map_md: str


TestAnalystOutput = AgentOutput[TestAnalysis]


# ---------------------------------------------------------------------------
# 2.9 CommitWriter (mechanical — no LLM)
# ---------------------------------------------------------------------------

CommitWriterTarget = Literal["codeforge_state", "source_code"]


class CommitFile(BaseModel):
    path: str
    content: str
    change_type: Literal["new", "modified", "deleted"]


class CommitWriterInput(BaseModel):
    target: CommitWriterTarget
    run_id: str
    codeforge_version: str
    feature_title: str
    ac_ids: list[str]
    codeforge_state: dict[str, Any] | None = None
    source_code: dict[str, Any] | None = None


class CommitWriterResult(BaseModel):
    target: CommitWriterTarget
    success: bool
    commit_sha: str | None = None
    pr_url: str | None = None
    error_message: str | None = None


# ===========================================================================
# Part 3 — Orchestrator internal state
# ===========================================================================

class ArtifactRef(BaseModel):
    artifact_id: str
    artifact_type: ArtifactType
    stored_at: str                          # ISO 8601
    content_hash: str
    schema_version: str


class CodeforgeRun(BaseModel):
    run_id: str
    codeforge_version: str
    run_mode: Literal["new_project", "continuation"]
    started_at: str                         # ISO 8601
    status: CodeforgeStatus

    config_snapshot: dict[str, Any]         # typed at load; stored as dict for flexibility

    retry_counters: RetryCounters
    agent_call_count: int = 0

    # In-memory pending writes — not persisted mid-run
    pending_writes: dict[str, Any] = Field(default_factory=dict)

    artifacts: dict[str, ArtifactRef | None] = Field(default_factory=dict)
    escalations: list[EscalationEvent] = Field(default_factory=list)


# ===========================================================================
# Part 6 — Project state document schemas
# ===========================================================================

class ArchitectureState(BaseModel):
    schema_version: str
    last_updated_run: str
    modules: list[ModuleSpec]
    interfaces: list[InterfaceSpec]
    data_flow: list[DataFlowSpec]


class TechStackState(BaseModel):
    schema_version: str
    decisions: list[dict[str, Any]]         # TechDecision + {run_id, confirmed_at}


FeatureStatus = Literal["implemented", "tested", "deprecated"]


class FeatureEntry(BaseModel):
    feature_title: str                      # stable key
    introduced_run: str
    last_modified_run: str
    status: FeatureStatus
    interfaces: list[InterfaceSpec]


class FeatureRegistry(BaseModel):
    schema_version: str
    features: list[FeatureEntry]


class DecisionEntry(BaseModel):
    entry_id: str
    run_id: str
    entry_type: Literal["agent_decision", "human_override"]
    source_agent: AgentId | None = None
    decision: str
    rationale: str
    created_at: str                         # ISO 8601


class DecisionsLog(BaseModel):
    schema_version: str
    entries: list[DecisionEntry]


class AssumptionEntry(BaseModel):
    id: str
    description: str
    impact: Literal["low", "medium", "high"]
    record: bool
    run_id: str
    source_agent: AgentId
    status: Literal["open", "resolved", "superseded"]


class AssumptionsLog(BaseModel):
    schema_version: str
    entries: list[AssumptionEntry]


class CoverageEntry(BaseModel):
    criterion_id: str
    run_id: str
    test_case_ids: list[str]
    status: Literal["covered", "partial", "not_covered"]
    notes: str


class TestCoverageMap(BaseModel):
    schema_version: str
    entries: list[CoverageEntry]


# ===========================================================================
# Part 8 — Orchestrator event log
# ===========================================================================

class CountersSnapshot(BaseModel):
    """Full counter snapshot attached to every orchestrator event."""
    code_review_loop: int = 0
    security_review_loop: int = 0
    test_loop: int = 0
    coder_validation: int = 0
    architecture_validation: int = 0
    infrastructure: int = 0
    environment_repair: int = 0
    dependency_repair: int = 0
    low_confidence_reprompt: int = 0
    malformed_output: int = 0
    codeforge_state_commit: int = 0
    source_code_commit: int = 0
    agent_call_count: int = 0


class OrchestratorEventBase(BaseModel):
    """Every event extends this base."""
    event_id: str                           # UUID
    event_type: EventType
    run_id: str
    sequence: int                           # monotonically increasing — authoritative ordering
    timestamp: str                          # ISO 8601 — for correlation; not authoritative
    codeforge_version: str
    counters: CountersSnapshot


class HandoffEvent(OrchestratorEventBase):
    event_type: Literal["handoff"] = "handoff"
    to_agent: LogActor
    invocation_type: HandoffInvocationType
    assembly_id: str | None = None
    context_package_ref: str | None = None
    stripped_fields: list[str] = Field(default_factory=list)
    reprompt_reason: Literal["malformed_output", "contract_violation", "low_confidence"] | None = None
    litellm_call_id: str | None = None


class GateEvent(OrchestratorEventBase):
    event_type: Literal["gate"] = "gate"
    rule: GateRule
    passed: bool
    source_agent: LogActor
    artifact_ref: str | None = None
    detail: str


class RoutingEvent(OrchestratorEventBase):
    event_type: Literal["routing"] = "routing"
    routing_table_row: str                  # stable, self-describing row id, e.g. "code_review_fail"
    decision: RoutingDecision
    counter_deltas: dict[str, int] = Field(default_factory=dict)
    counter_resets: list[str] = Field(default_factory=list)
    next_state: str
    detail: str = ""                        # human-readable context (e.g. error_phase + stderr) — optional


class StateWriteEvent(OrchestratorEventBase):
    event_type: Literal["state_write"] = "state_write"
    document: StateWriteTarget
    write_source: WriteSource
    gate_condition: str
    content_hash_before: str
    content_hash_after: str


class HumanInteractionEvent(OrchestratorEventBase):
    event_type: Literal["human_interaction"] = "human_interaction"
    interaction_kind: HumanInteractionKind
    direction: Literal["outbound", "inbound"]
    interaction_id: str
    payload_ref: str
    latency_seconds: float | None = None    # inbound only


class ToolCallEvent(OrchestratorEventBase):
    """A single read-only codebase tool call made by a tool-enabled agent.

    Emitted for EVERY invocation — allowed and denied — so a forbidden-agent
    attempt or a jail escape is permanently recorded in events.jsonl, alongside
    the per-package AccessEvent. result_summary is a short description (match
    count / bytes / first line) — never full file contents.
    """
    event_type: Literal["tool_call"] = "tool_call"
    agent_id: LogActor
    tool_name: str
    tool_input: dict[str, Any]              # jailed args (query/path/symbol)
    decision: Literal["allow", "deny"]
    deny_reason: str | None = None
    result_summary: str
    latency_ms: float
    litellm_call_id: str | None = None      # the model call that requested the tool


OrchestratorEventUnion = Union[
    HandoffEvent,
    GateEvent,
    RoutingEvent,
    StateWriteEvent,
    HumanInteractionEvent,
    ToolCallEvent,
]
