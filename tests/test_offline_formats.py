"""
tests/test_offline_formats.py — offline formats/grading layer tests.

Exercises the .drx envelope+codec, ASC CDL, LUT-header, .drt/.drp zip
formats, and the pure CDL grade-math layer against deterministic synthetic
inputs generated in-process.

None of this touches DaVinci Resolve or the network: everything here reads
and writes files/bytes/strings in-process only, per the OFFLINE tool-set
architecture invariant.
"""

from __future__ import annotations

import hashlib
import io
import struct
import xml.etree.ElementTree as ET
import zipfile

import pytest

from davinci_resolve_mcp.formats import drp
from davinci_resolve_mcp.formats import drt
from davinci_resolve_mcp.formats import drx_codec
from davinci_resolve_mcp.formats import lut
from davinci_resolve_mcp.formats.cdl import (
    export_ccc,
    export_cdl,
    import_cdl,
    import_cdl_all,
)
from davinci_resolve_mcp.formats.drx_xml import (
    DrxParseError,
    parse_drx,
    parse_drx_content,
    serialize_drx,
)
from davinci_resolve_mcp.grading import cdl_ops

def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _length_field(number: int, payload: bytes) -> bytes:
    return _encode_varint((number << 3) | 2) + _encode_varint(len(payload)) + payload


def _grade_blob(params: list[tuple[int, float]]) -> str:
    body = bytearray()
    for param_id, value in params:
        value_field = _encode_varint((1 << 3) | 5) + struct.pack("<f", value)
        entry = _encode_varint(1 << 3) + _encode_varint(param_id)
        entry += _length_field(2, value_field)
        body.extend(_length_field(3, entry))
    return drx_codec.encode_body(
        bytes(body), drx_codec.FRAME_STORED, compressed=False
    ).hex()


def _drx_document(name: str, index: int, params: list[tuple[int, float]]) -> str:
    blob = _grade_blob(params)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Gallery::GyStill DbId="00000000-0000-0000-0000-{index:012d}">\n'
        f' <FieldsBlob/><Label>{name}</Label>\n'
        ' <pClipFullVer>'
        f'<ListMgt::LmVersion DbId="10000000-0000-0000-0000-{index:012d}">'
        '<FieldsBlob/><Name/><HasCorrection>true</HasCorrection>'
        f'<Body>{blob}</Body></ListMgt::LmVersion></pClipFullVer>\n'
        '</Gallery::GyStill>\n'
    )


_SYNTHETIC_DRX_DOCUMENTS = [
    (
        "primary",
        _drx_document(
            "primary",
            1,
            [(100663301, 1.1), (100663320, 0.02), (100663325, 1.05)],
        ),
    ),
    (
        "secondary",
        _drx_document(
            "secondary",
            2,
            [(2248147136, 0.5), (2248147137, 1.2), (2248147218, 0.15)],
        ),
    ),
    (
        "qualifier",
        _drx_document(
            "qualifier",
            3,
            [(137363470, 0.3), (137363471, 0.4), (137363474, 0.9)],
        ),
    ),
]


# ---------------------------------------------------------------------------
# drx_xml — generated documents parse and round-trip byte-exact
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,xml", _SYNTHETIC_DRX_DOCUMENTS, ids=[name for name, _ in _SYNTHETIC_DRX_DOCUMENTS]
)
def test_every_generated_document_parses_without_raising(name, xml, tmp_path):
    path = tmp_path / f"{name}.drx"
    path.write_text(xml, encoding="utf-8")
    parsed = parse_drx(str(path))
    assert isinstance(parsed, dict)
    assert parsed["stills"], f"{path}: expected at least one Gallery_GyStill"
    assert parsed["nodes"], f"{path}: expected at least one correction node"
    assert parsed["version"] is not None


@pytest.mark.parametrize(
    "name,xml", _SYNTHETIC_DRX_DOCUMENTS, ids=[name for name, _ in _SYNTHETIC_DRX_DOCUMENTS]
)
def test_every_generated_document_xml_round_trips_byte_exact(name, xml):
    parsed = parse_drx_content(xml)
    reserialized = serialize_drx(parsed)
    assert reserialized == xml, f"{name}: serialize_drx(parse_drx(x)) != x"


