# Reference implementations

Two worked examples of the raven side of the contract in [`../SPEC.md`](../SPEC.md):

| File | What it shows |
|------|---------------|
| [`huginn_raven.py`](huginn_raven.py) | The leading raven: a higher `host_priority`, an attention badge, per-item actions, a token file, and a lifecycle **Quit** row |
| [`muninn_raven.py`](muninn_raven.py) | The companion raven: the same contract with a lower priority, link-only rows, and no token |

They are **documentation that runs**, not libraries. Neither is imported by
Roost and neither is imported by the real Huginn or Muninn — each project
implements the contract itself. These exist so the contract has an executable
form you can read end to end and point at when something disagrees.

## Both ravens are real now — read them too

When these files were written they were the only implementations of the protocol
that existed. They are not any more, and a production raven answers questions these
cannot:

| Project | Its raven side |
|---|---|
| [Huginn](https://github.com/tohuw/huginn) | [`huginn/raven.py`](https://github.com/tohuw/huginn/blob/master/huginn/raven.py) — descriptor, menu, and the authenticated `/api/menu` + `/api/menu/action` routes inside its existing FastAPI app (note: its default branch is `master`) |
| [Muninn](https://github.com/tohuw/muninn) | [`muninn/raven.py`](https://github.com/tohuw/muninn/blob/main/muninn/raven.py) for the descriptor and payload, [`muninn/ravenserve.py`](https://github.com/tohuw/muninn/blob/main/muninn/ravenserve.py) for the loopback listener and its publish/withdraw lifecycle |

The two shipped ravens sit at opposite ends of the contract's optional parts, which
is more instructive than either example: Huginn authenticates and offers actions,
Muninn publishes no `token_path` and every row is a link. Both are decisions the
protocol leaves to the raven, and each project records *why* it chose as it did —
which is the part an example cannot show you.

They also solve two problems these files sidestep. Muninn splits the payload from
the socket so its payload can be tested with no port bound, and both projects share
the descriptor mechanics through [`corvidae`](https://pypi.org/project/corvidae/), a
stdlib-only package, rather than each writing the atomic-0600-publish and
state-directory-resolution code twice. If you are implementing a third raven,
`corvidae` is probably what you want instead of copying from here.

So: read these for the shape of the contract end to end, and read the two real ones
for how it survives contact with a real application.

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

## Lifecycle, and the row that cannot exist

`huginn_raven.py` publishes a **Quit** row. Look at how little it took: an id in
`build_menu`, a branch in `perform_action`, and no change to Roost or to the
protocol version. That is the intended shape — stopping yourself is something a
process can do, so it is an ordinary action and the host never learns what it means.

The one thing to copy exactly is the *ordering*. `perform_action` sets an event
rather than exiting, and `main` waits on it, because the response still has to be
written to the socket — and because calling `shutdown()` from inside a request
handler deadlocks a threaded server against the very request that called it. A
raven that exits in its handler turns a successful quit into an action the host
reports as failed.

There is **no Start row in either example, and there cannot be one.** A stopped
raven has removed its descriptor, so Roost has no name, no port, and nothing to
draw — the row would have nowhere to live. Starting a stopped raven belongs to the
OS supervisor (`huginn install-agent` and its equivalents), never to the menu bar.
[SPEC.md §10](../SPEC.md#10-lifecycle-quitting-restarting-and-starting) records the
alternatives that were rejected and why.
