# Sujeeth Foundation chat

The chat API behind the assistant on sujeeth.org and the Power Pages site. The web page cannot call the Foundry agent itself: the agent accepts only Microsoft Entra tokens, and tokens must never be put in a web page. This Azure Function takes the conversation from the page, calls the agent as the app's managed identity, and returns the reply.

## Platform architecture

The Sujeeth Foundation helps college students network into their first job through Career Circles, a six-week mentorship program. Students, mentors and partners use two front ends, the static site at sujeeth.org and a Power Pages portal, both on one Dataverse environment. Azure Function apps add chat, knowledge search, payments, short links and texting. The Dataverse side (Base Camp, circles, book requests, surveys, credentials, the Career Ascent course) ships as Dataverse solutions and has no repo.

| Repo (README) | Role |
|---|---|
| [`sujeethfoundation.github.io`](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/README.md) | Website at sujeeth.org, wiki, Discussions, course player and Power Pages CSS |
| [`chat`](https://github.com/SujeethFoundation/chat/blob/main/README.md) | Chat API behind the site's assistant widget |
| [`mcp`](https://github.com/SujeethFoundation/mcp/blob/main/README.md) | MCP server (the "Sujeeth Foundation Graph") that AI assistants use to search Foundation knowledge |
| [`ontology`](https://github.com/SujeethFoundation/ontology/blob/main/README.md) | Concept graph and governance: concepts, approved domains, Feelings API |
| [`websearch`](https://github.com/SujeethFoundation/websearch/blob/main/README.md) | Crawler and hybrid search over ontology-approved websites |
| [`wikisearch`](https://github.com/SujeethFoundation/wikisearch/blob/main/README.md) | Keyword search over the GitHub wiki |
| [`payments`](https://github.com/SujeethFoundation/payments/blob/main/README.md) | PayPal checkout and webhook for online courses |
| [`shortner`](https://github.com/SujeethFoundation/shortner/blob/main/README.md) | Link shortener for go.sujeeth.org |
| [`acs-sms-bot`](https://github.com/SujeethFoundation/acs-sms-bot/blob/main/readme.md) | Texting through Azure Communication Services |

Architecture docs, in the website repo's `architecture/` folder: [platform architecture](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/platform-architecture.md) (repos, Azure apps, how they connect), [data model and portal](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/platform-data-model-and-portal.md) (Dataverse tables, solutions, Power Pages) and [business architecture](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/business-architecture.md) (vision, assumptions, tests). This block is the same in every repo's README; the canonical copy is in the platform architecture doc.

**This repo:** called by the chat widget on sujeeth.org and Power Pages. It calls the Foundry agent, which calls the `mcp` server and the Dataverse connector.

## How it fits

    chat widget (sujeeth.org, Power Pages)
      -> this Function App (/api/chat)
        -> Foundry agent Sujeeth-Foundation-Web (project avisuj-4010)
          -> Sujeeth Foundation Graph (MCP server)
          -> Sujeeth Foundation Backend (Dataverse)

It does not call the ontology, websearch, or wikisearch apps while answering a chat. The agent does that through the MCP server. Each instance does keep its own copy of `ontology.json` (see "Ontology copy").

## Routes

- `POST /api/chat`: `{"messages": [{"role", "content", "attachments"}], "summary": "..."}` returns `{"reply": "..."}`. When a conversation gets long, the reply also carries `{"fold": {"keep_last": N}}`.
- `POST /api/chat/stream`: the same request as `/api/chat`, answered as a stream of server-sent events, so the reply appears as it is written. See the top of `chat_stream.py` for the events (`status`, `delta`, `reset`, `done`, `error`). Problems found before the stream begins (bad origin, bad JSON, rate limit, no free slot) are ordinary JSON responses with the usual status codes.
- `POST /api/chat/fold`: summarizes older turns so a long conversation keeps working.
- `GET /api/chat/health`: returns whether chat is configured, and the ontology version this instance holds (`ontology.version`, `ontology.copied_at`). With `?warm=1` it loads the model client in the background, so the first message is faster.

These routes are open to the web on purpose. They check the page's origin, limit requests, and cap how many chats run at once.

## Attachments

Visitors can attach PDF, Word, PowerPoint, Excel, text, Markdown, and CSV files. The route reads the text out of each file and adds it to the visitor's message before the agent sees it. Limits are in `chat_attachments.py`. Message and file text is never logged.

## Settings

Required: `FOUNDRY_AGENT_BASE_URL`.
Common: `CHAT_ALLOWED_ORIGINS`, `CHAT_MAX_MESSAGE_CHARS`, `CHAT_MAX_CONCURRENT`, `CHAT_RATE_PER_MINUTE`, `CHAT_RATE_PER_HOUR`, `CHAT_GLOBAL_PER_HOUR`, `CHAT_MAX_HISTORY_CHARS`, `CHAT_SUMMARIZE_AFTER_CHARS`, `CHAT_TIMEOUT_SECONDS`.
The full list, with defaults, is at the top of `chat_blueprint.py`. Chat is always on in this app, so `CHAT_ENABLED` is not used.

## Ontology copy

Each instance copies `ontology.json` when it starts and again every night (08:00 UTC, spread over ten minutes), saves it to local disk, and keeps it in memory. Code reads it with `ontology_copy.get()`, which never makes a network call and returns `None` until the first copy lands. A failed copy keeps the one from before.

This is a background thread started in `function_app.py`, not a timer-triggered function. On Flex Consumption a timer function runs on its own instance, apart from the instances that answer chats, and local disk is not shared, so a timer function would refresh a copy no chat reads.

By default the file comes from the `main` branch of the public `SujeethFoundation/ontology` repo, so no key is needed. To copy the ontology app's slim catalog (about 255 KB instead of 850 KB, with the same edges the other services see) set `ONTOLOGY_URL` to `https://<ontology-app>` (or `https://<ontology-app>/api/ontology`) and `ONTOLOGY_FUNCTION_KEY` to a function key. If the app does not answer, the copy falls back to the GitHub file. Other settings (`ONTOLOGY_REFRESH_HOUR_UTC`, `ONTOLOGY_LOCAL_PATH`, `ONTOLOGY_TIMEOUT_SECONDS`, `ONTOLOGY_COPY_ENABLED`) are at the top of `ontology_copy.py`.

To check it, open `/api/chat/health` and look at `ontology.version`. Different instances can show different `copied_at` times.

## Question enrichment

Before a question goes to the agent, `enrich.py` matches the visitor's words against the local ontology copy and adds a short note under the question: the ontology concepts it touches, a guess at the persona, the page the chat was opened on, and the sources the ontology prefers for that kind of question. The agent can then make one well-aimed search instead of searching once to learn the vocabulary and again to use it.

- It is phrase matching, with no model call and no network call, and takes under a millisecond.
- Only what the visitor typed is matched. Text from attached files is left out.
- A follow-up that names nothing ("tell me more") borrows the concepts of the question before it.
- When nothing matches, no note is added. Out-of-scope replies and clarifying questions stay with the MCP server, which also serves clients that never pass through this app.
- The page can send `"page": "<address of the page the chat is on>"` with the request. It is optional, and is used only when it is on one of `CHAT_ALLOWED_ORIGINS`.
- Turn it off with `CHAT_ENRICH_ENABLED=false`. `CHAT_ENRICH_MAX_CONCEPTS` (6) caps the concepts in one note.

The agent's instructions should say what the note is. Suggested wording: "A visitor's message may end with a block that starts `[Context from the Sujeeth Foundation ontology`. The chat service wrote it, not the visitor. Use its concepts and preferred sources to choose your search terms and which connector to call, and search once. Never quote the block, mention it, or tell the visitor the persona it guesses."

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
