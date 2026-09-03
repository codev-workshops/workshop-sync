#!/usr/bin/env python3
"""Sync codev-workshops repos with their Cognition-Partner-Workshops sources.

Content, not history. A target repo has its own history made of *snapshot*
commits; upstream commits are never copied into it. Each snapshot commit records
the source commit it was taken from:

    Upstream-Commit: <sha>

which is the base for the next sync's three-way content merge, so target-side
commits keep surviving without any shared ancestry between the two repos.

Branch scope: the target's default branch, plus `main` and `develop` when they
exist on both sides. Nothing else is ever read or written.

Subcommands
    status     classify every mapped pair (default; read-only)
    apply      merge the current source content into every synced target branch
    squash     one-time: replace a target branch's history with a single snapshot
               commit of its current content (force push; --yes required)
    discover   re-fingerprint both orgs and report pairs missing from the map

Auth
    GITHUB_MIRROR_PAT  fine-grained PAT (Contents+Administration write on the target
                       org). Required for `apply --create-missing`; optional otherwise.
    Falls back to `gh auth token` for API calls and to ambient git credentials
    for pushes.

Examples
    scripts/sync_from_source.py status
    scripts/sync_from_source.py apply --dry-run
    scripts/sync_from_source.py apply --only=timesheet-app
    scripts/sync_from_source.py squash --only=calcom --yes
    scripts/sync_from_source.py discover
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

API = "https://api.github.com"
# `https://github.com:443/` is equivalent to `https://github.com/` but is not matched by
# an `insteadOf` rewrite, so token auth keeps working inside environments that route
# github.com through a credential-injecting proxy.
GIT_BASE = os.environ.get("SYNC_GITHUB_BASE", "https://github.com:443")
MAP_PATH = Path(__file__).resolve().parent.parent / "catalog" / "sync-map.yaml"

# Besides the default branch, only these may be synced.
EXTRA_BRANCHES = ("main", "develop")
TRAILER = "Upstream-Commit:"

IN_SYNC, UPDATE, UNSQUASHED = "in-sync", "update", "no-snapshot"
IDENT = ["-c", "user.name=workshop-sync", "-c", "user.email=workshop-sync@codev-workshops"]


class MergeConflict(RuntimeError):
    """The source content does not merge cleanly into the target branch."""


class Tokens:
    """Credentials per org.

    GITHUB_MIRROR_PAT is scoped to the target org only, so the source org is always
    read with the ambient token; mixing them up makes every source fetch 403.
    """

    def __init__(self, cfg):
        ambient = (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or gh_cli_token())
        pat = os.environ.get("GITHUB_MIRROR_PAT")
        if not ambient and not pat:
            sys.exit("No GitHub token: set GH_TOKEN/GITHUB_MIRROR_PAT or authenticate the gh CLI.")
        self.src = ambient or pat
        self.tgt = pat or ambient
        self.can_create = bool(pat)
        self._by_org = {cfg["source_org"]: self.src, cfg["target_org"]: self.tgt}

    def for_org(self, org: str) -> str:
        return self._by_org.get(org, self.src)


def gh_cli_token() -> str:
    try:
        return subprocess.check_output(["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return ""


def api(path: str, method: str = "GET", body: dict | None = None, token: str = "") -> dict:
    req = urllib.request.Request(
        f"{API}/{path.lstrip('/')}",
        method=method,
        data=json.dumps(body).encode() if body else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "workshop-sync",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read() or "{}")


def api_all(path: str, token: str) -> list[dict]:
    out, page = [], 1
    while True:
        sep = "&" if "?" in path else "?"
        chunk = api(f"{path}{sep}per_page=100&page={page}", token=token)
        if not chunk:
            break
        out.extend(chunk)
        page += 1
    return out


def redact(text: str) -> str:
    text = re.sub(r"(?i)\b(basic|bearer)\s+\S+", r"\1 ***", text)
    return re.sub(r"\b(github_pat_|ghp_|gho_|ghs_)\w+", r"\1***", text)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True,
        env: dict | None = None) -> str:
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True,
                       env={**os.environ, **env} if env else None)
    if check and p.returncode:
        raise RuntimeError(redact(f"{' '.join(cmd)}\n{p.stderr.strip()}"))
    return p.stdout.strip()


def auth_header(token: str) -> list[str]:
    """Pass the token via http.extraheader so it never lands in a remote URL or log."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]


