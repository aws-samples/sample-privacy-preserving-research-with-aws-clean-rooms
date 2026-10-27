# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AC7 preview: change documented config parameters, synthesize, and show exactly what the
templates would change, without code changes and without deploying.

Usage: .venv/bin/python -m scripts.ac7_config_diff --set dotted.path=value [--set ...] [--config PATH]

Paths under ``tables`` select the list item by its ``key`` (e.g.
``tables.outcomes.analysis_rule.dimension_columns=[medication_class]``); values are parsed
as YAML. The original config file and every ``*.py`` file are hashed before and after to
prove nothing but a temporary config copy was used.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from airlock_lib import ROOT, aws
from airlock_lib.config import config_path, load_config

# L3: create-only properties (live CloudFormation schemas). A change here replaces the resource.
CREATE_ONLY = {
    "AWS::CleanRooms::Collaboration": {"CreatorDisplayName", "CreatorMemberAbilities", "CreatorMLMemberAbilities",
                                       "DataEncryptionMetadata", "JobLogStatus", "QueryLogStatus", "Members",
                                       "CreatorPaymentConfiguration", "AutoApprovedChangeTypes",
                                       "AllowedResultRegions"},
    "AWS::CleanRooms::Membership": {"CollaborationIdentifier"},
    "AWS::CleanRooms::ConfiguredTableAssociation": {"ConfiguredTableIdentifier", "Name", "MembershipIdentifier"},
}
REPLACEMENT = "REPLACEMENT (create-only) — run teardown first"
_MISSING = object()


class SetError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    stack: str
    logical_id: str
    resource_type: str
    name: str
    path: str
    old: Any
    new: Any

    @property
    def replacement(self) -> bool:
        top = self.path.split(".", 1)[0]
        return top in CREATE_ONLY.get(self.resource_type, set())

    def render(self) -> str:
        label = f" ({self.name})" if self.name else ""
        old = "<absent>" if self.old is _MISSING else json.dumps(self.old)
        new = "<absent>" if self.new is _MISSING else json.dumps(self.new)
        text = f"{self.stack} {self.logical_id}{label} {self.path}: {old} -> {new}"
        return f"{text}   {REPLACEMENT}" if self.replacement else text


