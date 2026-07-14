"""Offline regression tests for the template-control parser in ``tools.fx_plugins``.

Drives ``_parse_template_controls`` with a synthetic Fusion ``.setting`` string so the
test is machine-independent (no installed pack, no Resolve). It pins the exact behaviour
verified against the real MotionVFX packs (ground truth: ``mTuber 4 Lower 01`` publishes
its header text as ``Input7`` whose default resolves to ``"Cinematographer"``, and groups
its header colour into ``Input10``–``Input13``):

  * the MacroOperator name is the ``macro_tool`` (== the placed tool name);
  * published text inputs are collected with their **resolved** default (read from the
    driven node when the InstanceInput has no own Default — recovers text placeholders);
  * R/G/B/A collapse into ONE colour control carrying all four keys;
  * ``'<X> Controls'`` section headers are dropped.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from davinci_resolve_mcp.tools import fx_plugins as fx  # noqa: E402

# Synthetic .setting mirroring a real MotionVFX title macro: a Header TextPlus node with a
# static StyledText/Size default, exposed on the macro via published InstanceInputs whose
# own Default is absent (so the default must be recovered from the node — the whole point
# of the resolver).
_SETTING = """
{
    Tools = ordered() {
        Header = TextPlus {
            Inputs = {
                StyledText = Input { Value = "Cinematographer", },
                Size = Input { Value = 0.08, },
            },
        },
        mTuber_4_Lower_01 = MacroOperator {
            Inputs = ordered() {
                HeaderControls = InstanceInput { Name = "Header Controls", },
                Input7 = InstanceInput { SourceOp = "Header", Source = "StyledText", Name = "Header Text", },
                Input10 = InstanceInput { SourceOp = "Header", Source = "Red1", Name = "Header Color", },
                Input11 = InstanceInput { SourceOp = "Header", Source = "Green1", Name = "Header Color", },
                Input12 = InstanceInput { SourceOp = "Header", Source = "Blue1", Name = "Header Color", },
                Input13 = InstanceInput { SourceOp = "Header", Source = "Alpha1", Name = "Header Color", },
                Input14 = InstanceInput { SourceOp = "Header", Source = "Size", Name = "Header Scale", },
            },
        },
    },
}
"""


def test_macro_tool_and_text_default_resolved():
    schema = fx._parse_template_controls(_SETTING)
    assert schema["macro_tool"] == "mTuber_4_Lower_01"
    tf = {t["input"]: t.get("default") for t in schema["text_fields"]}
    # ground truth: the header text default resolves from the driven node, not the InstanceInput.
    assert tf == {"Input7": "Cinematographer"}, tf


def test_color_channels_collapse_into_one_control():
    schema = fx._parse_template_controls(_SETTING)
    colors = [o for o in schema["options"] if o["kind"] == "color"]
    assert len(colors) == 1
    assert colors[0]["keys"] == ["Input10", "Input11", "Input12", "Input13"]


def test_section_header_dropped_and_scale_default_resolved():
    schema = fx._parse_template_controls(_SETTING)
    names = {o["name"] for o in schema["options"]}
    assert "Header Controls" not in names  # '<X> Controls' section header is not a control
    scale = next(o for o in schema["options"] if o["kind"] == "scale")
    assert scale["keys"] == ["Input14"] and scale["default"] == "0.08"


def test_driven_input_has_empty_default():
    # A published input whose Source has no static node value must NOT invent a default.
    setting = """
    { Tools = ordered() {
        N1 = Transform { Inputs = { Center = Input { SourceOp = "Path1", Source = "Value", }, }, },
        M = MacroOperator { Inputs = ordered() {
            Input1 = InstanceInput { SourceOp = "N1", Source = "Center", Name = "Position", },
        }, }, } }
    """
    schema = fx._parse_template_controls(setting)
    pos = next(o for o in schema["options"] if o["kind"] == "position")
    assert pos["default"] == ""  # driven/computed -> no static default (the honest ~1/3)
