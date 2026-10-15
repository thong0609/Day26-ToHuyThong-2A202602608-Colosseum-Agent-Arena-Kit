"""Pure control plane for the Day 26 tutor.

The arena owns execution and L1 logging. This module owns admission, learner
boundaries, transport routing, write preconditions and per-round cost limits.
Only trusted harness callbacks may supply peer admission and provenance.
"""

from __future__ import annotations

import json

from kit.mcp.specs import TOOL_SPECS, A2A_PEERS, cost_of
from agent.guardrails import scan_for_injected_instructions
from agent.strategy import successor_of

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

# kit.mcp.types is a collaborator's file (workspace hard rule 2: import it,
# degrade gracefully). It is present as of this writing and is core, stable
# infrastructure (CONTRACTS.md 3.1) — but this module must still not fail to
# IMPORT if a concurrent edit ever breaks it transiently. When it is
# unavailable, `Decision.call` type-checking is skipped (not enforced), and
# `Gateway.decide` falls back to a minimal local dict-shaped stand-in so the
# rest of this file — everything that does not need a *real* ToolCall — still
# runs.
try:
    from kit.mcp.types import ToolCall
    _TOOLCALL_AVAILABLE = True
except ImportError:  # pragma: no cover - collaborator file
    ToolCall = Any  # type: ignore[assignment, misc]
    _TOOLCALL_AVAILABLE = False

# kit.loop.agent is also a collaborator's file, used only by this module's
# own __main__ demo (to build real Commands the same way the arena's trusted
# canonicaliser would) — never by decide() itself, which never touches the
# loop. Degraded the same way.
try:
    from kit.loop.agent import canonicalise_action as _canonicalise_action
except ImportError:  # pragma: no cover - collaborator file
    _canonicalise_action = None

from agent.telemetry import RecordingGatewayContext, Telemetry

__all__ = [
    "COMMAND_KINDS",
    "DECISION_VERDICTS",
    "Command",
    "Decision",
    "GatewayContext",
    "Gateway",
]

# CONTRACTS.md 4.1: `Command.kind` — "mcp" | "a2a" | "discover". An "answer"
# action is NEVER a Command (kit/loop/agent.py's own module docstring: "an
# answer is not a tool call routed to a server, so it never becomes a
# Command at all") — it is handled entirely by the loop/arena and never
# reaches `Gateway.decide`.
COMMAND_KINDS: frozenset[str] = frozenset({"mcp", "a2a", "discover"})

# CONTRACTS.md 4.1: `Decision.verdict` — the closed three-member set.
DECISION_VERDICTS: frozenset[str] = frozenset({"forward", "deny", "rewrite"})


@dataclass(frozen=True, slots=True)
class Command:
    """CONTRACTS.md 4.1, field for field — "canonicalised by the arena
    BEFORE the student sees it". You never build one of these from your own
    agent's raw text; the arena's canonicaliser (kit/loop/agent.py's
    `canonicalise_action`, run inside the trusted envelope) already did that
    work and minted `cmd_id` by the time `decide()` sees it. The
    `from_action_dict` classmethod below exists only so this file's own demo
    (and your local tests, if you write any) can build a realistic `Command`
    without duplicating the arena's canonicalisation logic."""

    cmd_id: str
    kind: str  # "mcp" | "a2a" | "discover" — see COMMAND_KINDS
    raw: str
    server: str
    tool: str
    args: dict
    fields: tuple[str, ...]
    headers: dict
    lease_id: str | None
    call_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.cmd_id, str) or not self.cmd_id:
            raise ValueError(f"Command.cmd_id must be a non-empty str, got {self.cmd_id!r}")
        if self.kind not in COMMAND_KINDS:
            raise ValueError(f"Command.kind must be one of {sorted(COMMAND_KINDS)}, got {self.kind!r}")
        if not isinstance(self.server, str) or not self.server:
            raise ValueError(f"Command.server must be a non-empty str, got {self.server!r}")
        if not isinstance(self.tool, str) or not self.tool:
            raise ValueError(f"Command.tool must be a non-empty str, got {self.tool!r}")
        if not isinstance(self.args, dict):
            raise ValueError(f"Command.args must be a dict, got {type(self.args).__name__}")
        if not isinstance(self.headers, dict):
            raise ValueError(f"Command.headers must be a dict, got {type(self.headers).__name__}")
        if (
            not isinstance(self.call_index, int)
            or isinstance(self.call_index, bool)
            or self.call_index < 0
        ):
            raise ValueError(f"Command.call_index must be a non-negative int, got {self.call_index!r}")

    @classmethod
    def from_action_dict(cls, action: Mapping[str, Any], *, cmd_id: str) -> "Command":
        """Build a `Command` from the dict shape `kit.loop.agent.canonicalise_action`
        returns (`kind, raw, server, tool, args, fields, headers, lease_id,
        call_index` — everything except the arena-minted `cmd_id`, supplied
        here as a keyword). Raises `ValueError` if `action["kind"] ==
        "answer"` — an answer is never a Command (see the module docstring).
        This is a convenience for tests/demos, not something the real arena
        calls: the trusted envelope mints `cmd_id` itself and constructs the
        real `Command` on its own side of the boundary."""
        kind = action.get("kind")
        if kind == "answer":
            raise ValueError(
                "an 'answer' action never becomes a Command (kit/loop/agent.py: "
                "\"an answer is not a tool call routed to a server\") — do not "
                "route it through Gateway.decide at all"
            )
        return cls(
            cmd_id=cmd_id,
            kind=kind,
            raw=action["raw"],
            server=action["server"],
            tool=action["tool"],
            args=dict(action.get("args", {})),
            fields=tuple(action.get("fields", ())),
            headers=dict(action.get("headers", {})),
            lease_id=action.get("lease_id"),
            call_index=action.get("call_index", 0),
        )

    def to_dict(self) -> dict:
        return {
            "cmd_id": self.cmd_id,
            "kind": self.kind,
            "raw": self.raw,
            "server": self.server,
            "tool": self.tool,
            "args": dict(self.args),
            "fields": list(self.fields),
            "headers": dict(self.headers),
            "lease_id": self.lease_id,
            "call_index": self.call_index,
        }


