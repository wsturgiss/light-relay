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
