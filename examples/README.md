# Reference implementations

Two worked examples of the bird side of the contract in [`../SPEC.md`](../SPEC.md):

| File | What it shows |
|------|---------------|
| [`huginn_bird.py`](huginn_bird.py) | The leading bird: a higher `host_priority`, an attention badge, per-item actions, a token file, and a lifecycle **Quit** row — on the HTTP transport |
| [`muninn_bird.py`](muninn_bird.py) | The companion bird: a lower priority, link-only rows, no token — on the `unix` transport ([SPEC.md §9a](../SPEC.md#9a-the-unix-socket-and-named-pipe-transport)) |

**The two examples now sit on opposite transports on purpose.** `huginn_bird.py`
is unchanged HTTP: bind a port, check `Host`/`Origin`, serve `/api/menu` and
`/api/menu/action`. `muninn_bird.py` is the `unix` transport end to end: a
`multiprocessing.connection.Listener` on a Unix domain socket, a JSON `{"op":
...}` body instead of a path, and every link row resolved against a
`pages_dir` it renders itself rather than against a port. Reading them side by
side is the fastest way to see which parts of the contract are transport-
specific and which are not — a real bird picks one transport, but the
descriptor fields, the menu payload shape, and the token-isolation rule are
identical either way.

They are **documentation that runs**, not libraries. Neither is imported by
Roost and neither is imported by the real Huginn or Muninn — each project
implements the contract itself. These exist so the contract has an executable
form you can read end to end and point at when something disagrees.

## Both birds are real now — read them too

When these files were written they were the only implementations of the protocol
that existed. They are not any more, and a production bird answers questions these
cannot:

| Project | Its bird side |
|---|---|
| [Huginn](https://github.com/tohuw/huginn) | [`huginn/bird.py`](https://github.com/tohuw/huginn/blob/master/huginn/bird.py) — descriptor, menu, and the authenticated `/api/menu` + `/api/menu/action` routes inside its existing FastAPI app (note: its default branch is `master`) |
| [Muninn](https://github.com/tohuw/muninn) | [`muninn/raven.py`](https://github.com/tohuw/muninn/blob/main/muninn/raven.py) for the descriptor and payload, [`muninn/ravenserve.py`](https://github.com/tohuw/muninn/blob/main/muninn/ravenserve.py) for the listener (`unix` on POSIX, `pipe` on Windows) and its publish/withdraw lifecycle |

The two shipped birds sit at opposite ends of the contract's optional parts, which
is more instructive than either example: Huginn authenticates over HTTP and offers
actions; Muninn speaks the `unix`/`pipe` transport, publishes no `token_path` on
POSIX, and is mostly links (plus a `Quit`/`Restart` lifecycle section while its
daemon runs). Both are decisions the protocol leaves to the bird, and each project
records *why* it chose as it did — which is the part an example cannot show you.

They also solve two problems these files sidestep. Muninn splits the payload from
the socket so its payload can be tested with no port bound, and both projects share
the descriptor mechanics through [`corvidae`](https://pypi.org/project/corvidae/), a
stdlib-only package, rather than each writing the atomic-0600-publish and
state-directory-resolution code twice. If you are implementing a third bird,
`corvidae` is probably what you want instead of copying from here.

So: read these for the shape of the contract end to end, and read the two real ones
for how it survives contact with a real application.

Each file is standalone (stdlib only) and can be run directly:

```bash
python3 examples/huginn_bird.py
```

It publishes a descriptor, serves `/api/menu` and `/api/menu/action` on a free
loopback port, and removes its descriptor on exit.

```bash
python3 examples/muninn_bird.py
```

It publishes a `unix`-transport descriptor, binds a Unix domain socket, renders
its `pages_dir` fresh on every menu fetch, and removes its descriptor and
socket file on exit. POSIX only — this example does not demonstrate the
Windows `pipe` transport; see [SPEC.md §9a](../SPEC.md#9a-the-unix-socket-and-named-pipe-transport)
for that side of the contract, which the real Muninn implements but which
cannot be exercised from a POSIX machine.

Start one (or both) and the tray will show them. `roost birds` will explain
anything it will not show.

## What to copy, and what not to

**Copy the shapes:** the descriptor fields, the declared version *range*, the
menu JSON, and publishing the descriptor last (after your listener is bound)
then removing it on exit. Beyond that, what you copy depends on which
transport you pick: `huginn_bird.py`'s `Host`/`Origin` checks and
constant-time token comparison are what an HTTP transport owes the protocol
([SPEC.md §9](../SPEC.md#9-what-the-host-requires-of-your-http-surface));
`muninn_bird.py`'s `pages_dir` rendering and realpath-safe filenames are what
a `unix`/`pipe` transport owes it instead
([SPEC.md §9a](../SPEC.md#9a-the-unix-socket-and-named-pipe-transport)). A bird
speaks one transport, not both, so copy the half that matches the one you
chose.

**Do not copy the internals.** The session lists are hardcoded, the state is a
module global, and there is no persistence. A real bird has its own model and
its own store; the point of the contract is that Roost cannot tell.

## The two rules that are easy to get wrong

**Declare a range, not a version.** A bird declares `min_api`/`max_api` and
Roost accepts any bird whose window overlaps its own. Comparing versions for
equality is the bug behind huginn issue #38: one routine bump silently disabled
every participant, with nothing on screen to say why.

**Own your token; never expect one.** The descriptor names a `token_path` and
Roost reads it fresh on every request and sends it only to the port that
declared it. Roost never mints a credential and never shares one between
birds, so a bird that wants authentication has to publish its own — and a
bird that publishes none gets unauthenticated requests, which is its decision
to make.

## Lifecycle, and the row that cannot exist

`huginn_bird.py` publishes a **Quit** row. Look at how little it took: an id in
`build_menu`, a branch in `perform_action`, and no change to Roost or to the
protocol version. That is the intended shape — stopping yourself is something a
process can do, so it is an ordinary action and the host never learns what it means.

The one thing to copy exactly is the *ordering*. `perform_action` sets an event
rather than exiting, and `main` waits on it, because the response still has to be
written to the socket — and because calling `shutdown()` from inside a request
handler deadlocks a threaded server against the very request that called it. A
bird that exits in its handler turns a successful quit into an action the host
reports as failed.

There is **no Start row in either example, and there cannot be one.** A stopped
bird has removed its descriptor, so Roost has no name, no port, and nothing to
draw — the row would have nowhere to live. Starting a stopped bird belongs to the
OS supervisor (`huginn install-agent` and its equivalents), never to the menu bar.
[SPEC.md §10](../SPEC.md#10-lifecycle-quitting-restarting-and-starting) records the
alternatives that were rejected and why.