@dataclass(frozen=True, slots=True)
class Decision:
    """CONTRACTS.md 4.1, field for field.

    Validated strictly (`__post_init__`) because a *structurally* invalid
    `Decision` is charged exactly like a raised exception — CONTRACTS.md
    4.1's charging table: "malformed Decision (schema-invalid) -> 2 cr
    penalty, command denied." Failing loudly HERE, in your own process
    during development, is strictly better than discovering it live in a
    duel as an unexplained penalty.

    `verdict == "deny"` requires a non-empty `reason` (CONTRACTS.md 4.1:
    "required when verdict == 'deny'; shown in the combat log") and
    forbids `call` — a real denial has nothing left to carry out.
    `verdict` in `("forward", "rewrite")` requires `call` to be set — the
    arena executes exactly that `ToolCall`, nothing else, per the trusted
    envelope's whole point (see the module docstring)."""

    verdict: str  # "forward" | "deny" | "rewrite" — see DECISION_VERDICTS
    reason: str | None = None
    call: "ToolCall | None" = None
    quarantine: bool = False
    note: str | None = None

    def __post_init__(self) -> None:
        if self.verdict not in DECISION_VERDICTS:
            raise ValueError(
                f"Decision.verdict must be one of {sorted(DECISION_VERDICTS)}, got {self.verdict!r}"
            )
        if self.verdict == "deny":
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("Decision.verdict=='deny' requires a non-empty 'reason'")
            if self.call is not None:
                raise ValueError("Decision.verdict=='deny' must not carry a 'call' — there is nothing to run")
        else:  # forward | rewrite
            if self.call is None:
                raise ValueError(f"Decision.verdict=={self.verdict!r} requires 'call' to be set")
            if _TOOLCALL_AVAILABLE and not isinstance(self.call, ToolCall):
                raise ValueError(
                    f"Decision.call must be a kit.mcp.types.ToolCall instance, got {type(self.call).__name__}"
                )
        if not isinstance(self.quarantine, bool):
            raise ValueError(f"Decision.quarantine must be a bool, got {self.quarantine!r}")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError(f"Decision.note must be a str or None, got {self.note!r}")

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "call": self.call.to_dict() if self.call is not None and hasattr(self.call, "to_dict") else self.call,
            "quarantine": self.quarantine,
            "note": self.note,
        }


