"""
davinci_resolve_mcp.tools.off_fusion — offline Fusion composition (``.comp``)
inspector / editor.

Single action-dispatch entry point (``offline_fusion``) that reads, summarizes,
and structurally edits DaVinci Resolve Fusion composition files entirely
offline. A Fusion ``.comp`` file is a small Lua-like text format::

    {
        Tools = ordered() {
            MediaIn1 = MediaIn {
                CtrlWZoom = false,
                Inputs = {
                    GlobalIn = Input { Value = 0 },
                },
            },
            ColorCorrector1 = ColorCorrector {
                Inputs = {
                    MasterRGBGain = Input { Value = 1.05 },
                    Input = Input { SourceOp = "MediaIn1", Source = "Output" },
                },
            },
        },
        ActiveTool = "ColorCorrector1",
    }

This module implements a small, deliberately scoped recursive-descent parser
for that dialect (tables, constructor calls such as ``ordered()``/``Input``/
tool-type blocks, bracketed keys such as keyframe ``[time] = value`` entries,
strings, numbers, booleans, ``nil``, and ``--`` line comments) plus a matching
serializer, so a comp tree round-trips back to valid Fusion script text after
an edit. It intentionally does not aim to be a general Lua parser — Fusion
``.comp`` files only use this narrow subset of the language.

This module NEVER connects to DaVinci Resolve: no ``DaVinciResolveScript``
import, no live session, nothing at import time beyond registering the tool.
It only reads/writes plain files on disk. Every code path returns a JSON
string; nothing here raises — the top-level ``offline_fusion`` dispatcher
catches every exception and returns an ``"Error: ..."`` string instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from ..app import mcp

# ── A small Fusion .comp AST ─────────────────────────────────────────────


@dataclass
class CompTable:
    """A Lua-style table: an ordered mapping of keyed entries plus a list
    of purely positional entries (e.g. ``{ 1, 0.5, 0 }``)."""

    fields: Dict[str, Any] = field(default_factory=dict)
    items: List[Any] = field(default_factory=list)


@dataclass
class CompCtor:
    """A constructor call such as ``ordered()``, ``Input { ... }``, or a
    tool block like ``ColorCorrector { ... }``: an identifier, optional
    call arguments, and an optional trailing ``{ ... }`` table."""

    name: str
    args: List[Any] = field(default_factory=list)
    table: Optional[CompTable] = None


@dataclass
class CompIdent:
    """A bare identifier used as a value (an enum-like symbol) with no
    call arguments and no trailing table, e.g. ``FuID_Text``."""

    name: str


# ── Tokenizer ─────────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(
    r"""
      (?P<WS>\s+)
    | (?P<COMMENT>--[^\n]*)
    | (?P<STRING>"(?:\\.|[^"\\])*")
    | (?P<NUMBER>-?(?:\d+\.\d+|\.\d+|\d+)(?:[eE][+-]?\d+)?)
    | (?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<LBRACE>\{)
    | (?P<RBRACE>\})
    | (?P<LPAREN>\()
    | (?P<RPAREN>\))
    | (?P<LBRACKET>\[)
    | (?P<RBRACKET>\])
    | (?P<COMMA>,)
    | (?P<EQUALS>=)
    | (?P<OTHER>.)
    """,
    re.VERBOSE,
)

_Token = tuple  # (kind: str, text: str, line: int)


def _tokenize(text: str) -> List[Any]:
    tokens: List[Any] = []
    pos = 0
    line = 1
    length = len(text)
    while pos < length:
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ValueError(f"comp: unexpected character {text[pos]!r} at line {line}")
        kind = m.lastgroup
        value = m.group()
        if kind == "WS":
            line += value.count("\n")
        elif kind == "COMMENT":
            pass
        elif kind == "OTHER":
            raise ValueError(f"comp: unexpected character {value!r} at line {line}")
        else:
            tokens.append((kind, value, line))
        pos = m.end()
    tokens.append(("EOF", "", line))
    return tokens


def _unescape_string(raw: str) -> str:
    inner = raw[1:-1]
    return inner.replace('\\"', '"').replace("\\\\", "\\")


def _parse_number(text: str) -> Union[int, float]:
    if any(c in text for c in (".", "e", "E")):
        return float(text)
    try:
        return int(text)
    except ValueError:
        return float(text)


# ── Recursive-descent parser ─────────────────────────────────────────────


class _Parser:
    def __init__(self, tokens: List[Any]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Any:
        return self.tokens[self.pos]

    def advance(self) -> Any:
        tok = self.tokens[self.pos]
        if tok[0] != "EOF":
            self.pos += 1
        return tok

    def expect(self, kind: str) -> Any:
        tok = self.advance()
        if tok[0] != kind:
            raise ValueError(f"comp: expected {kind} but found {tok[0]} ({tok[1]!r}) at line {tok[2]}")
        return tok

    def parse_value(self) -> Any:
        kind, text, line = self.peek()
        if kind == "STRING":
            self.advance()
            return _unescape_string(text)
        if kind == "NUMBER":
            self.advance()
            return _parse_number(text)
        if kind == "LBRACE":
            return self.parse_table()
        if kind == "IDENT":
            if text == "nil":
                self.advance()
                return None
            if text == "true":
                self.advance()
                return True
            if text == "false":
                self.advance()
                return False
            self.advance()
            args: List[Any] = []
            if self.peek()[0] == "LPAREN":
                self.advance()
                while self.peek()[0] != "RPAREN":
                    if self.peek()[0] == "EOF":
                        raise ValueError(f"comp: unterminated argument list for {text!r} starting near line {line}")
                    args.append(self.parse_value())
                    if self.peek()[0] == "COMMA":
                        self.advance()
                self.expect("RPAREN")
            table: Optional[CompTable] = None
            if self.peek()[0] == "LBRACE":
                table = self.parse_table()
            if table is None and not args:
                return CompIdent(text)
            return CompCtor(name=text, args=args, table=table)
        raise ValueError(f"comp: unexpected token {kind} ({text!r}) at line {line}")

    def parse_table(self) -> CompTable:
        start_line = self.peek()[2]
        self.expect("LBRACE")
        fields: Dict[str, Any] = {}
        items: List[Any] = []
        while self.peek()[0] != "RBRACE":
            kind, text, line = self.peek()
            if kind == "EOF":
                raise ValueError(f"comp: unterminated table opened at line {start_line} (missing '}}')")
            if kind == "LBRACKET":
                self.advance()
                key_tok = self.advance()
                key = _unescape_string(key_tok[1]) if key_tok[0] == "STRING" else key_tok[1]
                self.expect("RBRACKET")
                self.expect("EQUALS")
                fields[str(key)] = self.parse_value()
            elif kind == "IDENT" and self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1][0] == "EQUALS":
                self.advance()
                self.expect("EQUALS")
                fields[text] = self.parse_value()
            else:
                items.append(self.parse_value())
            if self.peek()[0] == "COMMA":
                self.advance()
            elif self.peek()[0] != "RBRACE":
                tok = self.peek()
                raise ValueError(f"comp: expected ',' or '}}' but found {tok[0]} ({tok[1]!r}) at line {tok[2]}")
        self.expect("RBRACE")
        return CompTable(fields=fields, items=items)


def parse_comp(text: str) -> CompTable:
    """Parse Fusion ``.comp`` script text into a :class:`CompTable` tree.

    Raises ``ValueError`` (never any other exception type) on malformed
    input — unterminated tables, stray tokens, or trailing content after
    the top-level table.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("comp: empty composition text")
    tokens = _tokenize(text)
    parser = _Parser(tokens)
    value = parser.parse_value()
    if parser.peek()[0] != "EOF":
        tok = parser.peek()
        raise ValueError(f"comp: trailing content after top-level table at line {tok[2]}")
    if not isinstance(value, CompTable):
        raise ValueError("comp: file must be a single top-level '{ ... }' table")
    return value