def test_drx_parse_malformed_xml_raises_typed_error():
    with pytest.raises(DrxParseError):
        parse_drx_content("not xml <<<")


def test_drx_parse_wrong_root_element_raises_typed_error():
    with pytest.raises(DrxParseError):
        parse_drx_content("<NotAGalleryStill><Foo/></NotAGalleryStill>")


def test_drx_parse_missing_file_raises_typed_error():
    with pytest.raises(DrxParseError):
        parse_drx("/nonexistent/path/does-not-exist.drx")


def test_serialize_drx_rejects_a_dict_without_tree():
    with pytest.raises(DrxParseError):
        serialize_drx({"stills": []})


# ---------------------------------------------------------------------------
# drx_codec — decode generated grade blobs and re-encode byte-exact
# ---------------------------------------------------------------------------
def _all_generated_bodies():
    bodies = []
    for name, xml in _SYNTHETIC_DRX_DOCUMENTS:
        parsed = parse_drx_content(xml)
        for node in parsed["nodes"]:
            desc = node.get("body")
            if desc and desc.get("present"):
                bodies.append((name, node.get("db_id") or "?", desc["hex"]))
    return bodies


_GENERATED_BODIES = _all_generated_bodies()
_GENERATED_BODY_IDS = [f"{name}::{db_id}" for name, db_id, _ in _GENERATED_BODIES]


def test_three_drx_blobs_are_available_for_round_trip():
    assert len(_GENERATED_BODIES) == 3


@pytest.mark.parametrize("name,db_id,hexblob", _GENERATED_BODIES, ids=_GENERATED_BODY_IDS)
def test_generated_blob_decodes_to_named_parameters(name, db_id, hexblob):
    decoded = drx_codec.decode_fields(hexblob)
    assert decoded["error"] is None
    assert decoded["stored"] is True
    assert decoded["framing"] == drx_codec.FRAME_STORED
    # Every decoded parameter must carry a resolvable semantic name.
    for param in decoded["params"]:
        assert param["name"] == drx_codec.param_name(param["id"])
        assert isinstance(param["value"], float)


@pytest.mark.parametrize("name,db_id,hexblob", _GENERATED_BODIES, ids=_GENERATED_BODY_IDS)
def test_generated_blob_round_trips_byte_exact(name, db_id, hexblob):
    decoded = drx_codec.decode_fields(hexblob)
    reencoded = drx_codec.encode_fields(decoded)
    assert reencoded == hexblob, f"{name} ({db_id}): encode_fields(decode_fields(b)) != b"


def test_decode_fields_edits_a_value_and_reencodes_structurally():
    # Pick a generated blob with at least one parameter and flip its value;
    # the codec must still produce a well-formed (decodable) blob.
    name, db_id, hexblob = next(b for b in _GENERATED_BODIES if drx_codec.decode_fields(b[2])["count"] > 0)
    decoded = drx_codec.decode_fields(hexblob)
    original_value = decoded["params"][0]["value"]
    decoded["params"][0]["value"] = original_value + 0.25
    edited_hex = drx_codec.encode_fields(decoded)
    assert edited_hex != hexblob
    redecoded = drx_codec.decode_fields(edited_hex)
    assert redecoded["error"] is None
    assert redecoded["params"][0]["value"] == pytest.approx(original_value + 0.25, abs=1e-5)


@pytest.mark.parametrize(
    "blob",
    [b"", b"\x81", b"\x81\x28\xb5", b"\x80\x01\x02", "zz", "81", "80aabbcc"],
)
def test_decode_fields_never_raises_on_malformed_or_short_input(blob):
    result = drx_codec.decode_fields(blob)
    assert isinstance(result, dict)
    assert "params" in result
    assert "unparsed" in result
    assert isinstance(result["params"], list)


def test_encode_fields_raises_typed_error_for_unreconstructable_dict():
    with pytest.raises(drx_codec.DrxCodecError):
        drx_codec.encode_fields({"not": "a decode_fields() result"})


def test_param_name_maps_known_and_falls_back_for_unknown_ids():
    assert drx_codec.param_name(100663301) == "saturation"
    assert drx_codec.param_name(100663320) == "lift.r"
    assert drx_codec.param_name(999999999) == "param_999999999"


