# Protocol

The HTTP interface between the agent, the relay, the inbox and the phone. Most agents
don't need this: [`agent/relayctl.py`](../agent/relayctl.py) wraps the agent's side.

## Agent → relay: `POST /notify`

Plain text, with the headline on the first line and the detail after it:

```bash
curl -X POST https://<relay host>.<tailnet>.ts.net/notify -H 'Content-Type: text/plain' \
  --data-binary $'Relay test\nThis is a test message.'
```

Or JSON, which can also carry choices, a reference and a thread:

```json
{ "headline": "Book the 9:40?", "detail": "Holds expire at noon.", "choices": ["Yes", "No"], "ref": "trip-42" }
```

| Field | Limit |
|---|---|
| `headline` | Required, ≤ 120 characters |
| `detail` | ≤ 1000 characters |
| `choices` | ≤ 4, each ≤ 24 characters: labels, or objects `{"id", "label", "style"}` (see **Questions**) |
| `select` | Optional. `"many"`: the user picks any of the choices, then Done. Default: one tap answers. |
| `other` | Optional. `true` also offers "Other…", a typed answer. |
| `ref` | Optional, `[A-Za-z0-9_.:-]{1,64}`. Passed through to the phone. |
| `thread` | Optional. A message id (`m_…`) or a phone-started conversation (`t_…`) to continue. |
| `image` | Optional. A PNG or JPEG, base64 or a `data:image/…;base64,` URL. See **Pictures** below. |

**Questions.** A choice can be an object: `id` (`[A-Za-z0-9_-]{1,24}`, default `c1`, `c2`, …
by position), `label` (≤ 24 characters) and `style` (`"default"` or `"danger"`; the phone
shows a danger choice inverted and asks "Are you sure?" before sending it). Ids and labels
are distinct. The signed message keeps `choices` as the labels, so a tool from before this
still shows them, and adds `options: [{"id", "label", "style"}]` when an id or style differs
from the default, plus `"select": "many"` and `"other": true` when set. `select` "many" needs
2 or more choices; `select` and `other` need choices.

```json
{ "headline": "Clean up the old backups?", "choices": [{"id": "keep", "label": "Keep"},
  {"id": "del", "label": "Delete all", "style": "danger"}], "other": true }
```

Once answered, the phone locks the choices and puts a check on what was picked.

**Pictures.** A message can carry one picture for the phone to show under its text. In
JSON it's `image`, base64. Or send `multipart/form-data`, as `curl -F` does: text fields
`headline`, `detail`, `ref`, `thread`, `choice` (repeat it, up to 4; or `choices`, a JSON
list that may hold objects), `select`, `other` (`true`), and the file as
`image`:

```bash
curl https://<relay host>.<tailnet>.ts.net/notify -H "Authorization: Bearer $TOKEN" \
  -F headline='Shortlist' -F detail='Six duplexes, cheapest first.' -F image=@shortlist.png
```

The relay checks the bytes really are a PNG or JPEG (`415` otherwise, `400` if it can't be
read), takes up to `IMAGE_MAX_BYTES` (15 MB, `413` past it) and `IMAGE_MAX_PIXELS`, and
makes it fit the phone: turned upright (EXIF), transparency flattened onto white, scaled
down to at most 1080 × 3600 px, and saved again without metadata. A PNG stays PNG if that's
under `IMAGE_TARGET_BYTES` (400 KB), so tables and screenshots stay crisp; anything else
becomes a JPEG at the best quality under it. A picture that already fits and is small goes
through untouched. The `202` gains
`"image": {"id": "i_…", "type", "bytes", "width", "height", "sha256", "path": "/images/i_…"}`.

