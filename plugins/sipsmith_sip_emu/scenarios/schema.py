"""Pydantic v2 models for scenario YAML validation."""

from __future__ import annotations

from pydantic import BaseModel

VALID_ACTIONS = frozenset(
    {
        "call",
        "answer",
        "hangup",
        "hold",
        "unhold",
        "mute_audio",
        "mute_video",
        "dtmf",
        "wait",
        "assert_cdr",
    }
)


class ScenarioStep(BaseModel):
    action: str
    # endpoint name (for most actions)
    endpoint: str | None = None
    # destination URI for "call" action
    to: str | None = None
    # digits for "dtmf" action
    digits: str | None = None
    # seconds for "wait" action
    duration: float | None = None
    # muted state for "mute_audio" / "mute_video" actions
    muted: bool | None = None
    # event to wait for: "connected", "answered", "ended"
    wait_for: str | None = None
    # seconds to wait for wait_for
    timeout: float = 30.0
    # expected Q.850 cause code for "assert_cdr"
    cause_code: int | None = None
    # seconds to wait for CDR in "assert_cdr"
    wait_timeout: float = 60.0


class ScenarioDefinition(BaseModel):
    name: str
    description: str = ""
    steps: list[ScenarioStep]

    @classmethod
    def from_yaml(cls, content: str) -> ScenarioDefinition:
        import yaml  # pyyaml — bundled in offline installer

        data = yaml.safe_load(content)
        return cls.model_validate(data)

    def validate_actions(self) -> list[str]:
        """Return a list of human-readable validation error strings."""
        errors: list[str] = []
        for i, step in enumerate(self.steps):
            if step.action not in VALID_ACTIONS:
                errors.append(f"Step {i + 1}: unknown action '{step.action}'")
            if step.action == "call" and not step.to:
                errors.append(f"Step {i + 1}: 'call' requires 'to'")
            if step.action == "dtmf" and not step.digits:
                errors.append(f"Step {i + 1}: 'dtmf' requires 'digits'")
            if step.action == "wait" and step.duration is None:
                errors.append(f"Step {i + 1}: 'wait' requires 'duration'")
        return errors
