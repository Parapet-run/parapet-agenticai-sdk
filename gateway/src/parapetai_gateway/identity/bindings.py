"""Identity -> agent bindings.

A valid IdP token or client certificate proves who the IdP or CA says the
caller is. It does NOT say which Parapet agent that is, and therefore which
policy applies. Without an explicit mapping, any identity the IdP will vouch
for could act as any agent. A binding is that mapping, and a verified
identity with no binding is refused (403), never defaulted.

Same shape as the `bindings` array in docs/CONTROL_PLANE_API.md's gateway
config, so the control plane can later deliver the same records this module
loads from a local file today.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from parapetai_agent.identity import ANONYMOUS

# The agent_id ends up inside a Cedar entity literal (`Agent::"<id>"`), so it
# is held to a conservative alphabet: a quote or backslash would let a
# malformed binding rewrite the principal expression.
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


class BindingError(ValueError):
    """A binding record is malformed or ambiguous. Raised at load time so a bad
    table stops the gateway from starting rather than silently mis-mapping."""


@dataclass(frozen=True, slots=True)
class Binding:
    kind: str  # "jwt" | "mtls"
    agent_id: str
    issuer: str | None = None
    subject: str | None = None
    cn: str | None = None


class BindingTable:
    """Immutable lookup. Replace the whole table (assign a new instance) to
    change bindings -- never mutate one, so a lookup in flight sees either the
    old table or the new one, never a half-updated one."""

    def __init__(self, bindings: Iterable[Binding] = ()) -> None:
        self._jwt: dict[tuple[str, str], str] = {}
        self._mtls: dict[str, str] = {}
        for b in bindings:
            self._add(b)

    def _add(self, b: Binding) -> None:
        if b.agent_id == ANONYMOUS:
            # `anonymous` is the unauthenticated principal. Binding a verified
            # identity to it would be a privilege change disguised as a
            # mapping, in either direction.
            raise BindingError(f"agent_id {ANONYMOUS!r} is reserved and cannot be bound")
        if not _AGENT_ID_RE.match(b.agent_id):
            raise BindingError(f"agent_id {b.agent_id!r} has characters that are not allowed")
        if b.kind == "jwt":
            if not b.issuer or not b.subject:
                raise BindingError("a jwt binding needs both `issuer` and `subject`")
            key = (b.issuer, b.subject)
            existing = self._jwt.get(key)
            if existing is not None and existing != b.agent_id:
                raise BindingError(
                    f"jwt identity {key!r} is bound to both {existing!r} and {b.agent_id!r}"
                )
            self._jwt[key] = b.agent_id
        elif b.kind == "mtls":
            if not b.cn:
                raise BindingError("an mtls binding needs `cn`")
            existing = self._mtls.get(b.cn)
            if existing is not None and existing != b.agent_id:
                raise BindingError(
                    f"mtls cn {b.cn!r} is bound to both {existing!r} and {b.agent_id!r}"
                )
            self._mtls[b.cn] = b.agent_id
        else:
            raise BindingError(f"unknown binding kind {b.kind!r} (expected 'jwt' or 'mtls')")

    def agent_for_jwt(self, issuer: str, subject: str) -> str | None:
        return self._jwt.get((issuer, subject))

    def agent_for_mtls(self, cn: str) -> str | None:
        return self._mtls.get(cn)

    def __len__(self) -> int:
        return len(self._jwt) + len(self._mtls)

    @classmethod
    def from_dicts(cls, records: Iterable[Mapping[str, Any]]) -> BindingTable:
        bindings: list[Binding] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise BindingError("each binding must be a JSON object")
            agent_id = record.get("agent_id")
            kind = record.get("kind")
            if not isinstance(agent_id, str) or not isinstance(kind, str):
                raise BindingError("each binding needs string `kind` and `agent_id`")
            bindings.append(
                Binding(
                    kind=kind,
                    agent_id=agent_id,
                    issuer=_opt_str(record.get("issuer")),
                    subject=_opt_str(record.get("subject")),
                    cn=_opt_str(record.get("cn")),
                )
            )
        return cls(bindings)

    @classmethod
    def from_file(cls, path: str | Path) -> BindingTable:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BindingError(f"cannot read identity bindings from {path}: {exc}") from exc
        records = raw.get("bindings") if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            raise BindingError(f"{path}: expected a list, or an object with a `bindings` list")
        return cls.from_dicts(records)


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
