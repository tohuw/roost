# The Bird Protocol

**Protocol version:** 1
**Roost speaks:** 1..1
**Audience:** anyone implementing a bird

This is the contract between Roost — one shared status menu bar — and a
*bird*: a long-running local daemon that reports status into that menu.
[Huginn](https://github.com/tohuw/huginn) and
[Muninn](https://github.com/tohuw/muninn) both implement it. Nothing here is
specific to either.

Two runnable reference implementations live in [`examples/`](examples/). They are
documentation, not libraries: Roost does not import them and neither bird
does. Read them alongside this document — where the two disagree, the code in
`examples/` is the one that has been executed.

Both birds named above now implement this for real, and those are worth reading
next to the examples rather than instead of them — see
[Reference implementations](#reference-implementations) at the end.

---

## Contents

1. [The shape of the protocol](#1-the-shape-of-the-protocol)
2. [The descriptor](#2-the-descriptor)
3. [Version compatibility](#3-version-compatibility)
4. [The menu](#4-the-menu)
5. [Actions and links](#5-actions-and-links)
6. [Token isolation](#6-token-isolation)
7. [Host election](#7-host-election)
8. [Liveness and unavailability](#8-liveness-and-unavailability)
9. [What the host requires of your HTTP surface](#9-what-the-host-requires-of-your-http-surface)
   - [9a. The Unix socket and named pipe transport](#9a-the-unix-socket-and-named-pipe-transport)
10. [Lifecycle: quitting, restarting, and starting](#10-lifecycle-quitting-restarting-and-starting)
11. [A bird's checklist](#11-a-birds-checklist)

---

## 1. The shape of the protocol

Three moving parts, and no central authority:

1. A bird **publishes a descriptor** — one small JSON file in a shared
   directory, naming its port, its PID, and where its token lives.
2. The host **discovers descriptors** by listing that directory, validates each
   one, and checks that the process it names is alive.
3. For each live bird the host **fetches a menu** over loopback and renders it.

There is no registry a bird writes through, so no bird can corrupt another's
entry and a bird that is not running simply has no file. There is no
configuration listing known birds, so adding one is a matter of publishing a
descriptor.

The rule that makes this hold together:

> **The host renders a bird's menu without interpreting it.**
>
> It draws the labels a bird sends and hands the action ids back to the bird
> that published them. It does not know what any id means, it has no special case
> for any bird's name, and it never decides what a bird's menu should contain.

That is why a bird can change its own menu — add a section, rename a row, expose
a new action — with no change to Roost and no version bump. Anything that would
require the host to understand a bird's data does not belong in this protocol.

---

## 2. The descriptor

### Where it goes

One file per bird, named `{name}.json`, in a directory resolved by this rule —
**in this order**:

1. `$BIRDS_STATE_DIR`, if set and non-empty.
2. `$RAVENS_STATE_DIR`, if set and non-empty — the same override under its
   former name.
3. **Windows:** `%LOCALAPPDATA%\Birds`, falling back to
   `~\AppData\Local\Birds` when `LOCALAPPDATA` is unset.
4. **POSIX:** `$XDG_STATE_HOME/birds`, falling back to `~/.local/state/birds`.

Every participant must implement this identically. A bird that resolves it
differently publishes where the host is not looking, and the failure is silent —
an empty menu with nothing to explain it.

#### The `ravens` directory, and why the host still reads it

This contract was written when it had exactly two participants and both of them
were ravens, and it named its directory accordingly: `%LOCALAPPDATA%\Ravens` and
`~/.local/state/ravens`. Huginn and Muninn publish there **today**, because they
resolve the location through [`corvidae`](https://pypi.org/project/corvidae/)
rather than through the host, and will keep doing so until that package is next
released.

So the host **reads both directories and merges them**. A name present in both
resolves to the current one: a bird that has migrated may have left a stale
descriptor behind, and preferring the stale copy would advertise a dead port for
a live process.

Two things follow, and they are not the same thing:

- **A bird written today publishes to `birds`.** One directory, the rule above,
  no compatibility logic of its own. The legacy path is not yours to write to.
- **A host reads both.** Dropping the legacy read is a breaking change for every
  bird that has not moved, and it empties the menu with nothing on screen to say
  why.

When `corvidae` moves, the legacy read becomes dead weight and can go.

Two details are easy to get wrong:

- **`XDG_STATE_HOME` is not optional**, even if your own state directory ignores
  it. This directory is shared; putting it somewhere the other participant does
  not expect breaks discovery for both.
- **Windows is not an afterthought.** A hardcoded POSIX path breaks it outright.

### The fields

```json
{
  "api_version": 1,
  "min_api": 1,
  "max_api": 1,
  "name": "huginn",
  "display": "Huginn",
  "pid": 41213,
  "port": 56713,
  "started": 1785315600.482,
  "host_priority": 100,
  "token_path": "/Users/alice/.local/state/birds/huginn.token",
  "token_header": "X-Huginn-Token",
  "endpoints": {
    "menu": "/api/menu",
    "action": "/api/menu/action"
  },
  "launch": { "kind": "launchd", "id": "is.tohuw.huginn" }
}
```

That is the HTTP transport — no `transport` field at all, which is the point:
every descriptor written before this field existed is still exactly this
shape, and still means exactly this. A `unix`/`pipe` descriptor looks
different enough that showing it here rather than only in [§9a](#9a-the-unix-socket-and-named-pipe-transport)
is worth the repetition:

```json
{
  "api_version": 1,
  "min_api": 1,
  "max_api": 1,
  "name": "muninn",
  "display": "Muninn",
  "pid": 7092,
  "transport": "unix",
  "address": "/Users/alice/.local/state/birds/muninn.sock",
  "pages_dir": "/Users/alice/.local/state/birds/muninn/pages",
  "started": 1785619470.680397,
  "host_priority": 50,
  "endpoints": { "menu": "menu" }
}
```

No `port`. No `token_path`, on POSIX — see [§9a](#9a-the-unix-socket-and-named-pipe-transport)
for why that omission is a decision and not an oversight.

| Field | Required | Type | Meaning and constraints |
|---|---|---|---|
| `api_version` | yes | int | The protocol version you primarily speak. `1..101`. |
| `min_api` | no | int | Oldest version you accept. Defaults to `api_version`. |
| `max_api` | no | int | Newest version you accept. Defaults to `api_version`. |
| `name` | yes | string | Your slug: `[a-z0-9][a-z0-9-]{0,31}`. **Must equal the filename stem.** |
| `display` | no | string | Menu name, ≤64 chars. Defaults to `name`. |
| `pid` | yes | int | Your process id. Must be `> 0`. |
| `transport` | no | string | `"unix"` or `"pipe"`. **Omit it to speak HTTP** — see [§9a](#9a-the-unix-socket-and-named-pipe-transport). |
| `port` | required for HTTP | int | Your loopback port, `1..65535`. Absent — not merely unused — on `unix`/`pipe`. |
| `address` | required for `unix`/`pipe` | string | Your socket path or named-pipe path. Absent on HTTP. |
| `pages_dir` | required for `unix`/`pipe` | string | Absolute path to the directory you render link targets into. Absent on HTTP. |
| `started` | no | number | Your process start time, epoch seconds. **Supply it** — see [§8](#8-liveness-and-unavailability). |
| `host_priority` | no | int | Menu ordering, `-1000..1000`. Higher sorts earlier. Defaults to `0`. |
| `token_path` | no | string | HTTP: absolute path to your token file; omit for unauthenticated requests. `pipe`: absolute path to your `multiprocessing.connection` authkey — see [§9a](#9a-the-unix-socket-and-named-pipe-transport). Meaningless, and never read, on `unix`. |
| `token_header` | no | string | HTTP only. Header your token is presented in. A valid RFC 7230 token, ≤64 chars. Defaults to `X-{Name}-Token`. |
| `endpoints` | no | object | HTTP: a path map. `unix`/`pipe`: an *op name* map — see [§9a](#9a-the-unix-socket-and-named-pipe-transport). ≤12 entries; keys `[a-z][a-z0-9_]{0,31}`. |
| `launch` | no | object | How the host may ask this machine's supervisor to start you again. See below. |

Recognised endpoint keys are `menu` (default `/api/menu`) and `action` (default
`/api/menu/action`). Unknown keys are accepted and ignored, so a future version
can add one without breaking an older host.

#### `launch`, and why it is an identifier

Publish `launch` and a host may offer to start you when your process is gone.
Omit it and it will not — which is the whole of the difference, and is why a
bird that predates this field keeps working unchanged.

| Field | Type | Meaning |
|---|---|---|
| `kind` | string | One of `launchd`, `systemd`, `windows-run`. |
| `id` | string | The service identifier: a launchd label, a systemd **user** unit name, or an `HKCU\…\Run` value name. `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. |

**It names an identifier and never a command, and a host must never accept one
that does.** The descriptor directory is writable by anything running as this
user, and the host is a single process shared across every bird — so a
descriptor that named a program to execute would be a write-then-execute path
into the host. The host hands `id` to the platform's own supervisor and lets it
decide what to run. The command lives in the plist, the unit, or the `Run` key,
put there by an `install-agent` the user ran deliberately; the worst a forged
descriptor achieves is starting a service that is already installed.

A host must also ignore a `launch` whose `kind` is not this machine's supervisor
— a descriptor copied between machines can name `launchd` on Linux, and a row
that cannot work is worse than no row. Publishing `launch` is not a claim that
anything is installed: the supervisor answers "no such service" and the host
reports that.

### Constraints the host enforces

A descriptor is **untrusted input**: a file written by another process. It is
never `eval`'d and never trusted to be well-typed or truthful. The host applies:

- **A 16 KiB size cap**, enforced on the read rather than on a `stat` — a file can
  grow between the two, and the point of the cap is to bound what enters memory.
- **`name` must equal the filename stem.** Refused rather than reconciled: the
  filename is what discovery keys off, so allowing them to differ would let one
  bird publish a descriptor impersonating another.
- **Endpoint values must be `/`-rooted relative paths** with no `..` segment, no
  `//` or `/\` prefix, and no query or fragment. A value carrying a scheme or
  authority would redirect the host off the bird it is talking to — the
  descriptor equivalent of an open redirect. The host pins the origin to
  `127.0.0.1` and the port to the declared one regardless.
- **`token_path` must be absolute.** The path is checked for shape only; whether a
  token can be read is decided per request, because you may rotate at any moment.
- **No control characters, ANSI escapes, or bidirectional overrides** in any
  string field. These are *refused*, not cleaned: a control character means the
  file is not what it claims to be, and quietly repairing it would hide that. (In
  a *menu* payload they are stripped instead — see [§4](#4-the-menu).)
- **`bool` is not an `int`.** `true` in a numeric field is refused, not read as
  `1`.

Any violation makes you an **unavailable bird with a reason** ([§8](#8-liveness-and-unavailability)).
Never a crash, never a silent omission.

### Writing it

**Write atomically.** The host may read at any moment and must never see a partial
file. Stage in a temp file *in the same directory* (so the replace cannot cross a
filesystem boundary), `fsync`, then `os.replace`.

**Publish after you bind.** A descriptor naming a port that is not yet listening
makes the host report a healthy bird as unreachable during your startup.

**Remove it on exit,** best-effort. A stopped bird should have no descriptor
rather than a stale one. A hard kill that skips this is still handled — the host
checks your PID before trusting the file — so do not add complexity to guarantee
it.

---

## 3. Version compatibility

**Declare a range. Never compare for equality.**

You declare the inclusive window `min_api..max_api`. Roost advertises
`MIN_API_VERSION..API_VERSION` (currently `1..1`) and renders you if the two
windows **overlap**:

```
min_api <= ROOST_API_VERSION  AND  max_api >= ROOST_MIN_API_VERSION
```

This is not stylistic. Exact matching is the bug behind **huginn issue #38**: one
routine version bump silently disabled every plugin, with nothing on screen to
say why. Two properties follow from getting it right:

- **A bump on one side does not break the other.** A bird declaring `1..2`
  keeps working against a host that speaks `1..1`, and vice versa.
- **A genuine incompatibility is loud.** The host reports it as an unavailable
  bird whose reason *names both ranges*:

  > `Descriptor needs bird API [3, 4]; this menu bar speaks [1, 1].`

  Never a bird that quietly stops appearing.

A declared value is capped at `API_VERSION + 100`, so a hostile descriptor cannot
claim `max_api = 2**63` and stay "compatible" through every future breaking
change.

**Additive changes need no bump at all.** Unknown fields are dropped rather than
rejected throughout, so adding a menu field or an endpoint key is not a protocol
change. Reserve a bump for something that genuinely breaks an older reader.

Lifecycle ([§10](#10-lifecycle-quitting-restarting-and-starting)) is the worked
example, and the useful one because it *sounds* like a protocol change and is not:
a bird that publishes a **Quit** row is publishing an ordinary action id, so the
window stayed `1..1` and an older host renders the row correctly without knowing
what it does. Had it been spelled as a `lifecycle` field the host had to
recognise, every host below the bump would have had to be told — for a feature
that needed nothing from the host at all. **Reach for a new id before a new
field, and for a new field before a new version.**

---

## 4. The menu

The host `GET`s your `menu` endpoint and expects JSON:

```json
{
  "api_version": 1,
  "title": "Huginn",
  "badge": 2,
  "sections": [
    {
      "id": "attention",
      "title": "Needs attention",
      "items": [
        {
          "id": "focus:s-1",
          "label": "Approve: deploy to staging",
          "detail": "claude",
          "style": "attention"
        },
        {"separator": true},
        {"id": "open-console", "label": "Open Console", "url": "/"}
      ]
    }
  ]
}
```

### Top level

| Field | Type | Meaning |
|---|---|---|
| `title` | string | Replaces the descriptor's `display` for this render, so you can retitle your own section as your state changes. |
| `badge` | int | Your count of things wanting attention, `0..9999`. Shown beside your name; summed across birds. Zero is not shown. |
| `sections` | array | Up to 12. Anything else is dropped. |

### A section

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Opaque to the host. |
| `title` | string | A heading row. Omit for an untitled group. |
| `items` | array | **Required.** Up to 50 per section, 200 in total. A section with no renderable items is dropped entirely. |

### An item

| Field | Type | Meaning |
|---|---|---|
| `label` | string | **Required.** ≤120 chars. An item with no legible label is dropped — there is nothing to show. |
| `id` | string | Action id, ≤128 chars. POSTed back to your `action` endpoint. |
| `url` | string | Bird-local path, ≤512 chars. Opened as `http://127.0.0.1:{port}{url}`. |
| `detail` | string | Secondary text, ≤80 chars. Rendered after the label. |
| `enabled` | bool | Defaults `true`. An item with neither `id` nor `url` is forced to `false`. |
| `style` | string | `normal`, `attention`, or `muted`. Anything else becomes `normal`. |
| `separator` | bool | A divider. Every other field is ignored. |

### How the host treats it

- **`style` is an intent, not styling.** The host maps it to its own presentation
  (currently a `●` or `·` marker). A bird cannot supply a marker of its own,
  because that would be styling by another name — and the host deciding
  presentation is what keeps the two platforms' menus identical.
- **Strings are sanitised, not refused.** Unlike descriptor fields, a menu label
  has ANSI escapes, control characters, and bidi overrides *stripped*, whitespace
  collapsed, and length capped. A menu is live data on a hot path; a single bad
  character should degrade one row, not disable a running bird. The reason both
  policies exist: a bad descriptor is a broken bird, a bad label is a bad label.
- **Bounds are enforced while parsing, not after.** A hostile payload cannot make
  the host build a huge structure and only then discard it. Ten thousand items
  would hang the menu build inside the UI run loop, which reads to the user as a
  frozen desktop.
- **Leading, trailing, and doubled separators are dropped.**
- **Unknown fields are ignored.**
- **An unusable payload leaves you "up but silent."** If you answer but nothing
  survives parsing, the host draws your name with *"Nothing to report."* — which
  is visibly different from a bird it could not reach.

An empty `sections` array is a legitimate answer and produces the same
"Nothing to report." row.

---

## 5. Actions and links

An item carries **either** an `id` **or** a `url`. One with neither renders
disabled, because a row that looks clickable and does nothing is worse than one
that admits it.

### Actions

The host POSTs the id back, unchanged, to your `action` endpoint:

```http
POST /api/menu/action
Content-Type: application/json
X-Huginn-Token: <your token>

{"id": "focus:s-1"}
```

The id is **your** vocabulary, round-tripped through a host that does not parse
it. Any JSON object response is accepted; the host does not act on the body.

It still arrives over HTTP from another process, so **match it against what you
actually issued** rather than parsing it for meaning. Treat it as a request, not
an instruction.

The host bounds the call (5 s) and refuses to send an id that is not printable
text. A failure is logged and shown at the next refresh; it never raises into the
menu.

An action may do anything, **including ending the process that answered it** — see
[§10](#10-lifecycle-quitting-restarting-and-starting). Nothing about that is
special to the host, which is the point; the only requirement it places on you is
the ordinary one above, that you answer within the timeout rather than dying
mid-response.

### Links

> **A menu item cannot navigate the user anywhere except the bird that offered
> it.** That invariant is transport-neutral; how it is enforced is not.

On the HTTP transport, a `url` is opened in the browser as
`http://127.0.0.1:{port}{url}`, built from **your descriptor's own port**.
Query strings are allowed (a link legitimately carries parameters); fragments
are not.

On the `unix`/`pipe` transport there is no port to build that URL from at all,
so a `url` is resolved against your descriptor's own `pages_dir` instead, and
the invariant above is enforced by realpath containment rather than by origin
— see [§9a](#9a-the-unix-socket-and-named-pipe-transport) for the exact rule.

---

## 6. Token isolation

> **Each bird owns its own credential. The host never mints one, never shares
> one, and never caches one.**

Concretely:

- The host reads a token from the `token_path` in **your** descriptor and sends it
  only to **your** port, under the header **you** named.
- It reads **fresh on every request**. You rotate whenever you like — a new token
  per start is the expected pattern — with no coordination. A cached token would
  make the host authenticate with a dead credential and report you as broken.
- Request headers are built per call from a fixed allowlist. There is no path by
  which one bird's credential can appear in another bird's request.
- A token is capped at 4 KiB, and one containing CR/LF or control characters is
  refused rather than sent — it would inject headers into the host's own request.
- **No `token_path` means unauthenticated requests.** Whether that is acceptable
  is your decision, not the host's. It will not invent a credential to cover for
  you.

If a read fails (mid-rotation, say), the host sends the request unauthenticated
rather than failing the fetch. Your `401` then becomes a visible reason the user
can act on:

> `Rejected the credential from its own token file.`

Write your token file **owner-only, created with restrictive permissions from the
outset** (`os.open(..., 0o600)`), not chmodded after the fact — creating it first
leaves a window in which it is world-readable.

---

## 7. Host election

**Exactly one process draws the menu.** It is elected by an exclusive lock on a
single file under Roost's own state directory (`~/.local/state/roost/roost.lock`
on POSIX, `%LOCALAPPDATA%\Roost\roost.lock` on Windows; mode 0600): whoever takes it hosts. `flock` on POSIX, an exclusive open on
Windows — both released by the OS if the holder dies, so **there is no stale-lock
case to reason about**, unlike a PID file.

A second tray process finds the lock held and exits quietly. A process that
cannot *create* the lock at all — a read-only state directory — reports that
distinctly, because the two need different handling: contention is the normal
outcome of a duplicate launch, while an unwritable path means this machine cannot
host at all and the user has to be told.

**Which bird leads the menu is a separate question, answered by data.** You
declare `host_priority`; the host sorts by it, descending, then by name. Huginn
declares a higher priority than Muninn and therefore leads when both are present;
when Huginn is absent, Muninn's section simply sorts first and the same menu runs
standalone.

Neither bird knows the other exists, and the host knows neither name. Hardcoding
one would be the same mistake as a hardcoded catalog id — which is exactly the
mistake this repository's previous design made, twice, once per platform.

Birds do **not** participate in host election. You are not the host; you are
never asked to be; you never need to know who is.

---

## 8. Liveness and unavailability

### Liveness

The host checks that the process you named is alive, resisting **PID reuse**:

- `os.kill(pid, 0)` (or `psutil` on Windows) asks whether the process exists.
- When you supplied `started`, it is cross-checked against the OS's own record of
  when that process began, with two seconds of slack for clock differences. That
  check is the only reason `started` is in the schema.
- A non-positive PID is refused before it reaches any syscall.
- `PermissionError` counts as alive: the process exists, it just belongs to
  another user.
- If the start time **cannot be determined**, the check does not contradict — a
  missing cross-check must never turn a live bird into a dead one.
- A **zero or negative `started` means "unknown"**, and is treated exactly as if
  you had omitted the field. That is the value a bird writes when it could not
  read its own start time, so comparing against it would fail for every live
  process rather than only for recycled PIDs.

**Supply `started`, and read it from the OS** — not from the clock at the moment
you write the descriptor. Without it, a recycled PID can pass as a live bird.
With a wall-clock reading, the opposite happens the moment the two diverge by
more than the slack: any republish from a process that has been running a while
— a restart handled in-process, say — stamps "now" onto a process the OS says
began long ago, and the host declares a healthy bird gone.

### Unavailability is a first-class result

> An unreachable, stopped, stale, or malformed bird renders as a **disabled
> section with a visible reason**.

Never a crash. Never a silent omission — a bird that vanished from the menu is
indistinguishable from one that was never installed, and leaves the user nothing
to act on. Never "trusted anyway."

```
Muninn
  Not running (its recorded process is gone).
```

The reasons the host produces:

| Reason | Cause |
|---|---|
| `Not running (its recorded process is gone).` | PID dead, or `started` did not match |
| `Is not answering on its recorded port.` | Connection refused or failed |
| `Did not answer in time.` | Exceeded the fetch timeout |
| `Rejected the credential from its own token file.` | Answered `401`/`403` |
| `Answered HTTP {code}.` | Any other error status |
| `Answered with something that is not JSON.` | Unparseable body |
| `Sent a response that is too large.` | Over 256 KiB |
| `Descriptor …` | Any validation failure, quoting the specific problem |

Failure is **not contagious**: one broken bird never prevents another's section
from rendering, and the host survives a bird that hangs, floods, or lies.

`roost birds` prints the same reasons in a terminal.

---

## 9. What the host requires of your HTTP surface

The host is **not** a security boundary on your behalf. It never forwards an
inbound request to you — it is purely an outbound client — so anything reaching
your port came from something else. Defend it yourself.

**Required:**

- **Bind loopback only.** `127.0.0.1`, never `0.0.0.0`.
- **Validate `Host` is loopback.** A page served from any other hostname carries
  that hostname in `Host` even when it resolves to `127.0.0.1`. This is what stops
  DNS rebinding, and it is the check a relay must never be able to launder past —
  see the note below.
- **Reject any request carrying an `Origin`.** Your menu API serves no page a
  script should be calling; an `Origin` means one did.
- **Guard `Content-Length`.** Reject `length < 0` or `> cap` before reading a
  byte. A negative length passed to `read()` means "until EOF" — no bound at all.
- **Compare tokens in constant time** (`hmac.compare_digest`). `==` on a secret
  leaks its prefix through timing.
- **Send `X-Content-Type-Options: nosniff`** on every response, and a
  `Content-Security-Policy` on any HTML.

**The host's side of the bargain:** every call is bounded — a 2 s menu timeout, a
5 s action timeout, a 256 KiB response cap enforced on the read rather than on
your declared `Content-Length`, and redirects refused outright (following one
would send the host, and the token it just attached, to an origin your descriptor
never declared). A hung bird degrades to a disabled section, never to a frozen
menu.

> **Why the host relays nothing.** An earlier design in this repository ran an
> unauthenticated proxy on a fixed loopback port that rewrote `Host` to a clean
> loopback value. Any web page could reach it, and what arrived upstream looked
> locally originated — laundering an attack straight past the app's own
> `require_local_origin` check. The fix was structural, not a filter: there is no
> upstream. Roost's only listener is the Help page, which forwards nothing,
> refuses a foreign `Host` and **any** `Origin`, takes no request body, and routes
> only `GET`.
>
> Do not add a relay to this protocol. If a bird needs a fixed external URL, it
> owns that endpoint itself, with its own authentication.

---

## 9a. The Unix socket and named pipe transport

Everything in [§9](#9-what-the-host-requires-of-your-http-surface) is about
one problem: a web page open in the user's browser can reach a loopback TCP
port, so a bird on that transport has to defend against it with `Host` and
`Origin` checks. A Unix domain socket cannot be reached that way at all — no
browser API opens one, and there is no `Host` header to spoof because there is
no HTTP. This section is the transport that takes that problem off the table
instead of mitigating it, and it is a straight client-side reading of Muninn's
own [docs/specs/021-unix-socket-transport.md](https://github.com/tohuw/muninn/blob/main/docs/specs/021-unix-socket-transport.md),
which is the normative source for the wire contract below. If the two ever
disagree, that document wins.

**Declare it, or don't.** A descriptor with no `transport` field — every
descriptor this protocol has ever had, and Huginn's today — means HTTP,
exactly as [§2](#2-the-descriptor) through [§9](#9-what-the-host-requires-of-your-http-surface)
already describe, with no change of any kind. Declaring `transport` is opting
*in*, per bird, forever optional: nothing requires a bird to migrate, and
nothing here bumps `api_version`.

### The transports

| `transport` | Platform | Address names | Credential |
|---|---|---|---|
| absent, or `"http"` | any | `port` | `token_path` (optional), HTTP header |
| `"unix"` | POSIX | `address`: a socket path | none — the socket's own file mode is the whole boundary |
| `"pipe"` | Windows | `address`: a named-pipe path (`\\.\pipe\...`) | `token_path`: a file holding a `multiprocessing.connection` authkey |

`"unix"` carries no token because there is nothing a token would add: opening
the socket already requires filesystem permission on its own inode, which is
the same guarantee a token would exist to approximate. `"pipe"` is the
platform Python's `socket` module has no `AF_UNIX` for, and a named pipe's
default security descriptor does not restrict connections to its creator —
`multiprocessing.connection`'s own documentation recommends an `authkey`
there for exactly that reason, so this is the one case in the whole protocol
where a socket-transport bird should publish `token_path`.

### The wire contract

Both transports speak `multiprocessing.connection` — `Client`/`Listener`,
`family="AF_UNIX"` or `"AF_PIPE"` — rather than a hand-rolled protocol, because
it already abstracts exactly this platform difference and its
`send_bytes`/`recv_bytes` give length-prefixed framing for free. One
connection per call: connect, send one message, read one reply, close. No
keep-alive, no pipelining.

There is no URL space to route a request on, so the request names an **op**
in its JSON body instead of a path:

```json
{"op": "menu"}
{"op": "action", "id": "quit"}
```

and the reply is `{"ok": true, "body": ...}` or `{"ok": false, "error": "..."}`.
`endpoints` still exists in the descriptor and still means "what do I call
this," but its values are now op names rather than paths — `"menu": "menu"` is
what you send as `{"op": "menu"}`, not a route to `GET`. A menu-fetch reply's
`body` is the same payload [§4](#4-the-menu) describes, unwrapped from its
`ok`/`body` envelope; an action reply is whatever object you answered with,
same as the HTTP transport's response body, just with no status code to carry
success or failure — that is what `ok` is for here, and an action reply
missing it, or setting it to anything but `true`, is a failure.

**`Client`/`Connection` take no timeout parameter.** The host gets a bounded
wait from `multiprocessing.connection.wait([conn], timeout=T)` before
`recv_bytes()`, using the same 2 s menu / 5 s action budgets [§9](#9-what-the-host-requires-of-your-http-surface)
already commits to — the transport changed; the budget did not.

### Links, resolved against `pages_dir` instead of a port

A socket-transport descriptor has no port for a `url` to resolve against, so
it names `pages_dir`: a directory where you render, on every menu build, a
static file for every link your menu just emitted. The host resolves a `url`
against it by one rule, applied exactly:

- `url == "/"` → the candidate file is `pages_dir/index.html`.
- Any other `url` → the candidate file is `pages_dir/<url without its leading
  slash>.html` (`/session/abc123` → `pages_dir/session/abc123.html`).
- The candidate's **realpath** must have `pages_dir`'s own realpath as a
  prefix, and must name a file that **already exists**. Anything else —
  containment failure or a missing file — is refused, with no distinction
  between the two in what the user sees.

That containment check, not the string manipulation above it, is what
actually enforces [the Links invariant](#links): a bird can only ever open
something it itself rendered under its own directory, because the host never
trusts a `url` to describe a real file — it demands the file and checks where
it actually is.

**Render only what you just linked to.** The host's containment check treats
"no file" as "refused," with nothing to distinguish that from a hostile
attempt — so a page rendered for a `url` your current menu build did not just
emit is a page that check cannot protect, and a stale one left over from an
earlier menu is a live, unrefereed link for as long as it sits there. Render
`pages_dir` fresh from the same payload you are about to return, inside the
same call that builds it.

**Filenames still need their own containment, independent of the host's.**
`pages_dir/<url>.html` is a path *you* write to before the host ever reads it,
so a `url` that survived into your own menu payload with a `..` segment or an
absolute shape is a traversal risk on your side of the socket before it is
ever the host's problem — constrain the identifier a `url` is built from (a
session id, say) to a narrow character class once, the same way you already
decide what to put in a `url` at all.

### What a socket-transport bird still owes the protocol

Nothing in [§1](#1-the-shape-of-the-protocol) through [§8](#8-liveness-and-unavailability)
or [§10](#10-lifecycle-quitting-restarting-and-starting) changes: the descriptor is still
untrusted input, validated the same way; liveness is still PID plus `started`;
Quit and Restart are still ordinary action ids answered before you exit. Only
the fields that name *how to reach you* and *how a link resolves* are
different, and they are exactly the fields this section describes.

---

## 10. Lifecycle: quitting, restarting, and starting

Birds replaced menu bars that owned the daemon's lifecycle — they started a dead
daemon, stopped a live one, and offered a restart. This section says how each of
those is expressed here, and it is deliberately short in one direction: **two of
the three need nothing new, and the third is not the host's job.**

### Quit and Restart are ordinary actions

A running bird can stop or restart *itself*. It is a process; it has a signal
handler and an exit path; it does not need a second process to end it. So these
are published exactly like any other row:

```json
{"id": "quit", "label": "Quit Huginn"}
{"id": "restart", "label": "Restart Huginn"}
```

There is **no `lifecycle` field, no reserved id, and no version bump**, because
nothing above changes what the host does. The host draws the label and POSTs the
id back, as it does for `focus:s-1`. It does not know that `quit` ends a process
any more than it knows what `focus:s-1` focuses, and that ignorance is the
property [§1](#1-the-shape-of-the-protocol) is built on — a host that recognised
`quit` would be interpreting a companion's data, and would then owe every bird
an opinion about what stopping means.

Two consequences follow, and both are on the bird:

- **Answer before you exit.** The host waits up to 5 s for a response
  ([§5](#5-actions-and-links)). A bird that dies inside the request handler makes
  a successful quit look like a failure. Reply, *then* shut down — ask your own
  event loop to stop and let the HTTP response drain first.
- **Withdraw on the way out**, as [§2](#2-the-descriptor) already requires. A quit
  that leaves a descriptor behind is a bird the host reports as
  `Not running (its recorded process is gone).` rather than one that is simply
  gone.

A bird that offers no lifecycle rows is complete and correct. These are not
required ids; nothing here reserves the words `quit` or `restart`, and a bird may
name them anything, translate them, or omit them.

### Starting a stopped bird is not the host's job

> **Roost never starts a bird.** Not by `Popen`, not by `launchctl`, not from a
> path recorded in a file. If a bird is stopped, Roost has nothing to click.

This is the one place the protocol says *no* rather than *how*, so it is worth
being precise about why — the naive fix looks small and is not.

**There is nothing to click on.** A stopped bird has no descriptor: [§2](#2-the-descriptor)
requires withdrawal on exit, so the directory is empty and the host has no name,
no port, and no row. Verified: with nothing running, `~/.local/state/birds/` has
no files in it. An action is a row in *some bird's* menu, and there is no menu.
"Start Huginn" is therefore not expressible as an action — not by convention, but
by construction.

**The obvious repairs each reintroduce something worse.** All three were
considered and rejected:

| Rejected | Why |
|---|---|
| A **persistent registration** written at install time, naming an interpreter and a checkout for the host to run | This is a write-then-execute path with the file as the only gate. Huginn already shipped exactly this — `daemon.json` records `python` and `repo` so a tray could relaunch a dead daemon — and it needed 0600, an ownership check, a group/world-writable check on every parent, and a bounded ancestor walk before it was safe, because the old macOS app *executed* the interpreter named in it. Moving that into a shared host multiplies it: every bird's registration becomes an exec path in one process. |
| A **withdrawn-but-present descriptor** marked `stopped` | It contradicts [§2](#2-the-descriptor) and [§8](#8-liveness-and-unavailability). A file that outlives its process is exactly what the PID and `started` cross-check exists to disbelieve, and a `stopped` flag would be a self-reported claim the host cannot verify — a crashed bird and a cleanly-stopped one would be indistinguishable except by a field the dead process did not get to write. It also turns "uninstalled" into a state nobody ever clears. |
| The host asking the **OS supervisor** (`launchctl kickstart`, `systemctl --user start`) on the bird's behalf | Closer — the exec is the supervisor's, not the host's — but the host still has to learn *which* unit belongs to which bird, from a descriptor that is not there. It would need a second persistent registry keyed by bird name, which is the first row of this table again with a launchd label in place of an interpreter path. |

**What answers the need instead.** The OS supervisor already does this, without
the host in the picture at all: a bird registers a login agent (Huginn's
`install-agent` — launchd on macOS, a systemd user unit on Linux, a `Run` key on
Windows) and the OS starts it at login and, on macOS, restarts it after a crash.
That is a supervisor relationship between the bird and the OS. Roost is not a
party to it, holds no path from it, and executes nothing.

So the honest division is:

| Want | Who does it |
|---|---|
| Stop a running bird | The bird, via an action id it published |
| Restart a running bird | The bird, the same way |
| Start it at login, keep it up | The OS supervisor the bird registered with |
| Start it right now, from stopped | The user — a shell, or the login agent — **not the menu bar** |

**A note for a bird implementing Quit.** If a supervisor with a restart policy is
installed, quitting may not stick: launchd's `KeepAlive` relaunches the daemon even
after a clean exit — deliberately, and Huginn documents removing the agent first.
That conflict is between the bird and its supervisor, and it is one the host
cannot mediate, which is another way of saying the host was never the right place
for a start button.

---

## 11. A bird's checklist

Written for the HTTP transport, since that is still what most of this
protocol's participants speak and what a bird predating [§9a](#9a-the-unix-socket-and-named-pipe-transport)
has already implemented. A `unix`/`pipe` bird follows the same shape with the
substitutions [§9a](#9a-the-unix-socket-and-named-pipe-transport) spells
out — bind a socket or pipe instead of step 1, render `pages_dir` instead of
step 6's `Host`/`Origin` guards, and so on; nothing else here changes.

**Startup**

1. Bind a port on `127.0.0.1`.
2. Mint a token and write it `0600` (or decide, explicitly, to accept
   unauthenticated requests).
3. Write your descriptor **atomically**, and only **after** the bind. Include
   `started`. Declare `min_api`/`max_api` as a **range**.

**While running**

4. Serve `menu` returning the JSON in [§4](#4-the-menu). Keep it fast: it is on
   the host's menu-build path.
5. Serve `action` if you publish any ids, and match each against what you issued.
   If any of them stop or restart you, answer *before* you exit
   ([§10](#10-lifecycle-quitting-restarting-and-starting)).
6. On every request: check `Host`, reject any `Origin`, guard `Content-Length`,
   compare the token in constant time.
7. Rewrite the descriptor if your port or token path changes.

**Shutdown**

8. Remove your descriptor and token file, best-effort.

**Never**

- Compare `api_version` for equality.
- Assume the host will authenticate for you, or supply a credential.
- Assume the host understands what any of your ids mean — including one that
  stops you.
- Assume the host will start you. It never will
  ([§10](#10-lifecycle-quitting-restarting-and-starting)); register with your
  platform's login agent instead.
- Assume you are the host, or that another bird is running.
- Return an unbounded response, or block a menu fetch indefinitely.

---

## Reference implementations

### Executable, in this repository

| File | Shows |
|---|---|
| [`examples/huginn_bird.py`](examples/huginn_bird.py) | Leading bird: higher priority, badge, token, per-item actions |
| [`examples/muninn_bird.py`](examples/muninn_bird.py) | Companion bird: lower priority, link-only rows, the `unix` transport end to end ([§9a](#9a-the-unix-socket-and-named-pipe-transport); POSIX only — `pipe` is not exercised here) |

Both are stdlib-only and runnable:

```bash
python3 examples/huginn_bird.py
```

Start one or both and the tray will show them; `roost birds` will explain
anything it will not show.

### Shipped, in the birds themselves

Both birds implement this contract in production, and the two sit at opposite ends
of its optional parts — which makes them the better answer to "how do I actually do
this in an application that already exists":

| Project | Its bird side |
|---|---|
| [Huginn](https://github.com/tohuw/huginn) | [`huginn/bird.py`](https://github.com/tohuw/huginn/blob/master/huginn/bird.py) — authenticated `menu` *and* `action` routes inside an existing FastAPI app, behind the same token gate as the rest of its API |
| [Muninn](https://github.com/tohuw/muninn) | [`muninn/raven.py`](https://github.com/tohuw/muninn/blob/main/muninn/raven.py) (descriptor and payload) plus [`muninn/ravenserve.py`](https://github.com/tohuw/muninn/blob/main/muninn/ravenserve.py) (its listener) — the `unix`/`pipe` transport of [§9a](#9a-the-unix-socket-and-named-pipe-transport), no `token_path` on POSIX, an authkey `token_path` on Windows, and a `Quit`/`Restart` lifecycle section when its daemon runs it |

Neither is a dependency of Roost and Roost is not a dependency of either. The
descriptor mechanics [§2](#2-the-descriptor) describes — the state-directory
resolution rule, the atomic 0600 publish, the ownership-checked withdraw, the
liveness cross-check — are shared between them through
[`corvidae`](https://pypi.org/project/corvidae/), a stdlib-only package with no
dependencies. **A third bird should probably use it rather than reimplement §2**;
the resolution rule in particular fails silently when two participants disagree,
which is the whole reason it was extracted. What a descriptor *says*, and the menu
itself, stay per-project on purpose.