# ── Tree helpers ──────────────────────────────────────────────────────────


def _table_fields(value: Any) -> Dict[str, Any]:
    """Return the field dict of a value that is either a bare CompTable or
    a CompCtor wrapping one (e.g. ``ordered() { ... }``); {} otherwise."""
    if isinstance(value, CompCtor) and value.table is not None:
        return value.table.fields
    if isinstance(value, CompTable):
        return value.fields
    return {}


def _tools_fields(root: CompTable) -> Dict[str, Any]:
    if "Tools" not in root.fields:
        raise ValueError("comp: missing 'Tools' table - not a valid Fusion composition")
    tools_fields = _table_fields(root.fields["Tools"])
    if not tools_fields:
        raise ValueError("comp: 'Tools' table is empty or malformed")
    return tools_fields


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, CompTable):
        if value.fields and not value.items:
            return {k: _to_jsonable(v) for k, v in value.fields.items()}
        if value.items and not value.fields:
            return [_to_jsonable(v) for v in value.items]
        if not value.fields and not value.items:
            return {}
        return {
            "fields": {k: _to_jsonable(v) for k, v in value.fields.items()},
            "items": [_to_jsonable(v) for v in value.items],
        }
    if isinstance(value, CompCtor):
        out: Dict[str, Any] = {"_type": value.name}
        if value.args:
            out["_args"] = [_to_jsonable(a) for a in value.args]
        if value.table is not None:
            out["_table"] = _to_jsonable(value.table)
        return out
    if isinstance(value, CompIdent):
        return value.name
    return value


