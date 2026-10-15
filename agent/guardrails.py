"""Deterministic answer checks: grounding, instruction redirection, credentials
and explicit arithmetic. These helpers do not claim semantic verification.
Call prepare_answer before submission when integrating a model-driven loop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# kit.world.anchor is a collaborator's file (workspace hard rule 2). Present
# and stable as of this writing; degraded gracefully so `check_grounding`
# still runs (with the anchor-syntax leg of the check skipped, not silently
# treated as passing) if it is ever briefly unimportable.
try:
    from kit.world.anchor import Anchor, AnchorSyntaxError
    _ANCHOR_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    Anchor = None  # type: ignore[assignment]
    AnchorSyntaxError = ValueError  # type: ignore[assignment, misc]
    _ANCHOR_AVAILABLE = False

__all__ = [
    "GroundingResult",
    "check_grounding",
    "InjectionScanResult",
    "scan_for_injected_instructions",
    "RedactionResult",
    "redact",
    "ArithmeticCheckResult",
    "verify_arithmetic",
    "abstention_policy",
]


# ---------------------------------------------------------------------------
# 1. GROUNDING — real, working.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GroundingResult:
    grounded: bool
    cited: tuple[str, ...]
    ungrounded: tuple[str, ...]  # cited, syntactically valid, but never retrieved this exchange
    malformed: tuple[str, ...]  # cited but not even valid Anchor syntax


def check_grounding(
    answer: Mapping[str, Any],
    retrieved_anchors: Iterable[str],
    *,
    require_citation: bool = True,
) -> GroundingResult:
    """"Every claim traces to a returned anchor" (this task's own brief),
    made concrete: every string in `answer["cited_anchors"]` must (a) parse
    as valid `ns:slug[/rev][/idx][#span]` syntax (`kit.world.anchor.Anchor`)
    and (b) be a member of `retrieved_anchors` — the anchors YOUR exchange
    actually got back from a `tool_result` this round, not anchors you
    recognise from having seen them before, and not anchors you are
    inferring exist.

    `retrieved_anchors` is YOUR responsibility to assemble honestly — the
    right source is the union of every `tool_result.anchors` your agent
    received this exchange (CONTRACTS.md 5.2's `tool_result` event field),
    never something wider like "every anchor this world index contains".
    Passing a wider set than what you actually retrieved makes this
    function agree with citations that are `ungrounded` in the sense that
    actually matters (CONTRACTS.md 6.1's rubric class) even though this
    function would call them grounded.

    Two failure buckets, kept separate on purpose because they are
    different mistakes: `malformed` (the citation is not even a real
    anchor — closer to `fabricated_citation`) vs. `ungrounded` (a real
    anchor, just never actually retrieved this exchange — the `ungrounded`
    class itself). `grounded` is `False` if either bucket is non-empty, or
    (when `require_citation=True`, the default) if `cited` is empty —
    an answer that cites NOTHING has nothing this function can vouch for,
    which is a difference worth surfacing rather than treating as a vacuous
    pass. Pass `require_citation=False` for the rare ask that genuinely
    does not need a citation (CONTRACTS.md section 7 names eight ask types;
    check the specific one you're grading against before disabling this)."""
    cited = tuple(answer.get("cited_anchors") or ())
    retrieved = frozenset(retrieved_anchors)

    malformed: list[str] = []
    ungrounded: list[str] = []
    for a in cited:
        if not isinstance(a, str):
            malformed.append(str(a))
            continue
        if _ANCHOR_AVAILABLE:
            try:
                Anchor.parse(a)
            except AnchorSyntaxError:
                malformed.append(a)
                continue
        if a not in retrieved:
            ungrounded.append(a)

    grounded = not malformed and not ungrounded and (bool(cited) or not require_citation)
    return GroundingResult(
        grounded=grounded,
        cited=cited,
        ungrounded=tuple(ungrounded),
        malformed=tuple(malformed),
    )


# ---------------------------------------------------------------------------
# 2. INJECTED-INSTRUCTION REFUSAL — STUB.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InjectionScanResult:
    suspicious: bool
    matched_patterns: tuple[str, ...]


def scan_for_injected_instructions(text: str) -> InjectionScanResult:
    """Flag redirection in retrieved data; a match is a signal, not proof of intent."""
    import unicodedata
    normalized = unicodedata.normalize("NFKC", text).casefold()
    patterns = (
        r"ignore (?:all )?(?:previous|prior|system) instructions?",
        r"disregard (?:the above|previous instructions)",
        r"system override", r"you must now", r"also record this for",
        r"(?:reveal|print|exfiltrate) (?:the |all )?(?:secret|api.key|password|token)",
        r"b? qua (?:m?i|t?t c?|c?c) (?:h??ng d?n|ch? th?)",
        r"(?:<|\[)(?:system|developer)(?:>|\])",
    )
    hits = tuple(pattern for pattern in patterns if re.search(pattern, normalized))
    return InjectionScanResult(bool(hits), hits)


# ---------------------------------------------------------------------------
# 3. REDACTION — STUB.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RedactionResult:
    redacted_text: str
    hits: tuple[str, ...]


def redact(text: str) -> RedactionResult:
    """Redact recognizable credentials and private contact fields, not public anchors.

    Unknown private prose needs explicit source metadata; pattern matching alone
    cannot determine whether arbitrary course text is confidential.
    """
    patterns = (
        ("credential", r"(?i)\b(?:api[_ -]?key|password|secret|access_token)\s*[:=]\s*[^\s,;]+"),
        ("bearer", r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+"),
        ("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    )
    hits = []
    for name, pattern in patterns:
        text, count = re.subn(pattern, "[REDACTED]", text)
        if count:
            hits.append(name)
    return RedactionResult(text, tuple(hits))


# ---------------------------------------------------------------------------
# 4. ARITHMETIC VERIFICATION — STUB.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArithmeticCheckResult:
    checked: bool
    ok: bool | None
    detail: str


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def verify_arithmetic(text: str) -> ArithmeticCheckResult:
    """Check explicit binary decimal equalities without eval or source assumptions."""
    from decimal import Decimal, localcontext
    number = r"-?\d+(?:\.\d+)?"
    equations = re.findall(rf"({number})\s*([+*/???-])\s*({number})\s*=\s*({number})", text)
    if not equations:
        return ArithmeticCheckResult(False, None, "No explicit arithmetic equality; source precision is unverified")
    if any(len(value) > 100 for equation in equations for value in equation):
        return ArithmeticCheckResult(False, None, "Numeric input exceeds verification limit")
    with localcontext() as ctx:
        ctx.prec = 220
        for left, op, right, expected in equations:
            a, b, c = Decimal(left), Decimal(right), Decimal(expected)
            if op in ("/", "?") and b == 0:
                return ArithmeticCheckResult(True, False, "Division by zero")
            actual = {"+": lambda: a+b, "-": lambda: a-b, "?": lambda: a-b,
                      "*": lambda: a*b, "?": lambda: a*b,
                      "/": lambda: a/b, "?": lambda: a/b}[op]()
            if actual != c:
                return ArithmeticCheckResult(True, False, f"Incorrect equality: {left} {op} {right} = {expected}")
    return ArithmeticCheckResult(True, True, f"Verified {len(equations)} explicit equalities; source precision is unverified")


# ---------------------------------------------------------------------------
# 5. ABSTENTION POLICY — real, naive.
# ---------------------------------------------------------------------------


def abstention_policy(grounding: GroundingResult) -> bool:
    """`True` iff you should abstain (answer with an honest "insufficient
    grounding" rather than submit this ANSWER as-is). Naive on purpose: it
    reuses the ONE guardrail this file can actually vouch for
    (`check_grounding`) and nothing else — your own confidence, a
    conflicting second source (`unflagged_conflict`, CONTRACTS.md 6.1),
    and the ask's own `require`d fields (CONTRACTS.md section 7) all go
    completely unweighed here. CONTRACTS.md's own prompt guidance
    (kit/loop/prompt.py's `SYSTEM_PROMPT`) puts it plainly: "a wrong answer
    costs more than an honest 'insufficient grounding'" — this function is
    the bare floor of that policy, not the ceiling."""
    return not grounding.grounded


def prepare_answer(answer: Mapping[str, Any], retrieved_anchors: Iterable[str], *,
                   private_texts: Iterable[str] = (), required: Iterable[str] = ()) -> dict:
    """Validate a proposed answer without mutating it; abstain if evidence is missing.

    private_texts must come from source privacy metadata. The gateway never sees
    ANSWER actions, so the loop must invoke this helper explicitly.
    """
    result = dict(answer)
    text = str(result.get("text", ""))
    normalized = " ".join(text.casefold().split())
    for private in private_texts:
        private = " ".join(str(private).casefold().split())
        if len(private) >= 40 and any(private[i:i+40] in normalized for i in range(len(private)-39)):
            return {"text": "Cannot disclose private learner information.", "cited_anchors": []}
    grounding = check_grounding(result, retrieved_anchors)
    arithmetic = verify_arithmetic(text)
    missing = any(name.split(".")[0] not in result for name in required)
    if not grounding.grounded or missing or arithmetic.ok is False:
        return {"text": "Insufficient verified evidence to answer this question.", "cited_anchors": []}
    result["text"] = redact(text).redacted_text
    return result