The file is kept in `images/` next to `outbox.db` (its row in the outbox's `images` table),
and goes when its message does. The signed message carries
`"image": {"id", "type", "bytes", "width", "height", "sha256"}`; the push stays small, and the
phone downloads the file from the inbox (`GET /images/<id>`) and checks its SHA-256 against
the signed message before showing it. Older tools ignore the field and show the text.

| Status | Meaning |
|---|---|
| `202` | Stored: `{"id": "m_…", "at": "…", "thread": "m_…", "pushed": true}` |
| `400` | The message broke a limit. The error says which. |
| `403` | The caller isn't in `ALLOWED_PEERS`. |
| `429` | Over `NOTIFY_RATE_PER_HOUR`. |
| `503` | Not paired: `PUSH_KEY` isn't set. |

**Threads.** A message without `thread` starts a conversation, and its own id is the
thread. The relay resolves any `m_…` id you name to the conversation's first message,
so naming the message a reply was about is always correct.

**`pushed`** is `false` when there is no `PUSH_ENDPOINT` or the push server refused the
message (the relay logs why). The message is stored either way and the phone fetches it,
so don't resend.

## Relay → phone: the push

One POST to the tool's UnifiedPush endpoint. The body is
`v1.<hex HMAC-SHA256(PUSH_KEY, json)>.<json>`, where the JSON is
`{"v":1,"id","at","headline","detail","choices"[,"ref"][,"thread"][,"from"][,"image"]}`, where `from` names the agent that sent it (see docs/agents.md), so the tool can keep one channel per agent. The tool drops
anything that doesn't verify, so knowing the endpoint isn't enough to put text on the
phone. The request carries `TTL` and `Urgency` headers. If Light's push server needs
more (VAPID, for example), change `push()` in `lightrelay/relay.py`.

## Phone ↔ inbox

Every inbox route takes `Authorization: Bearer <REPLY_TOKEN>`. Without a valid token the
inbox answers `401` before it reads the body.

### `GET /messages`, `GET /replies`

Both take `?after=<seq>&limit=<n ≤ 100>` and return rows oldest first. The tool tracks its
own position, and reads change nothing on the server.

- `GET /messages` → `{"messages": [{"seq": 4, "body": "v1.<mac>.<json>"}]}`. `body` is
  byte-for-byte the push body, so the tool verifies it the same way.
- `GET /replies` → `{"replies": [{"seq", "id", "messageId", "thread", "title", "choice",
  "text", "sentAt", "receivedAt", "choiceIds", "other", "answeredAt", "secondAnswer"}]}`. These are the tool's own replies, so it can rebuild
  the conversation after a reinstall. Reading them doesn't move the agent's cursor.

Messages and replies are kept for 90 days, up to 5000 rows each.

### `GET /images/<id>`

A picture an agent sent with a message (`image.id` in the signed message), as
`image/png` or `image/jpeg`, or `404` once its message has gone. The tool checks the
bytes against the message's `image.sha256`.

### `GET /agents`

`{"agents": ["Huginn", "Claude"]}`: the inbox's `AGENTS` setting, the default first. The
tool offers these under **To** when you start a conversation; with fewer than two it
doesn't ask. Empty when `AGENTS` isn't set.

### `GET /transcripts`

Voice notes' text, when the inbox transcribes them (`TRANSCRIBE_API_KEY`). Same paging:
`{"transcripts": [{"seq": 1, "id": "a_…", "text": "…"}]}`, where `id` is the voice note's
attachment. A note the service couldn't do has `"text": ""` and an `"error"` for the phone
to show, such as `"No transcription credit left"`; when the inbox restarts it tries those
again, and a transcript that follows comes with a new `seq`. A transcript usually lands a few seconds after the reply, so the tool follows this
feed rather than waiting for it on the reply. `GET /replies` also carries it, as `transcript`
on the attachment, once there is one. Kept 90 days, past the audio's 30.

### `POST /replies`

Body ≤ 4 KB. Two forms:

```json
{ "id": "r_…", "messageId": "m_…", "choice": "Yes", "text": null, "sentAt": "…" }
{ "id": "r_…", "thread": "t_…", "title": "Plan Saturday", "text": "Plan Saturday\nHike or the market?", "sentAt": "…" }
```

- A reply needs a `choice` (≤ 120 characters) or `text` (≤ 1000 characters).
- Answering a question: `"choiceIds": ["del"]` (1-4 distinct, all from the message; more than
  one only for `select` "many"), and the inbox sets `choice` to their labels in the message's
  order, joined with ", ". Or `"other": true` with `text`, if the message offers Other.
  The inbox checks them against the message when its outbox still has it. A `choice` without
  ids (a tool from before this) answers too.
- The first answer to a message sets its `answeredAt`. A later one is stored but flagged
  `"secondAnswer": true`, with the first's `answeredAt`, and the response says so:
  `{"ok": true, "answeredAt": "…", "secondAnswer": true}`. A resend of the first stays the first.
  Text without `other` doesn't answer.
- The phone generates `id`. A resend with the same `id` is stored once, so the phone can
  retry freely.
- The second form starts a conversation from the phone. It has no `messageId`; `thread`
  (`t_` and 16 hex digits) is the new conversation's id, and `title` (≤ 120 characters)
  is its first line. The agent answers with `notify` and that `thread`.
- The second form can carry `"to": "Huginn"`: which agent the conversation is for, picked
  on the phone from `GET /agents`. It's on every reply in the conversation until an agent
  answers; after that, `messageId` says whose it is. A name in `AGENTS` comes back spelled
  as configured. Without `to`, the conversation is unaddressed.
- Either form can carry `"attachments": ["a_…"]`: up to 4 voice notes or photos,
  uploaded first. A reply with attachments needs no `choice` or `text`. Naming one that
  hasn't been uploaded is a `400`, so upload it and send the reply again.

### `PUT /attachments/<id>`

A voice note or a photo, as the raw body. The phone generates `id` (`a_` and 16 hex
digits); a resend of the same bytes is stored once, and the same `id` with different
bytes is a `409`.

| `Content-Type` | Must start with |
|---|---|
| `audio/ogg` (Opus) | `OggS` |
| `audio/webm` | the EBML header |
| `audio/mp4` (AAC) | `ftyp` at byte 4 |
| `image/jpeg`, `image/png`, `image/webp` | their usual signatures |

| Status | Meaning |
|---|---|
| `201` | Stored |
| `200` | Already stored (a retry) |
| `400` | The bytes aren't the declared type, or the id is malformed |
| `409` | The id already holds a different file |
| `413` | Over `ATTACHMENT_MAX_BYTES` (10 MB). Scale photos down on the phone. |
| `415` | Not one of the types above |

Attachments are kept for 30 days, and the oldest go first past 500 MB in all. The reply
outlives them: once one has gone, its entry stays in the reply and a download is a `404`.
There's no route to read an attachment back through the inbox.

### Sessions: `POST /session`, on the LAN

With `SESSION_HOST` set, the inbox routes above take a **session token** as the bearer,
and the reply token itself is refused (unless `ACCEPT_REPLY_TOKEN=1`). The phone gets a
session from a second, LAN-only door: `https://<SESSION_HOST>:18443/session`, with a
self-signed certificate the tool pins.

```json
{ "at": 1791900000, "nonce": "<32-64 hex digits>", "proof": "<hex HMAC-SHA256(REPLY_TOKEN, \"renew:<at>:<nonce>\")>" }
```

→ `200 {"session": "s1.<expiry>.<mac>", "expiresAt": "…"}`. A session token is
`s1.<expiry in unix seconds>.<hex HMAC-SHA256(REPLY_TOKEN, "session:<expiry>")>`. New keys
end every session. Each renewal retires every session issued more than 10 minutes before
it: the inbox keeps only the newest session's expiry (`session-newest.json`), so a pairing
is for one phone.

| Status | Meaning |
|---|---|
| `400` | `at` is more than 5 minutes from the inbox's clock, or the body is malformed |
| `401` | The proof doesn't verify, or the nonce was already used |
| `429` | Over `SESSION_RATE_PER_MIN` (10) |

## Agent ← relay: `GET /replies`, `POST /replies/ack`

`GET /replies[?limit=<n ≤ 200>][&after=<seq>]` returns replies after the caller's
acknowledged cursor, oldest first:

```json
{ "replies": [{ "seq": 3, "id": "r_…", "messageId": "m_…", "thread": null, "title": null,
                "choice": "Yes", "text": null, "sentAt": "…", "receivedAt": "…" }], "cursor": 2 }
```

Each reply also carries `"attachments": [{"id": "a_…", "type": "audio/ogg", "bytes": 5004}]`,
empty when there are none. A voice note the inbox has transcribed also has `"transcript"`, or `"transcriptError"` when it couldn't.

`GET /images/<id>` returns a picture the agent sent, as the phone gets it (the
`path` on `/notify`'s answer and on `/channel` entries), with the same peer and token rules.

`GET /attachments/<id>` returns the file, with its type as `Content-Type`, or `404` once
it has expired. With `TRANSCRIBE_API_KEY` set on the inbox, a voice note's `transcript` is on
its attachment; without one, transcribing it is the agent's job.

`POST /replies/ack {"upTo": 3}` moves the cursor and returns `{"cursor": 3}`. Acknowledge
only after the agent has acted on a reply, so a crash in between re-delivers it instead of
losing it.

**Each agent has its own cursor**, by tailnet IP. Every agent sees every reply, including
replies to other agents' messages, and acknowledging moves only the caller's cursor. So an
agent acknowledges up to whatever it has handled or ignored, and can never clear a reply for
another agent. (Before this, one cursor was shared; each agent starts from where it was.)

## Agent ← relay: `GET /channel`

`GET /channel[?limit=<n ≤ 1000, default 100>]` returns the caller's channel, as the phone
shows it: the agent's own messages (`direction: "out"`) and the replies that are for it
(`"in"`, the same rule as `/replies`), merged and oldest first. With a limit, it returns the
newest n, still oldest first. It needs a named agent (a token, or an `AGENT_<NAME>_IP`).
Otherwise it answers `403`. It is read-only: no cursor moves.

```json
{ "agent": "Jarvis", "total": 12, "thread": "t_…",
  "entries": [
    { "direction": "out", "kind": "message", "seq": 57, "at": "2026-10-17T03:07:23Z", "id": "m_…",
      "thread": "m_…", "headline": "…", "detail": "…", "choices": [],
      "image": { "id": "i_…", "type": "image/png", "bytes": 263076, "width": 1080, "height": 639,
                 "sha256": "…", "path": "/images/i_…" } },
    { "direction": "in", "kind": "reply", "seq": 75, "at": "2026-10-17T03:08:02Z", "id": "r_…",
      "messageId": "m_…", "thread": "m_…", "to": null, "title": null, "choice": null, "text": "…",
      "attachments": [{ "id": "a_…", "type": "audio/mp4", "bytes": 23324, "transcript": "…",
                        "path": "/attachments/a_…" }],
      "sentAt": "…", "receivedAt": "…" } ] }
```

`seq` is the outbox's for `out` entries and the inbox's for `in` ones (the same `seq` as
`/replies` and `/replies/ack`). `at` is UTC, to the second: when the relay stored the message or received
the reply. Within one second, messages come before replies. `thread` is each entry's
conversation (its first message, or the phone's `t_…`). The top-level `thread` is the newest
entry's, so `POST /notify {"headline": …, "thread": <that>}` continues where the channel left
off.

`GET /healthz` → `{"ok": true, "paired": <PUSH_KEY set>, "push": <PUSH_ENDPOINT set>}`. It
is the only relay route open to every tailnet device.

## Design notes

The relay began as a design doc. It departs from that doc in four ways:

- **The phone generates the push key.** The phone has to hold it to verify signatures,
  and typing 43 characters into a Light Phone is harder than copying them off one. The
  key never reaches the agent.
- **The agent reads replies from the relay, not the inbox.** The relay reads the inbox's
  database through a read-only mount.
- **The inbox isn't write-only.** The tool fetches messages and its reply history through
  it. That makes push optional (LightOS may not give a sideloaded tool an endpoint) and
  lets the tool rebuild its history.
- **Devices are authenticated by tailnet IP.** The relay takes the IP from the header
  Tailscale Serve adds.
