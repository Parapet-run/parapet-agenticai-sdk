"""Operator-declared vendor/CRUD facts for MCP tools.

An MCP `tools/call` carries only a tool name -- `Case.create`, `create_issue`.
On its own that is all Cedar can see, so "Salesforce reads are fine, Salesforce
deletes are not" is inexpressible when every MCP tool from every downstream
server shares one resource. This maps a tool to what it actually does against a
vendor, filling the same `Snapshot` fields the in-process SDK fills from
`@declare_vendor_call` (parapetai_agent.vendor_calls): `vendor_system`,
`vendor_operation`, `crud_action`.

WHO DECLARES IT. The operator, in gateway configuration -- not the agent and not
the downstream server. An agent cannot change what its own tool is classified
as, which makes this a stronger signal than the in-process decorator (whose
author is the tool's author). It is still *declared*, not observed: nothing
verifies what the tool does over the wire (see corroboration.md for that half).

Config (JSON). The first level is the MCP target name -- the `jira` in
`/a/<agent>/mcp/jira` -- and `"*"` means every target, including a bare `/mcp`
with no target. A named target's entry wins over `"*"`:

    {
      "salesforce": {
        "Case.delete": {"vendor_system": "salesforce", "resource_type": "Case",
                        "crud_action": "delete"},
        "salesforce_request": {
          "vendor_system": "salesforce", "resource_type": "Record",
          "crud_action_from": {"arg": "method",
                               "map": {"GET": "read", "POST": "create",
                                       "PATCH": "update", "DELETE": "delete"}}}
      },
      "*": {"create_issue": {"vendor_system": "atlassian", "resource_type": "Issue",
                             "crud_action": "create"}}
    }

A generic passthrough tool (`salesforce_request(method, path, ...)`) has no
single CRUD verb; `crud_action_from` derives it from one top-level argument. The
in-process SDK does this with a callable, which JSON cannot carry. A value not
in `map` -- or an absent argument -- resolves to `crud_action == "unknown"`
rather than to "no mapping": the tool is still known to be Salesforce, and a
policy of the form `permit ... when { context.crud_action == "read" }` then
correctly refuses it. Silently dropping the vendor facts would instead let an
unrecognised verb slip past a `forbid ... crud_action == "delete"` rule.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The crud_action vocabulary is deliberately not a closed set (the SDK's own is
# not either: read/create/update/delete/admin are conventions, and a connector
# may add more). Shape is checked instead, which is what catches a typo like
# "Delete" or "delete " that would otherwise produce a mapping no policy ever
# matches.
_VERB_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")

ANY_TARGET = "*"
UNKNOWN_VERB = "unknown"


class ToolMapError(ValueError):
    """The tool map is malformed. Raised at load time, so a bad map stops the
    gateway from starting instead of leaving tools silently unclassified."""


@dataclass(frozen=True, slots=True)
class CrudFromArg:
    arg: str
    verbs: Mapping[str, str]  # lower-cased argument value -> verb


@dataclass(frozen=True, slots=True)
class ToolRule:
    vendor_system: str
    resource_type: str
    crud_action: str | None = None
    crud_from: CrudFromArg | None = None
    operation: str | None = None


@dataclass(frozen=True, slots=True)
class VendorFacts:
    vendor_system: str
    vendor_operation: str
    crud_action: str


class ToolMap:
    """Immutable. Replace the instance to change the mapping."""

    def __init__(self, rules: Mapping[str, Mapping[str, ToolRule]]) -> None:
        self._rules = {target: dict(tools) for target, tools in rules.items()}

    def __len__(self) -> int:
        return sum(len(tools) for tools in self._rules.values())

    def resolve(
        self, target: str | None, tool_name: str, args: Mapping[str, Any]
    ) -> VendorFacts | None:
        """The declared facts for this call, or None if the tool is not mapped
        (the caller then leaves the snapshot untouched: `undeclared`)."""
        rule = None
        if target is not None:
            rule = self._rules.get(target, {}).get(tool_name)
        if rule is None:
            rule = self._rules.get(ANY_TARGET, {}).get(tool_name)
        if rule is None:
            return None

        crud = rule.crud_action if rule.crud_from is None else _verb_from_arg(rule.crud_from, args)
        return VendorFacts(
            vendor_system=rule.vendor_system,
            vendor_operation=rule.operation or f"{rule.resource_type}.{crud}",
            crud_action=crud or UNKNOWN_VERB,
        )

    @classmethod
    def from_dict(cls, raw: Any) -> ToolMap:
        if not isinstance(raw, dict):
            raise ToolMapError("the tool map must be a JSON object of target -> tool -> rule")
        rules: dict[str, dict[str, ToolRule]] = {}
        for target, tools in raw.items():
            if not isinstance(target, str) or not (target == ANY_TARGET or _NAME_RE.match(target)):
                raise ToolMapError(f"invalid target name {target!r}")
            if not isinstance(tools, dict):
                raise ToolMapError(f"target {target!r}: expected an object of tool -> rule")
            rules[target] = {name: _parse_rule(target, name, rule) for name, rule in tools.items()}
        return cls(rules)

    @classmethod
    def from_source(cls, source: str) -> ToolMap:
        """`source` is inline JSON (starts with `{`) or a path to a JSON file --
        the same either/or PARAPETAI_MCP_UPSTREAMS' inline form and the identity
        bindings' file form each offer, since a real tool map is usually too
        long for an environment variable."""
        text = source.strip()
        if not text.startswith("{"):
            try:
                text = Path(text).read_text(encoding="utf-8")
            except OSError as exc:
                raise ToolMapError(f"cannot read tool map from {source!r}: {exc}") from exc
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise ToolMapError(f"the tool map is not valid JSON: {exc}") from exc


def _verb_from_arg(spec: CrudFromArg, args: Mapping[str, Any]) -> str | None:
    value = args.get(spec.arg)
    if not isinstance(value, str):
        return None  # absent, or not a string: no basis to pick a verb
    return spec.verbs.get(value.strip().lower())


def _parse_rule(target: str, tool: str, raw: Any) -> ToolRule:
    where = f"{target}/{tool}"
    if not isinstance(tool, str) or not tool:
        raise ToolMapError(f"target {target!r}: empty tool name")
    if not isinstance(raw, dict):
        raise ToolMapError(f"{where}: a rule must be an object")
    unknown = set(raw) - {
        "vendor_system",
        "resource_type",
        "crud_action",
        "crud_action_from",
        "operation",
    }
    if unknown:
        # Refuse rather than ignore: "crud_actoin" ignored silently would leave
        # the tool with no verb and nothing to say why.
        raise ToolMapError(f"{where}: unknown field(s) {sorted(unknown)}")

    vendor = _name(where, "vendor_system", raw.get("vendor_system"))
    resource_type = _name(where, "resource_type", raw.get("resource_type"))
    operation = raw.get("operation")
    if operation is not None:
        operation = _name(where, "operation", operation)

    literal, derived = raw.get("crud_action"), raw.get("crud_action_from")
    if (literal is None) == (derived is None):
        raise ToolMapError(f"{where}: give exactly one of `crud_action` or `crud_action_from`")
    if literal is not None:
        return ToolRule(vendor, resource_type, _verb(where, literal), None, operation)

    if not isinstance(derived, dict) or set(derived) != {"arg", "map"}:
        raise ToolMapError(f"{where}: `crud_action_from` needs exactly `arg` and `map`")
    arg, mapping = derived["arg"], derived["map"]
    if not isinstance(arg, str) or not arg:
        raise ToolMapError(f"{where}: `crud_action_from.arg` must be a non-empty string")
    if not isinstance(mapping, dict) or not mapping:
        raise ToolMapError(f"{where}: `crud_action_from.map` must be a non-empty object")
    verbs: dict[str, str] = {}
    for value, verb in mapping.items():
        key = str(value).strip().lower()
        if key in verbs and verbs[key] != verb:
            raise ToolMapError(f"{where}: {value!r} maps to two different verbs")
        verbs[key] = _verb(where, verb)
    return ToolRule(vendor, resource_type, None, CrudFromArg(arg, verbs), operation)


def _name(where: str, field_name: str, value: Any) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ToolMapError(f"{where}: `{field_name}` is missing or has invalid characters")
    return value


def _verb(where: str, value: Any) -> str:
    if not isinstance(value, str) or not _VERB_RE.match(value):
        raise ToolMapError(
            f"{where}: crud verb {value!r} must be lower-case letters, digits or underscores "
            "(e.g. read, create, update, delete, admin)"
        )
    if value == UNKNOWN_VERB:
        raise ToolMapError(f"{where}: {UNKNOWN_VERB!r} is reserved for an unmapped argument value")
    return value


def build_tool_map(source: str | None) -> ToolMap | None:
    """None when unconfigured: no tool is classified and behaviour is unchanged."""
    return ToolMap.from_source(source) if source else None