def git_auth(argv: list[str], token: str, cwd: Path | None = None) -> str:
    """Run a network git command with an explicit token, falling back to ambient creds."""
    try:
        return run(["git", *auth_header(token), *argv], cwd=cwd)
    except RuntimeError as exc:
        if token and any(s in str(exc) for s in ("could not read Username", "Authentication failed", "403")):
            return run(["git", *argv], cwd=cwd)
        raise


class Repo:
    """A throwaway working copy holding the three trees a sync needs, and no history.

    Source commits are fetched with `--depth=1` (one tree, no ancestry), the target
    branch with `--filter=tree:0` (its own snapshot chain, trees on demand). A 1 GB
    upstream history is therefore never downloaded, let alone copied over.
    """

    def __init__(self, cfg, source: str, target: str, tk: "Tokens"):
        self.cfg, self.source, self.target, self.tk = cfg, source, target, tk
        self.dir = Path(tempfile.mkdtemp(prefix="wsync-"))
        run(["git", "init", "-q", "."], cwd=self.dir)
        run(["git", "remote", "add", "s", f"{GIT_BASE}/{cfg['source_org']}/{source}"], cwd=self.dir)
        run(["git", "remote", "add", "t", f"{GIT_BASE}/{cfg['target_org']}/{target}"], cwd=self.dir)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fetch(self, remote: str, ref: str, history: bool = False) -> str:
        """Fetch a commit and return its sha; `history` keeps the (treeless) commit chain."""
        token = self.tk.src if remote == "s" else self.tk.tgt
        flt = ["--filter=tree:0"] if history else ["--depth=1"]
        git_auth(["fetch", "-q", *flt, remote, ref], token, cwd=self.dir)
        return run(["git", "rev-parse", "FETCH_HEAD"], cwd=self.dir)

    def branches(self, remote: str) -> list[str]:
        token = self.tk.src if remote == "s" else self.tk.tgt
        out = git_auth(["ls-remote", "--heads", remote], token, cwd=self.dir)
        return [line.split("refs/heads/")[-1] for line in out.splitlines() if line]

    def upstream_base(self, sha: str) -> str | None:
        """Source commit recorded by the newest snapshot commit reachable from `sha`.

        Commits pushed by humans on top of a snapshot are expected, so the whole
        target branch is searched, newest first, not just its head.
        """
        log = run(["git", "log", "--format=%B%x01", sha], cwd=self.dir)
        for body in log.split("\x01"):
            for line in reversed(body.splitlines()):
                if line.startswith(TRAILER):
                    return line.split(":", 1)[1].strip()
        return None

    def tree(self, sha: str) -> str:
        return run(["git", "rev-parse", f"{sha}^{{tree}}"], cwd=self.dir)

    def merge_trees(self, base: str, ours: str, theirs: str) -> str:
        """Three-way merge of three unrelated trees; returns the merged tree sha.

        `read-tree -m` plus `merge-index` (git's `resolve` strategy) needs no common
        ancestor, which is the point: the two repos share no commits. It runs against
        a scratch index and work tree, so several branches can be merged in one clone.
        """
        scratch = Path(tempfile.mkdtemp(prefix="merge-"))
        env = {"GIT_DIR": str(self.dir / ".git"), "GIT_WORK_TREE": str(scratch),
               "GIT_INDEX_FILE": str(scratch / ".idx")}
        try:
            run(["git", "read-tree", "-m", "-u", base, ours, theirs], cwd=scratch, env=env)
            p = subprocess.run(["git", "merge-index", "-o", "git-merge-one-file", "-a"],
                               cwd=scratch, text=True, capture_output=True,
                               env={**os.environ, **env})
            if p.returncode:
                raise MergeConflict(redact(p.stdout.strip() or p.stderr.strip()))
            try:
                return run(["git", "write-tree"], cwd=scratch, env=env)
            except RuntimeError as exc:
                raise MergeConflict(redact(str(exc))) from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def snapshot(self, tree: str, upstream: str, branch: str, parent: str | None) -> str:
        """Commit `tree` as a snapshot of the source, optionally on top of `parent`."""
        msg = (f"Sync content from {self.cfg['source_org']}/{self.source}@{branch}\n\n"
               f"Source content only; upstream history is not copied.\n\n"
               f"{TRAILER} {upstream}\n")
        cmd = ["git", *IDENT, "commit-tree", tree, "-m", msg]
        if parent:
            cmd += ["-p", parent]
        return run(cmd, cwd=self.dir)

    def push(self, sha: str, branch: str, force: bool = False):
        ref = f"{'+' if force else ''}{sha}:refs/heads/{branch}"
        git_auth(["push", "t", ref], self.tk.tgt, cwd=self.dir)


