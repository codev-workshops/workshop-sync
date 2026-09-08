"""Classification rules for prune_branches: no network."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import prune_branches as P  # noqa: E402

NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def ref(name, days, login="alice", email="alice@example.com", protected=False):
    who = {"name": login, "email": email, "user": {"login": login}}
    return {
        "name": name,
        "branchProtectionRule": {"id": "x"} if protected else None,
        "target": {"committedDate": (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z"),
                   "author": who, "committer": who},
    }


def run(br, open_prs=set(), default="main"):
    protect = P.ALWAYS_KEEP | {default}
    return P.classify(br, default, protect, open_prs, 30, 90, NOW)


class Rules(unittest.TestCase):
    def test_protected_names_never_deleted(self):
        for name in ("main", "develop", "master"):
            self.assertEqual(run(ref(name, 1000))["action"], "keep")
        row = run(ref("trunk", 1000), default="trunk")
        self.assertEqual((row["action"], row["reason"]), ("keep", "default branch"))

    def test_github_protection_rule_kept(self):
        self.assertEqual(run(ref("release", 1000, protected=True))["action"], "keep")

    def test_devin_branch_thresholds(self):
        self.assertEqual(run(ref("devin/1-x", 31))["action"], "delete")
        self.assertEqual(run(ref("devin/1-x", 30))["action"], "keep")
        by_author = ref("feature", 31, login="devin-ai-integration[bot]")
        self.assertTrue(run(by_author)["devin"])
        self.assertEqual(run(by_author)["action"], "delete")

    def test_user_branch_thresholds(self):
        self.assertEqual(run(ref("feature", 91))["action"], "delete")
        self.assertEqual(run(ref("feature", 90))["action"], "keep")

    def test_open_pr_head_kept(self):
        self.assertEqual(run(ref("feature", 400), open_prs={"feature"})["action"], "keep")

    def test_unknown_pr_status_never_deletes(self):
        row = run(ref("devin/1-x", 400), open_prs=None)
        self.assertEqual(row["action"], "keep")
        self.assertIn("unknown", row["reason"])


if __name__ == "__main__":
    unittest.main()
