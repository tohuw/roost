# The Raven Protocol

**Protocol version:** 1
**Appistry speaks:** 1..1
**Audience:** anyone implementing a raven

This is the contract between Appistry — one shared status menu bar — and a
*raven*: a long-running local daemon that reports status into that menu.
[Huginn](https://github.com/tohuw/huginn) and
[Muninn](https://github.com/tohuw/muninn) both implement it. Nothing here is
specific to either.

Two runnable reference implementations live in [`examples/`](examples/). They are
documentation, not libraries: Appistry does not import them and neither raven
does. Read them alongside this document — where the two disagree, the code in
`examples/` is the one that has been executed.

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
10. [A raven's checklist](#10-a-ravens-checklist)

---

## 1. The shape of the protocol

Three moving parts, and no central authority:

1. A raven **publishes a descriptor** — one small JSON file in a shared
   directory, naming its port, its PID, and where its token lives.
2. The host **discovers descriptors** by listing that directory, validates each
   one, and checks that the process it names is alive.
3. For each live raven the host **fetches a menu** over loopback and renders it.

There is no registry a raven writes through, so no raven can corrupt another's
entry and a raven that is not running simply has no file. There is no
configuration listing known ravens, so adding one is a matter of publishing a
descriptor.

The rule that makes this hold together:

> **The host renders a raven's menu without interpreting it.**
>
> It draws the labels a raven sends and hands the action ids back to the raven
> that published them. It does not know what any id means, it has no special case
> for any raven's name, and it never decides what a raven's menu should contain.

That is why a raven can change its own menu — add a section, rename a row, expose
a new action — with no change to Appistry and no version bump. Anything that would
require the host to understand a raven's data does not belong in this protocol.

---

## 2. The descriptor

### Where it goes

One file per raven, named `{name}.json`, in a directory resolved by this rule —
**in this order**:

1. `$RAVENS_STATE_DIR`, if set and non-empty.
2. **Windows:** `%LOCALAPPDATA%\Ravens`, falling back to
   `~\AppData\Local\Ravens` when `LOCALAPPDATA` is unset.
3. **POSIX:** `$XDG_STATE_HOME/ravens`, falling back to `~/.local/state/ravens`.

Every participant must implement this identically. A raven that resolves it
differently publishes where the host is not looking, and the failure is silent —
an empty menu with nothing to explain it.

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
  "token_path": "/Users/alice/.local/state/ravens/huginn.token",
  "token_header": "X-Huginn-Token",
  "endpoints": {
    "menu": "/api/menu",
    "action": "/api/menu/action"
  }
}
```

| Field | Required | Type | Meaning and constraints |
|---|---|---|---|
| `api_version` | yes | int | The protocol version you primarily speak. `1..101`. |
| `min_api` | no | int | Oldest version you accept. Defaults to `api_version`. |
| `max_api` | no | int | Newest version you accept. Defaults to `api_version`. |
| `name` | yes | string | Your slug: `[a-z0-9][a-z0-9-]{0,31}`. **Must equal the filename stem.** |
| `display` | no | string | Menu name, ≤64 chars. Defaults to `name`. |
| `pid` | yes | int | Your process id. Must be `> 0`. |
| `port` | yes | int | Your loopback port, `1..65535`. |
| `started` | no | number | Your process start time, epoch seconds. **Supply it** — see [§8](#8-liveness-and-unavailability). |
| `host_priority` | no | int | Menu ordering, `-1000..1000`. Higher sorts earlier. Defaults to `0`. |
| `token_path` | no | string | Absolute path to your token file. Omit if you accept unauthenticated requests. |
| `token_header` | no | string | Header your token is presented in. A valid RFC 7230 token, ≤64 chars. Defaults to `X-{Name}-Token`. |
| `endpoints` | no | object | Path map. ≤12 entries; keys `[a-z][a-z0-9_]{0,31}`. |

Recognised endpoint keys are `menu` (default `/api/menu`) and `action` (default
`/api/menu/action`). Unknown keys are accepted and ignored, so a future version
can add one without breaking an older host.

### Constraints the host enforces

A descriptor is **untrusted input**: a file written by another process. It is
never `eval`'d and never trusted to be well-typed or truthful. The host applies:

- **A 16 KiB size cap**, enforced on the read rather than on a `stat` — a file can
  grow between the two, and the point of the cap is to bound what enters memory.
- **`name` must equal the filename stem.** Refused rather than reconciled: the
  filename is what discovery keys off, so allowing them to differ would let one
  raven publish a descriptor impersonating another.
- **Endpoint values must be `/`-rooted relative paths** with no `..` segment, no
  `//` or `/\` prefix, and no query or fragment. A value carrying a scheme or
  authority would redirect the host off the raven it is talking to — the
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

Any violation makes you an **unavailable raven with a reason** ([§8](#8-liveness-and-unavailability)).
Never a crash, never a silent omission.

### Writing it

**Write atomically.** The host may read at any moment and must never see a partial
file. Stage in a temp file *in the same directory* (so the replace cannot cross a
filesystem boundary), `fsync`, then `os.replace`.

**Publish after you bind.** A descriptor naming a port that is not yet listening
makes the host report a healthy raven as unreachable during your startup.

**Remove it on exit,** best-effort. A stopped raven should have no descriptor
rather than a stale one. A hard kill that skips this is still handled — the host
checks your PID before trusting the file — so do not add complexity to guarantee
it.

---

## 3. Version compatibility

**Declare a range. Never compare for equality.**

You declare the inclusive window `min_api..max_api`. Appistry advertises
`MIN_API_VERSION..API_VERSION` (currently `1..1`) and renders you if the two
windows **overlap**:

```
min_api <= APPISTRY_API_VERSION  AND  max_api >= APPISTRY_MIN_API_VERSION
```

This is not stylistic. Exact matching is the bug behind **huginn issue #38**: one
routine version bump silently disabled every plugin, with nothing on screen to
say why. Two properties follow from getting it right:

- **A bump on one side does not break the other.** A raven declaring `1..2`
  keeps working against a host that speaks `1..1`, and vice versa.
- **A genuine incompatibility is loud.** The host reports it as an unavailable
  raven whose reason *names both ranges*:

  > `Descriptor needs raven API [3, 4]; this menu bar speaks [1, 1].`

  Never a raven that quietly stops appearing.

A declared value is capped at `API_VERSION + 100`, so a hostile descriptor cannot
claim `max_api = 2**63` and stay "compatible" through every future breaking
change.

**Additive changes need no bump at all.** Unknown fields are dropped rather than
rejected throughout, so adding a menu field or an endpoint key is not a protocol
change. Reserve a bump for something that genuinely breaks an older reader.

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
| `badge` | int | Your count of things wanting attention, `0..9999`. Shown beside your name; summed across ravens. Zero is not shown. |
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
| `url` | string | Raven-local path, ≤512 chars. Opened as `http://127.0.0.1:{port}{url}`. |
| `detail` | string | Secondary text, ≤80 chars. Rendered after the label. |
| `enabled` | bool | Defaults `true`. An item with neither `id` nor `url` is forced to `false`. |
| `style` | string | `normal`, `attention`, or `muted`. Anything else becomes `normal`. |
| `separator` | bool | A divider. Every other field is ignored. |

### How the host treats it

- **`style` is an intent, not styling.** The host maps it to its own presentation
  (currently a `●` or `·` marker). A raven cannot supply a marker of its own,
  because that would be styling by another name — and the host deciding
  presentation is what keeps the two platforms' menus identical.
- **Strings are sanitised, not refused.** Unlike descriptor fields, a menu label
  has ANSI escapes, control characters, and bidi overrides *stripped*, whitespace
  collapsed, and length capped. A menu is live data on a hot path; a single bad
  character should degrade one row, not disable a running raven. The reason both
  policies exist: a bad descriptor is a broken raven, a bad label is a bad label.
- **Bounds are enforced while parsing, not after.** A hostile payload cannot make
  the host build a huge structure and only then discard it. Ten thousand items
  would hang the menu build inside the UI run loop, which reads to the user as a
  frozen desktop.
- **Leading, trailing, and doubled separators are dropped.**
- **Unknown fields are ignored.**
- **An unusable payload leaves you "up but silent."** If you answer but nothing
  survives parsing, the host draws your name with *"Nothing to report."* — which
  is visibly different from a raven it could not reach.

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

### Links

A `url` is opened in the browser as `http://127.0.0.1:{port}{url}`, built from
**your descriptor's own port**. A menu item cannot navigate the user anywhere
except the raven that offered it. Query strings are allowed (a link legitimately
carries parameters); fragments are not.

---

## 6. Token isolation

> **Each raven owns its own credential. The host never mints one, never shares
> one, and never caches one.**

Concretely:

- The host reads a token from the `token_path` in **your** descriptor and sends it
  only to **your** port, under the header **you** named.
- It reads **fresh on every request**. You rotate whenever you like — a new token
  per start is the expected pattern — with no coordination. A cached token would
  make the host authenticate with a dead credential and report you as broken.
- Request headers are built per call from a fixed allowlist. There is no path by
  which one raven's credential can appear in another raven's request.
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
single file under Appistry's own state directory (`~/.appistry/menubar.lock`,
mode 0600): whoever takes it hosts. `flock` on POSIX, an exclusive open on
Windows — both released by the OS if the holder dies, so **there is no stale-lock
case to reason about**, unlike a PID file.

A second tray process finds the lock held and exits quietly. A process that
cannot *create* the lock at all — a read-only state directory — reports that
distinctly, because the two need different handling: contention is the normal
outcome of a duplicate launch, while an unwritable path means this machine cannot
host at all and the user has to be told.

**Which raven leads the menu is a separate question, answered by data.** You
declare `host_priority`; the host sorts by it, descending, then by name. Huginn
declares a higher priority than Muninn and therefore leads when both are present;
when Huginn is absent, Muninn's section simply sorts first and the same menu runs
standalone.

Neither raven knows the other exists, and the host knows neither name. Hardcoding
one would be the same mistake as a hardcoded catalog id — which is exactly the
mistake this repository's previous design made, twice, once per platform.

Ravens do **not** participate in host election. You are not the host; you are
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
  missing cross-check must never turn a live raven into a dead one.

**Supply `started`.** Without it, a recycled PID can pass as a live raven and the
user sees a raven that is not running.

### Unavailability is a first-class result

> An unreachable, stopped, stale, or malformed raven renders as a **disabled
> section with a visible reason**.

Never a crash. Never a silent omission — a raven that vanished from the menu is
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

Failure is **not contagious**: one broken raven never prevents another's section
from rendering, and the host survives a raven that hangs, floods, or lies.

`appistry ravens` prints the same reasons in a terminal.

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
never declared). A hung raven degrades to a disabled section, never to a frozen
menu.

> **Why the host relays nothing.** An earlier design in this repository ran an
> unauthenticated proxy on a fixed loopback port that rewrote `Host` to a clean
> loopback value. Any web page could reach it, and what arrived upstream looked
> locally originated — laundering an attack straight past the app's own
> `require_local_origin` check. The fix was structural, not a filter: there is no
> upstream. Appistry's only listener is the Help page, which forwards nothing,
> refuses a foreign `Host` and **any** `Origin`, takes no request body, and routes
> only `GET`.
>
> Do not add a relay to this protocol. If a raven needs a fixed external URL, it
> owns that endpoint itself, with its own authentication.

---

## 10. A raven's checklist

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
6. On every request: check `Host`, reject any `Origin`, guard `Content-Length`,
   compare the token in constant time.
7. Rewrite the descriptor if your port or token path changes.

**Shutdown**

8. Remove your descriptor and token file, best-effort.

**Never**

- Compare `api_version` for equality.
- Assume the host will authenticate for you, or supply a credential.
- Assume the host understands what any of your ids mean.
- Assume you are the host, or that another raven is running.
- Return an unbounded response, or block a menu fetch indefinitely.

---

## Reference implementations

| File | Shows |
|---|---|
| [`examples/huginn_raven.py`](examples/huginn_raven.py) | Leading raven: higher priority, badge, token, per-item actions |
| [`examples/muninn_raven.py`](examples/muninn_raven.py) | Companion raven: lower priority, link-only rows, no token |

Both are stdlib-only and runnable:

```bash
python3 examples/huginn_raven.py
```

Start one or both and the tray will show them; `appistry ravens` will explain
anything it will not show.
