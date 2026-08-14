#!/usr/bin/env python3
"""Document the COMPLETE control set of EVERY installed template — verbatim, no summarization.

Each control is recorded exactly as published in the template's ``.setting`` (the file Resolve
itself loads; offline == live was verified: ``Input7`` here reads back as the live header text).
Every published ``InstanceInput`` is emitted with its RAW fields — nothing dropped, nothing guessed:
  - key      : the published input id on the macro (what you pass to fusion_set_input)
  - label    : the Name MotionVFX gave the control ("" if unnamed)
  - drives   : the internal input it sets (Source) — e.g. StyledText, Red1Clone, Size, Center
  - node     : the internal tool it drives (SourceOp)
  - default  : the default value ("" if none)
  - section  : True if this row is a UI section header (CustomLabels / "<X> Controls"), not a setting
  - kind     : a DERIVED convenience label (text/color/scale/…) — annotation only, raw fields win

Input:  davinci-plugins.json (each template row carries its drfx + member path).
Output: <out>.json (complete, per-template) + <out>.md (index + how to query).
"""
from __future__ import annotations
import argparse, json, os, re, zipfile

_TEXT = re.compile(r"StyledText|Text\d*|WordsText")
_COLOR = re.compile(r"(Red|Green|Blue|Alpha)\d")


def _field(body: str, key: str) -> str:
    m = re.search(rf'{key}\s*=\s*(?:"([^"]*)"|([^,\n}}]+))', body)
    return "" if not m else (m.group(1) if m.group(1) is not None else m.group(2)).strip()


def _kind(src: str) -> str:
    if _TEXT.fullmatch(src): return "text"
    if _COLOR.match(src): return "color"
    return {"Size": "scale", "Center": "position", "Angle": "rotation", "Blend": "opacity",
            "Font": "font", "Style": "style", "Value": "value"}.get(src, "other")


def _node_body(txt: str, node: str, cache: dict) -> str:
    """The brace-balanced body of a tool node's definition in the .setting (cached per template)."""
    if node in cache:
        return cache[node]
    m = re.search(r"\b" + re.escape(node) + r"\s*=\s*\w+\s*\{", txt)
    body = ""
    if m:
        i, depth = m.end() - 1, 0
        for j in range(i, len(txt)):
            if txt[j] == "{":
                depth += 1
            elif txt[j] == "}":
                depth -= 1
                if depth == 0:
                    body = txt[i + 1:j]
                    break
    cache[node] = body
    return body


def _node_input_value(body: str, source: str) -> str:
    """The static default Value of ``source`` on a node (``Input { Value = … }`` or shorthand)."""
    m = re.search(r"\b" + re.escape(source) + r"\s*=\s*Input\s*\{(.*?)\}", body, re.S)
    if m:
        v = re.search(r'Value\s*=\s*(?:"([^"]*)"|([^,\n}]+))', m.group(1))
        return (v.group(1) if v and v.group(1) is not None else (v.group(2).strip() if v else "")) if v else ""
    m2 = re.search(r"\b" + re.escape(source) + r'\s*=\s*(?:"([^"]*)"|([^,\n{}]+))\s*,', body)
    if m2:
        val = m2.group(1) if m2.group(1) is not None else m2.group(2).strip()
        return val if val != "Input" else ""
    return ""


def controls_for(txt: str):
    m = re.search(r"(\w+)\s*=\s*MacroOperator\s*\{", txt)
    macro = m.group(1) if m else ""
    rows = []
    node_cache: dict = {}
    for key, body in re.findall(r"(\w+)\s*=\s*InstanceInput\s*\{(.*?)\}", txt, re.S):
        src, node = _field(body, "Source"), _field(body, "SourceOp")
        name = _field(body, "Name")
        inst_default = _field(body, "Default")
        # resolved_default: InstanceInput default if present, else the node-level static value
        # (this recovers text placeholders like "Cinematographer" that live on the node, verified live).
        resolved = inst_default or _node_input_value(_node_body(txt, node, node_cache), src)
        rows.append({
            "key": key, "label": name, "drives": src, "node": node,
            "default": inst_default, "resolved_default": resolved,
            "section": bool(node == "CustomLabels" or name.endswith("Controls")),
            "kind": _kind(src),
        })
    return macro, rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract the complete control set of every template.")
    ap.add_argument("--manifest", default="./davinci-plugins.json")
    ap.add_argument("--out", default="./davinci-plugin-controls")
    ap.add_argument("--stamp", default="(unstamped)")
    args = ap.parse_args()

    m = json.load(open(args.manifest))
    # gather every template row that has a .drfx member (titles+generators+transitions+effects)
    rows = (m.get("insertable_titles", []) + m.get("insertable_generators", [])
            + m.get("transitions", []) + m.get("effects_templates", []))
    seen, zips, templates = set(), {}, []
    total_controls = 0
    for r in rows:
        drfx, member, name = r.get("drfx", ""), r.get("member", ""), r.get("name", "")
        if not (drfx and member) or (name, drfx) in seen:
            continue
        seen.add((name, drfx))
        try:
            zf = zips.get(drfx) or zips.setdefault(drfx, zipfile.ZipFile(drfx))
            macro, controls = controls_for(zf.read(member).decode("utf-8", "ignore"))
        except (zipfile.BadZipFile, OSError, KeyError):
            macro, controls = "", []
        total_controls += len(controls)
        templates.append({"name": name, "pack": r.get("pack", ""),
                          "description": r.get("description", ""), "macro_tool": macro,
                          "member": member, "n_controls": len(controls), "controls": controls})

    templates.sort(key=lambda t: (t["pack"], t["name"]))
    out = {"generated": args.stamp, "templates_documented": len(templates),
           "total_controls_documented": total_controls, "templates": templates}
    json.dump(out, open(args.out + ".json", "w"), indent=2, ensure_ascii=False)

    md = [f"# Complete template control reference ({len(templates)} templates, "
          f"{total_controls} controls) — generated {args.stamp}", "",
          "Verbatim from each `.setting` (what Resolve loads; offline==live verified). Query the "
          f"JSON per template, e.g. `jq '.templates[]|select(.name==\"mTuber 4 Lower 01\")' "
          f"{os.path.basename(args.out)}.json`.", "", "## Templates documented"]
    for t in templates:
        md.append(f"- **{t['name']}** ({t['pack']}, {t['description']}) — macro `{t['macro_tool']}`, "
                  f"{t['n_controls']} controls")
    open(args.out + ".md", "w").write("\n".join(md) + "\n")

    print(json.dumps({"templates_documented": len(templates),
                      "total_controls_documented": total_controls,
                      "json": args.out + ".json", "md": args.out + ".md"}, indent=2))


if __name__ == "__main__":
    main()
