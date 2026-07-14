#!/usr/bin/env python3
"""Generate a keyframed Ken-Burns / animated-zoom Fusion .comp for ImportFusionComp.

PROVEN reverse-engineered path for ANIMATED clip transforms in DaVinci Resolve via
scripting (verified live 2026-07-14). Two Resolve API walls make the "obvious" routes
impossible:

  * ``TimelineItem.AddKeyframe`` does NOT exist -> add_transform_keyframe crashes
    ('NoneType' object is not callable). Edit-page transform keyframes are unscriptable.
  * ``TimelineItem.AddFusionComp`` returns None on a plain footage clip, and
    ``ImportFusionComp`` also fails on a base clip that carries linked audio -> you
    cannot attach a Fusion comp to the footage clip directly.

BYPASS (both walls avoided): put a VIDEO-ONLY CARRIER of the same source range on an
upper track, then ``ImportFusionComp`` a comp whose keyframes live INSIDE the .comp file:

    MediaIn1 -> Transform1 (Size driven by a BezierSpline) -> MediaOut1

Resolve rebinds MediaIn1 to the carrier's footage on import, so the carrier shows the
SAME frames as the base clip beneath it, zooming. mediaType:1 keeps audio untouched.

Usage (called from execute_resolve_code or a future MCP tool):
    comp_text = build_kenburns_comp(dur_frames=1745, zoom_from=1.0, zoom_to=1.08,
                                    cx_from=0.5, cy_from=0.5, cx_to=0.5, cy_to=0.5)
    # write comp_text to a .comp file, then:
    #   mediaPool.AppendToTimeline([{mediaPoolItem, startFrame, endFrame,
    #       trackIndex=<upper>, recordFrame=<clip tl start>, mediaType:1}])
    #   carrier.ImportFusionComp(path)
"""


def build_kenburns_comp(
    dur_frames: int,
    zoom_from: float = 1.0,
    zoom_to: float = 1.08,
    cx_from: float = 0.5,
    cy_from: float = 0.5,
    cx_to: float = 0.5,
    cy_to: float = 0.5,
) -> str:
    """Return full Composition { ... } text for a keyframed Transform (Size + Center).

    - dur_frames: carrier length; keyframes span [0 .. dur_frames-1] (comp time == clip time).
    - zoom_from/zoom_to: Transform ``Size`` at start/end (1.0 = no zoom; 1.08 = +8%).
    - cx/cy_from/to: Transform ``Center`` (0..1, 0.5,0.5 = frame centre) for a pan while zooming.

    Center is a Point, so it needs a Path/2-key spline per axis; when from==to we emit a
    static Center Input (cheaper) and only keyframe Size.
    """
    last = max(1, int(dur_frames) - 1)
    pan = (abs(cx_to - cx_from) > 1e-6) or (abs(cy_to - cy_from) > 1e-6)

    size_spline = (
        "\t\tTransform1Size = BezierSpline {\n"
        "\t\t\tSplineColor = { Red = 237, Green = 142, Blue = 243 },\n"
        "\t\t\tNameSet = true,\n"
        "\t\t\tKeyFrames = {\n"
        f"\t\t\t\t[0] = {{ {zoom_from}, Flags = {{ Linear = true }} }},\n"
        f"\t\t\t\t[{last}] = {{ {zoom_to}, Flags = {{ Linear = true }} }}\n"
        "\t\t\t}\n"
        "\t\t},\n"
    )

    if pan:
        center_input = (
            "\t\t\t\tCenter = Input {\n"
            '\t\t\t\t\tSourceOp = "Transform1CenterPath",\n'
            '\t\t\t\t\tSource = "Position",\n'
            "\t\t\t\t},\n"
        )
        center_op = (
            "\t\tTransform1CenterPath = PolyPath {\n"
            "\t\t\tDrawMode = \"InsertAndModify\",\n"
            "\t\t\tInputs = {\n"
            "\t\t\t\tDisplacement = Input {\n"
            "\t\t\t\t\tSourceOp = \"Transform1CenterPathDisplacement\",\n"
            "\t\t\t\t\tSource = \"Value\",\n"
            "\t\t\t\t},\n"
            f"\t\t\t\tPolyLine = Input {{ Value = Polyline {{ Points = {{ "
            f"{{ Linear = true, LockY = true, X = {cx_from}, Y = {cy_from} }}, "
            f"{{ Linear = true, LockY = true, X = {cx_to}, Y = {cy_to} }} }} }} }} }},\n"
            "\t\t\t}\n"
            "\t\t},\n"
            "\t\tTransform1CenterPathDisplacement = BezierSpline {\n"
            "\t\t\tKeyFrames = {\n"
            "\t\t\t\t[0] = { 0.0, Flags = { Linear = true } },\n"
            f"\t\t\t\t[{last}] = {{ 1.0, Flags = {{ Linear = true }} }}\n"
            "\t\t\t}\n"
            "\t\t},\n"
        )
    else:
        center_input = (
            f"\t\t\t\tCenter = Input {{ Value = {{ {cx_from}, {cy_from} }}, }},\n"
        )
        center_op = ""

    return (
        "Composition {\n"
        "\tCurrentTime = 0,\n"
        f"\tRenderRange = {{ 0, {last} }},\n"
        f"\tGlobalRange = {{ 0, {last} }},\n"
        "\tHiQ = true,\n"
        "\tProxy = false,\n"
        "\tTools = ordered() {\n"
        "\t\tMediaIn1 = MediaIn {\n"
        "\t\t\tInputs = { Layer = Input { Value = \"0\", }, },\n"
        "\t\t\tViewInfo = OperatorInfo { Pos = { 0, 0 } },\n"
        "\t\t},\n"
        "\t\tTransform1 = Transform {\n"
        "\t\t\tCtrlWZoom = false,\n"
        "\t\t\tInputs = {\n"
        "\t\t\t\tSize = Input { SourceOp = \"Transform1Size\", Source = \"Value\", },\n"
        f"{center_input}"
        "\t\t\t\tInput = Input { SourceOp = \"MediaIn1\", Source = \"Output\", },\n"
        "\t\t\t},\n"
        "\t\t\tViewInfo = OperatorInfo { Pos = { 165, 0 } },\n"
        "\t\t},\n"
        f"{size_spline}"
        f"{center_op}"
        "\t\tMediaOut1 = MediaOut {\n"
        "\t\t\tInputs = { Input = Input { SourceOp = \"Transform1\", Source = \"Output\", }, },\n"
        "\t\t\tViewInfo = OperatorInfo { Pos = { 330, 0 } },\n"
        "\t\t}\n"
        "\t}\n"
        "}\n"
    )


if __name__ == "__main__":
    # quick self-test: emit a 3s +8% push to stdout
    print(build_kenburns_comp(90, 1.0, 1.08))