def _describe_input(value: Any) -> Dict[str, Any]:
    """Summarize a single ``Inputs`` table entry (usually ``Input { ... }``)."""
    fields = _table_fields(value)
    if "SourceOp" in fields or "Source" in fields:
        source_op = fields.get("SourceOp")
        source = fields.get("Source")
        ref = f"{source_op}.{source}" if source_op and source else (source_op or source)
        return {"connection": ref}
    if "KeyFrames" in fields:
        kf_fields = _table_fields(fields["KeyFrames"])
        return {"keyframed": True, "keyframe_count": len(kf_fields)}
    if "Value" in fields:
        return {"value": _to_jsonable(fields["Value"])}
    if "Expression" in fields:
        return {"expression": _to_jsonable(fields["Expression"])}
    if fields:
        return {"raw": {k: _to_jsonable(v) for k, v in fields.items()}}
    return {"value": _to_jsonable(value)}


def summarize_comp(root: CompTable) -> Dict[str, Any]:
    """Build a node summary {node_count, active_tool, nodes:[...]} from a
    parsed comp tree. Raises ValueError if there is no usable 'Tools' table."""
    tools_fields = _tools_fields(root)
    nodes = []
    for name, node_value in tools_fields.items():
        node_type = node_value.name if isinstance(node_value, CompCtor) else "Unknown"
        node_fields = _table_fields(node_value)
        inputs_summary: Dict[str, Any] = {}
        inputs_value = node_fields.get("Inputs")
        if inputs_value is not None:
            for input_name, input_value in _table_fields(inputs_value).items():
                inputs_summary[input_name] = _describe_input(input_value)
        nodes.append(
            {
                "name": name,
                "type": node_type,
                "input_count": len(inputs_summary),
                "inputs": inputs_summary,
            }
        )
    active_tool = root.fields.get("ActiveTool")
    return {
        "node_count": len(nodes),
        "active_tool": active_tool if isinstance(active_tool, str) else None,
        "nodes": nodes,
    }


# ── Serializer (used by the 'edit' action to write the tree back out) ────


def _format_number(value: Union[int, float]) -> str:
    if isinstance(value, int):
        return str(value)
    if value == int(value) and abs(value) < 1e15:
        return f"{value:.1f}"
    return f"{value:.10g}"


def _serialize_value(value: Any, indent: int) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, CompIdent):
        return value.name
    if isinstance(value, CompTable):
        return _serialize_table(value, indent)
    if isinstance(value, CompCtor):
        head = value.name
        if value.args:
            head += "(" + ", ".join(_serialize_value(a, indent) for a in value.args) + ")"
        if value.table is not None:
            return head + " " + _serialize_table(value.table, indent)
        return head
    if isinstance(value, list):
        return _serialize_table(CompTable(fields={}, items=list(value)), indent)
    if isinstance(value, dict):
        return _serialize_table(CompTable(fields=dict(value), items=[]), indent)
    raise ValueError(f"comp: cannot serialize value of type {type(value).__name__}")


def _serialize_table(table: CompTable, indent: int) -> str:
    pad = "\t" * indent
    pad_in = "\t" * (indent + 1)
    lines = []
    for key, value in table.fields.items():
        lines.append(f"{pad_in}{key} = {_serialize_value(value, indent + 1)},")
    for item in table.items:
        lines.append(f"{pad_in}{_serialize_value(item, indent + 1)},")
    if not lines:
        return "{}"
    return "{\n" + "\n".join(lines) + f"\n{pad}}}"


def serialize_comp(root: CompTable) -> str:
    """Serialize a comp tree back to Fusion ``.comp`` script text."""
    return _serialize_value(root, 0) + "\n"


# ── Editing ────────────────────────────────────────────────────────────