@runtime_checkable
class GatewayContext(Protocol):
    """CONTRACTS.md 4.2 — "read-only, arena-provided". Note what this is
    NOT: unlike `Command`/`Decision` above, CONTRACTS.md writes this as a
    plain `class`, not a `@dataclass(frozen=True, ...)`. That is a real
    signal, not an inconsistency — a `GatewayContext` is a LIVE VIEW the
    arena owns and updates as the duel actually progresses (credits get
    spent, rounds advance, leases mint and expire), not a frozen snapshot
    you receive once. This file therefore models it as a structural
    `Protocol`: the arena hands your `Gateway.__init__` some object shaped
    like this (see `agent/telemetry.py`'s `RecordingGatewayContext` for a
    concrete example your own tests/demos can build), and you read
    `self.ctx.<field>` fresh every time `decide()` runs — NEVER cache a
    context field into a local variable across calls; persistent memory
    that persists across the duel (a cache, a budget tracker, drift notes),
    keep it on YOUR OWN `Gateway` instance attributes instead (CONTRACTS.md
    4.3: "Gateway is instantiated once per duel... persists across the 10
    rounds").

    There is deliberately no `execute` method here — see the module
    docstring's "WHY THERE IS NO execute() METHOD" section. `emit` is the
    ONE thing you can do that has any side effect at all, and it writes to
    `own_telemetry` only: your own side, never scored, never shown to the
    opponent (`agent/telemetry.py` wraps it)."""

    act: str  # "learner:sv-0417" — WHOM YOU SERVE. Authority derives from this.
    sub: str  # "agent:vlearn-tutor" — what you are. NOT authority.
    scopes: frozenset[str]  # {"wiki.read"}; writes need "wiki.write:progress" etc.
    credits: int  # remaining this duel
    round: int
    call_index: int
    leases: tuple[str, ...]  # live lease ids, arena-tracked
    history: tuple[Mapping[str, Any], ...]  # YOUR OWN prior (Command, Decision, outcome) triples this duel

    def emit(self, name: str, **payload: Any) -> None: ...


