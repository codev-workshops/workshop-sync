#!/usr/bin/env python3
"""Apply the protect-default-branch ruleset to every active codev-workshops repo.

Same rules as the 92 sync targets: PR required, 1 approval, approval from someone
other than the last pusher, no deletion, no non-fast-forward. Org admins bypass;
no Integration bypass actor, so no app can merge without review.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

ORG = "codev-workshops"
TOKEN = os.environ["GITHUB_MIRROR_PAT"]
RULES = [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": True,
        "required_review_thread_resolution": False,
        "allowed_merge_methods": ["merge", "squash", "rebase"],
    }},
]


def api(path, method="GET", body=None):
    req = urllib.request.Request(
        f"https://api.github.com/{path}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json", "User-Agent": "ruleset-apply"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "{}")


def payload(bypass):
    return {"name": "protect-default-branch", "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "rules": RULES, "bypass_actors": bypass}


ADMIN = [{"actor_id": 1, "actor_type": "OrganizationAdmin", "bypass_mode": "always"}]

repos, page = [], 1
while True:
    chunk = api(f"orgs/{ORG}/repos?per_page=100&page={page}&type=all")
    if not chunk:
        break
    repos += chunk
    page += 1
active = [r for r in repos if not r["archived"]]
print(f"{len(repos)} repos, {len(active)} active", flush=True)

c, notes = Counter(), []
for r in sorted(active, key=lambda r: r["name"]):
    name = r["name"]
    if r.get("size", 0) == 0 and not r.get("default_branch"):
        c["empty repo (no default branch)"] += 1
        notes.append((name, "empty"))
        continue
    try:
        rs = [x for x in api(f"repos/{ORG}/{name}/rulesets") if x["name"] == "protect-default-branch"]
    except urllib.error.HTTPError as e:
        c[f"list {e.code}"] += 1
        notes.append((name, f"list {e.code}"))
        continue
    try:
        if rs:
            full = api(f"repos/{ORG}/{name}/rulesets/{rs[0]['id']}")
            keep = [a for a in full["bypass_actors"] if a["actor_type"] != "Integration"]
            need = (full["enforcement"] != "active"
                    or len(keep) != len(full["bypass_actors"])
                    or sorted(x["type"] for x in full["rules"]) != sorted(x["type"] for x in RULES))
            if not need:
                c["already correct"] += 1
                continue
            api(f"repos/{ORG}/{name}/rulesets/{rs[0]['id']}", "PUT",
                payload(keep or ADMIN))
            c["updated"] += 1
        else:
            api(f"repos/{ORG}/{name}/rulesets", "POST", payload(ADMIN))
            c["created"] += 1
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200]
        c[f"write {e.code}"] += 1
        notes.append((name, f"write {e.code}: {detail}"))

print(c)
for n in notes:
    print(" ", n[0], "|", n[1])
sys.stdout.flush()