# --------------------------------------------------------------------------- --set
def _parse_value(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return text


def apply_set(raw: dict, dotted: str, value_text: str) -> dict:
    """Return a deep copy of ``raw`` with ``dotted`` set to the YAML-parsed value."""
    out = copy.deepcopy(raw)
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        raise SetError(f"--set {dotted!r}: empty path")
    node: Any = out
    for i, part in enumerate(parts[:-1]):
        here = ".".join(parts[: i + 1])
        if isinstance(node, dict):
            if part not in node:
                raise SetError(f"--set {dotted}: unknown path {here!r}")
            node = node[part]
        elif isinstance(node, list):
            node = _select(node, part, dotted, here)
        else:
            raise SetError(f"--set {dotted}: {here!r} is not a mapping or list")
    last = parts[-1]
    value = _parse_value(value_text)
    if isinstance(node, dict):
        if last not in node:
            raise SetError(f"--set {dotted}: unknown path {dotted!r}")
        node[last] = value
    elif isinstance(node, list):
        item = _select(node, last, dotted, dotted)
        node[node.index(item)] = value
    else:
        raise SetError(f"--set {dotted}: parent is not a mapping or list")
    return out


def _select(items: list, part: str, dotted: str, here: str) -> Any:
    for item in items:
        if isinstance(item, dict) and (item.get("key") == part or item.get("id") == part):
            return item
    if part.isdigit() and int(part) < len(items):
        return items[int(part)]
    raise SetError(f"--set {dotted}: no list item with key/id {part!r} at {here!r}")


# --------------------------------------------------------------------------- template diff
def _walk(old: Any, new: Any, path: str, out: list[tuple[str, Any, Any]]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in list(old) + [k for k in new if k not in old]:
            _walk(old.get(k, _MISSING), new.get(k, _MISSING), f"{path}.{k}" if path else k, out)
        return
    if (isinstance(old, list) and isinstance(new, list) and len(old) == len(new)
            and any(isinstance(x, (dict, list)) for x in old + new)):
        for i, (a, b) in enumerate(zip(old, new)):
            _walk(a, b, f"{path}.{i}", out)
        return
    if old != new:
        out.append((path, old, new))


def diff_templates(old: dict[str, dict], new: dict[str, dict]) -> list[Change]:
    """Compare ``{stack_name: template}`` maps by logical ID and property path."""
    changes: list[Change] = []
    for stack in sorted(set(old) | set(new)):
        ro = (old.get(stack) or {}).get("Resources", {})
        rn = (new.get(stack) or {}).get("Resources", {})
        for lid in list(ro) + [k for k in rn if k not in ro]:
            a, b = ro.get(lid), rn.get(lid)
            rtype = (a or b).get("Type", "")
            props_a = (a or {}).get("Properties", {})
            props_b = (b or {}).get("Properties", {})
            name = props_b.get("Name") if isinstance(props_b.get("Name"), str) else (
                props_a.get("Name") if isinstance(props_a.get("Name"), str) else "")
            if a is None or b is None:
                changes.append(Change(stack, lid, rtype, name, "<resource>", "present" if a else _MISSING,
                                      "present" if b else _MISSING))
                continue
            diffs: list[tuple[str, Any, Any]] = []
            _walk(props_a, props_b, "", diffs)
            changes.extend(Change(stack, lid, rtype, name, p, o, n) for p, o, n in diffs)
        oo = (old.get(stack) or {}).get("Outputs", {})
        on = (new.get(stack) or {}).get("Outputs", {})
        diffs = []
        _walk(oo, on, "", diffs)
        changes.extend(Change(stack, "Outputs", "", "", p, o, n) for p, o, n in diffs)
    return changes


# --------------------------------------------------------------------------- driver
def _hashes() -> dict[str, str]:
    out = {}
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in (".venv", "cdk.out") or "__pycache__" in rel.parts:
            continue
        out[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _synth(cdk: str, cfg_file: Path, outdir: Path) -> dict[str, dict]:
    try:
        aws.run_cli([cdk, "synth", "--quiet", "-o", str(outdir)], None,
                    env_extra={"AIRLOCK_CONFIG": str(cfg_file)}, quiet=True)
    except aws.CliError as exc:
        print("\n".join(exc.tail[-40:]), file=sys.stderr)
        raise
    return {p.name.removesuffix(".template.json"): json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(outdir.glob("*.template.json"))}


def run(sets: list[str], config: str | None) -> int:
    cfg_file = config_path(config)
    load_config(cfg_file)  # original must be valid
    cfg_sha_before = hashlib.sha256(cfg_file.read_bytes()).hexdigest()
    py_before = _hashes()
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))

    pairs = []
    for s in sets:
        if "=" not in s:
            raise SetError(f"--set {s!r}: expected dotted.path=value")
        k, v = s.split("=", 1)
        pairs.append((k.strip(), v))
    modified = raw
    for k, v in pairs:
        modified = apply_set(modified, k, v)

    cdk = aws.cdk_bin()
    tmp = Path(tempfile.mkdtemp(prefix="airlock-ac7-"))
    try:
        def write_cfg(name: str, data: dict) -> Path:
            p = tmp / name
            p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            load_config(p)  # ConfigError -> exit 1
            return p

        mod_file = write_cfg("modified.yaml", modified)
        print(f"Config: {cfg_file} (unchanged); modified copy: {mod_file}")
        for k, v in pairs:
            print(f"  --set {k}={v}")

        original_t = _synth(cdk, cfg_file, tmp / "synth-original")
        modified_t = _synth(cdk, mod_file, tmp / "synth-modified")
        changes = diff_templates(original_t, modified_t)

        print("\nTemplate changes (original -> modified):")
        if not changes:
            print("  (none)")
        for c in changes:
            print(f"  {c.render()}")

        runtime_only = []
        if len(pairs) == 1:
            if not changes:
                runtime_only.append(pairs[0][0])
        else:
            for i, (k, v) in enumerate(pairs):
                single = write_cfg(f"single-{i}.yaml", apply_set(raw, k, v))
                if not diff_templates(original_t, _synth(cdk, single, tmp / f"synth-single-{i}")):
                    runtime_only.append(k)
        print("\nruntime-only keys (consumed by the demo scripts, not the templates):")
        print("  " + (", ".join(runtime_only) if runtime_only else "(none)"))

        replacements = [c for c in changes if c.replacement]
        if replacements:
            print(f"\n{len(replacements)} change(s) marked {REPLACEMENT}")
        else:
            print("\nNo create-only property changes: a plain deploy applies these in place.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    py_after = _hashes()
    cfg_sha_after = hashlib.sha256(cfg_file.read_bytes()).hexdigest()
    if py_after != py_before:
        changed = sorted(k for k in set(py_before) | set(py_after) if py_before.get(k) != py_after.get(k))
        raise aws.AirlockError(f"code changed during the check: {changed}")
    if cfg_sha_after != cfg_sha_before:
        raise aws.AirlockError(f"{cfg_file} changed during the check")
    print(f"py hashes unchanged ({len(py_after)} files); {cfg_file.name} sha256 unchanged ({cfg_sha_after[:16]}…)")
    print("No deploy was run.")
    return 0


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m scripts.ac7_config_diff", description=__doc__.splitlines()[0])
    ap.add_argument("--set", dest="sets", action="append", required=True, metavar="dotted.path=value",
                    help="config override (repeatable); tables.<key>.… selects a table by key")
    ap.add_argument("--config", help="config file (default: $AIRLOCK_CONFIG or config/airlock.yaml)")
    args = ap.parse_args(argv)

    def body() -> int:
        try:
            return run(args.sets, args.config)
        except SetError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1

    aws.run_main(body)


if __name__ == "__main__":
    main()