# ---------------------------------------------------------------------------
# CDL — ASC CDL v1.2 import/export round-trip + malformed-input errors
# ---------------------------------------------------------------------------
_SAMPLE_CDL = {
    "slope": [1.05, 0.98, 1.02],
    "offset": [0.01, -0.005, 0.0],
    "power": [1.0, 1.02, 0.99],
    "sat": 1.1,
}


@pytest.mark.parametrize("doc_type", ["cdl", "cc", "ccc"])
def test_cdl_export_import_round_trips_for_every_doc_type(doc_type):
    xml = export_cdl(_SAMPLE_CDL, id="Shot_010", description="test shot", doc_type=doc_type)
    # Must be parseable by plain ElementTree (the format contract).
    root = ET.fromstring(xml)
    assert root is not None

    back = import_cdl(xml)
    for key in ("slope", "offset", "power"):
        assert back[key] == pytest.approx(_SAMPLE_CDL[key], abs=1e-6)
    assert back["sat"] == pytest.approx(_SAMPLE_CDL["sat"], abs=1e-6)
    assert back["id"] == "Shot_010"
    assert back["description"] == "test shot"


def test_cdl_accepts_rgb_mapping_channels_on_input():
    mapped = {
        "slope": {"r": 1.0, "g": 1.0, "b": 1.0},
        "offset": {"r": 0.0, "g": 0.0, "b": 0.0},
        "power": {"r": 1.0, "g": 1.0, "b": 1.0},
        "sat": 1.0,
    }
    xml = export_cdl(mapped)
    back = import_cdl(xml)
    assert back["slope"] == [1.0, 1.0, 1.0]
    assert back["offset"] == [0.0, 0.0, 0.0]
    assert back["power"] == [1.0, 1.0, 1.0]