class Gateway:
    """One instance per duel; all authorization decisions are synchronous and pure."""

    def __init__(self, ctx: GatewayContext) -> None:
        self.ctx = ctx
        self._telemetry = Telemetry(ctx)
        self._seen_anchors: dict[str, Any] = {}
        self._credits_authorised = 0
        self._denied_cmd_ids: set[str] = set()

    def note_card(self, server: str, card: dict) -> None:
        """Accept admission metadata supplied by the trusted harness, never by args."""
        if not hasattr(self, "_cards"):
            self._cards = {}
        self._cards[server] = {"verified": card.get("verified") is True,
                               "skills": tuple(card.get("skills") or ())}

    def note_provenance(self, anchor: str, etag: str) -> None:
        self._seen_anchors[anchor] = (etag, self.ctx.round, self.ctx.call_index)

    def decide(self, cmd: Command) -> Decision:
        """Pure admission, authority, protocol and cost checks on every call."""

        self._telemetry.decision_seen(cmd)
        headers = {}
        for key, value in cmd.headers.items():
            name = str(key).lower()
            if name in headers and headers[name] != value:
                return self.deny(cmd, "conflicting case-insensitive headers")
            headers[name] = value
        server, tool = successor_of(cmd.server, cmd.tool) or (cmd.server, cmd.tool)
        spec = TOOL_SPECS.get((server, tool))
        if spec is None:
            return self.deny(cmd, "unknown tool; discover a supported tool first")
        if any(cmd.args.get(k) for k in ("route", "_route", "replica")):
            return self.deny(cmd, "replica routing must use the transport header")
        if headers.get("x-server-fingerprint") in ("unvouched", "invalid"):
            return self.deny(cmd, "server identity is not vouched for")
        if headers.get("x-card-signature") == "invalid":
            return self.deny(cmd, "invalid peer card signature")
        if cmd.args.get("peer_unverified"):
            return self.deny(cmd, "peer result needs independent verification")
        for key in ("note", "instructions", "peer_answer", "retrieved_content"):
            if scan_for_injected_instructions(str(cmd.args.get(key, ""))).suspicious:
                return self.deny(cmd, "retrieved instructions cannot authorize an action")
        if len(str(cmd.args.get("catalog", ""))) > 1024:
            return self.deny(cmd, "unbounded catalog; use a targeted lookup")
        replica = headers.get("mcp-replica", "w")
        if replica not in ("w", "c"):
            return self.deny(cmd, "invalid replica header")
        headers["mcp-replica"] = replica
        headers.pop("x-mcp-body-route", None)

        def learner(value):
            return str(value).lower().removeprefix("learner:")

        for key in ("learner", "learner_id", "subject"):
            target = cmd.args.get(key)
            if target and learner(target) != learner(self.ctx.act):
                return self.deny(cmd, "learner target exceeds the authority of act")
        anchor_target = str(cmd.args.get("anchor", ""))
        if anchor_target.lower().startswith("learner:"):
            owner = anchor_target.split("/", 1)[0].split("#", 1)[0]
            if learner(owner) != learner(self.ctx.act):
                return self.deny(cmd, "learner anchor exceeds the authority of act")
        if not spec.is_write and "wiki.read" not in self.ctx.scopes:
            return self.deny(cmd, "missing read scope")
        if headers.get("act") and learner(headers["act"]) != learner(self.ctx.act):
            return self.deny(cmd, "delegation act differs from the served learner")
        aud = headers.get("aud")
        if aud is not None and aud not in (server, "a2a:" + server, "mcp:" + server):
            return self.deny(cmd, "delegation audience does not match destination")
        if server in A2A_PEERS:
            card = getattr(self, "_cards", {}).get(server, {})
            if not card.get("verified") or tool not in card.get("skills", ()):
                return self.deny(cmd, "peer and skill have not been admitted")
            if aud not in (server, "a2a:" + server):
                return self.deny(cmd, "A2A requires an audience-bound delegation")
        if spec.needs_lease and (not cmd.lease_id or cmd.lease_id not in self.ctx.leases):
            return self.deny(cmd, "get_frame requires a live query lease")
        if spec.is_write:
            if "wiki.write:" + server not in self.ctx.scopes:
                return self.deny(cmd, "missing write scope")
            if any(not headers.get(h) for h in spec.required_headers):
                return self.deny(cmd, "write requires If-Match and Idempotency-Key")
            target = cmd.args.get("target")
            if target and server == "progress" and learner(target) != learner(self.ctx.act):
                return self.deny(cmd, "write target is not owned by act")
            anchor = str(cmd.args.get("anchor", ""))
            provenance = self._seen_anchors.get(anchor)
            if not provenance or provenance[0] != headers.get("if-match") or provenance[1] != self.ctx.round:
                return self.deny(cmd, "read fresh provenance before writing")
            if cmd.call_index - provenance[2] > 3:
                return self.deny(cmd, "write provenance expired")
            writes = getattr(self, "_writes", {})
            key = headers["idempotency-key"]
            payload = json.dumps([server, tool, cmd.args], sort_keys=True)
            if key in writes:
                return self.deny(cmd, "write key already authorized; reconcile its outcome first")
        fields = tuple(cmd.fields)
        if (cmd.server, cmd.tool) == ("slides", "search"):
            fields = tuple("body" if f == "snippet" else f for f in fields if f != "anchor")
        if not fields or "*" in fields:
            fields = spec.default_fields
            if (server, tool) == ("registry", "list_servers"):
                fields = ("name",)
            elif (server, tool) == ("glossary", "list_terms"):
                fields = ("term",)
        if any(f not in spec.all_fields for f in fields):
            return self.deny(cmd, "unsupported field mask")
        args = dict(cmd.args)
        rows = 1
        if spec.row_weight:
            limit = args.get("limit", 3)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                return self.deny(cmd, "query limit must be a positive integer")
            rows = min(limit, 3)
            args["limit"] = rows
        call = ToolCall(server=server, tool=tool, args=args, fields=fields,
                        headers=headers, lease_id=cmd.lease_id, call_index=cmd.call_index)
        estimated = cost_of(call, n_rows=rows)
        if getattr(self, "_budget_round", None) != self.ctx.round:
            self._budget_round = self.ctx.round
            self._round_spent = 0
        if estimated > self.ctx.credits or self._round_spent + estimated > 11:
            return self.deny(cmd, "insufficient round or duel budget")
        if spec.rate_limit:
            count, window = spec.rate_limit
            uses = getattr(self, "_rate_uses", {})
            recent = [r for r in uses.get((server, tool), ()) if r > self.ctx.round - window]
            if len(recent) >= count:
                return self.deny(cmd, "tool rate window exhausted")
            uses[(server, tool)] = recent + [self.ctx.round]
            self._rate_uses = uses
        if spec.is_write:
            writes[key] = payload
            self._writes = writes
        self._round_spent += estimated
        self._credits_authorised += estimated
        changed = (server, tool, fields, headers, args) != (cmd.server, cmd.tool, cmd.fields, cmd.headers, cmd.args)
        decision = Decision(verdict="rewrite" if changed else "forward", call=call)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def deny(self, cmd: Command, reason: str) -> Decision:
        """Deny for free and record the reason in own telemetry."""
        self._denied_cmd_ids.add(cmd.cmd_id)
        decision = Decision(verdict="deny", reason=reason)
        self._telemetry.decision_made(cmd, decision)
        return decision

    def _to_tool_call(self, cmd: Command) -> "ToolCall":
        """`Command` -> the `ToolCall` (CONTRACTS.md 3.1) the arena will
        actually execute on a `forward`/`rewrite` verdict. When
        `kit.mcp.types` is unavailable (see the module-level import guard),
        falls back to a plain dict carrying the identical fields — `Decision`
        accepts it either way (the `ToolCall` isinstance check inside
        `Decision.__post_init__` only runs when the real class loaded)."""
        fields = {
            "server": cmd.server,
            "tool": cmd.tool,
            "args": dict(cmd.args),
            "fields": cmd.fields,
            "headers": dict(cmd.headers),
            "lease_id": cmd.lease_id,
            "call_index": cmd.call_index,
        }
        if _TOOLCALL_AVAILABLE:
            return ToolCall(**fields)
        return fields  # type: ignore[return-value]
