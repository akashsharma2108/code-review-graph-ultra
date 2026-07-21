# Team Sync validation and developer-risk model

This document records the practical problems Team Sync is expected to solve for two or
more developers using coding agents from separate machines. It also distinguishes tested
guarantees from boundaries that require additional infrastructure or company policy.

## Developer and agent problems

| Problem | Consequence without Team Sync | Implemented mitigation |
|---|---|---|
| A developer pulls unfamiliar agent-written code | They spend time reconstructing intent and impact | Commit, developer, file, symbol, dependency, test, decision, and question context is queryable by repository |
| Two people or agents change the same area | Overlap is discovered late during merge/review | Each live checkpoint is visible by developer, agent, branch, checkout, file, and symbol |
| A developer stops with uncommitted work | Reasoning and work-in-progress context disappears | Tracked, staged, unstaged, and untracked work is checkpointed automatically |
| The central service is temporarily unavailable | Hooks either block development or lose events | Every payload is committed to a WAL-enabled local outbox before networking and retried later |
| The server permanently rejects a queued capsule | The outbox retries it on every lifecycle event forever | Permanent rejections and retry-exhausted entries are dead-lettered, stay inspectable in `team status`, and re-queue when their content changes |
| A slow or stalled client holds a connection open | One client blocks every other request | Requests are handled concurrently with store-level serialization and a per-connection read timeout |
| A long-lived repository has extensive history | A naive post-commit hook repeatedly scans every ancestor | Bare revisions capture exactly one commit; only explicit ranges backfill history |
| SSH and HTTPS clones represent the same origin | One repository is split into two central histories | Repository identity hashes normalized host/path rather than clone transport |
| One developer uses two clones/worktrees on one branch | Live WIP records overwrite one another | An opaque per-checkout ID participates in the live-record identity |
| WIP is committed | An old `in_progress` record falsely appears active | The live checkpoint is marked completed and linked to the superseding commit |
| Work starts before the first commit | `HEAD` does not exist and ordinary diffs fail | Unborn branches capture cached and untracked paths against an empty baseline |
| Existing hooks exit or use another interpreter | Appended automation never executes or corrupts the hook | Shell blocks are inserted before user code; non-shell hooks are wrapped with their original preserved and restored on uninstall |
| Several hooks write concurrently | SQLite locks or last-write races lose work | Busy timeouts, WAL mode, atomic metadata initialization, and idempotent outbox keys |
| The same company has many repositories | Context from unrelated products becomes mixed | Every event and capsule is repository-scoped inside its organization |
| Pull, checkout, rebase, amend, or push changes Git state | Context becomes stale between sessions | Git lifecycle hooks update the graph, publish explicit commit ranges, and synchronize events |
| Agents produce rationale that Git cannot infer | Automatic records can become confidently fictional | Automation records only observable facts; explicit agent handoffs remain the source for intent and decisions |

## Automated validation matrix

The test suite covers the following dimensions:

- central schema creation, idempotent upsert, temporal events, organization isolation,
  repository isolation, token authentication, token revocation, portable-path validation,
  remote credential stripping, and request-size bounds;
- commit capture for initial, ordinary, range, rename, delete, Unicode, untracked,
  symlink, empty-diff, and unborn-repository states;
- working-tree checkpoint coalescing, per-checkout separation, WIP completion, commit-author
  attribution, stable repository identity across SSH/HTTPS, and oldest-first CI ranges;
- local outbox persistence, concurrent writers, atomic checkout identity, network failure,
  server restart, forced recovery, dead-lettering of permanent rejections and exhausted
  retries, event cursors, and offline query fallback;
- concurrent request handling with a stalled slow client, stable checkpoint payloads for
  unchanged trees, and literal `%`/`_` matching in context filters;
- two real clones using installed hooks for commit, push, pull, central publication, local
  synchronization, and receiving-developer lookup without an explicit publish command;
- shell hooks containing early exits, non-shell hook wrapping/restoration, custom
  `core.hooksPath`, linked worktrees, idempotent upgrades, native agent-hook schemas, and
  surgical uninstall;
- CLI forwarding, central HTTP integration, multi-checkout handoff retrieval, and
  environment enrollment without writing bearer tokens to disk.

The complete project regression suite is run after these focused scenarios. Localhost
HTTP tests require permission to bind ephemeral loopback ports.

## Remaining production boundaries

These are explicit constraints, not silently claimed guarantees:

1. Access tokens are organization-wide. Teams requiring repository-specific RBAC, SSO,
   audit identities, or service accounts need an authorization layer in front of or added
   to protocol version 2.
2. The central implementation is one API process with SQLite on persistent local disk.
   It is not a multi-region or horizontally writable control plane. Do not put the SQLite
   file on an unsafe network filesystem.
3. Source contents are not uploaded, but filenames, symbol names, commit messages, and
   manually supplied narratives are metadata and may still be sensitive.
4. Git author email is the deterministic commit-developer identity. Companies using
   several emails for one person need an organization-managed alias directory; applying
   a checkout-local alias during backfill would make the same commit nondeterministic.
5. Co-author trailers are retained in commit messages but are not yet modeled as multiple
   developer relations.
6. Team Sync exposes overlapping work; it does not lock files or prevent merge conflicts.
7. Automatic capture cannot recover unrecorded design intent, private agent reasoning, or
   tests an agent ran without reporting. Rich handoff tools are still important.
8. Native agent hooks are validated structurally and through generated scripts, but every
   vendor/version combination cannot be launched in CI. Platform changes require ongoing
   compatibility testing.
9. A hook manager can overwrite hooks after installation. Re-run `code-review-graph
   install` after changing Husky, pre-commit, or another hook manager configuration.
10. Shared-history retention, deletion, legal hold, and data-subject workflows are not
    exposed in protocol version 1.

## Operational acceptance checklist

Before company rollout:

1. Assign one stable organization slug and one explicit repository key per repository.
2. Put the API behind HTTPS and centralized authentication/network policy.
3. Store tokens in a secret manager and rotate them with named-token revocation.
4. Back up the SQLite database and WAL with a SQLite-safe snapshot procedure.
5. Verify two representative developer machines with `team status` and a real push/pull.
6. Review whether repository metadata is permitted to leave developer machines.
7. Set monitoring for API health, database size, backup age, and client outbox growth.
8. Define repository RBAC and retention requirements before onboarding sensitive projects.
