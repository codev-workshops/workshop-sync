#!/usr/bin/env python3
"""Sync codev-workshops repos with their Cognition-Partner-Workshops sources.

Content, not history. A target repo has its own history made of *snapshot*
commits; upstream commits are never copied into it. Each snapshot commit records
the source commit it was taken from:

    Upstream-Commit: <sha>

which is the base for the next sync's three-way content merge, so target-side
commits keep surviving without any shared ancestry between the two repos.

Branch scope: the target's default branch, plus `main` and `develop` when they
exist on both sides. No other branch's content is ever read or synced.

A pair may declare `rewrite:` rules (see `Rewrite`). They are applied to the
*source* trees (recorded base and current source) before the merge, so a target
that deliberately differs from its source in a mechanical way (e.g. links pointing
at the target org) does not conflict every time upstream touches those lines.

By default `apply` puts the merged snapshot on a `sync/upstream-<branch>` branch
and opens a pull request, so the default-branch ruleset (PR + a peer approval)
applies to the sync as well.
With `--auto-merge` the PR is merged right away using GITHUB_SYNC_BOT_PAT, the
token of the dedicated `codev-sync-bot` account that the ruleset lets bypass;
the PR stays as the audit trail and a merge that GitHub refuses is left open.
With `--direct-push` (the scheduled automation's mode) the snapshot is instead
pushed straight onto the synced branch with GITHUB_SYNC_BOT_PAT — a plain
fast-forward on top of the branch head, never a force — and the PR route is only
used as a fallback when GitHub refuses that push.

Subcommands
    status     classify every mapped pair (default; read-only)
    apply      open a PR merging the current source content into every synced branch
    squash     one-time: replace a target branch's history with a single snapshot
               commit of its current content (force push; --yes required)
    discover   re-fingerprint both orgs and report pairs missing from the map
    publish-map  commit the local change to catalog/sync-map.yaml on a branch of
               this repo, open a PR and (with --auto-merge) merge it as the sync bot

Auth
    GITHUB_MIRROR_PAT  fine-grained PAT (Contents+Administration write on the target
                       org). Required for `apply --create-missing`; optional otherwise.
    GITHUB_SYNC_BOT_PAT  fine-grained PAT of the `codev-sync-bot` machine user
                       (Contents+Pull requests write on the target org). Required
                       for `apply --auto-merge` / `--direct-push`; never used otherwise.
    Falls back to `gh auth token` for API calls and to ambient git credentials
    for pushes.

Examples
    scripts/sync_from_source.py status
    scripts/sync_from_source.py apply --dry-run
    scripts/sync_from_source.py apply --only=timesheet-app
    scripts/sync_from_source.py apply --auto-merge
    scripts/sync_from_source.py apply --direct-push --auto-merge
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
# Snapshots land here and reach the synced branch through a reviewed pull request.
SYNC_BRANCH = "sync/upstream-{branch}"
IDENT = ["-c", "user.name=workshop-sync", "-c", "user.email=workshop-sync@codev-workshops"]
MAP_BRANCH = "sync/map-maintenance"


class Rewrite:
    """Textual rewrite of a source tree, declared per pair in the map:

        rewrite:
          - org-references              # <source_org>/<repo> -> <target_org>/<repo> for every
                                        # mapped repo, and bare mentions of the source org
          - from: "literal text"        # plain literal replacement
            to: "replacement"

    Only UTF-8 text blobs are touched; binaries pass through unchanged.
    """

    def __init__(self, cfg, mp, rules):
        self.rules = []
        repos = sorted({p.get("target", p["source"]) for p in mp.get("pairs") or []}
                       | {e.get("target", e["source"]) for e in mp.get("new_repos") or []},
                       key=len, reverse=True)
        for rule in rules or []:
            if rule == "org-references":
                src, tgt = cfg["source_org"], cfg["target_org"]
                alt = "|".join(re.escape(r) for r in repos)
                pat = re.compile(rf"{re.escape(src)}(?=/(?:{alt})\b|/\s|/$)"
                                 rf"|(?<=/orgs/){re.escape(src)}"
                                 rf"|{re.escape(src)}(?![/\w-])")
                self.rules.append((pat, tgt))
            elif isinstance(rule, dict) and "from" in rule and "to" in rule:
                self.rules.append((re.compile(re.escape(rule["from"])), rule["to"]))
            else:
                raise ValueError(f"unknown rewrite rule: {rule!r}")

    def __bool__(self):
        return bool(self.rules)

    def text(self, data: bytes) -> bytes:
        if b"\0" in data:
            return data
        try:
            s = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        for pat, to in self.rules:
            s = pat.sub(to, s)
        return s.encode("utf-8")


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
        # GITHUB_MIRROR_PAT only carries Contents+Administration, so it cannot open pull
        # requests; the ambient (app) token can, and is used for that one call.
        self.pulls = ambient or pat
        self.can_create = bool(pat)
        # The bot token is the only credential allowed to land a sync on a protected
        # branch. It opens and merges the sync PR and touches nothing else.
        self.bot = os.environ.get("GITHUB_SYNC_BOT_PAT", "")
        if self.bot:
            self.pulls = self.bot
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

    def rewrite_tree(self, tree: str, rw: "Rewrite") -> str:
        """Return `tree` with `rw` applied to every text blob (same tree when no rule)."""
        if not rw:
            return tree
        scratch = Path(tempfile.mkdtemp(prefix="rw-"))
        env = {"GIT_DIR": str(self.dir / ".git"), "GIT_INDEX_FILE": str(scratch / ".idx")}
        try:
            run(["git", "read-tree", tree], cwd=scratch, env=env)
            entries = run(["git", "ls-tree", "-r", "-z", tree], cwd=self.dir)
            info = []
            for ent in entries.split("\0"):
                if not ent:
                    continue
                meta, path = ent.split("\t", 1)
                mode, kind, sha = meta.split()
                if kind != "blob" or mode == "120000":
                    continue
                data = subprocess.run(["git", "cat-file", "blob", sha], cwd=self.dir,
                                      capture_output=True, check=True).stdout
                new = rw.text(data)
                if new == data:
                    continue
                new_sha = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.dir,
                                         input=new, capture_output=True, check=True).stdout.decode().strip()
                info.append(f"{mode} {new_sha}\t{path}")
            if info:
                subprocess.run(["git", "update-index", "--index-info"], cwd=scratch,
                               input=("\n".join(info) + "\n").encode(), check=True,
                               capture_output=True, env={**os.environ, **env})
            return run(["git", "write-tree"], cwd=scratch, env=env)
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

    def push_direct(self, sha: str, branch: str) -> str:
        """Fast-forward `branch` to `sha` with the bot token only; '' or the refusal."""
        try:
            run(["git", *auth_header(self.tk.bot), "push", "t", f"{sha}:refs/heads/{branch}"],
                cwd=self.dir)
            return ""
        except RuntimeError as exc:
            lines = [l for l in redact(str(exc)).splitlines() if l.strip()]
            return (lines[-1] if lines else "push refused")[:160]


def open_pr(cfg, target: str, head: str, base: str, source: str, src: str, tk: Tokens) -> dict:
    """Open (or reuse) the sync PR for one branch and return it."""
    org = cfg["target_org"]
    existing = api(f"repos/{org}/{target}/pulls?state=open&head={org}:{head}&base={base}",
                   token=tk.pulls)
    if existing:
        return existing[0]
    body = (f"Content snapshot of `{cfg['source_org']}/{source}@{base}` at `{src}`, "
            f"three-way merged with this branch's own content.\n\n"
            f"Upstream history is not copied and no other branch is touched. "
            f"Merge to accept; a conflicting merge is never pushed.\n")
    return api(f"repos/{org}/{target}/pulls", "POST",
               {"title": f"Sync content from {cfg['source_org']}/{source}@{base}",
                "head": head, "base": base, "body": body}, token=tk.pulls)


def merge_pr(cfg, target: str, pr: dict, sha: str, tk: Tokens) -> str:
    """Merge the sync PR with the bot token; returns '' on success, else the reason.

    Rebase keeps the snapshot commit (and its Upstream-Commit trailer) as the branch
    head instead of burying it under a merge commit. `sha` pins the merge to the
    snapshot just pushed, so a head that moved in between is refused by GitHub.
    """
    org = cfg["target_org"]
    try:
        api(f"repos/{org}/{target}/pulls/{pr['number']}/merge", "PUT",
            {"merge_method": "rebase", "sha": sha}, token=tk.bot)
        return ""
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode(errors="replace"))
        try:
            detail = json.loads(detail).get("message", detail)
        except ValueError:
            pass
        return f"{exc.code} {detail[:160]}"


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


def classify_branch(r: Repo, sb: str, tb: str, rw: Rewrite) -> dict:
    src = r.fetch("s", sb)
    tgt = r.fetch("t", tb, history=True)
    base = r.upstream_base(tgt)
    if base is None:
        state = UNSQUASHED
    elif base == src or r.rewrite_tree(r.tree(src), rw) == r.tree(tgt):
        state = IN_SYNC
    else:
        state = UPDATE
    return dict(sb=sb, tb=tb, src=src, tgt=tgt, base=base, state=state)


def classify(cfg, mp, pair, tk) -> dict:
    source, target = pair["source"], pair["target"]
    r = Repo(cfg, source, target, tk)
    try:
        rw = Rewrite(cfg, mp, pair.get("rewrite"))
        sb = default_branch(cfg["source_org"], source, tk)
        tb = default_branch(cfg["target_org"], target, tk)
        branches = [classify_branch(r, s, t, rw) for s, t in synced_branches(r, sb, tb)]
        return dict(source=source, target=target, branches=branches, repo=r, rewrite=rw, error="")
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
        res = classify(cfg, mp, pair, tk)
        results.append(res)
        print(f"  {res['source']} -> {res['target']}: {describe(res)}", flush=True)
        if res["repo"] and not args.keep:
            res["repo"].close()
            res["repo"] = None
    return results


def cmd_apply(cfg, mp, tk, args):
    args.keep = True
    if (args.auto_merge or args.direct_push) and not tk.bot:
        sys.exit("--auto-merge/--direct-push need GITHUB_SYNC_BOT_PAT (the sync bot token).")
    results = cmd_status(cfg, mp, tk, args)
    changed, conflicts, pending, failed, merged, unmerged, pushed = [], [], [], [], [], [], []
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
                rw: Rewrite = res["rewrite"]
                r.fetch("s", b["base"])
                try:
                    tree = r.merge_trees(r.rewrite_tree(r.tree(b["base"]), rw), r.tree(b["tgt"]),
                                         r.rewrite_tree(r.tree(b["src"]), rw))
                except MergeConflict as exc:
                    conflicts.append((res, b))
                    print(f"  {label}: CONFLICT {str(exc).splitlines()[0][:120]}")
                    continue
                if args.dry_run:
                    how = ("push directly" if args.direct_push
                           else "open and merge a PR" if args.auto_merge else "open a PR")
                    print(f"  {label}: DRY-RUN would {how} with a snapshot of {b['src'][:8]}")
                else:
                    sha = r.snapshot(tree, b["src"], b["sb"], parent=b["tgt"])
                    if args.direct_push:
                        why = r.push_direct(sha, b["tb"])
                        if not why:
                            pushed.append((res, b, sha))
                            print(f"  {label}: pushed {sha[:8]} <- {b['src'][:8]} onto {b['tb']}")
                            changed.append((res, b))
                            continue
                        print(f"  {label}: direct push refused ({why}) — falling back to a PR")
                    head = SYNC_BRANCH.format(branch=b["tb"])
                    r.push(sha, head, force=True)
                    pr = open_pr(cfg, res["target"], head, b["tb"], res["source"],
                                 b["src"], tk)
                    url = pr["html_url"]
                    print(f"  {label}: PR {url} (snapshot {sha[:8]} <- {b['src'][:8]})")
                    if args.auto_merge:
                        why = merge_pr(cfg, res["target"], pr, sha, tk)
                        if why:
                            unmerged.append((res, b, url, why))
                            print(f"  {label}: NOT MERGED ({why}) — PR left open")
                        else:
                            merged.append((res, b, url))
                            print(f"  {label}: merged")
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
          f"awaiting squash: {len(pending)} | failed: {len(failed)}"
          + (f" | pushed directly: {len(pushed)}" if args.direct_push else "")
          + (f" | merged: {len(merged)} | left open: {len(unmerged)}" if args.auto_merge else ""))
    for res, b, sha in pushed:
        print(f"  pushed: {res['target']}@{b['tb']} "
              f"https://github.com/{cfg['target_org']}/{res['target']}/commit/{sha}")
    for res, b, url, why in unmerged:
        print(f"  left open: {res['target']}@{b['tb']} {url}: {why}")
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
            seed(cfg, source, target, tk, Rewrite(cfg, mp, entry.get("rewrite")))
            print(f"  seeded {source} -> {target}")
        except Exception as exc:
            print(f"  FAILED {source} -> {target}: "
                  f"{redact(str(exc)).splitlines()[-1][:200]}")


def seed(cfg, source: str, target: str, tk: Tokens, rw: Rewrite | None = None):
    """Populate an empty target with one snapshot commit of the source default branch.

    No upstream history and no branch other than the default is copied.

    A token without the Workflows permission cannot push commits that touch
    `.github/workflows/`, so that failure is reported rather than worked around.
    """
    r = Repo(cfg, source, target, tk)
    try:
        sb = default_branch(cfg["source_org"], source, tk)
        src = r.fetch("s", sb)
        tree = r.rewrite_tree(r.tree(src), rw) if rw else r.tree(src)
        sha = r.snapshot(tree, src, sb, parent=None)
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


def cmd_publish_map(cfg, mp, tk, args):
    """Land a change to catalog/sync-map.yaml without a peer review.

    Guard: the working tree of this repo may differ from HEAD *only* in the map file,
    so the bot never lands code. The change is committed on MAP_BRANCH, pushed with
    the ambient token, opened as a PR and — with --auto-merge — rebase-merged by the
    sync bot exactly like a sync PR. A refused merge leaves the PR open for a human.
    """
    root = Path(args.map).resolve().parent.parent
    rel = str(Path(args.map).resolve().relative_to(root))
    repo = run(["git", "rev-parse", "--show-toplevel"], cwd=root)
    if Path(repo).resolve() != root:
        sys.exit(f"{args.map} is not at <repo>/catalog/sync-map.yaml")
    porcelain = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=root, capture_output=True, text=True, check=True).stdout
    dirty = [l[3:] for l in porcelain.splitlines() if l.strip()]
    if not dirty:
        print("map unchanged — nothing to publish")
        return
    if dirty != [rel]:
        sys.exit(f"publish-map lands only {rel}; also modified: {[d for d in dirty if d != rel]}")
    if args.auto_merge and not tk.bot:
        sys.exit("--auto-merge needs GITHUB_SYNC_BOT_PAT (the sync bot token).")
    origin = run(["git", "remote", "get-url", "origin"], cwd=root)
    m = re.search(r"github\.com(?::443)?/([^/]+)/([^/.]+?)(?:\.git)?/?$", origin)
    if not m:
        sys.exit(f"cannot tell the GitHub repo from origin {origin}")
    org, name = m.groups()
    base = run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=root,
               check=False).split("/")[-1] or "main"
    title = args.message or "sync-map: scheduled maintenance"
    if args.dry_run:
        print(f"DRY-RUN would push {rel} as '{title}' to {org}/{name}:{MAP_BRANCH} and open a PR")
        return
    blob = run(["git", "hash-object", "-w", rel], cwd=root)
    head = run(["git", "rev-parse", "HEAD"], cwd=root)
    scratch = Path(tempfile.mkdtemp(prefix="map-"))
    env = {"GIT_INDEX_FILE": str(scratch / ".idx")}
    try:
        run(["git", "read-tree", "HEAD"], cwd=root, env=env)
        subprocess.run(["git", "update-index", "--index-info"], cwd=root, check=True,
                       input=f"100644 {blob}\t{rel}\n".encode(), capture_output=True,
                       env={**os.environ, **env})
        tree = run(["git", "write-tree"], cwd=root, env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    sha = run(["git", *IDENT, "commit-tree", tree, "-p", head, "-m", title], cwd=root)
    url = f"{GIT_BASE}/{org}/{name}"
    git_auth(["push", "-q", url, f"+{sha}:refs/heads/{MAP_BRANCH}"], tk.src, cwd=root)
    existing = api(f"repos/{org}/{name}/pulls?state=open&head={org}:{MAP_BRANCH}&base={base}",
                   token=tk.pulls)
    pr = existing[0] if existing else api(
        f"repos/{org}/{name}/pulls", "POST",
        {"title": title, "head": MAP_BRANCH, "base": base,
         "body": "Map-only maintenance committed by the sync automation "
                 "(`publish-map`). Nothing outside `catalog/sync-map.yaml` is changed.\n"},
        token=tk.pulls)
    print(f"map PR {pr['html_url']} ({sha[:8]})")
    if args.auto_merge:
        why = merge_pr({"target_org": org}, name, pr, sha, tk)
        if why:
            print(f"NOT MERGED ({why}) — PR left open")
        else:
            print("merged")
            run(["git", "fetch", "-q", "origin", base], cwd=root, check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="status",
                    choices=["status", "apply", "squash", "discover", "publish-map"])
    ap.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    ap.add_argument("--only", help="restrict to one repo (source or target name)")
    ap.add_argument("--create-missing", action="store_true", help="apply: also create/seed new_repos")
    ap.add_argument("--auto-merge", action="store_true",
                    help="apply: merge each sync PR immediately with GITHUB_SYNC_BOT_PAT")
    ap.add_argument("--direct-push", action="store_true",
                    help="apply: fast-forward the synced branch itself with GITHUB_SYNC_BOT_PAT; "
                         "fall back to the PR route if GitHub refuses the push")
    ap.add_argument("--yes", action="store_true", help="squash: confirm the irreversible rewrite")
    ap.add_argument("--again", action="store_true",
                    help="squash: re-squash branches that already have a snapshot marker")
    ap.add_argument("--message", help="publish-map: commit message / PR title")
    ap.add_argument("--map", default=str(MAP_PATH))
    args = ap.parse_args()
    args.keep = False

    mp = yaml.safe_load(open(args.map))
    cfg = mp["defaults"]
    tk = Tokens(cfg)
    {"status": cmd_status, "apply": cmd_apply, "squash": cmd_squash,
     "discover": cmd_discover, "publish-map": cmd_publish_map}[args.command](cfg, mp, tk, args)


if __name__ == "__main__":
    main()
