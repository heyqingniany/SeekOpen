import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from seekopen import (
    AppConfig,
    FileRecord,
    NO_EXTENSION,
    add_favorite_paths,
    add_recent_file,
    file_type_key,
    filter_records_by_type,
    normalize_extension,
    normalize_extensions,
    remove_path_records,
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

    def test_favorites_are_deduplicated_without_reordering(self):
        first = Path("C:/work/main.py")
        second = Path("C:/tools/flash.py")
        favorites = add_favorite_paths([str(first)], [first, second, first])

        self.assertEqual(favorites, [str(first), str(second)])

    def test_recent_files_move_to_front_and_respect_limit(self):
        recent = [str(Path(f"C:/files/{index}.txt")) for index in range(4)]
        updated = add_recent_file(recent, recent[2], limit=3)

        self.assertEqual(updated, [recent[2], recent[0], recent[1]])

    def test_remove_records_does_not_touch_other_paths(self):
        paths = ["C:/one.py", "C:/two.py", "C:/three.py"]
        self.assertEqual(
            remove_path_records(paths, ["C:/two.py"]),
            ["C:/one.py", "C:/three.py"],
        )

    def test_quick_access_settings_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            with patch("seekopen.config_path", return_value=settings_path):
                config = AppConfig(
                    favorite_paths=["C:/tools/run.py"],
                    recent_files=["C:/work/main.c"],
                    track_recent_files=False,
                    last_view="favorites",
                )
                config.save()
                loaded = AppConfig.load()

            self.assertEqual(loaded.favorite_paths, [str(Path("C:/tools/run.py"))])
            self.assertEqual(loaded.recent_files, [str(Path("C:/work/main.c"))])
            self.assertFalse(loaded.track_recent_files)
            self.assertEqual(loaded.last_view, "favorites")


if __name__ == "__main__":
    unittest.main()