def default_branch(org: str, repo: str, tk: Tokens) -> str:
    return api(f"repos/{org}/{repo}", token=tk.for_org(org))["default_branch"]


def synced_branches(r: Repo, sb_default: str, tb_default: str) -> list[tuple[str, str]]:
    """(source branch, target branch) pairs in scope: the default plus main/develop."""
    pairs = [(sb_default, tb_default)]
    src, tgt = set(r.branches("s")), set(r.branches("t"))
    for b in EXTRA_BRANCHES:
        if b not in (sb_default, tb_default) and b in src and b in tgt:
            pairs.append((b, b))
    return pairs


def classify_branch(r: Repo, sb: str, tb: str) -> dict:
    src = r.fetch("s", sb)
    tgt = r.fetch("t", tb, history=True)
    base = r.upstream_base(tgt)
    if base is None:
        state = UNSQUASHED
    elif base == src or r.tree(src) == r.tree(tgt):
        state = IN_SYNC
    else:
        state = UPDATE
    return dict(sb=sb, tb=tb, src=src, tgt=tgt, base=base, state=state)


def classify(cfg, pair, tk) -> dict:
    source, target = pair["source"], pair["target"]
    r = Repo(cfg, source, target, tk)
    try:
        sb = default_branch(cfg["source_org"], source, tk)
        tb = default_branch(cfg["target_org"], target, tk)
        branches = [classify_branch(r, s, t) for s, t in synced_branches(r, sb, tb)]
        return dict(source=source, target=target, branches=branches, repo=r, error="")
    except Exception as exc:
        r.close()
        return dict(source=source, target=target, branches=[], repo=None,
                    error=f"{redact(str(exc))}".splitlines()[-1][:160])


def describe(res) -> str:
    if res["error"]:
        return f"error: {res['error']}"
    return ", ".join(f"{b['tb']}={b['state']}" for b in res["branches"])


def cmd_status(cfg, mp, tk, args) -> list[dict]:
    results = []
    pairs = [p for p in mp["pairs"] if not args.only or args.only in (p["source"], p["target"])]
    for pair in pairs:
        if pair.get("sync", cfg["sync"]) == "off":
            continue
        res = classify(cfg, pair, tk)
        results.append(res)
        print(f"  {res['source']} -> {res['target']}: {describe(res)}", flush=True)
        if res["repo"] and not args.keep:
            res["repo"].close()
            res["repo"] = None
    return results


