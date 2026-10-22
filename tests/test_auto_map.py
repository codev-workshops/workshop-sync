"""Tests for automatic mapping of new upstream repositories."""
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync_from_source as S  # noqa: E402


MAP_FIXTURE = """# Last verified: 2026-01-02
defaults: {}
pairs: []
new_repos:
  # Existing explanation remains untouched.
  - source: old-repo

# Same-name repositories must be reviewed.
collisions:
  - source: collision
    real_target: actual-target
ignore_targets: []
"""


class AddNewReposTest(unittest.TestCase):
    def test_adds_sorted_names_before_collisions_and_preserves_comments(self):
        updated = S.add_new_repos(MAP_FIXTURE, ["zeta", "alpha", "old-repo"], "2026-02-03")

        self.assertIn("# Last verified: 2026-02-03\n", updated)
        self.assertIn("  # Existing explanation remains untouched.\n", updated)
        self.assertIn("# Same-name repositories must be reviewed.\n", updated)
        self.assertEqual(updated.count("  # auto-mapped 2026-02-03 by discover --auto-map"), 1)
        self.assertEqual(
            updated[updated.index("# Same-name repositories must be reviewed.") :],
            MAP_FIXTURE[MAP_FIXTURE.index("# Same-name repositories must be reviewed.") :],
        )
        self.assertLess(updated.index("  - source: old-repo"), updated.index(
            "  # auto-mapped 2026-02-03 by discover --auto-map"))
        self.assertLess(updated.index("  - source: alpha"), updated.index("  - source: zeta"))
        self.assertLess(updated.index("  - source: zeta"), updated.index(
            "# Same-name repositories must be reviewed."))

        before = yaml.safe_load(MAP_FIXTURE)
        after = yaml.safe_load(updated)
        self.assertEqual([entry["source"] for entry in after["new_repos"]],
                         ["old-repo", "alpha", "zeta"])
        self.assertEqual(after["collisions"], before["collisions"])

    def test_empty_list_is_converted_to_block(self):
        text = """# Last verified: 2026-01-02
new_repos: []

# Keep this section comment.
collisions: []
"""
        updated = S.add_new_repos(text, ["fresh"], "2026-02-03")

        self.assertIn("new_repos:\n  # auto-mapped 2026-02-03 by discover --auto-map\n", updated)
        parsed = yaml.safe_load(updated)
        self.assertEqual(parsed["new_repos"], [{"source": "fresh"}])
        self.assertEqual(parsed["collisions"], [])

    def test_empty_block_accepts_a_new_entry(self):
        text = "new_repos:\n\ncollisions: []\n"
        updated = S.add_new_repos(text, ["fresh"], "2026-02-03")
        self.assertEqual(yaml.safe_load(updated)["new_repos"], [{"source": "fresh"}])

    def test_duplicates_and_empty_names_do_not_change_text(self):
        self.assertEqual(S.add_new_repos(MAP_FIXTURE, [], "2026-02-03"), MAP_FIXTURE)
        self.assertEqual(S.add_new_repos(MAP_FIXTURE, ["old-repo"], "2026-02-03"), MAP_FIXTURE)
        updated = S.add_new_repos(MAP_FIXTURE, ["old-repo", "new-repo"], "2026-02-03")
        self.assertEqual(updated.count("  - source: old-repo"), 1)
        self.assertEqual(updated.count("  - source: new-repo"), 1)


