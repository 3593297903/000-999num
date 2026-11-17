"""Generation state is separate from latest verification and backup state."""

GENERATION_STATES = (
    "CREATED", "GENERATING", "WRITING", "FROZEN", "AUDITING", "COMMITTING",
    "COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED", "DAMAGED",
)
VERIFICATION_STATES = ("UNKNOWN", "RUNNING", "PASS", "FAIL", "ERROR", "TIMEOUT", "CANCELLED")
TRANSITIONS = {
    "CREATED": {"GENERATING", "FAILED", "INTERRUPTED", "CANCELLED"},
    "GENERATING": {"WRITING", "FAILED", "INTERRUPTED", "CANCELLED"},
    "WRITING": {"FROZEN", "FAILED", "INTERRUPTED", "CANCELLED"},
    "FROZEN": {"AUDITING", "FAILED", "INTERRUPTED", "CANCELLED", "DAMAGED"},
    "AUDITING": {"COMMITTING", "FAILED", "INTERRUPTED", "CANCELLED", "DAMAGED"},
    "COMMITTING": {"COMPLETED", "FAILED", "INTERRUPTED", "DAMAGED"},
    "COMPLETED": {"DAMAGED"},
    "FAILED": {"AUDITING", "DAMAGED"},
    "INTERRUPTED": {"AUDITING", "DAMAGED"},
    "CANCELLED": {"AUDITING", "DAMAGED"},
    "DAMAGED": set(),
}


def require_transition(old, new):
    if new not in GENERATION_STATES or old not in GENERATION_STATES:
        raise ValueError("未知生成状态")
    if new != old and new not in TRANSITIONS[old]:
        raise ValueError(f"非法状态迁移：{old} → {new}")