def cmd_apply(cfg, mp, tk, args):
    args.keep = True
    results = cmd_status(cfg, mp, tk, args)
    changed, conflicts, pending, failed = [], [], [], []
    for res in results:
        if res["error"]:
            failed.append((res, res["error"]))
            continue
        try:
            for b in res["branches"]:
                label = f"{res['target']}@{b['tb']}"
                if b["state"] == IN_SYNC:
                    continue
                if b["state"] == UNSQUASHED:
                    pending.append((res, b))
                    print(f"  {label}: no {TRAILER} snapshot marker — run `squash` first")
                    continue
                r: Repo = res["repo"]
                r.fetch("s", b["base"])
                try:
                    tree = r.merge_trees(r.tree(b["base"]), r.tree(b["tgt"]), r.tree(b["src"]))
                except MergeConflict as exc:
                    conflicts.append((res, b))
                    print(f"  {label}: CONFLICT {str(exc).splitlines()[0][:120]}")
                    continue
                if args.dry_run:
                    print(f"  {label}: DRY-RUN would snapshot {b['src'][:8]}")
                else:
                    sha = r.snapshot(tree, b["src"], b["sb"], parent=b["tgt"])
                    r.push(sha, b["tb"])
                    print(f"  {label}: snapshot {sha[:8]} <- {b['src'][:8]}")
                changed.append((res, b))
        except Exception as exc:
            # One unpushable repo (branch protection, push protection, revoked scope)
            # must never abort the rest of the run.
            failed.append((res, redact(str(exc)).splitlines()[-1][:200]))
            print(f"  FAILED {res['target']}: {failed[-1][1]}")
        finally:
            if res["repo"]:
                res["repo"].close()
                res["repo"] = None

    if args.create_missing:
        print("== new repos ==")
        create_missing(cfg, mp, tk, args)

    print(f"\nupdated: {len(changed)} | conflicts: {len(conflicts)} | "
          f"awaiting squash: {len(pending)} | failed: {len(failed)}")
    for res, b in conflicts:
        print(f"  conflict: {res['source']} -> {res['target']}@{b['tb']}")
    for res, why in failed:
        print(f"  failed: {res['source']} -> {res['target']}: {why}")


def cmd_squash(cfg, mp, tk, args):
    """Drop imported upstream history: one snapshot commit of the target's own content.

    The tree comes from the *target*, so everything committed in codev-workshops
    (lab work, merges) is kept; only the commit history is discarded. The current
    source commit is recorded as the base for future three-way merges.
    """
    if not args.yes and not args.dry_run:
        sys.exit("squash rewrites target history irreversibly: pass --yes (or --dry-run)")
    pairs = [p for p in mp["pairs"] if not args.only or args.only in (p["source"], p["target"])]
    done, failed = [], []
    for pair in pairs:
        if pair.get("sync", cfg["sync"]) == "off":
            continue
        source, target = pair["source"], pair["target"]
        r = Repo(cfg, source, target, tk)
        try:
            sb = default_branch(cfg["source_org"], source, tk)
            tb = default_branch(cfg["target_org"], target, tk)
            for s, t in synced_branches(r, sb, tb):
                src = r.fetch("s", s)
                tgt = r.fetch("t", t, history=True)
                if r.upstream_base(tgt) and not args.again:
                    print(f"  {target}@{t}: already a snapshot, skipping")
                    continue
                if args.dry_run:
                    print(f"  {target}@{t}: DRY-RUN would replace history with a snapshot "
                          f"of {tgt[:8]} (base {src[:8]})")
                    continue
                sha = r.snapshot(r.tree(tgt), src, s, parent=None)
                r.push(sha, t, force=True)
                print(f"  {target}@{t}: history replaced by {sha[:8]} (content of {tgt[:8]}, "
                      f"base {src[:8]})", flush=True)
                done.append((target, t))
        except Exception as exc:
            failed.append((target, redact(str(exc)).splitlines()[-1][:200]))
            print(f"  FAILED {target}: {failed[-1][1]}", flush=True)
        finally:
            r.close()
    print(f"\nsquashed: {len(done)} | failed: {len(failed)}")
    for target, why in failed:
        print(f"  failed: {target}: {why}")


def create_missing(cfg, mp, tk, args):
    if not tk.can_create:
        print("  GITHUB_MIRROR_PAT is not set — repo creation needs Administration:write; skipping")
        return
    existing = {r["name"] for r in api_all(f"orgs/{cfg['target_org']}/repos?type=all", tk.tgt)}
    for entry in mp.get("new_repos") or []:
        source = entry["source"]
        target = entry.get("target", source)
        if args.only and args.only not in (source, target):
            continue
        if args.dry_run:
            print(f"  DRY-RUN would snapshot {source} -> {cfg['target_org']}/{target}"
                  + (" (existing empty repo)" if target in existing else " (new repo)"))
            continue
        if target not in existing:
            src_meta = api(f"repos/{cfg['source_org']}/{source}", token=tk.for_org(cfg["source_org"]))
            api(f"orgs/{cfg['target_org']}/repos", "POST", {
                "name": target,
                "description": f"Content snapshot of {cfg['source_org']}/{source}",
                "private": src_meta["private"],
                "auto_init": False,
            }, token=tk.tgt)
        try:
            seed(cfg, source, target, tk)
            print(f"  seeded {source} -> {target}")
        except Exception as exc:
            print(f"  FAILED {source} -> {target}: "
                  f"{redact(str(exc)).splitlines()[-1][:200]}")


