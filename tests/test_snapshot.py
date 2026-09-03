"""Fixture test for the snapshot sync: local source/target repos, no network."""
import os
import subprocess
import sys
import tempfile
import unittest
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


class SnapshotTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
