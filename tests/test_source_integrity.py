import ast
import unittest
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SourceIntegrityTests(unittest.TestCase):
    def test_window_classes_do_not_redefine_methods(self):
        source_path = ROOT / "appector" / "window.py"
        module = ast.parse(source_path.read_text(encoding="utf-8"))

        for node in ast.walk(module):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = defaultdict(list)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[child.name].append(child.lineno)
            duplicates = {
                name: lines for name, lines in methods.items() if len(lines) > 1
            }
            self.assertEqual(duplicates, {}, f"Duplicate methods in {node.name}")

    def test_helpers_are_not_duplicated_across_modules(self):
        """F16: shared helpers had up to three copies in the codebase.

        These names are single-source-of-truth utilities: package-name
        validation/dedup, APT simulation parsing, and list formatting. A new
        definition in any module means a divergence waiting to happen, so each
        must be defined exactly once across the whole package (re-exports of
        a single canonical module are fine).
        """
        single_definition_names = {
            "SAFE_PACKAGE_ID_RE",
            "_deduplicate_package_ids",
            "_split_safe_package_ids",
            "_parse_apt_simulation_removed_packages",
            "_format_package_list",
        }
        definitions = defaultdict(list)

        for path in sorted((ROOT / "appector").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in single_definition_names:
                        definitions[node.name].append(f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Name)
                            and target.id in single_definition_names
                            and not isinstance(node.value, (ast.Attribute, ast.Name))
                        ):
                            # A fresh definition (call, comprehension, ...),
                            # not a re-export of another module's object.
                            definitions[target.id].append(
                                f"{path.name}:{node.lineno}"
                            )

        duplicated = {
            name: locations
            for name, locations in definitions.items()
            if len(locations) > 1
        }
        self.assertEqual(
            duplicated,
            {},
            f"Helpers re-defined instead of imported: {duplicated}",
        )


if __name__ == "__main__":
    unittest.main()
