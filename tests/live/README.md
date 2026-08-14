# `tests/live/` — deferred live-calibration harness

Everything under `tests/` (except this directory) is **offline**: it never
starts, connects to, or otherwise touches a running copy of DaVinci Resolve.
That's why every
write action in the offline tool set (`drx` `write`/`attach_lut`/`apply_op`,
`drp` `author`/`edit`, ...) ships with a `"verified": false` field: the
`.drx`/`.drp`/`.drt` byte formats are validated by re-parsing what was written
— never by a real Resolve instance.

This directory is the harness that closes that gap **once a machine with
DaVinci Resolve installed and running is available**. It cannot run in CI
(there is no Resolve there), so every test here:

- is marked `@pytest.mark.requires_resolve` (via the module-level
  `pytestmark` in `test_resolve_roundtrip.py`),
- is fully **collectable** with no Resolve present (`pytest --co` never
  executes a fixture body, so collection never tries to connect), and
- **skips** (never fails) the moment it actually runs and can't reach
  Resolve, via the `resolve_conn` fixture calling `pytest.skip(...)`.

## Prerequisites to actually run it

1. DaVinci Resolve (Studio or free) installed and **running**, with a
   database open.
2. Preferences → General → **External scripting using** set to `Local`
   (or `Network` if this test runner is on a different machine than
   Resolve).
3. `RESOLVE_SCRIPT_LIB` / `RESOLVE_SCRIPT_API` set if Resolve is installed
   in a non-default location — see
   `src/davinci_resolve_mcp/connection.py`'s `_PLATFORM_DEFAULT_PATHS` for
   the defaults per OS, which are used automatically if these env vars are
   unset.
4. `pip install -e .[offline]` (or at least `zstandard` + `pyyaml`) so the
   offline codec layer these tests exercise is present.

## Running it

```bash
# Collect only (works with or without Resolve; this is what CI runs):
./.venv/bin/python -m pytest tests/live -q -m requires_resolve --co -q

# Actually run it (needs Resolve running per the prerequisites above):
./.venv/bin/python -m pytest tests/live -v -m requires_resolve
```

Without Resolve reachable, the second command reports every test as
**skipped**, with a message pointing back at this checklist — never a
failure or an error.

## What each test verifies

Every Resolve-side test works inside its own throwaway, uuid-suffixed
scratch project (`mcp-live-roundtrip-<hex>`), which is always closed and
deleted on teardown. Nothing here touches, renames, or leaves artifacts
behind in a project you already have open.

| Test class | Procedure | What it proves |
| --- | --- | --- |
| `TestDrpRoundTrip` | author a `.drp` offline → `ProjectManager.ImportProject` → read the live project name / timeline list / Media-Pool structure back through the scripting API → compare to what was authored | the `.drt`/Media-Pool-XML authoring path (`formats/drp.py`, `formats/drt.py`) produces a `.drp` Resolve actually accepts, with the right project name, timeline name, and clip naming/`ClipPathBlob` linkage |
| `TestDrxWriteCdlReadback` | patch node 0's `saturation` in `colorslice-grid.drx` offline → `Graph.ApplyGradeFromDRX` on a live "Solid Color" generator clip → `TimelineItem.GetCDL()["Saturation"]` | the numeric UI-unit scale factor for `drx_codec`'s `saturation` parameter — the **only** grade parameter Resolve's scripting API exposes a getter for |
| `TestNonCdlGradeParamsRemainUncalibrated` | patch node 0's `log_midtone.r/g/b` in `log-wheels-grid.drx` offline → apply to a live clip → assert Resolve accepts it without erroring → grab a still | only the **structural** half of the round trip for every parameter that has *no* scripting readback path at all (see the list below) |

## Which `.drx` parameters remain uncalibrated even after this passes

Resolve's scripting API only exposes two read paths for a node's grade:
`TimelineItem.GetCDL()` (Slope/Offset/Power/Saturation) and the node LUT
get/set. Everything else `formats/drx_codec.py` knows how to decode/encode
has **no** scripted getter, so its numeric scale can only be verified by
opening the Color page and comparing the panel to the intended value by
eye:

- Log-wheel controls: `log_shadow.{r,g,b}`, `log_midtone.{r,g,b}`,
  `log_highlight.{r,g,b}`
- Classic primary-wheel controls: `lift.{r,g,b,master}`,
  `gain.{r,g,b,master}`, `gamma.{r,g,b,master}`, `offset.{r,g,b}`
- Curve/global controls: `contrast`, `pivot`, `color_boost`,
  `midtone_detail`
- Qualifier fields: `qualifier.hue_center`, `qualifier.hue_width`,
  `qualifier.hue_sym`, `qualifier.hue_soft`
- Any parameter `drx_codec.param_name()` doesn't recognize (surfaced as
  `param_<id>`) — these decode/re-encode byte-exact, but their *meaning*
  is entirely unconfirmed
- LUT attach (`off_drx` action `attach_lut`) and the grading-catalog ops
  (`off_drx` action `apply_op`: `contrast_normalize`, `saturation_match`,
  `black_balance`) — both synthesize values that have never been checked
  against a live grade

### Manual checklist for the parameters above

Since there is no scripted readback, calibrating any of these means:

1. Run `TestNonCdlGradeParamsRemainUncalibrated` (or a similar ad hoc
   `off_drx` `write` + `color.apply_grade_from_drx` call) with a known
   target value.
2. On the Color page, open the node's Log wheels / primaries panel by
   hand and read off the value Resolve actually applied.
3. Compare it to the value passed to `off_drx`'s `overrides_json`, work
   out the scale/offset relationship (if any), and update
   `formats/drx_codec.py`'s decode/encode path plus its `"verified"`
   contract accordingly.
4. Repeat per parameter family — the mapping is not guaranteed to be
   uniform across Log wheels, classic wheels, and the qualifier/curve
   fields.

Until that manual pass has been done and the corresponding docstrings
updated, treat every `"verified": false` field the offline `drx`/`drp`
tools return exactly as advertised: structurally valid, semantically
unconfirmed.
