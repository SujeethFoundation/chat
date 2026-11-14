# Sujeeth Foundation chat

The chat API behind the assistant on sujeeth.org and the Power Pages site. The web page cannot call the Foundry agent itself: the agent accepts only Microsoft Entra tokens, and tokens must never be put in a web page. This Azure Function takes the conversation from the page, calls the agent as the app's managed identity, and returns the reply.

## How it fits

    chat widget (sujeeth.org, Power Pages)
      -> this Function App (/api/chat)
        -> Foundry agent Sujeeth-Foundation-Web (project avisuj-4010)
          -> Sujeeth Foundation Graph (MCP server)
          -> Sujeeth Foundation Backend (Dataverse)

It does not call the ontology, websearch, or wikisearch apps directly. The agent does that through the MCP server.

## Routes

- `POST /api/chat`: `{"messages": [{"role", "content", "attachments"}], "summary": "..."}` returns `{"reply": "..."}`. When a conversation gets long, the reply also carries `{"fold": {"keep_last": N}}`.
- `POST /api/chat/stream`: the same request as `/api/chat`, answered as a stream of server-sent events, so the reply appears as it is written. See the top of `chat_stream.py` for the events (`status`, `delta`, `reset`, `done`, `error`). Problems found before the stream begins (bad origin, bad JSON, rate limit, no free slot) are ordinary JSON responses with the usual status codes.
- `POST /api/chat/fold`: summarizes older turns so a long conversation keeps working.
- `GET /api/chat/health`: returns whether chat is configured. With `?warm=1` it loads the model client in the background, so the first message is faster.

These routes are open to the web on purpose. They check the page's origin, limit requests, and cap how many chats run at once.

## Attachments

Visitors can attach PDF, Word, PowerPoint, Excel, text, Markdown, and CSV files. The route reads the text out of each file and adds it to the visitor's message before the agent sees it. Limits are in `chat_attachments.py`. Message and file text is never logged.

## Settings

Required: `FOUNDRY_AGENT_BASE_URL`.
Common: `CHAT_ALLOWED_ORIGINS`, `CHAT_MAX_MESSAGE_CHARS`, `CHAT_MAX_CONCURRENT`, `CHAT_RATE_PER_MINUTE`, `CHAT_RATE_PER_HOUR`, `CHAT_GLOBAL_PER_HOUR`, `CHAT_MAX_HISTORY_CHARS`, `CHAT_SUMMARIZE_AFTER_CHARS`, `CHAT_TIMEOUT_SECONDS`.
The full list, with defaults, is at the top of `chat_blueprint.py`. Chat is always on in this app, so `CHAT_ENABLED` is not used.

## Streaming

`/api/chat/stream` uses the FastAPI request and response types, which Azure Functions needs in order to stream. This takes the app setting `PYTHON_ENABLE_INIT_INDEXING=1` and the `azurefunctions-extensions-http-fastapi` package (in `requirements.txt`). The other routes keep the standard types, and `/api/chat` is unchanged, so a page that has not been updated keeps working.

To check that text really arrives in pieces (not all at the end), run this and watch the events appear one by one:

    curl -N -H "Origin: https://www.sujeeth.org" -H "Content-Type: text/plain" \
      -d '{"messages":[{"role":"user","content":"What is a Career Circle?"}]}' \
      https://<chat-app-name>.azurewebsites.net/api/chat/stream

The log line `chat <id> stream ok` reports `first_text` (seconds until the first piece of the answer) and `seconds` (the whole run). Compare the two to see what streaming saves.

## Hosting

- Azure Functions, Python, Flex Consumption.
- The app's managed identity needs a role on the Foundry project (avisuj-4010). Without it, chats fail with "agent refused the app's identity".
- A chat that waits for a free slot still counts as a request in flight, so a full instance makes the platform start another one instead of turning visitors away. Keep `CHAT_MAX_CONCURRENT` at or above the platform's per-instance HTTP concurrency. Flex Consumption sets that per app, for example `az functionapp scale config set --name <app> --resource-group <group> --trigger-type http --trigger-settings perInstanceConcurrency=8`. Check the current value before changing either setting.
- Set `CHAT_SEND_CORS_HEADERS=false` if the Function App's own CORS list already allows the page.
- Deploy from `main` with the workflow the portal's Deployment Center creates. The ontology app uses Python 3.14 and a remote build.

## Moving from the ontology app

1. Create this app and its settings, and give its managed identity the Foundry role.
2. Check `GET /api/chat/health?warm=1` and one real chat against it.
3. Point the widget (`js/foundry-chat-connection.js`, the `preconnect` link in `chat.html`, and the Power Pages embed) at the new address.
4. Keep the old route for a short while so cached pages still work, then set `CHAT_ENABLED=false` on the ontology app.

## Run and test locally

    pip install -r requirements.txt pytest
    python -m pytest -q

`tests/load_test.py` runs a load test against the chat route. It spends real model tokens, so start small.