def _from_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return CompTable(fields={k: _from_jsonable(v) for k, v in value.items()}, items=[])
    if isinstance(value, list):
        return CompTable(fields={}, items=[_from_jsonable(v) for v in value])
    return value


def _get_or_create_table(fields: Dict[str, Any], key: str) -> CompTable:
    existing = fields.get(key)
    table = existing.table if isinstance(existing, CompCtor) and existing.table is not None else None
    if table is None and isinstance(existing, CompTable):
        table = existing
    if table is None:
        table = CompTable(fields={}, items=[])
        if isinstance(existing, CompCtor):
            existing.table = table
        else:
            fields[key] = table
    return table


def _apply_edit(root: CompTable, edit: Any, index: int) -> Dict[str, Any]:
    if not isinstance(edit, dict):
        raise ValueError(f"comp: edits[{index}] must be an object")

    tool_name = edit.get("tool")
    if not tool_name or not isinstance(tool_name, str):
        raise ValueError(f"comp: edits[{index}] missing required string field 'tool'")

    tools_fields = _tools_fields(root)
    node = tools_fields.get(tool_name)
    if node is None:
        return {"tool": tool_name, "applied": False, "reason": f"tool '{tool_name}' not found"}

    node_table = _get_or_create_table(tools_fields, tool_name) if isinstance(node, (CompCtor, CompTable)) else None
    if node_table is None:
        return {"tool": tool_name, "applied": False, "reason": f"tool '{tool_name}' has no editable body"}

    inputs_table = _get_or_create_table(node_table.fields, "Inputs")

    remove_input = edit.get("remove_input")
    if remove_input:
        if remove_input in inputs_table.fields:
            del inputs_table.fields[remove_input]
            return {"tool": tool_name, "input": remove_input, "applied": True, "action": "remove_input"}
        return {"tool": tool_name, "input": remove_input, "applied": False, "reason": "input not present"}

    input_name = edit.get("input")
    if not input_name or not isinstance(input_name, str):
        raise ValueError(f"comp: edits[{index}] needs a string 'input' (or 'remove_input')")

    connect = edit.get("connect")
    if connect:
        if not isinstance(connect, dict) or not connect.get("source_op"):
            raise ValueError(f"comp: edits[{index}].connect requires an object with 'source_op'")
        source_op = connect["source_op"]
        source = connect.get("source", "Output")
        inputs_table.fields[input_name] = CompCtor(
            name="Input",
            table=CompTable(fields={"SourceOp": source_op, "Source": source}),
        )
        return {
            "tool": tool_name,
            "input": input_name,
            "applied": True,
            "action": "connect",
            "source": f"{source_op}.{source}",
        }

    if "value" not in edit:
        raise ValueError(f"comp: edits[{index}] needs 'value', 'connect', or 'remove_input'")

    raw_value = edit["value"]
    inputs_table.fields[input_name] = CompCtor(
        name="Input", table=CompTable(fields={"Value": _from_jsonable(raw_value)})
    )
    return {"tool": tool_name, "input": input_name, "applied": True, "action": "set_value", "value": raw_value}


# ── Built-in sample (used by 'inspect' when no path/content is given) ────

SAMPLE_COMP = """{
\tTools = ordered() {
\t\tMediaIn1 = MediaIn {
\t\t\tCtrlWZoom = false,
\t\t\tInputs = {
\t\t\t\tGlobalIn = Input { Value = 0 },
\t\t\t\tGlobalOut = Input { Value = 100 },
\t\t\t},
\t\t},
\t\tColorCorrector1 = ColorCorrector {
\t\t\tCtrlWZoom = false,
\t\t\tInputs = {
\t\t\t\tMasterRGBGain = Input { Value = 1.05 },
\t\t\t\tMasterRGBGamma = Input { Value = 1.0 },
\t\t\t\tInput = Input { SourceOp = "MediaIn1", Source = "Output" },
\t\t\t},
\t\t},
\t\tMediaOut1 = MediaOut {
\t\t\tCtrlWZoom = false,
\t\t\tInputs = {
\t\t\t\tIndex = Input { Value = 0 },
\t\t\t\tInput = Input { SourceOp = "ColorCorrector1", Source = "Output" },
\t\t\t},
\t\t},
\t},
\tActiveTool = "ColorCorrector1",
}
"""

_VALID_ACTIONS = ("inspect", "edit")


