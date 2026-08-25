#!/usr/bin/env python3

"""Enforce selective line-coverage thresholds from an Xcode result bundle."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Sequence


EXIT_POLICY_FAILURE = 1
EXIT_REPORT_ERROR = 2


@dataclass(frozen=True)
class CoverageFile:
    path: str
    covered_lines: int
    executable_lines: int

    @property
    def percentage(self) -> Decimal:
        return Decimal(self.covered_lines) * Decimal(100) / Decimal(self.executable_lines)


def percentage(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"invalid percentage: {value}") from error
    if parsed < 0 or parsed > 100:
        raise argparse.ArgumentTypeError("percentage must be between 0 and 100")
    return parsed


def nonnegative_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check selected production-code coverage in an Xcode result bundle."
    )
    parser.add_argument("--xcresult", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--aggregate-min", default=Decimal("80"), type=percentage)
    parser.add_argument("--file-min", default=Decimal("50"), type=percentage)
    parser.add_argument("--file-min-lines", default=10, type=nonnegative_integer)
    return parser.parse_args()


def report_error(message: str, detail: str | None = None) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    if detail:
        print(detail.rstrip(), file=sys.stderr)
    return EXIT_REPORT_ERROR


def load_report(xcresult: Path) -> dict[str, Any]:
    if not xcresult.exists():
        raise RuntimeError(f"xcresult does not exist: {xcresult}")

    command = [
        "xcrun",
        "xccov",
        "view",
        "--report",
        "--json",
        str(xcresult),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise RuntimeError(f"could not execute xccov: {error}") from error

    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "No diagnostic output."
        raise RuntimeError(f"xccov failed with exit code {completed.returncode}:\n{detail}")
    if not completed.stdout.strip():
        raise RuntimeError("xccov returned an empty coverage report")

    try:
        report = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"xccov returned malformed JSON at line {error.lineno}, column {error.colno}"
        ) from error
    if not isinstance(report, dict):
        raise RuntimeError("xccov JSON root is not an object")
    return report


def canonical_target_name(name: str) -> str:
    for suffix in (".app", ".framework", ".xctest"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def select_target(report: dict[str, Any], requested_name: str) -> dict[str, Any]:
    targets = report.get("targets")
    if not isinstance(targets, list):
        raise RuntimeError("coverage report has no valid targets array")

    named_targets = [target for target in targets if isinstance(target, dict)]
    exact_matches = [target for target in named_targets if target.get("name") == requested_name]
    if len(exact_matches) == 1:
        return exact_matches[0]

    canonical_matches = [
        target
        for target in named_targets
        if isinstance(target.get("name"), str)
        and canonical_target_name(target["name"]) == requested_name
    ]
    if len(canonical_matches) == 1:
        return canonical_matches[0]

    available = sorted(
        target["name"] for target in named_targets if isinstance(target.get("name"), str)
    )
    if len(exact_matches) > 1 or len(canonical_matches) > 1:
        raise RuntimeError(f"coverage target name is ambiguous: {requested_name}")
    available_text = ", ".join(available) if available else "none"
    raise RuntimeError(
        f"coverage target not found: {requested_name} (available targets: {available_text})"
    )


def validated_line_count(file_data: dict[str, Any], key: str, display_path: str) -> int:
    value = file_data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"invalid {key} for coverage file: {display_path}")
    return value


def filesystem_relative_path(path: Path, root: Path) -> Path | None:
    resolved_path = path.resolve(strict=False)
    resolved_root = root.resolve(strict=False)
    if not resolved_path.exists() or not resolved_root.is_dir():
        return None

    components: list[str] = []
    current = resolved_path
    while True:
        try:
            if os.path.samefile(current, resolved_root):
                return Path(*reversed(components))
        except OSError:
            return None

        parent = current.parent
        if parent == current:
            return None
        components.append(current.name)
        current = parent


def repository_relative_path(
    raw_path: str, source_root: Path
) -> tuple[Path, Path] | None:
    source_path = Path(raw_path).expanduser()
    if source_path.is_absolute():
        resolved_path = source_path.resolve(strict=False)
    else:
        resolved_path = (source_root / source_path).resolve(strict=False)

    relative_path = filesystem_relative_path(resolved_path, source_root)
    if relative_path is None:
        return None
    return resolved_path, relative_path


def is_test_source(relative_path: Path) -> bool:
    parts = relative_path.parts
    return any(part == "Tests" or part.endswith("Tests") for part in parts[:-1]) or (
        relative_path.name.endswith("Tests.swift")
    )


def is_generated_source(relative_path: Path) -> bool:
    return (
        relative_path.name.endswith(".generated.swift")
        or relative_path.name.endswith(".g.swift")
        or "Generated" in relative_path.parts
    )


def policy_exclusion_reason(relative_path: str) -> str | None:
    path = Path(relative_path)
    parts = path.parts
    basename = path.name

    if basename == "SignalLabApp.swift":
        return "application entry point"
    if "Views" in parts:
        return "SwiftUI Views directory"
    if "Previews" in parts:
        return "preview source"
    if basename.endswith("View.swift"):
        return "SwiftUI View filename"
    if basename.endswith(".generated.swift") or basename.endswith(".g.swift"):
        return "generated source filename"
    if "Generated" in parts:
        return "generated source directory"
    return None


def coverage_files(
    target_data: dict[str, Any], source_root: Path, production_root: Path
) -> tuple[int, list[tuple[str, str]], list[CoverageFile]]:
    files = target_data.get("files")
    if not isinstance(files, list):
        raise RuntimeError("selected coverage target has no valid files array")

    swift_file_count = 0
    mapped_swift_file_count = 0
    production_file_count = 0
    unmapped_swift_paths: list[str] = []
    excluded: list[tuple[str, str]] = []
    selected: list[CoverageFile] = []

    for file_data in files:
        if not isinstance(file_data, dict):
            raise RuntimeError("coverage target contains a malformed file record")
        raw_path = file_data.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise RuntimeError("coverage file record has no valid path")
        if not raw_path.endswith(".swift"):
            continue

        swift_file_count += 1
        covered_lines = validated_line_count(file_data, "coveredLines", raw_path)
        executable_lines = validated_line_count(file_data, "executableLines", raw_path)
        if covered_lines > executable_lines:
            raise RuntimeError(
                f"coveredLines exceeds executableLines for coverage file: {raw_path}"
            )

        mapped_path = repository_relative_path(raw_path, source_root)
        if mapped_path is None:
            unmapped_swift_paths.append(raw_path)
            excluded.append((raw_path, "outside repository source root"))
            continue

        mapped_swift_file_count += 1
        resolved_path, repository_path = mapped_path
        display_path = repository_path.as_posix()

        production_relative_path = filesystem_relative_path(
            resolved_path, production_root
        )
        if production_relative_path is None:
            excluded.append(
                (
                    display_path,
                    f"outside production source root ({production_root.name}/)",
                )
            )
            continue

        if is_test_source(production_relative_path):
            excluded.append((display_path, "test source"))
            continue

        production_file_count += 1
        reason = policy_exclusion_reason(display_path)
        if reason is not None:
            excluded.append((display_path, reason))
            continue
        if executable_lines == 0:
            excluded.append((display_path, "zero executable lines"))
            continue

        selected.append(CoverageFile(display_path, covered_lines, executable_lines))

    if swift_file_count == 0:
        raise RuntimeError("selected coverage target contains no Swift source-file records")
    if mapped_swift_file_count == 0:
        paths = "\n".join(f"- {path}" for path in sorted(unmapped_swift_paths))
        raise RuntimeError(
            "none of the selected target's Swift source paths could be mapped "
            f"under source root {source_root}:\n{paths}"
        )

    excluded.sort(key=lambda item: (item[0], item[1]))
    selected.sort(key=lambda item: item.path)
    return production_file_count, excluded, selected


def meets_threshold(covered: int, executable: int, threshold: Decimal) -> bool:
    return Decimal(covered) * Decimal(100) >= Decimal(executable) * threshold


def print_report(
    target_name: str,
    production_file_count: int,
    excluded: Sequence[tuple[str, str]],
    selected: Sequence[CoverageFile],
    aggregate_min: Decimal,
    file_min: Decimal,
    file_min_lines: int,
) -> int:
    print(f"Coverage target: {target_name}")
    print(f"Production Swift files discovered: {production_file_count}")
    print()
    print("Excluded files:")
    if excluded:
        for path, reason in excluded:
            print(f"- {path}: {reason}")
    else:
        print("- none")

    print()
    print(f"Selected executable files: {len(selected)}")
    total_covered = sum(file.covered_lines for file in selected)
    total_executable = sum(file.executable_lines for file in selected)
    print(f"Selected executable lines: {total_executable}")

    if not selected:
        print(f"Required aggregate: {aggregate_min:.2f}%")
        print(
            f"Per-file minimum: {file_min:.2f}% "
            f"for files with >= {file_min_lines} executable lines"
        )
        print("Coverage gate: NOT APPLICABLE")
        print("Result: PASS")
        return 0

    print()
    print("Selected file coverage:")
    for file in selected:
        print(
            f"- {file.path}: {file.covered_lines}/{file.executable_lines} "
            f"({file.percentage:.2f}%)"
        )

    aggregate_percentage = (
        Decimal(total_covered) * Decimal(100) / Decimal(total_executable)
    )
    aggregate_passes = meets_threshold(total_covered, total_executable, aggregate_min)
    file_failures = [
        file
        for file in selected
        if file.executable_lines >= file_min_lines
        and not meets_threshold(file.covered_lines, file.executable_lines, file_min)
    ]

    print()
    print(
        f"Selected aggregate: {total_covered}/{total_executable} "
        f"({aggregate_percentage:.2f}%)"
    )
    print(f"Required aggregate: {aggregate_min:.2f}%")
    print(
        f"Per-file minimum: {file_min:.2f}% "
        f"for files with >= {file_min_lines} executable lines"
    )

    if file_failures:
        print("Per-file failures:")
        for file in file_failures:
            print(f"- {file.path}: {file.percentage:.2f}% < {file_min:.2f}%")

    if aggregate_passes and not file_failures:
        print("Coverage gate: APPLICABLE")
        print("Result: PASS")
        return 0

    if not aggregate_passes:
        print(
            f"Aggregate failure: {aggregate_percentage:.2f}% < {aggregate_min:.2f}%"
        )
    print("Coverage gate: APPLICABLE")
    print("Result: FAIL")
    return EXIT_POLICY_FAILURE


def main() -> int:
    arguments = parse_arguments()
    source_root = arguments.source_root.expanduser().resolve(strict=False)
    if not source_root.is_dir():
        return report_error(f"source root is not a directory: {source_root}")
    production_root = (source_root / arguments.target).resolve(strict=False)
    if not production_root.is_dir():
        return report_error(
            f"production source root is not a directory: {production_root}"
        )

    try:
        report = load_report(arguments.xcresult.expanduser().resolve(strict=False))
        target_data = select_target(report, arguments.target)
        target_name = target_data.get("name")
        if not isinstance(target_name, str) or not target_name:
            raise RuntimeError("selected coverage target has no valid name")
        # CI guarantees source-content freshness by generating and consuming the
        # xcresult atomically from one checkout. xccov may legitimately omit Swift
        # files with no independently instrumented executable regions.
        production_count, excluded, selected = coverage_files(
            target_data, source_root, production_root
        )
    except RuntimeError as error:
        return report_error("coverage report could not be evaluated", str(error))

    return print_report(
        target_name,
        production_count,
        excluded,
        selected,
        arguments.aggregate_min,
        arguments.file_min,
        arguments.file_min_lines,
    )


if __name__ == "__main__":
    sys.exit(main())
