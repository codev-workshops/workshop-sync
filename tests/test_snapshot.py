"""Fixture test for the snapshot sync: local source/target repos, no network."""
import os
import subprocess
import sys
import tempfile
import io
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync_from_source as S  # noqa: E402


def git(args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


def make_repo(path: Path, files: dict, branch="main"):
    path.mkdir(parents=True)
    git(["init", "-q", "-b", branch, "."], path)
    write(path, files)
    git(["add", "-A"], path)
    git(["commit", "-qm", "init"], path)
    return path


def write(path: Path, files: dict):
    for name, body in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def commit(path: Path, files: dict, msg="c"):
    write(path, files)
    git(["add", "-A"], path)
    git(["commit", "-qm", msg], path)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


class FakeTokens:
    src = tgt = ""
    can_create = False

    def for_org(self, org):
        return ""


class RepoFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.src = make_repo(self.root / "src", {"a.txt": "1\n", "keep.txt": "s\n"})
        self.tgt = make_repo(self.root / "tgt", {"a.txt": "1\n", "keep.txt": "s\n"})
        git(["config", "receive.denyCurrentBranch", "updateInstead"], self.tgt)
        cfg = {"source_org": "SRC", "target_org": "TGT"}
        self.r = S.Repo.__new__(S.Repo)
        self.r.cfg, self.r.source, self.r.target, self.r.tk = cfg, "src", "tgt", FakeTokens()
        self.r.dir = self.root / "work"
        self.r.dir.mkdir()
        S.run(["git", "init", "-q", "."], cwd=self.r.dir)
        S.run(["git", "remote", "add", "s", str(self.src)], cwd=self.r.dir)
        S.run(["git", "remote", "add", "t", str(self.tgt)], cwd=self.r.dir)


class SnapshotTest(RepoFixture):
    def test_squash_then_sync_keeps_target_commits(self):
        # 1. squash: target content becomes a single root commit, no source history
        src = self.r.fetch("s", "main")
        tgt = self.r.fetch("t", "main")
        snap = self.r.snapshot(self.r.tree(tgt), src, "main", parent=None)
        parents = S.run(["git", "rev-list", "--count", snap], cwd=self.r.dir)
        self.assertEqual(parents, "1", "snapshot must be a root commit")
        self.assertEqual(self.r.upstream_base(snap), src)
        S.run(["git", "push", "-q", "-f", str(self.tgt), f"{snap}:refs/heads/main"], cwd=self.r.dir)

        # 2. both sides move: upstream edits a.txt, the lab adds its own file
        new_src = commit(self.src, {"a.txt": "2\n"}, "upstream change")
        commit(self.tgt, {"lab.txt": "lab\n"}, "lab work")

        src2 = self.r.fetch("s", "main")
        tgt_head = self.r.fetch("t", "main", history=True)
        self.assertEqual(src2, new_src)
        # the marker survives a human commit pushed on top of the snapshot
        base = self.r.upstream_base(tgt_head)
        self.assertEqual(base, src)

        # 3. three-way merge of unrelated trees: upstream edit + lab file both land
        self.r.fetch("s", base)
        tree = self.r.merge_trees(self.r.tree(base), self.r.tree(tgt_head), self.r.tree(src2))
        files = S.run(["git", "ls-tree", "-r", "--name-only", tree], cwd=self.r.dir).split()
        self.assertIn("lab.txt", files)
        blob = S.run(["git", "show", f"{tree}:a.txt"], cwd=self.r.dir)
        self.assertEqual(blob, "2")

        merged = self.r.snapshot(tree, src2, "main", parent=tgt_head)
        self.assertEqual(self.r.upstream_base(merged), src2)
        # target history stays independent of the source: no source commit reachable
        shas = S.run(["git", "rev-list", merged], cwd=self.r.dir).split()
        self.assertNotIn(src2, shas)
        self.assertNotIn(src, shas)

    def test_conflicting_edit_raises(self):
        src = self.r.fetch("s", "main")
        tgt = self.r.fetch("t", "main")
        snap = self.r.snapshot(self.r.tree(tgt), src, "main", parent=None)
        S.run(["git", "push", "-q", "-f", str(self.tgt), f"{snap}:refs/heads/main"], cwd=self.r.dir)
        commit(self.src, {"a.txt": "source side\n"})
        commit(self.tgt, {"a.txt": "lab side\n"})
        src2 = self.r.fetch("s", "main")
        tgt2 = self.r.fetch("t", "main")
        with self.assertRaises(S.MergeConflict):
            self.r.merge_trees(self.r.tree(src), self.r.tree(tgt2), self.r.tree(src2))

    def test_deletion_upstream_propagates(self):
        src = self.r.fetch("s", "main")
        tgt = self.r.fetch("t", "main")
        os.remove(self.src / "keep.txt")
        commit(self.src, {}, "drop keep.txt")
        src2 = self.r.fetch("s", "main")
        tree = self.r.merge_trees(self.r.tree(src), self.r.tree(tgt), self.r.tree(src2))
        files = S.run(["git", "ls-tree", "-r", "--name-only", tree], cwd=self.r.dir).split()
        self.assertNotIn("keep.txt", files)

    def test_rewrite_org_references_makes_target_rewrite_merge_cleanly(self):
        mp = {"pairs": [{"source": "otter"}, {"source": "src", "target": "tgt"}]}
        rw = S.Rewrite(self.r.cfg, mp, ["org-references"])
        self.assertEqual(rw.text(b"see SRC/otter and SRC/\notter, SRC org, /orgs/SRC, SRC/unmapped"),
                         b"see TGT/otter and TGT/\notter, TGT org, /orgs/TGT, SRC/unmapped")
        self.assertEqual(rw.text(b"SRC-lab SRCx"), b"SRC-lab SRCx")
        png = b"\x89PNG\0SRC/otter"
        self.assertEqual(rw.text(png), png)

        # target hand-rewrote the org in a prompt; upstream then edited the same line
        commit(self.src, {"a.txt": "clone SRC/otter now\n", "b.png": "x"})
        src = self.r.fetch("s", "main")
        commit(self.tgt, {"a.txt": "clone TGT/otter now\n", "b.png": "x"})
        tgt = self.r.fetch("t", "main")
        self.assertEqual(self.r.rewrite_tree(self.r.tree(src), rw), self.r.tree(tgt))
        self.assertEqual(self.r.rewrite_tree(self.r.tree(src), S.Rewrite(self.r.cfg, mp, None)),
                         self.r.tree(src))
        new_src = commit(self.src, {"a.txt": "clone SRC/otter today\n"})
        src2 = self.r.fetch("s", new_src)
        tree = self.r.merge_trees(self.r.rewrite_tree(self.r.tree(src), rw), self.r.tree(tgt),
                                  self.r.rewrite_tree(self.r.tree(src2), rw))
        self.assertEqual(S.run(["git", "show", f"{tree}:a.txt"], cwd=self.r.dir),
                         "clone TGT/otter today")


class FastPathTest(RepoFixture):
    """`classify_branch` must not fetch when the API already proves the pair in sync."""

    def _classify(self, recorded_base):
        default, heads = self.r.refs("s")
        self.assertEqual(default, "main")
        _, tgt_heads = self.r.refs("t")
        with unittest.mock.patch.object(S, "recent_upstream_base", return_value=recorded_base):
            return S.classify_branch(self.r.cfg, self.r, "tgt", "main", "main", heads["main"],
                                     tgt_heads["main"], S.Rewrite(self.r.cfg, {}, None), self.r.tk)

    def test_in_sync_without_any_fetch(self):
        src = S.run(["git", "rev-parse", "HEAD"], cwd=self.src)
        with unittest.mock.patch.object(S.Repo, "fetch", side_effect=AssertionError("fetched")):
            b = self._classify(recorded_base=src)
        self.assertEqual(b["state"], S.IN_SYNC)
        self.assertEqual(b["src"], src)

    def test_stale_base_falls_back_to_full_classification(self):
        src = self.r.fetch("s", "main")
        tgt = self.r.fetch("t", "main")
        snap = self.r.snapshot(self.r.tree(tgt), src, "main", parent=None)
        S.run(["git", "push", "-q", "-f", str(self.tgt), f"{snap}:refs/heads/main"], cwd=self.r.dir)
        commit(self.src, {"a.txt": "2\n"}, "upstream change")
        b = self._classify(recorded_base=src)
        self.assertEqual(b["state"], S.UPDATE)
        self.assertEqual(b["base"], src)

    def test_no_marker_within_lookback_is_still_detected(self):
        b = self._classify(recorded_base=None)
        self.assertEqual(b["state"], S.UNSQUASHED)

    def test_trailer_from_log_prefers_newest(self):
        self.assertEqual(S.trailer_from_log(["human\n", f"snap\n\n{S.TRAILER} aaa\n",
                                             f"old\n\n{S.TRAILER} bbb\n"]), "aaa")
        self.assertIsNone(S.trailer_from_log(["nothing"]))


class ReportTest(unittest.TestCase):
    def test_needs_attention_merges_commands(self):
        path = Path(tempfile.mkdtemp()) / "r.json"
        S.write_report(str(path), "apply", {"updated": ["x@main"], "conflicts": [], "failed": []})
        self.assertEqual(S.json.loads(path.read_text())["needs_attention"], [])
        S.write_report(str(path), "discover", {"gone": [], "unmapped": ["new-repo"]})
        rep = S.json.loads(path.read_text())
        self.assertEqual(rep["needs_attention"], ["unmapped"])
        self.assertEqual(rep["apply"]["updated"], ["x@main"])


class AutoMergeTest(unittest.TestCase):
    cfg = {"source_org": "SRC", "target_org": "TGT"}

    def tokens(self, env):
        with unittest.mock.patch.dict(os.environ, env, clear=True), \
                unittest.mock.patch.object(S, "gh_cli_token", return_value=""):
            return S.Tokens(self.cfg)

    def test_bot_token_only_drives_pull_requests(self):
        tk = self.tokens({"GH_TOKEN": "app", "GITHUB_MIRROR_PAT": "pat", "GITHUB_SYNC_BOT_PAT": "bot"})
        self.assertEqual((tk.src, tk.tgt, tk.pulls, tk.bot), ("app", "pat", "bot", "bot"))
        tk = self.tokens({"GH_TOKEN": "app"})
        self.assertEqual((tk.pulls, tk.bot), ("app", ""))

    def test_merge_pr_uses_bot_and_pins_sha(self):
        calls = []

        def fake_api(path, method="GET", body=None, token=""):
            calls.append((path, method, body, token))
            return {}

        tk = self.tokens({"GH_TOKEN": "app", "GITHUB_SYNC_BOT_PAT": "bot"})
        with unittest.mock.patch.object(S, "api", fake_api):
            why = S.merge_pr(self.cfg, "repo", {"number": 7}, "abc123", tk)
        self.assertEqual(why, "")
        self.assertEqual(calls, [("repos/TGT/repo/pulls/7/merge", "PUT",
                                  {"merge_method": "rebase", "sha": "abc123"}, "bot")])

    def test_merge_refused_is_reported_not_raised(self):
        def fake_api(path, method="GET", body=None, token=""):
            raise urllib.error.HTTPError(path, 405, "", {}, io.BytesIO(
                b'{"message":"At least 1 approving review is required"}'))

        tk = self.tokens({"GH_TOKEN": "app", "GITHUB_SYNC_BOT_PAT": "bot"})
        with unittest.mock.patch.object(S, "api", fake_api):
            why = S.merge_pr(self.cfg, "repo", {"number": 7}, "abc123", tk)
        self.assertEqual(why, "405 At least 1 approving review is required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
