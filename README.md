# Upstream Sync

How `codev-workshops` repos are kept current with their `Cognition-Partner-Workshops`
originals: [`catalog/sync-map.yaml`](catalog/sync-map.yaml) says what is paired with what,
and [`scripts/sync_from_source.py`](scripts/sync_from_source.py) does the work. A bi-weekly
Devin automation runs it against this repo.

(Repo provenance and the workshop catalog itself live in `codev-workshops/workshop-content`;
this repo only holds the sync map and tooling, so that the catalog can be an exact mirror of
its upstream.)

## Rules

The sync is one-directional and copies *content*, never history:

- **Never write back to `Cognition-Partner-Workshops`** — no pushes, branches, PRs or settings
  changes. It is the read-only source of truth.
- **Upstream history is never copied.** Workshop targets carry only their own history: a chain
  of snapshot commits. Upstream repos are ~3.5 GB of history that no workshop needs (`calcom`
  alone was 1 GB), and a snapshot keeps the working content identical without it.
- **Branch scope: the target default branch, plus `main` and `develop` when they exist on both
  sides.** No other branch's content is ever read or synced; tags, releases, issues and repo
  settings are out of scope.
- **Only the sync automation lands on a synced branch directly.** The scheduled run passes
  `--direct-push --auto-merge`: the merged snapshot is fast-forwarded onto the synced branch
  with `GITHUB_SYNC_BOT_PAT`, the token of the sync bot account — the only non-admin identity
  the ruleset lets bypass (see [Auto-merge](#auto-merge-codev-sync-bot)). Nobody else, and no
  other Devin session, pushes to those branches. If GitHub refuses the push, the run falls back
  to putting the snapshot on `sync/upstream-<branch>` and opening a pull request (merged at once
  by the bot when it can; otherwise left open for a human). A manual `apply` without the flags
  always takes the PR route.
- **Target commits are preserved.** Each sync three-way merges the new upstream content against
  the previously recorded upstream content, so lab work committed in `codev-workshops` survives.
  Only a real content conflict is escalated to a human — renaming/archiving a target is never
  part of a sync run.
- A sync only ever appends a commit to a synced branch (a fast-forward push or its PR); the only branch it
  rewrites is its own `sync/upstream-*`. The single exception is the one-time `squash` command,
  which is what removed the imported upstream history in the first place.

### How a snapshot sync works

Every snapshot commit records the source commit its content came from:

```
Sync content from Cognition-Partner-Workshops/<repo>@main

Upstream-Commit: 05d54eb8…
```

A run reads that marker from the target branch (`base`), fetches the current source commit
(`theirs`, `--depth=1` — one tree, no history) and the target head (`ours`), and merges the
three *trees*. The result is committed on top of the target head with a new marker, pushed to
`sync/upstream-<branch>` and offered as a pull request. Because nothing is compared by commit
ancestry, the two repos share no commits at all.

| Target branch vs. source content | behaviour |
| --- | --- |
| marker matches the source commit, or trees identical | nothing |
| source moved on | three-way merge of trees, one new commit appended |
| the merge conflicts | reported for a human; nothing pushed |
| a sync PR is already open for that branch | the branch is updated, the PR is reused |
| `--direct-push` and the bot may bypass | the snapshot is fast-forwarded onto the branch; no PR |
| `--direct-push` but GitHub refuses the push | falls back to the PR route below |
| `--auto-merge` and the bot may bypass | the PR is rebase-merged at once; the PR remains as the audit trail |
| `--auto-merge` but GitHub refuses the merge | reported as `left open`; the PR waits for a human |
| no `Upstream-Commit:` marker anywhere in the branch | reported; run `squash` for that repo first |

`sync: off` on a pair excludes it entirely.

## Removing imported history (`squash`)

`squash` replaces a target branch with a *single root commit* holding **the target's own current
tree** — so all merged lab content is kept — and records the current source commit as the base
for future merges. It force-pushes, it is irreversible, and it therefore requires `--yes`:

```bash
scripts/sync_from_source.py squash --only=calcom --dry-run
scripts/sync_from_source.py squash --only=calcom --yes
```

It is a migration step, not part of a scheduled run: a branch that already has a marker is
skipped unless `--again` is passed. GitHub does not reclaim the disk immediately — the
unreferenced objects stay in the repo's pack until GitHub's own gc runs, so the reported repo
size keeps showing the old figure for a while even though the history is gone from the branch.

## Why the map is explicit, not by name

The copies were made before the `ts-`/`uc-` naming convention existed, and four repos existed
in both orgs under the *same* name with completely unrelated history — syncing those by name
would have overwritten live work. Every pair was established by a shared root commit and is
listed explicitly.

On 2026-09-01 the targets were renamed to their source names, so `target` now equals `source`
for every pair except `workshop-content -> workshop-metadata`. That is cosmetic: the map stays
authoritative and pairs must still never be added by name. For each of the four collisions the
unrelated same-name repo was renamed to `<name>-lab` and archived (nothing deleted, history
intact) before the real copy took the source name; `collisions:` records both names.

Since the imported history was squashed away, the pairs can no longer be *re-derived* from a
shared root commit at all — the map is the only record of what belongs to what, so keep it
under review and never repair it by name matching.

## Running it

```bash
pip install pyyaml

scripts/sync_from_source.py status               # read-only classification of every pair
scripts/sync_from_source.py apply --dry-run      # what a run would change
scripts/sync_from_source.py apply                # open/refresh a sync PR per branch
scripts/sync_from_source.py apply --auto-merge   # …and merge it as the sync bot
scripts/sync_from_source.py apply --direct-push --auto-merge   # scheduled run: push, PR only as fallback
scripts/sync_from_source.py apply --create-missing   # also seed repos under new_repos:
scripts/sync_from_source.py squash --yes         # one-time: drop imported upstream history
scripts/sync_from_source.py discover             # find upstream renames / unmapped repos
scripts/sync_from_source.py publish-map --auto-merge   # land a sync-map.yaml-only change as the bot
```

Credentials: reading the source org and opening sync PRs works with any token that has contents
access to both orgs plus pull-request write on the target (`gh auth login` is enough). No
bypass of the default-branch ruleset is needed, and the Devin GitHub App deliberately no longer
has one — the sync's own PRs need a human approval like anyone else's. Creating the repos under
`new_repos:` additionally needs `GITHUB_MIRROR_PAT` — a fine-grained PAT on
`codev-workshops` with **Administration: write** and **Contents: write**, plus
**Workflows: write** if the content includes `.github/workflows/`. `squash` needs a token that
the default-branch ruleset lets force-push, which now means an org-admin one.

Set `SYNC_GITHUB_BASE=https://github.com` if your environment does not rewrite github.com
through a credential proxy.

### Auto-merge (`codev-sync-bot`)

Waiting for a peer approval on every sync PR meant targets silently fell behind, so the
scheduled run merges its own PRs. GitHub cannot grant a bypass to a *token* — bypass actors are
roles, teams and apps, and a PAT simply acts as its owner — so the bypass is tied to an account
that does nothing else:

- `codev-sync-bot` (GitHub login `reslk`) is a machine user, a plain member of
  `codev-workshops`, and the only member of the team `upstream-sync`. The team must be
  *visible* (closed), not Secret — GitHub refuses secret teams as bypass actors with
  `Actor … team must be part of the ruleset source or owner organization`.
- `apply_rulesets.py --bypass-team upstream-sync` grants that team write access on every
  active repo (the org base permission is read-only), then adds it (`bypass_mode: always`) next
  to org admins on every `protect-default-branch` ruleset and strips any other Team or
  Integration actor. Nothing else — no human, no Devin App — gains a bypass.
- `GITHUB_SYNC_BOT_PAT` is the bot's fine-grained PAT (`codev-workshops`, all repositories,
  Contents: write, Pull requests: write). It is stored as a secret **scoped to the upstream-sync
  automation only**, so no other session or automation in the org holds it, and the script uses
  it for exactly two calls: open the sync PR and merge it (`PUT …/pulls/N/merge`, rebase, pinned
  to the snapshot SHA). Pushing `sync/upstream-*`, reading the source and everything else keep
  using the ambient token / `GITHUB_MIRROR_PAT`.
- `--auto-merge` exits early if the bot token is missing, so a misconfigured run degrades to the
  review-required behaviour instead of pushing anything.

What this does *not* change: the sync still never writes to the source org, never touches a
branch outside the default/`main`/`develop` scope, never force-pushes anything but its own
`sync/upstream-*`, and still leaves conflicts and push-protection rejections to a human. Those
guardrails in the script are now the review, so changes to `sync_from_source.py` itself deserve
a careful look.

### Per-pair rewrites

Some targets deliberately differ from upstream in a purely mechanical way — `workshop-content`
rewrites every `Cognition-Partner-Workshops/<repo>` link to `codev-workshops/<repo>` so
attendees land in the org they have access to. Left alone, every upstream edit of such a line
conflicts, and reconciling by hand does not help: the next upstream edit conflicts again. A pair
can therefore declare

```yaml
  - source: workshop-content
    target: workshop-content
    rewrite:
      - org-references          # <source_org>/<mapped repo>, /orgs/<source_org>, bare org name
      - {from: "literal", to: "replacement"}
```

The rules are applied to the *source* side (recorded base and current tree) before the
three-way merge, and to the seed of a new repo; `status` compares the rewritten source tree.
Only UTF-8 text blobs are touched, binaries pass through, and `Upstream-Commit:` still records
the real source SHA. `org-references` leaves `<source_org>/<unmapped repo>` alone, so a link
to a repo that has no copy in the target org keeps pointing where it works.

### Map maintenance without a peer review (`publish-map`)

`catalog/sync-map.yaml` changes on nearly every scheduled run (`# Last verified:`, newly
discovered repos, seeded repos moving into `pairs:`). Waiting for an approval on each of those
PRs blocked the automation, so `publish-map` commits the map change on `sync/map-maintenance`,
opens a PR and — with `--auto-merge` — rebase-merges it as `codev-sync-bot`, exactly like a sync
PR. Its guard is that the working tree may differ from `HEAD` **only** in the map file: the bot
never lands code. Changes to `scripts/` still go through a normal reviewed PR.

## Default-branch protection

[`scripts/apply_rulesets.py`](scripts/apply_rulesets.py) puts the same `protect-default-branch`
ruleset on every non-archived `codev-workshops` repo — PR required, 1 approval, approval from
someone other than the last pusher, no deletion, no non-fast-forward — with **org admins and the
`upstream-sync` team as the only bypass actors**. Run it after a new repo appears:

```bash
GITHUB_MIRROR_PAT=… scripts/apply_rulesets.py --bypass-team upstream-sync
```

It also strips `Integration` bypass actors and any Team actor other than the one passed. There
used to be an Integration actor for the Devin GitHub App (so the sync could push directly), and
because bypass is ruleset-wide it let any Devin session merge into a default branch with zero
reviews. That is why the sync's bypass is now a single-purpose account instead; never re-add an
app as a bypass actor.

Rulesets need GitHub Team on private repos: 20 private repos in the org answer
`403 Upgrade to GitHub Pro or make this repository public` and are therefore unprotected.

## Push protection

GitHub push protection rejects commits that contain secrets, which blocked four repos
(`timesheet-app`, `eventflow-storefront`, `uc-appsec-nodegoat`, `eventflow-devin-integration`).
The sync reports these as `FAILED` and moves on — it never bypasses the control on its own.
(Snapshots make this rarer: only secrets present in the *current* content can trip it, not ones
buried in upstream history.)

They were unblocked on 2026-09-01 by granting a per-finding bypass on the *target* repo and
pushing immediately (bypasses expire within minutes):

```bash
# placeholder ids come from the unblock-secret/<id> URLs in the rejected push output
curl -X POST -H "Authorization: Bearer $GITHUB_MIRROR_PAT" \
  ".../repos/codev-workshops/<repo>/secret-scanning/push-protection-bypasses" \
  -d '{"reason":"used_in_tests","placeholder_id":"<id>"}'
```

No org or repo security setting was changed and no history was rewritten, so the secrets stay
identical to the source and are still reported as alerts on the target. The credentials
involved (Slack, Stripe, an Azure AD app secret in `eventflow-storefront`'s `public/ops.js`,
AWS keys in NodeGoat's vendored `node_modules`) live in the *source* history and should be
rotated there.

## Pruning stale branches

[`scripts/prune_branches.py`](scripts/prune_branches.py) deletes unused branches across every
non-archived `codev-workshops` repo. A bi-weekly Devin automation (every other Saturday,
22:00) runs it.

Rules, in order:

- **Never deleted:** the repo's default branch, `main`, `develop` (and `master`), anything under
  a GitHub branch protection rule, extra names passed with `--protect`.
- **Open PRs idle for more than 14 days** (no update of any kind) are commented on and closed,
  and their head branch is deleted. PRs from forks are left alone.
- **Never deleted while the head of an active open PR.** If no available token can list a
  repo's PRs, its branches are held rather than deleted.
- **Devin branches** (`devin/...` name, or tip commit authored/committed by a `devin` identity)
  are deleted when the tip commit is more than **30 days** old.
- **Any other branch** is deleted when the tip commit is more than **90 days** old.

"Age" is the tip commit's committer date — a branch is kept alive by pushing to it.

```bash
scripts/prune_branches.py                       # dry run: report only
scripts/prune_branches.py --apply               # actually delete
scripts/prune_branches.py --repo angular2-hn    # one repo
scripts/prune_branches.py --json report.json    # per-branch report
scripts/prune_branches.py --pr-days 30          # be more lenient with idle PRs
```

Credentials: `GITHUB_MIRROR_PAT` (Contents: write, plus Pull requests: write so idle PRs can be
listed and closed) is used for deletion; any ambient token (`GH_TOKEN`, `gh auth token`) is tried as a
read fallback for the PR check. Deletion failures are reported per branch and make the run
exit non-zero.

## Lab Tracker sync

`scripts/lab_tracker/` backs the `!sync_lab_tracker` playbook, which appends new lab modules
from `workshop-content` to the Lab Tracker Google Sheet. The sheet is only reachable through
its anonymous share link (no Sheets API), so it is read out of the session's already-running
Chrome over raw CDP; nothing is installed.

```
node scripts/lab_tracker/read_sheet.mjs "Lab Tracker" --out ~/labsync       # TSV + HTML copy of the tab
scripts/lab_tracker/lab_tracker.py --repo ~/repos/workshop-content \
    --sheet ~/labsync/sheet_Lab_Tracker.html --since "8 days ago"           # diff + ready-to-paste rows
```

`lab_tracker.py` writes `report.md`/`report.json` (missing modules, workshops absent from
`Appears In`, changes to existing rows — reported, never edited) and, when something is
missing, `new_rows.tsv` in the sheet's exact column order with `Not Started` in the four status
columns. Exit code 10 means there are rows to append; 0 means the tracker is in sync. With
`--since`, the per-row change comparison is skipped when no commit touched `labs/` or
`workshops/` in that window, which is what keeps a no-change run to a couple of minutes.

## Maintenance

`discover` reports source repos that are absent from the map and, for each, whether some
some `codev-workshops` repo already carries the same name — that is how upstream renames and
newly added workshops surface. Since targets no longer share history with their sources, root
commit fingerprinting no longer works and every new pair must be confirmed by a human by
comparing content. Move the resolved ones from `new_repos:` into `pairs:` after they are seeded.

This repo is deliberately *not* in the map: it has no upstream counterpart, so the catalog repo
(`workshop-content`) can stay an exact mirror of its source instead of carrying local tooling
commits that would make it permanently target-ahead.