class DiscoverAutoMapTest(unittest.TestCase):
    cfg = {"source_org": "source-org", "target_org": "target-org"}
    source_names = {"a", "b", "c"}
    target_names = {"b"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.map_path = Path(self.temp.name) / "sync-map.yaml"
        map_path_patch = mock.patch.object(S, "MAP_PATH", self.map_path)
        map_path_patch.start()
        self.addCleanup(map_path_patch.stop)
        self.map_path.write_text(
            "# Last verified: 2026-01-02\ndefaults: {}\npairs: []\n"
            "new_repos: []\nignore_targets: []\n",
            encoding="utf-8",
        )
        self.report_path = Path(self.temp.name) / "report.json"

    def api_all(self, path, token):
        names = self.source_names if "source-org" in path else self.target_names
        return [{"name": name} for name in names]

    def run_discover(self, *, dry_run=False, auto_map=True):
        mp = yaml.safe_load(self.map_path.read_text(encoding="utf-8"))
        args = SimpleNamespace(
            auto_map=auto_map,
            dry_run=dry_run,
            map=str(self.map_path),
            report=str(self.report_path),
        )
        output = io.StringIO()
        with mock.patch.object(S, "api_all", side_effect=self.api_all), redirect_stdout(output):
            S.cmd_discover(self.cfg, mp, SimpleNamespace(src="src", tgt="tgt"), args)
        return output.getvalue(), yaml.safe_load(self.report_path.read_text(encoding="utf-8"))

    def test_auto_maps_only_names_without_same_name_target(self):
        output, report = self.run_discover()
        updated = yaml.safe_load(self.map_path.read_text(encoding="utf-8"))

        self.assertEqual({entry["source"] for entry in updated["new_repos"]}, {"a", "c"})
        self.assertEqual(report["discover"]["unmapped"], ["b"])
        self.assertEqual(report["discover"]["auto_mapped"], ["a", "c"])
        self.assertEqual(report["needs_attention"], ["unmapped"])
        self.assertIn("auto-mapped: a", output)
        self.assertIn("auto-mapped: c", output)
        self.assertNotIn("auto_mapped", report["needs_attention"])

    def test_dry_run_keeps_map_untouched_and_candidates_unmapped(self):
        original = self.map_path.read_text(encoding="utf-8")
        output, report = self.run_discover(dry_run=True)

        self.assertEqual(self.map_path.read_text(encoding="utf-8"), original)
        self.assertEqual(report["discover"]["unmapped"], ["a", "b", "c"])
        self.assertEqual(report["discover"]["auto_mapped"], [])
        self.assertIn("DRY-RUN would auto-map: a", output)
        self.assertIn("DRY-RUN would auto-map: c", output)

    def test_auto_map_rejects_different_map_path_without_writing(self):
        other_map_path = self.map_path.parent / "other-map.yaml"
        other_map_path.write_text("new_repos: []\n", encoding="utf-8")
        original_map = self.map_path.read_text(encoding="utf-8")
        original_other_map = other_map_path.read_text(encoding="utf-8")
        args = SimpleNamespace(
            auto_map=True,
            dry_run=False,
            map=str(other_map_path),
            report=str(self.report_path),
        )
        mp = yaml.safe_load(original_map)

        with mock.patch.object(S, "api_all", side_effect=self.api_all):
            with self.assertRaises(SystemExit) as raised:
                S.cmd_discover(self.cfg, mp, SimpleNamespace(src="src", tgt="tgt"), args)

        self.assertIn("--auto-map/--create-missing edit only", str(raised.exception))
        self.assertEqual(self.map_path.read_text(encoding="utf-8"), original_map)
        self.assertEqual(other_map_path.read_text(encoding="utf-8"), original_other_map)

    def test_ignore_targets_are_not_auto_mapped(self):
        self.map_path.write_text(
            "# Last verified: 2026-01-02\ndefaults: {}\npairs: []\n"
            "new_repos: []\nignore_targets:\n  - c\n",
            encoding="utf-8",
        )
        output, report = self.run_discover()
        updated = yaml.safe_load(self.map_path.read_text(encoding="utf-8"))

        self.assertEqual([entry["source"] for entry in updated["new_repos"]], ["a"])
        self.assertEqual(report["discover"]["unmapped"], ["b", "c"])
        self.assertEqual(report["discover"]["auto_mapped"], ["a"])
        self.assertNotIn("auto-mapped: c", output)

    def test_without_auto_map_keeps_existing_report_shape(self):
        original = self.map_path.read_text(encoding="utf-8")
        _, report = self.run_discover(auto_map=False)

        self.assertEqual(self.map_path.read_text(encoding="utf-8"), original)
        self.assertEqual(report["discover"]["unmapped"], ["a", "b", "c"])
        self.assertNotIn("auto_mapped", report["discover"])


class PublishMapReportTest(unittest.TestCase):
    def test_refused_merge_writes_left_open_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            map_path = root / "catalog" / "sync-map.yaml"
            map_path.parent.mkdir()
            map_path.write_text("new_repos: []\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "origin",
                            "https://github.com/codev-workshops/workshop-sync.git"],
                           cwd=root, check=True)
            subprocess.run(["git", "add", "catalog/sync-map.yaml"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=test", "-c",
                            "user.email=test@example.com", "commit", "-qm", "initial"],
                           cwd=root, check=True)
            map_path.write_text("new_repos:\n  - source: new\n", encoding="utf-8")
            report_path = root / "report.json"
            args = SimpleNamespace(
                map=str(map_path),
                auto_merge=True,
                dry_run=False,
                message="sync-map: test",
                report=str(report_path),
            )
            pr = {"number": 7, "html_url": "https://github.com/codev-workshops/workshop-sync/pull/7"}

            def fake_api(path, method="GET", body=None, token=""):
                return [] if method == "GET" else pr

            tokens = SimpleNamespace(src="app", pulls="bot", bot="bot")
            with mock.patch.object(S, "api", side_effect=fake_api), \
                    mock.patch.object(S, "git_auth"), \
                    mock.patch.object(S, "merge_pr", return_value="405 review required"), \
                    redirect_stdout(io.StringIO()):
                S.cmd_publish_map({"target_org": "codev-workshops"}, {}, tokens, args)

            report = yaml.safe_load(report_path.read_text(encoding="utf-8"))
            self.assertEqual(
                report["publish-map"]["left_open"],
                ["workshop-sync map PR https://github.com/codev-workshops/workshop-sync/pull/7: "
                 "405 review required"],
            )
            self.assertEqual(report["needs_attention"], ["left_open"])


