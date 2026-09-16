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


def prs(*specs):
    """specs: (branch, number, idle_days[, same_repo])"""
    out = {}
    for branch, number, idle, *rest in specs:
        out.setdefault(branch, []).append(dict(number=number, idle_days=idle, same_repo=rest[0] if rest else True))
    return out


def run(br, open_prs=None, default="main"):
    protect = P.ALWAYS_KEEP | {default}
    return P.classify(br, default, protect, {} if open_prs is None else open_prs, 30, 90, 14, NOW)


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

    def test_active_open_pr_head_kept(self):
        row = run(ref("feature", 400), open_prs=prs(("feature", 7, 14)))
        self.assertEqual((row["action"], row["close_prs"]), ("keep", []))

    def test_idle_open_pr_closed_and_branch_deleted(self):
        row = run(ref("feature", 20), open_prs=prs(("feature", 7, 15)))
        self.assertEqual((row["action"], row["close_prs"]), ("delete", [7]))
        self.assertIn("#7 idle 15d", row["reason"])

    def test_idle_pr_still_respects_protection(self):
        self.assertEqual(run(ref("develop", 20), open_prs=prs(("develop", 7, 15)))["action"], "keep")

    def test_one_active_pr_on_branch_keeps_it(self):
        row = run(ref("feature", 20), open_prs=prs(("feature", 7, 15), ("feature", 8, 2)))
        self.assertEqual(row["action"], "keep")

    def test_fork_pr_never_closed(self):
        self.assertEqual(run(ref("feature", 20), open_prs=prs(("feature", 7, 15, False)))["action"], "keep")

    def test_open_prs_by_head(self):
        pulls = [{"number": 1, "head": {"ref": "a", "repo": {"full_name": "o/r"}},
                  "updated_at": (NOW - timedelta(days=20)).isoformat().replace("+00:00", "Z")},
                 {"number": 2, "head": {"ref": "b", "repo": None}, "updated_at": NOW.isoformat().replace("+00:00", "Z")}]
        got = P.open_prs_by_head(pulls, "o/r", NOW)
        self.assertEqual(got, {"a": [dict(number=1, idle_days=20, same_repo=True)],
                               "b": [dict(number=2, idle_days=0, same_repo=False)]})

    def test_unknown_pr_status_never_deletes(self):
        row = P.classify(ref("devin/1-x", 400), "main", P.ALWAYS_KEEP | {"main"}, None, 30, 90, 14, NOW)
        self.assertEqual(row["action"], "keep")
        self.assertIn("unknown", row["reason"])


if __name__ == "__main__":
    unittest.main()
