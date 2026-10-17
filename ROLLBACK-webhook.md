# Rolling back the reply webhook (2026-10-16)

The previous image is tagged `light-relay:pre-webhook`. To go back:

    docker tag light-relay:pre-webhook light-relay:local
    STATE=$HOME/.local/share/light-relay
    mv $STATE/webhook.env $STATE/webhook.env.disabled   # stops the webhook even on the new image
    docker rm -f light-relay-inbox
    docker run -d --name light-relay-inbox --restart unless-stopped --network host --user "$(id -u):$(id -g)" \
      --env-file $STATE/inbox.env -e ROLE=inbox -e HOST=127.0.0.1 -e PORT=18081 \
      -v $STATE/inbox:/data -v $STATE/relay:/outbox:ro -v $STATE/session:/session:ro light-relay:local
    # (drop the /session mount if inbox.env has no SESSION_HOST)

Code: the change is on branch `reply-webhook`; `git switch choose-recipient` returns to the prior branch.
Just disabling it: move webhook.env away and re-run scripts/up.sh (only the inbox reads it).
The relay container (light-relay) is unchanged by this work.

# Rolling back named agents + Grok transcription (2026-10-16, 21:57 EDT)

Deployed from branch `grok-transcribe` (PRs #13 and #14). The image before it is tagged `light-relay:pre-agents`
(the reply webhook, gpt-4o-mini-transcribe). Backups of the settings it changed are in ~/.local/share/light-relay:

    S=$HOME/.local/share/light-relay
    cp -p $S/inbox.env.bak-before-agents-grok $S/inbox.env     # OpenAI key + AGENTS=Huginn,Claude; drops the Grok settings
    mv $S/agents.env $S/agents.env.disabled                    # otherwise up.sh loads it again
    cp -p $S/webhook.env.bak-before-agents $S/webhook.env      # unchanged by this deploy; here for completeness
    docker tag light-relay:pre-agents light-relay:local
    # then recreate both containers with the old image. Not up.sh from this branch: it rebuilds.
    cd /tmp && rm -rf lr-old && mkdir lr-old && git -C ~/Projects/light-relay archive reply-webhook | tar -x -C lr-old
    sed -i 's/^docker build .*/: # skip build/' lr-old/scripts/up.sh && lr-old/scripts/up.sh

To keep the named agents but go back to OpenAI only: restore inbox.env from the backup, then put the AGENTS line
back out of it (AGENTS lives in agents.env now), and re-run scripts/up.sh from this branch.
The outbox's new `agent` column is harmless to the old code. cursor.json gains `agent:<Name>` keys, which the old code ignores.
Bot tokens: on the box at ~/light-relay/agents/<Name>.token (chmod 600); the relay copy is in agents.env.

# Rolling back the signed sender name (2026-10-16, ~23:25 EDT)

Deployed from branch `message-sender` (one change: /notify from a named agent adds `"from"` to the signed
message). The image before it is tagged `light-relay:pre-sender` (named agents + Grok). Settings are unchanged.

    docker tag light-relay:pre-sender light-relay:local
    cd /tmp && rm -rf lr-old && mkdir lr-old && git -C ~/Projects/light-relay archive grok-transcribe | tar -x -C lr-old
    sed -i 's/^docker build .*/: # skip build/' lr-old/scripts/up.sh && lr-old/scripts/up.sh

Phones keep working either way: the app reads `from` when it's there and otherwise works out the agent itself.