def test_cdl_export_ccc_and_import_all_round_trip_multiple_corrections():
    corrections = [
        {"id": "A", "description": "shot a", "params": _SAMPLE_CDL},
        {"id": "B", "params": {"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 1.0}},
    ]
    xml = export_ccc(corrections)
    ET.fromstring(xml)  # parseable by plain ElementTree

    all_back = import_cdl_all(xml)
    assert [c["id"] for c in all_back] == ["A", "B"]
    assert all_back[0]["sat"] == pytest.approx(_SAMPLE_CDL["sat"])

    only_b = import_cdl(xml, id="B")
    assert only_b["sat"] == pytest.approx(1.0)


def test_cdl_export_ccc_requires_at_least_one_correction():
    with pytest.raises(ValueError):
        export_ccc([])


@pytest.mark.parametrize(
    "bad_params,expected_field",
    [
        ({"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1]}, "sat"),
        ({"slope": [1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 1.0}, "slope"),
        ({"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [0, 1, 1], "sat": 1.0}, "power"),
        ({"slope": [-1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": 1.0}, "slope"),
        ({"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": -1.0}, "sat"),
        ({"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "sat": "not-a-number"}, "sat"),
    ],
)
def test_cdl_malformed_params_raise_value_error_naming_offending_field(bad_params, expected_field):
    with pytest.raises(ValueError, match=expected_field):
        export_cdl(bad_params)


def test_cdl_import_malformed_xml_raises_value_error():
    with pytest.raises(ValueError):
        import_cdl("not xml <<<")


def test_cdl_import_non_cdl_document_raises_value_error():
    with pytest.raises(ValueError):
        import_cdl("<NotCDL/>")


def test_cdl_import_unknown_id_raises_value_error():
    xml = export_cdl(_SAMPLE_CDL, id="Shot_010")
    with pytest.raises(ValueError):
        import_cdl(xml, id="Shot_999_does_not_exist")


# ---------------------------------------------------------------------------
# LUT — .cube / .3dl header parsing + attach-reference
# ---------------------------------------------------------------------------
def test_parse_cube_lut_header(tmp_path):
    path = tmp_path / "look.cube"
    body = "\n".join("0 0 0" for _ in range(27))
    # DOMAIN_MIN/MAX must precede the *_SIZE keyword: the header parser stops
    # reading as soon as it has the size (the rest of the file is data rows).
    path.write_text(
        'TITLE "My Look"\n'
        "DOMAIN_MIN 0.0 0.0 0.0\n"
        "DOMAIN_MAX 1.0 1.0 1.0\n"
        "LUT_3D_SIZE 3\n" + body + "\n"
    )
    info = lut.parse_lut(str(path))
    assert info["type"] == "cube"
    assert info["kind"] == "3D"
    assert info["size"] == 3
    assert info["title"] == "My Look"
    assert info["domain_min"] == [0.0, 0.0, 0.0]
    assert info["domain_max"] == [1.0, 1.0, 1.0]


def test_parse_3dl_lut_lustre_style(tmp_path):
    path = tmp_path / "look.3dl"
    path.write_text("0 64 128 192 256 320 384 448 512\n0 0 0\n")
    info = lut.parse_lut(str(path))
    assert info == {"type": "3dl", "kind": "3D", "size": 9, "title": None, "style": "lustre"}


def test_parse_3dl_lut_nuke_style(tmp_path):
    path = tmp_path / "look_nuke.3dl"
    path.write_text("3DMESH\nMesh 4 6\n0 341 682 1023\n0 0 0\n")
    info = lut.parse_lut(str(path))
    assert info == {"type": "3dl", "kind": "3D", "size": 4, "title": None, "style": "nuke"}


def test_parse_lut_malformed_file_names_the_offending_line(tmp_path):
    path = tmp_path / "bad.cube"
    path.write_text('0.1 0.2 0.3\nTITLE "x"\n')
    with pytest.raises(lut.LutParseError, match="line 1"):
        lut.parse_lut(str(path))


def test_parse_lut_missing_size_header_raises_typed_error(tmp_path):
    path = tmp_path / "nosize.cube"
    path.write_text('TITLE "No Size"\n')
    with pytest.raises(lut.LutParseError):
        lut.parse_lut(str(path))


def test_attach_ref_returns_path_hash_and_size_without_loading_whole_lut(tmp_path):
    path = tmp_path / "big.cube"
    content = 'TITLE "Big"\nLUT_3D_SIZE 2\n' + "\n".join("0 0 0" for _ in range(8)) + "\n"
    path.write_text(content)
    ref = lut.attach_ref(str(path))
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert ref == {"path": str(path), "sha256": expected_hash, "size": len(content.encode("utf-8"))}


# ---------------------------------------------------------------------------
# .drt — timeline zip/SeqContainer: author -> parse round trip + validation
# ---------------------------------------------------------------------------
_DRT_SPEC = {
    "timelines": [
        {
            "name": "Scene 010",
            "frameRate": 23.976,
            "startTimecode": "01:00:00:00",
            "resolution": "1920x1080",
            "tracks": [
                {
                    "type": "video",
                    "clips": [
                        {"start": 0, "duration": 48, "in": 0, "out": 48, "mediaFilePath": "/media/a.mov"},
                        {"start": 48, "duration": 24, "in": 100, "out": 124, "mediaFilePath": "/media/b.mov"},
                    ],
                },
                {"type": "audio", "clips": []},
            ],
        }
    ],
    "metadata": {"createdBy": "test"},
}


def test_drt_author_parse_round_trips_timeline_shape():
    blob = drt.author_drt(_DRT_SPEC)
    parsed = drt.parse_drt(blob)

    assert len(parsed["timelines"]) == 1
    tl = parsed["timelines"][0]
    assert tl["name"] == "Scene 010"
    assert tl["frameRate"] == pytest.approx(23.976)
    assert tl["resolution"] == "1920x1080"
    assert len(tl["videoTracks"]) == 1
    assert len(tl["audioTracks"]) == 1

    clips = tl["videoTracks"][0]["clips"]
    assert len(clips) == 2
    assert clips[0]["mediaFilePath"] == "/media/a.mov"
    assert clips[0]["duration"] == 48
    assert clips[1]["start"] == 48
    assert clips[1]["mediaFilePath"] == "/media/b.mov"

    assert parsed["metadata"] == {"createdBy": "test"}


def test_drt_validate_accepts_a_well_formed_authored_drt():
    blob = drt.author_drt(_DRT_SPEC)
    result = drt.validate_drt(blob)
    assert result == {"valid": True, "errors": []}


def test_drt_validate_flags_a_drt_that_wrongly_contains_project_xml():
    blob = drt.author_drt(_DRT_SPEC)
    buf = io.BytesIO(blob)
    with zipfile.ZipFile(buf, "a") as zf:
        zf.writestr("project.xml", "<SM_Project/>")
    result = drt.validate_drt(buf.getvalue())
    assert result["valid"] is False
    assert any(e["code"] == "has-project" for e in result["errors"])


def test_drt_validate_flags_missing_seq_containers():
    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w"):
        pass
    result = drt.validate_drt(empty.getvalue())
    assert result["valid"] is False
    assert any(e["code"] == "no-seq" for e in result["errors"])


def test_drt_parse_non_zip_bytes_raises_typed_error():
    with pytest.raises(drt.DrtError):
        drt.parse_drt(b"this is not a zip file at all")


def test_drt_inject_and_extract_seq_container_preserves_other_members_byte_for_byte():
    blob = drt.author_drt(_DRT_SPEC)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        original_members = {name: zf.read(name) for name in zf.namelist()}

    new_xml = drt.build_seq_container_xml({"name": "Injected", "tracks": []})
    updated = drt.inject_seq_container(blob, new_xml, name="Primary1/SeqContainer2.xml")

    with zipfile.ZipFile(io.BytesIO(updated)) as zf:
        for name, content in original_members.items():
            assert zf.read(name) == content, f"member {name} was not preserved byte-for-byte"
        assert "Primary1/SeqContainer2.xml" in zf.namelist()

    extracted = drt.extract_seq_container(updated, "SeqContainer2.xml")
    assert "Injected" in extracted


# ---------------------------------------------------------------------------
# .drp — project zip: author -> parse round trip + Media-Pool-blob linkage
# ---------------------------------------------------------------------------
_DRP_SPEC = {
    "name": "Demo Project",
    "timelines": [{"name": "T1", "frameRate": 24, "tracks": []}],
    "clips": [{"name": "clipA", "mediaPath": "/media/clipA.mov", "kind": "video"}],
    "folders": [
        {"name": "Sub", "clips": [{"name": "clipB", "mediaPath": "/media/clipB.mov"}]},
    ],
}


def test_drp_author_read_round_trips_project_shape():
    blob = drp.author_drp(_DRP_SPEC)
    read = drp.read_drp(blob)

    assert read["name"] == "Demo Project"
    assert read["hasProjectXml"] is True

    root_names = {c["name"] for c in read["clips"]}
    assert "clipA" in root_names
    assert any(c["name"] == "T1" and c["kind"] == "timeline" for c in read["clips"])

    assert read["timelines"][0]["name"] == "T1"

    all_names = {c["name"] for c in read["allClips"]}
    assert {"clipA", "clipB", "T1"} <= all_names


def test_drp_media_pool_clip_links_by_the_clip_path_blob_not_plain_text():
    blob = drp.author_drp(_DRP_SPEC)
    read = drp.read_drp(blob)
    clip_a = next(c for c in read["clips"] if c["name"] == "clipA")
    assert clip_a["linkedByBlob"] is True
    assert clip_a["mediaPath"] == "/media/clipA.mov"

    # The blob codec itself round-trips independently of the surrounding XML.
    encoded = drp.encode_clip_path_blob("/media/clipA.mov")
    assert drp.decode_clip_path_blob(encoded) == "/media/clipA.mov"


def test_drp_edit_add_and_remove_clip_leaves_other_clips_untouched():
    blob = drp.author_drp(_DRP_SPEC)

    added = drp.edit_drp(blob, "add_clip", clip={"name": "clipC", "mediaPath": "/media/clipC.mov"})
    after_add = {c["name"] for c in drp.read_drp(added)["clips"]}
    assert {"clipA", "T1", "clipC"} <= after_add

    removed = drp.edit_drp(added, "remove_clip", name="clipA")
    after_remove = drp.read_drp(removed)
    names_after_remove = {c["name"] for c in after_remove["clips"]}
    assert "clipA" not in names_after_remove
    assert "clipC" in names_after_remove  # untouched by the removal
    assert "T1" in names_after_remove  # untouched by the removal


def test_drp_edit_remove_clip_requires_name_or_db_id():
    blob = drp.author_drp(_DRP_SPEC)
    with pytest.raises(drp.DrpFormatError):
        drp.edit_drp(blob, "remove_clip")


def test_drp_edit_unknown_action_raises_typed_error():
    blob = drp.author_drp(_DRP_SPEC)
    with pytest.raises(drp.DrpFormatError):
        drp.edit_drp(blob, "not_a_real_action")


def test_drp_read_missing_mp_folder_raises_typed_error():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project.xml", "<SM_Project/>")
    with pytest.raises(drp.DrpFormatError):
        drp.read_drp(buf.getvalue())


# ---------------------------------------------------------------------------
# grading.cdl_ops — pure CDL grade math: neutrality / idempotence / degeneracy
# ---------------------------------------------------------------------------
_NEUTRAL = {"slope": [1.0, 1.0, 1.0], "offset": [0.0, 0.0, 0.0], "power": [1.0, 1.0, 1.0], "sat": 1.0}


def test_contrast_normalize_neutral_identity_input_returns_neutral_no_drift():
    result = cdl_ops.contrast_normalize(_NEUTRAL, _NEUTRAL)
    assert result["slope"] == pytest.approx([1.0, 1.0, 1.0])
    assert result["offset"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["clamped"] is False
    assert result["verified"] is False


def test_saturation_match_neutral_identity_input_returns_neutral_no_drift():
    result = cdl_ops.saturation_match(_NEUTRAL, _NEUTRAL)
    assert result["sat"] == pytest.approx(1.0)
    assert result["clamped"] is False
    assert result["verified"] is False


def test_black_balance_neutral_identity_input_returns_neutral_no_drift():
    result = cdl_ops.black_balance(_NEUTRAL)
    assert result["already_neutral"] is True
    assert result["offset"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["verified"] is False


def test_saturation_match_moves_src_saturation_toward_ref():
    # Ratio (1.2 / 0.8 = 1.5) stays within the default clamp_scale [0.5, 2.0]
    # so the match lands exactly on ref's saturation.
    src = {**_NEUTRAL, "sat": 0.8}
    ref = {**_NEUTRAL, "sat": 1.2}
    result = cdl_ops.saturation_match(src, ref)
    assert result["sat"] > src["sat"]
    assert result["sat"] == pytest.approx(ref["sat"], abs=1e-6)
    assert result["clamped"] is False


def test_saturation_match_is_idempotent_when_src_equals_ref():
    src = {**_NEUTRAL, "sat": 1.3}
    once = cdl_ops.saturation_match(src, src)
    assert once["sat"] == pytest.approx(src["sat"])
    twice = cdl_ops.saturation_match(once, once)
    assert twice["sat"] == pytest.approx(once["sat"])


def test_contrast_normalize_degenerate_zero_luma_span_returns_safe_clamped_result():
    degenerate_src = {"slope": [0.0, 0.0, 0.0], "offset": [0.0, 0.0, 0.0], "power": [1.0, 1.0, 1.0], "sat": 1.0}
    result = cdl_ops.contrast_normalize(degenerate_src, _NEUTRAL)
    assert result["clamped"] is True
    assert result["slope"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["offset"] == pytest.approx([0.0, 0.0, 0.0])


def test_saturation_match_degenerate_zero_src_saturation_returns_safe_clamped_result():
    zero_sat_src = {**_NEUTRAL, "sat": 0.0}
    result = cdl_ops.saturation_match(zero_sat_src, _NEUTRAL)
    assert result["clamped"] is True
    assert result["sat"] == pytest.approx(0.0)


def test_black_balance_removes_a_shadow_cast_via_offset_only():
    cast = {"slope": [1.0, 1.0, 1.0], "offset": [0.02, 0.0, -0.01], "power": [0.9, 1.0, 1.1], "sat": 1.2}
    result = cdl_ops.black_balance(cast)
    assert result["already_neutral"] is False
    assert max(result["offset"]) - min(result["offset"]) == pytest.approx(0.0, abs=1e-9)
    # only offset moved; slope/power/sat pass through unchanged
    assert result["slope"] == pytest.approx(cast["slope"])
    assert result["power"] == pytest.approx(cast["power"])
    assert result["sat"] == pytest.approx(cast["sat"])


def test_apply_cdl_identity_transform_leaves_rgb_unchanged():
    rgb = [0.2, 0.5, 0.8]
    out = cdl_ops.apply_cdl(_NEUTRAL, rgb)
    assert out == pytest.approx(rgb, abs=1e-6)


def test_apply_cdl_rejects_wrong_length_rgb():
    with pytest.raises(ValueError):
        cdl_ops.apply_cdl(_NEUTRAL, [0.1, 0.2])