def seed(cfg, source: str, target: str, tk: Tokens):
    """Populate an empty target with one snapshot commit of the source default branch.

    No upstream history and no branch other than the default is copied.

    A token without the Workflows permission cannot push commits that touch
    `.github/workflows/`, so that failure is reported rather than worked around.
    """
    r = Repo(cfg, source, target, tk)
    try:
        sb = default_branch(cfg["source_org"], source, tk)
        src = r.fetch("s", sb)
        sha = r.snapshot(r.tree(src), src, sb, parent=None)
        try:
            r.push(sha, sb)
        except RuntimeError as exc:
            if "workflow" in str(exc).lower():
                raise RuntimeError(
                    f"{source}: content includes .github/workflows/ — the token needs the "
                    "Workflows permission to push it") from exc
            raise
    finally:
        r.close()


def root_commit(org: str, repo: str, token: str) -> str | None:
    """Oldest commit on the default branch, via the Link header's last page."""
    req = urllib.request.Request(
        f"{API}/repos/{org}/{repo}/commits?per_page=1",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "workshop-sync"})
    try:
        with urllib.request.urlopen(req) as resp:
            link = resp.headers.get("Link", "")
            first = json.loads(resp.read())
    except urllib.error.HTTPError:
        return None
    last = ""
    for part in link.split(","):
        if 'rel="last"' in part:
            last = part.split("page=")[-1].split(">")[0]
    if not last:
        return first[0]["sha"] if first else None
    page = api(f"repos/{org}/{repo}/commits?per_page=1&page={last}", token=token)
    return page[0]["sha"] if page else None


def cmd_discover(cfg, mp, tk, args):
    src = {r["name"] for r in api_all(f"orgs/{cfg['source_org']}/repos?type=all", tk.src)}
    tgt = {r["name"] for r in api_all(f"orgs/{cfg['target_org']}/repos?type=all", tk.tgt)}
    mapped_src = {p["source"] for p in mp["pairs"]} | {e["source"] for e in mp.get("new_repos") or []}

    gone = mapped_src - src
    if gone:
        print("source repos in the map that no longer exist (renamed or deleted upstream):")
        for g in sorted(gone):
            print(f"  - {g}")

    unmapped = sorted(src - mapped_src)
    if unmapped:
        print("\nsource repos missing from the map:")
        for s in unmapped:
            hint = "same name exists in the target — verify by content, never assume" \
                if s in tgt else "no counterpart -> add under new_repos:"
            print(f"  - {s}: {hint}")
    if not gone and not unmapped:
        print("map is complete: every source repo is either paired or listed under new_repos")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="status",
                    choices=["status", "apply", "squash", "discover"])
    ap.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    ap.add_argument("--only", help="restrict to one repo (source or target name)")
    ap.add_argument("--create-missing", action="store_true", help="apply: also create/seed new_repos")
    ap.add_argument("--yes", action="store_true", help="squash: confirm the irreversible rewrite")
    ap.add_argument("--again", action="store_true",
                    help="squash: re-squash branches that already have a snapshot marker")
    ap.add_argument("--map", default=str(MAP_PATH))
    args = ap.parse_args()
    args.keep = False

    mp = yaml.safe_load(open(args.map))
    cfg = mp["defaults"]
    tk = Tokens(cfg)
    {"status": cmd_status, "apply": cmd_apply, "squash": cmd_squash,
     "discover": cmd_discover}[args.command](cfg, mp, tk, args)


if __name__ == "__main__":
    main()
