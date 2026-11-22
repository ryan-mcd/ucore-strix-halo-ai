#!/usr/bin/env python3
"""Validate runtime configuration without requiring the documentation checkout."""
import argparse
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def validate_runtime():
    loader = importlib.machinery.SourceFileLoader("ai_box_validation", str(ROOT / "system_files/usr/bin/ai-box"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    runtime = importlib.util.module_from_spec(spec)
    loader.exec_module(runtime)
    errors = []
    example = ROOT / "config/ai-box.example.json"
    installed = ROOT / "system_files/usr/share/ai-box/config.example.json"
    if example.read_bytes() != installed.read_bytes():
        errors.append("The checkout and installed example configuration differ")
    for path in [*ROOT.glob("config/*.json"), *ROOT.glob("system_files/usr/share/ai-box/*.json")]:
        try:
            data = json.loads(path.read_text(), object_pairs_hook=unique_keys)
            if not isinstance(data, dict) or set(data) != runtime.KEYS:
                raise ValueError("Configuration keys do not match the runtime contract")
            # Templates intentionally require operator-reviewed integrity values.
            # Substitute syntactically valid dummy values only for static validation.
            if path.name.endswith("example.json"):
                data["image"] = "docker.io/kyuz0/amd-strix-halo-toolboxes@sha256:" + "a" * 64
                data["model_sha256"] = "b" * 64
            runtime.validated(data)
        except (ValueError, OSError) as error:
            errors.append(f"{path.relative_to(ROOT)}: {error}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, help="Also validate an explicitly selected documentation checkout (requires its dependencies)")
    args = parser.parse_args()
    errors = validate_runtime()
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return 1
    print("Runtime configuration schema and example consistency checks passed.")
    if args.docs is not None:
        validator = args.docs.resolve() / "scripts/validate.py"
        if not validator.is_file():
            print(f"Documentation validator not found: {validator}", file=sys.stderr)
            return 1
        return subprocess.run([sys.executable, str(validator)], check=False).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
