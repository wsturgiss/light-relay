# Named agents

Several agents can share the phone. Each one:

- has one conversation on the phone (`GET /agents`, from `AGENTS`),
- reads only the replies that are for it (`GET /replies`) and acknowledges them for itself (`POST /replies/ack`),
- is woken by its own webhook when a reply for it arrives.

## Who a reply is for

1. The agent it names: `to`, the conversation it was written in. The phone sends it on every reply, and it wins over anything else the reply carries.
2. From a phone that predates `to` on every reply: the agent that sent the message it answers (`messageId`). The relay records the sender of every `/notify`.
3. Otherwise the default agent, which is the first in `AGENTS`. Replies to messages sent before senders were recorded, or by an agent without a name, belong to the default agent.

There are no threads. A `thread` on `/notify`, or a `thread` or `title` on a reply, from a caller written before this, is accepted and ignored.

## How an agent is known to the relay

Every caller still has to come from a tailnet IP in `ALLOWED_PEERS`. Within that:

- `Authorization: Bearer <AGENT_<NAME>_TOKEN>` names the agent. Several agents can share one tailnet IP (agents on one machine), so the token is what tells them apart. An unknown token gets 401.
- With no token, a caller whose IP is `AGENT_<NAME>_IP` is that agent. Its acknowledged position stays keyed by that IP in `cursor.json`, so an agent that was keyed by IP keeps its place.
- Any other caller is unnamed and sees every reply, as before.

An agent named by token alone keeps its position under `agent:<Name>` in `cursor.json`. On its first read it starts from the shared position.

`/notify` from a named agent records it as the sender. `/replies` from a named agent returns only its own replies, plus `"agent": "<Name>"`.

## Settings

`<NAME>` is the agent's name in capitals, with anything other than letters and digits turned into `_`. For example, `Marcus Aurelius` becomes `AGENT_MARCUS_AURELIUS_*`.

`<state dir>/agents.env` (chmod 600; read by both containers):

    AGENTS=Huginn,Jarvis,Othala,Luna,Marcus Aurelius,Real Estate,Shopping
    AGENT_HUGINN_IP=100.83.6.74
    AGENT_JARVIS_TOKEN=<32+ random chars>
    ...

`<state dir>/webhook.env` (chmod 600; read by the inbox only):

    AGENT_JARVIS_WEBHOOK_URL=https://...
    AGENT_JARVIS_WEBHOOK_KEY=...

When a reply for an agent arrives, the inbox POSTs `{"newest_seq": <its seq>, "to": "<Name>"}` with `Authorization: Bearer <key>` to that agent's webhook, and to no other. Failed POSTs are retried with backoff. While the agent's acknowledged position is behind, the POST is repeated at most every `WEBHOOK_REPING_SECONDS` (900).

The older single `WEBHOOK_URL` / `WEBHOOK_KEY` / `WEBHOOK_AGENT` acts as the default agent's webhook, unless that agent has its own. With no `AGENTS` it fires for every reply.

Generate a token with `head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'`.