MOVE_FIXTURE = """# Last verified: 2026-01-02
defaults: {}
pairs:
  - source: existing-pair
    target: existing-pair
    # seeded 2026-01-01 as a content snapshot
new_repos:
  # discovered 2026-01-01; keep this manual note byte-for-byte
  # this repo stays in new_repos until its seed succeeds
  - source: dbx-migration-plugin

  # auto-mapped 2026-01-02 by discover --auto-map
  - source: auto-repo
    target: auto-target
    rewrite:
      - from: "source/path"
        to: "target/path"

# Collisions documentation must remain untouched.
collisions:
  - source: collision
    real_target: actual-target
"""


class MoveToPairsTest(unittest.TestCase):
    def test_moves_entry_and_preserves_manual_comments_and_rewrite(self):
        parsed = yaml.safe_load(MOVE_FIXTURE)
        moved_entry = parsed["new_repos"][1]
        updated = S.move_to_pairs(MOVE_FIXTURE, [moved_entry], "2026-02-03")

        self.assertIn("# Last verified: 2026-02-03\n", updated)
        self.assertIn(
            "  # discovered 2026-01-01; keep this manual note byte-for-byte\n"
            "  # this repo stays in new_repos until its seed succeeds\n"
            "  - source: dbx-migration-plugin\n",
            updated,
        )
        self.assertNotIn("# auto-mapped 2026-01-02 by discover --auto-map", updated)
        self.assertIn(
            "  - source: auto-repo\n"
            "    target: auto-target\n"
            "    # seeded 2026-02-03 as a content snapshot\n"
            "    rewrite:\n"
            "      - from: \"source/path\"\n"
            "        to: \"target/path\"\n",
            updated,
        )
        self.assertEqual(
            updated[updated.index("# Collisions documentation") :],
            MOVE_FIXTURE[MOVE_FIXTURE.index("# Collisions documentation") :],
        )

        before = yaml.safe_load(MOVE_FIXTURE)
        after = yaml.safe_load(updated)
        self.assertEqual(after["new_repos"], [before["new_repos"][0]])
        self.assertEqual(after["pairs"][:-1], before["pairs"])
        self.assertEqual(after["pairs"][-1], {
            "source": "auto-repo",
            "target": "auto-target",
            "rewrite": [{"from": "source/path", "to": "target/path"}],
        })


CREATE_MAP = """# Last verified: 2026-01-02
defaults:
  source_org: source-org
  target_org: target-org
  sync: ff-only
  new_repos: mirror
pairs: []
new_repos:
  # Keep this failed seed and its note in new_repos.
  - source: failure
    target: failure-target
  - source: success
    target: success-target
    rewrite:
      - from: "before"
        to: "after"

collisions: []
"""


class CreateMissingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.map_path = Path(self.temp.name) / "sync-map.yaml"
        map_path_patch = mock.patch.object(S, "MAP_PATH", self.map_path)
        map_path_patch.start()
        self.addCleanup(map_path_patch.stop)
        self.map_path.write_text(CREATE_MAP, encoding="utf-8")
        self.mp = yaml.safe_load(CREATE_MAP)
        self.cfg = self.mp["defaults"]
        self.tokens = SimpleNamespace(
            can_create=True,
            src="source-token",
            tgt="target-token",
            for_org=lambda org: "source-token",
        )

    def test_success_moves_to_pairs_and_failure_stays_new(self):
        def fake_seed(cfg, source, target, tk, rw):
            if source == "failure":
                raise RuntimeError("seed rejected")

        def fake_api(path, method="GET", body=None, token=""):
            return {"private": False} if method == "GET" else {}

        args = SimpleNamespace(only=None, dry_run=False, map=str(self.map_path))
        output = io.StringIO()
        with mock.patch.object(S, "api_all", return_value=[]) as api_all, \
                mock.patch.object(S, "api", side_effect=fake_api) as api, \
                mock.patch.object(S, "seed", side_effect=fake_seed) as seed, \
                redirect_stdout(output):
            failed = S.create_missing(self.cfg, self.mp, self.tokens, args)

        updated_text = self.map_path.read_text(encoding="utf-8")
        updated = yaml.safe_load(updated_text)
        self.assertEqual(failed, ["failure -> failure-target: seed rejected"])
        self.assertEqual([entry["source"] for entry in updated["new_repos"]], ["failure"])
        self.assertEqual(updated["pairs"], [{
            "source": "success",
            "target": "success-target",
            "rewrite": [{"from": "before", "to": "after"}],
        }])
        self.assertIn("moved to pairs: success", output.getvalue())
        self.assertNotIn("moved to pairs: failure", output.getvalue())
        self.assertEqual(api_all.call_count, 1)
        self.assertEqual(api.call_count, 4)
        self.assertEqual(seed.call_count, 2)

    def test_dry_run_does_not_change_map_or_seed(self):
        original = self.map_path.read_text(encoding="utf-8")
        args = SimpleNamespace(only=None, dry_run=True, map=str(self.map_path))
        with mock.patch.object(S, "api_all", return_value=[]), \
                mock.patch.object(S, "api") as api, \
                mock.patch.object(S, "seed") as seed:
            failed = S.create_missing(self.cfg, self.mp, self.tokens, args)

        self.assertEqual(failed, [])
        self.assertEqual(self.map_path.read_text(encoding="utf-8"), original)
        api.assert_not_called()
        seed.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
