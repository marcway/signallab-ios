import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))

import check_coverage


def compile_section(path: Path, target: str = "SignalLab") -> dict:
    return {
        "commandInvocationDetails": {
            "commandDetails": (
                f"SwiftCompile normal arm64 {path} "
                f"(in target '{target}' from project 'SignalLab')"
            ),
            "exitCode": 0,
        },
        "location": {"url": path.as_uri()},
        "result": "succeeded",
        "subsections": [],
    }


def instrumented_section(target: str = "SignalLab") -> dict:
    return {
        "commandInvocationDetails": {
            "commandDetails": (
                "SwiftDriver Compilation -profile-coverage-mapping -profile-generate "
                f"(in target '{target}' from project 'SignalLab')"
            ),
            "exitCode": 0,
        },
        "result": "succeeded",
        "subsections": [],
    }


class CoverageReconciliationTests(unittest.TestCase):
    def test_production_file_present_in_xccov_is_accounted_for(self) -> None:
        self.assertEqual(
            check_coverage.reconcile_sources(
                {"SignalLab/Behavior.swift"},
                {"SignalLab/Behavior.swift"},
                {"SignalLab/Behavior.swift"},
            ),
            [],
        )

    def test_compiled_zero_region_file_may_be_absent_from_xccov(self) -> None:
        self.assertEqual(
            check_coverage.reconcile_sources(
                {"SignalLab/Instrument.swift"},
                {"SignalLab/Instrument.swift"},
                set(),
            ),
            [
                (
                    "SignalLab/Instrument.swift",
                    "no independently instrumented executable regions",
                )
            ],
        )

    def test_unaccounted_executable_production_file_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Behavior.swift"):
            check_coverage.reconcile_sources(
                {"SignalLab/Behavior.swift"}, set(), set()
            )

    def test_mapped_and_external_coverage_records_are_both_handled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "Repository"
            production_root = source_root / "SignalLab"
            production_root.mkdir(parents=True)
            mapped_source = production_root / "Behavior.swift"
            mapped_source.write_text("func behavior() {}\n")
            external_source = temporary_root / "External.swift"
            external_source.write_text("func external() {}\n")
            target = {
                "files": [
                    {
                        "path": str(external_source),
                        "coveredLines": 1,
                        "executableLines": 1,
                    },
                    {
                        "path": str(mapped_source),
                        "coveredLines": 1,
                        "executableLines": 1,
                    },
                ]
            }

            reported, excluded, selected = check_coverage.coverage_files(
                target, source_root, production_root
            )

            self.assertEqual(reported, {"SignalLab/Behavior.swift"})
            self.assertEqual(
                excluded,
                [(str(external_source), "outside repository source root")],
            )
            self.assertEqual(len(selected), 1)

    def test_no_safely_mapped_coverage_records_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            source_root = temporary_root / "Repository"
            production_root = source_root / "SignalLab"
            production_root.mkdir(parents=True)
            external_source = temporary_root / "External.swift"
            external_source.write_text("func external() {}\n")

            with self.assertRaisesRegex(RuntimeError, "none.*could be mapped"):
                check_coverage.coverage_files(
                    {
                        "files": [
                            {
                                "path": str(external_source),
                                "coveredLines": 1,
                                "executableLines": 1,
                            }
                        ]
                    },
                    source_root,
                    production_root,
                )

    def test_malformed_build_log_json_fails(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="{", stderr="")
        with patch("check_coverage.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "malformed build-log JSON"):
                check_coverage.load_build_log(Path("Result.xcresult"))

    def test_missing_coverage_instrumentation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "Behavior.swift"
            source.write_text("func behavior() {}\n")
            build_log = {
                "subsections": [compile_section(source)],
            }

            with self.assertRaisesRegex(RuntimeError, "no Swift driver invocation"):
                check_coverage.compiled_swift_paths(build_log, "SignalLab")

    def test_all_requested_target_swift_drivers_instrumented_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "Behavior.swift"
            source.write_text("func behavior() {}\n")
            build_log = {
                "subsections": [
                    instrumented_section(),
                    instrumented_section(),
                    compile_section(source),
                ]
            }

            self.assertEqual(
                check_coverage.compiled_swift_paths(build_log, "SignalLab"),
                {source},
            )

    def test_uninstrumented_requested_target_swift_driver_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "Behavior.swift"
            source.write_text("func behavior() {}\n")
            uninstrumented = instrumented_section()
            uninstrumented["commandInvocationDetails"]["commandDetails"] = (
                "SwiftDriver Compilation "
                "(in target 'SignalLab' from project 'SignalLab')"
            )
            build_log = {
                "subsections": [
                    instrumented_section(),
                    uninstrumented,
                    compile_section(source),
                ]
            }

            with self.assertRaisesRegex(RuntimeError, "not every Swift driver"):
                check_coverage.compiled_swift_paths(build_log, "SignalLab")

    def test_uninstrumented_other_target_swift_driver_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "Behavior.swift"
            source.write_text("func behavior() {}\n")
            other_target = instrumented_section(target="OtherTarget")
            other_target["commandInvocationDetails"]["commandDetails"] = (
                "SwiftDriver Compilation "
                "(in target 'OtherTarget' from project 'SignalLab')"
            )
            build_log = {
                "subsections": [
                    instrumented_section(),
                    other_target,
                    compile_section(source),
                ]
            }

            self.assertEqual(
                check_coverage.compiled_swift_paths(build_log, "SignalLab"),
                {source},
            )

    def test_compile_records_are_associated_with_requested_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "Other.swift"
            source.write_text("func other() {}\n")
            build_log = {
                "subsections": [
                    instrumented_section(),
                    compile_section(source, target="OtherTarget"),
                ]
            }

            with self.assertRaisesRegex(RuntimeError, "no successful SwiftCompile"):
                check_coverage.compiled_swift_paths(build_log, "SignalLab")


if __name__ == "__main__":
    unittest.main()
