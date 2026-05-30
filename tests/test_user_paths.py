"""Tests for the ~/.racelink data-dir helpers and legacy-rename migration."""

import os
import shutil
import tempfile
import unittest

from racelink._user_paths import migrate_legacy_name, user_data_path


class UserDataPathTests(unittest.TestCase):
    def test_user_data_path_joins_under_racelink_home(self):
        p = user_data_path("rl_scenes.json")
        self.assertTrue(p.endswith(os.path.join(".racelink", "rl_scenes.json")))


class MigrateLegacyNameTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="rl_migrate_")

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _path(self, name):
        return os.path.join(self._dir, name)

    def test_renames_legacy_file_when_new_missing(self):
        legacy = self._path("scenes.json")
        new = self._path("rl_scenes.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("payload")
        migrate_legacy_name(new, "scenes.json")
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(new))
        with open(new, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "payload")

    def test_noop_when_new_already_exists(self):
        legacy = self._path("scenes.json")
        new = self._path("rl_scenes.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            fh.write("legacy")
        with open(new, "w", encoding="utf-8") as fh:
            fh.write("current")
        migrate_legacy_name(new, "scenes.json")
        # Both untouched: the legacy file is left in place, new is unchanged.
        self.assertTrue(os.path.exists(legacy))
        with open(new, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "current")

    def test_noop_when_legacy_absent(self):
        new = self._path("rl_scenes.json")
        migrate_legacy_name(new, "scenes.json")
        self.assertFalse(os.path.exists(new))

    def test_renames_legacy_directory(self):
        legacy = self._path("presets")
        new = self._path("rl_wled_presets")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "presets_x.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        migrate_legacy_name(new, "presets")
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.isfile(os.path.join(new, "presets_x.json")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
