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
| `choices` | ≤ 4, each ≤ 24 characters |
| `ref` | Optional, `[A-Za-z0-9_.:-]{1,64}`. Passed through to the phone. |
| `thread` | Optional. A message id (`m_…`) or a phone-started conversation (`t_…`) to continue. |

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
`{"v":1,"id","at","headline","detail","choices"[,"ref"][,"thread"]}`. The tool drops
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
  "text", "sentAt", "receivedAt"}]}`. These are the tool's own replies, so it can rebuild
  the conversation after a reinstall. Reading them doesn't move the agent's cursor.

Messages and replies are kept for 90 days, up to 5000 rows each.

### `POST /replies`

Body ≤ 4 KB. Two forms:

```json
{ "id": "r_…", "messageId": "m_…", "choice": "Yes", "text": null, "sentAt": "…" }
{ "id": "r_…", "thread": "t_…", "title": "Plan Saturday", "text": "Plan Saturday\nHike or the market?", "sentAt": "…" }
```

- A reply needs a `choice` (≤ 64 characters) or `text` (≤ 1000 characters).
- The phone generates `id`. A resend with the same `id` is stored once, so the phone can
  retry freely.
- The second form starts a conversation from the phone. It has no `messageId`; `thread`
  (`t_` and 16 hex digits) is the new conversation's id, and `title` (≤ 120 characters)
  is its first line. The agent answers with `notify` and that `thread`.

## Agent ← relay: `GET /replies`, `POST /replies/ack`

`GET /replies[?limit=<n ≤ 200>][&after=<seq>]` returns replies after the acknowledged
cursor, oldest first:

```json
{ "replies": [{ "seq": 3, "id": "r_…", "messageId": "m_…", "thread": null, "title": null,
                "choice": "Yes", "text": null, "sentAt": "…", "receivedAt": "…" }], "cursor": 2 }
```

`POST /replies/ack {"upTo": 3}` moves the cursor and returns `{"cursor": 3}`. Acknowledge
only after the agent has acted on a reply, so a crash in between re-delivers it instead of
losing it.

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
