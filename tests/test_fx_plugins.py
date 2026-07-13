"""Offline unit tests for the ``tools.fx_plugins`` Resolve-independent surface.

These tests exercise the parts of ``fx_plugins`` that need NO running DaVinci
template/OFX scanners, and the DCTL/Fuse installers — driven entirely against
seeded ``tmp_path`` fixtures. Nothing here starts, mocks, or otherwise touches
a live Resolve instance; the module must import with Resolve absent (the
package's lazy-connect architecture guarantees this).

Mirrors the offline-test style of ``tests/test_offline_exposure.py``: import the
real package with no Resolve present and assert on the actual behaviour of the
registered tool callables. The ``@mcp.tool()`` decorator returns the underlying
function unchanged, so each tool is invoked directly.

installer at :23157 and the ``dctl`` installer at :23392): a NEW plugin is
written verbatim into its install directory, unsafe names are rejected, and an
``overwrite=False`` collision writes nothing.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# Make the ``src`` layout importable when pytest is invoked without the package
# installed (``python -m pytest tests/test_fx_plugins.py``). Repo root is the
# parent of this ``tests`` directory; the package lives under ``<root>/src``.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from davinci_resolve_mcp.tools import fx_plugins as fx  # noqa: E402


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def _registry_payload():
    """Invoke the tool and return its parsed JSON payload (asserting success)."""
    raw = fx.get_resolvefx_registry()
    assert not raw.startswith("Error:"), raw
    data = json.loads(raw)
    assert data["success"] is True
    return data


def test_registry_loads_vendored_json_with_no_resolve():
    data = _registry_payload()
    # it was loaded from disk with no Resolve session available.
    assert data["count"] == len(data["plugins"])
    assert data["count"] > 0
    assert os.path.isfile(data["source"])


def test_every_entry_regid_is_ofx_prefixed_wireid():
    data = _registry_payload()
    for entry in data["plugins"]:
        wire_id = entry["wireId"]
        assert wire_id, f"entry has empty wireId: {entry}"
        assert not str(wire_id).startswith("ofx."), (
            f"wireId must NOT carry the 'ofx.' prefix: {wire_id}"
        )
        assert entry["regid"] == f"ofx.{wire_id}", (
            f"RegID for {entry['name']!r} must be 'ofx.'+wireId, got {entry['regid']!r}"
        )


def test_sampled_entry_regid_matches_ofx_plus_wireid():
    data = _registry_payload()
    # Sample the first entry explicitly (the acceptance criterion) and confirm
    # its Fusion RegID is exactly 'ofx.' + wireId.
    sample = data["plugins"][0]
    assert sample["regid"] == "ofx." + sample["wireId"]


# ---------------------------------------------------------------------------
# enumerate_templates — seeded Fusion/Templates tree
# ---------------------------------------------------------------------------


def _seed_template(root, category, name, ext=".setting"):
    """Create ``<root>/Edit/<category>/<name><ext>`` and return its path."""
    d = os.path.join(root, "Edit", category)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{name}{ext}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{\n\tTools = {}\n}\n")
    return path


def test_enumerate_templates_finds_seeded_setting(tmp_path, monkeypatch):
    root = tmp_path / "Fusion" / "Templates"
    seeded = _seed_template(str(root), "Titles", "My Lower Third")

    monkeypatch.setattr(fx, "_template_roots", lambda scope: [("user", str(root))])

    raw = fx.enumerate_templates("user")
    assert not raw.startswith("Error:"), raw
    data = json.loads(raw)
    assert data["success"] is True

    names = {t["name"] for t in data["templates"]}
    assert "My Lower Third" in names, data["templates"]

    entry = next(t for t in data["templates"] if t["name"] == "My Lower Third")
    # The bare Insert* name is the file basename minus its extension.
    assert entry["insert_name"] == "My Lower Third"
    assert entry["category"] == "Titles"
    assert entry["type"] == "setting"
    assert entry["insertable"] is True
    assert entry["path"] == seeded


def test_enumerate_templates_missing_directory_returns_empty(tmp_path, monkeypatch):
    missing = tmp_path / "does_not_exist" / "Fusion" / "Templates"
    monkeypatch.setattr(fx, "_template_roots", lambda scope: [("user", str(missing))])

    data = json.loads(fx.enumerate_templates("user"))
    assert data["success"] is True
    assert data["templates"] == []
    assert data["count"] == 0
    # A non-existent root is skipped, not scanned.
    assert data["scanned_roots"] == []


# ---------------------------------------------------------------------------
# enumerate_ofx — seeded OFX plugins dir
# ---------------------------------------------------------------------------


def test_enumerate_ofx_finds_seeded_bundle(tmp_path, monkeypatch):
    root = tmp_path / "OFX" / "Plugins"
    bundle = root / "MyPlugin.ofx.bundle" / "Contents"
    os.makedirs(str(bundle), exist_ok=True)

    monkeypatch.setattr(fx, "_ofx_plugin_roots", lambda: [str(root)])

    data = json.loads(fx.enumerate_ofx())
    assert data["success"] is True
    names = {p["name"] for p in data["plugins"]}
    assert "MyPlugin.ofx.bundle" in names, data["plugins"]
    entry = next(p for p in data["plugins"] if p["name"] == "MyPlugin.ofx.bundle")
    assert entry["path"] == str(root / "MyPlugin.ofx.bundle")
    # The bundle is recorded but NOT descended into.
    assert not any(
        p["path"].startswith(str(root / "MyPlugin.ofx.bundle") + os.sep)
        for p in data["plugins"]
    )


def test_enumerate_ofx_missing_directory_returns_empty(tmp_path, monkeypatch):
    missing = tmp_path / "no_ofx_here"
    monkeypatch.setattr(fx, "_ofx_plugin_roots", lambda: [str(missing)])

    data = json.loads(fx.enumerate_ofx())
    assert data["success"] is True
    assert data["plugins"] == []
    assert data["count"] == 0
    assert data["scanned_roots"] == []


# ---------------------------------------------------------------------------
# install_dctl — round-trip + edge/failure
# ---------------------------------------------------------------------------

_DCTL_SOURCE = "__DEVICE__ float3 transform(int p_Width, int p_Height, "
_DCTL_SOURCE += "int p_X, int p_Y, float p_R, float p_G, float p_B) { return "
_DCTL_SOURCE += "make_float3(p_R, p_G, p_B); }\n"


def test_install_dctl_round_trips_source(tmp_path, monkeypatch):
    lut_dir = tmp_path / "LUT"
    monkeypatch.setattr(fx, "_dctl_root", lambda category: str(lut_dir))

    raw = fx.install_dctl("MyLook", _DCTL_SOURCE)
    assert not raw.startswith("Error:"), raw
    data = json.loads(raw)
    assert data["success"] is True
    assert data["category"] == "lut"
    assert data["ext"] == ".dctl"

    path = data["path"]
    assert os.path.isfile(path)
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == _DCTL_SOURCE


def test_install_dctl_rejects_path_separator_name(tmp_path, monkeypatch):
    lut_dir = tmp_path / "LUT"
    monkeypatch.setattr(fx, "_dctl_root", lambda category: str(lut_dir))

    raw = fx.install_dctl("../evil", _DCTL_SOURCE)
    assert raw.startswith("Error:"), raw
    # Nothing was written anywhere under the install root (or its parent).
    if lut_dir.exists():
        assert list(lut_dir.rglob("*")) == []
    assert not (tmp_path / "evil.dctl").exists()
    assert not (tmp_path / "evil").exists()


def test_install_dctl_overwrite_false_collision_writes_nothing(tmp_path, monkeypatch):
    lut_dir = tmp_path / "LUT"
    monkeypatch.setattr(fx, "_dctl_root", lambda category: str(lut_dir))

    first = json.loads(fx.install_dctl("Keeper", _DCTL_SOURCE))
    path = first["path"]
    assert os.path.isfile(path)

    # A second install with a DIFFERENT source and overwrite defaulting to False
    # must error and leave the original file byte-for-byte intact.
    other = "__DEVICE__ float3 transform() { return make_float3(0,0,0); }\n"
    raw = fx.install_dctl("Keeper", other)
    assert raw.startswith("Error:"), raw
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == _DCTL_SOURCE

    # And overwrite=True does replace it.
    ok = json.loads(fx.install_dctl("Keeper", other, overwrite=True))
    assert ok["success"] is True
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == other


# ---------------------------------------------------------------------------
# install_fuse — round-trip + edge/failure
# ---------------------------------------------------------------------------

_FUSE_SOURCE = 'FuRegisterClass("MyFuse", CT_Tool, {}) \nfunction Process(req) end\n'


def test_install_fuse_round_trips_source(tmp_path, monkeypatch):
    fuses_dir = tmp_path / "Fuses"
    monkeypatch.setattr(
        fx, "_plugin_install_paths", lambda: {"fuses_dir": str(fuses_dir)}
    )

    raw = fx.install_fuse("MyFuse", _FUSE_SOURCE)
    assert not raw.startswith("Error:"), raw
    data = json.loads(raw)
    assert data["success"] is True

    path = data["path"]
    assert os.path.isfile(path)
    assert path == str(fuses_dir / "MyFuse.fuse")
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == _FUSE_SOURCE


def test_install_fuse_rejects_unsafe_name(tmp_path, monkeypatch):
    fuses_dir = tmp_path / "Fuses"
    monkeypatch.setattr(
        fx, "_plugin_install_paths", lambda: {"fuses_dir": str(fuses_dir)}
    )

    # A leading-digit / separator-bearing name violates [A-Za-z_][A-Za-z0-9_]*.
    raw = fx.install_fuse("bad/name", _FUSE_SOURCE)
    assert raw.startswith("Error:"), raw
    if fuses_dir.exists():
        assert list(fuses_dir.rglob("*")) == []
    assert not (tmp_path / "name.fuse").exists()


def test_install_fuse_overwrite_false_collision_writes_nothing(tmp_path, monkeypatch):
    fuses_dir = tmp_path / "Fuses"
    monkeypatch.setattr(
        fx, "_plugin_install_paths", lambda: {"fuses_dir": str(fuses_dir)}
    )

    first = json.loads(fx.install_fuse("Keeper", _FUSE_SOURCE))
    path = first["path"]
    assert os.path.isfile(path)

    other = 'FuRegisterClass("Keeper", CT_Tool, {})\n'
    raw = fx.install_fuse("Keeper", other)
    assert raw.startswith("Error:"), raw
    with open(path, "r", encoding="utf-8") as fh:
        assert fh.read() == _FUSE_SOURCE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
