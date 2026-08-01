# Reference implementations

Two worked examples of the raven side of the contract in [`../SPEC.md`](../SPEC.md):

| File | What it shows |
|------|---------------|
| [`huginn_raven.py`](huginn_raven.py) | The leading raven: a higher `host_priority`, an attention badge, per-item actions, and a token file |
| [`muninn_raven.py`](muninn_raven.py) | The companion raven: the same contract with a lower priority, link-only rows, and no token |

They are **documentation that runs**, not libraries. Neither is imported by
Roost and neither is imported by the real Huginn or Muninn — each project
implements the contract itself. These exist so the contract has an executable
form you can read end to end and point at when something disagrees.

Each file is standalone (stdlib only) and can be run directly:

```bash
python3 examples/huginn_raven.py
```

It publishes a descriptor, serves `/api/menu` and `/api/menu/action` on a free
loopback port, and removes its descriptor on exit. Start one (or both) and the
tray will show them. `roost ravens` will explain anything it will not show.

## What to copy, and what not to

**Copy the shapes:** the descriptor fields, the declared version *range*, the
menu JSON, the `Host`/`Origin` checks, the constant-time token comparison, and
publishing the descriptor last (after the port is bound) then removing it on
exit.

**Do not copy the internals.** The session lists are hardcoded, the state is a
module global, and there is no persistence. A real raven has its own model and
its own store; the point of the contract is that Roost cannot tell.

## The two rules that are easy to get wrong

**Declare a range, not a version.** A raven declares `min_api`/`max_api` and
Roost accepts any raven whose window overlaps its own. Comparing versions for
equality is the bug behind huginn issue #38: one routine bump silently disabled
every participant, with nothing on screen to say why.

**Own your token; never expect one.** The descriptor names a `token_path` and
Roost reads it fresh on every request and sends it only to the port that
declared it. Roost never mints a credential and never shares one between
ravens, so a raven that wants authentication has to publish its own — and a
raven that publishes none gets unauthenticated requests, which is its decision
to make.
