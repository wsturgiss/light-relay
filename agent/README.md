# Reaching Will's Light Phone

This is for the agent. `relayctl.py`, in this directory, sends short messages to Will's
Light Phone III and collects his answers. It needs `RELAY_URL` set, and it must run on a
tailnet device the relay allows. It holds no secrets.

```bash
export RELAY_URL=https://baconstation.tailda71f7.ts.net
```

## The phone

It's a small, mostly black-and-white screen, and Will uses it to be *less* reachable.
Every message interrupts him.

- Send one only for a decision you need, or for news he'd want to hear now.
  At most a few a day.
- **Headline:** up to 120 characters, plain text. It should make sense on its own.
- **Detail:** up to 1000 characters. In practice, two or three plain sentences. No
  markdown, no links worth tapping (the phone can't open them), no tables.
- **Choices:** when you need a decision, offer up to 4, each up to 24 characters. He
  can tap one, or type a reply instead.

## Commands

| What | Command |
|---|---|
| Start a conversation | `relayctl.py notify "<headline>" "<detail>" [--choice A --choice B]` |
| Continue one | `relayctl.py notify "<headline>" "<detail>" --thread <message id>` |
| Unhandled replies | `relayctl.py replies` |
| Mark replies handled | `relayctl.py ack <seq>` (everything up to and including `seq`) |

`notify` prints `{"id": "m_…", "thread": "m_…", "pushed": true|false}`. Keep the `id`.

## Conversations

Each message either starts a conversation or continues one. On the phone, a
conversation is one row in the inbox and one screen when he opens it.

- **Answering a reply? Continue its thread** with `--thread <the reply's messageId>`.
  Any message id in the conversation works; the relay finds the start.
- **Will can start a conversation too.** It arrives in `replies` with no `messageId`, a
  `thread` (`t_…`) and a `title` (his first line). Answer with `--thread <that thread>`.
  Keep using that `thread` for the rest of the conversation.
- **New subject? Leave `--thread` off.**
- Don't open a new conversation for a follow-up. It shows up on the phone as an
  unrelated message.

## Replies

`relayctl.py replies` returns a JSON list, oldest first:

```json
[{ "seq": 8, "id": "r_…", "messageId": "m_356f72d08c65d0ee", "thread": null, "title": null,
   "choice": "Nice", "text": null, "sentAt": "2026-10-05T04:41:02Z", "receivedAt": "2026-10-05T04:41:02Z" },
 { "seq": 9, "id": "r_…", "messageId": null, "thread": "t_8c1f0a2b3d4e5f60", "title": "Plan Saturday",
   "choice": null, "text": "Plan Saturday\nHike or the market?", "sentAt": "…", "receivedAt": "…" }]
```

- `messageId` is the message he was answering. Match it to the `id` you kept.
- No `messageId` but a `thread`: he started a conversation (see **Conversations**), or
  added to it before you answered. Once you've answered, his replies carry your
  message's id as `messageId`, like any other reply.
- He either tapped a `choice` or typed `text`, sometimes both.
- **Act first, then `ack`.** Anything not acknowledged comes back next time, so a crash
  between the two can't lose a reply. Don't `ack` past a reply you haven't handled.
- A reply can arrive minutes or hours later. Check on a schedule.

## Things not to do

- **Don't resend** because `pushed` is `false`. The message is stored either way, and
  the phone picks it up within 15 minutes or when he opens the app. A resend shows up
  twice.
- Don't send to check that it works, and don't send a reply that only acknowledges his,
  such as "Got it". Close the loop only if he'd want to know.
- Don't put secrets, passwords or codes in a message.

## Errors

| Answer | Meaning |
|---|---|
| `400` | The message broke a limit above. The error says which one. |
| `403` | This device isn't in the relay's `ALLOWED_PEERS`. Tell Will; don't retry. |
| `429` | Over the hourly limit (60 by default). Wait, and send less. |
| `503` | The relay isn't paired with the phone. Tell Will. |
| no connection | This machine isn't on the tailnet, or the relay is down. |