def _load_text(path: Optional[str], content: Optional[str]) -> tuple:
    """Resolve (text, source_label) from either inline content, a file path,
    or (inspect-only) the built-in sample. Raises ValueError/OSError on
    unreadable paths — callers let those surface as 'Error: ...'."""
    if content is not None:
        return content, "inline content"
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), path
    return None, None


def _do_inspect(path: Optional[str], content: Optional[str]) -> str:
    text, source_label = _load_text(path, content)
    if text is None:
        text, source_label = SAMPLE_COMP, "built-in sample"
    root = parse_comp(text)
    summary = summarize_comp(root)
    result = {"action": "inspect", "source": source_label, **summary}
    return json.dumps(result, indent=2)


def _do_edit(
    path: Optional[str],
    content: Optional[str],
    edits: Optional[List[Any]],
    output_path: Optional[str],
) -> str:
    if not edits:
        raise ValueError("comp: 'edit' requires a non-empty 'edits' list")
    if not isinstance(edits, list):
        raise ValueError("comp: 'edits' must be a list of edit objects")

    text, source_label = _load_text(path, content)
    if text is None:
        raise ValueError("comp: 'edit' requires either 'path' or 'content'")

    root = parse_comp(text)
    edit_results = [_apply_edit(root, edit, i) for i, edit in enumerate(edits)]
    new_text = serialize_comp(root)

    written_to = None
    if path:
        target = output_path or path
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(new_text)
        written_to = target

    result: Dict[str, Any] = {
        "action": "edit",
        "source": source_label,
        "edits": edit_results,
        "applied_count": sum(1 for r in edit_results if r.get("applied")),
        "written_to": written_to,
        "verified": False,
    }
    if written_to is None:
        result["comp"] = new_text
    return json.dumps(result, indent=2)


@mcp.tool()
def offline_fusion(
    action: str,
    path: Optional[str] = None,
    content: Optional[str] = None,
    edits: Optional[List[Dict[str, Any]]] = None,
    output_path: Optional[str] = None,
) -> str:
    """Read/inspect/edit a Fusion composition (.comp) file offline. Never touches Resolve.

    Parameters:
    - action: one of
        - "inspect": parse a comp and return a node summary — node count,
          the ActiveTool name, and per-node {name, type, input_count,
          inputs}. Each input is summarized as {"value": ...} for a plain
          value, {"connection": "SourceOp.Source"} for a wired input,
          {"keyframed": true, "keyframe_count": N} for a KeyFrames block,
          or {"expression": ...} for a Fusion expression. Give either
          `path` (a .comp file on disk) or `content` (raw .comp text
          inline); if neither is given, a small built-in sample
          composition (MediaIn1 -> ColorCorrector1 -> MediaOut1) is
          inspected instead, so this action always has something to show.
        - "edit": apply a list of structural edits to a comp and re-write
          it. Requires `edits` plus either `path` or `content`. Each entry
          in `edits` is one of:
            - {"tool": str, "input": str, "value": <json>} — set (or
              create) that tool's input to a literal value.
            - {"tool": str, "input": str, "connect": {"source_op": str,
              "source": str (default "Output")}} — wire that input to
              another tool's output instead of a literal value.
            - {"tool": str, "remove_input": str} — delete an input entry.
          Edits targeting a tool name that isn't found in the comp are
          reported per-edit as {"applied": false, "reason": ...} rather
          than failing the whole call. If `path` is given the edited comp
          is written to `output_path` (or back to `path` if `output_path`
          is omitted); if only `content` is given, the edited text is
          returned inline as the "comp" field instead of being written
          anywhere. The result always carries "verified": false — this
          only guarantees the file re-parses as valid Fusion script text,
          not that a live Fusion page will accept every edited value.

      Any other value returns the list of valid actions instead of erroring.

    Malformed comp text (missing/empty 'Tools' table, unbalanced braces,
    unexpected tokens, or an unreadable path) never raises — it comes back
    as an "Error: ..." string, per the offline tool-set convention.
    """
    action_lower = action.strip().lower() if isinstance(action, str) else ""
    try:
        if action_lower == "inspect":
            return _do_inspect(path, content)
        if action_lower == "edit":
            return _do_edit(path, content, edits, output_path)
        return json.dumps(
            {
                "error": f"Unknown action '{action}'.",
                "valid_actions": list(_VALID_ACTIONS),
            },
            indent=2,
        )
    except Exception as e:  # noqa: BLE001 - offline tools never raise
        return f"Error: {e}"
