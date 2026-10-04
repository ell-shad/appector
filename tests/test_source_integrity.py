import ast
import unittest
from collections import defaultdict
from pathlib import Path


class SourceIntegrityTests(unittest.TestCase):
    def test_window_classes_do_not_redefine_methods(self):
        source_path = Path(__file__).resolve().parents[1] / "appector/window.py"
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


if __name__ == "__main__":
    unittest.main()
