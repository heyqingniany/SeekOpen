import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from seekopen import (
    AppConfig,
    FileRecord,
    NO_EXTENSION,
    file_type_key,
    filter_records_by_type,
    normalize_extension,
    normalize_extensions,
    scan_project,
)


class SeekOpenCoreTests(unittest.TestCase):
    def test_normalize_extensions(self):
        self.assertEqual(normalize_extension("*.PY"), ".py")
        self.assertEqual(normalize_extension("obj"), ".obj")
        self.assertEqual(normalize_extensions(["py", ".PY", " *.obj "]), [".obj", ".py"])

    def test_scan_project_applies_ignore_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "build").mkdir()
            (root / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
            (root / "src" / "main.pyc").write_bytes(b"ignored")
            (root / "build" / "output.txt").write_text("ignored", encoding="utf-8")

            records = scan_project(root, {".pyc"}, {"build"}, threading.Event())
            relative_paths = {str(record.relative) for record in records}

            self.assertIn(str(Path("src") / "main.py"), relative_paths)
            self.assertNotIn(str(Path("src") / "main.pyc"), relative_paths)
            self.assertFalse(any(path.startswith("build") for path in relative_paths))

    def test_dynamic_file_type_filter_modes(self):
        records = [
            FileRecord(Path("src"), Path("src"), True),
            FileRecord(Path("src/main.py"), Path("src/main.py"), False),
            FileRecord(Path("src/main.c"), Path("src/main.c"), False),
            FileRecord(Path("LICENSE"), Path("LICENSE"), False),
        ]
        shown = filter_records_by_type(records, "show", {".py", NO_EXTENSION})
        ignored = filter_records_by_type(records, "ignore", {".c"})

        self.assertEqual(file_type_key(Path("LICENSE")), NO_EXTENSION)
        self.assertEqual(
            {str(record.relative) for record in shown},
            {"src", str(Path("src/main.py")), "LICENSE"},
        )
        self.assertNotIn(str(Path("src/main.c")), {str(record.relative) for record in ignored})

    def test_filter_enabled_setting_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with patch("seekopen.config_path", return_value=settings_path):
                config = AppConfig(filter_enabled=False)
                config.save()
                loaded = AppConfig.load()

            self.assertFalse(loaded.filter_enabled)


if __name__ == "__main__":
    unittest.main()
