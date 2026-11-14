"""The sujeeth.org chat API as its own Function App. The handlers are in chat_blueprint.py and chat_stream.py; chat_stream.register adds every route, all with the FastAPI types."""
import threading

import azure.functions as func

import chat_blueprint as cb
import ontology_copy

from chat_stream import register as register_stream

# The chat routes are open to the web on purpose and say so themselves (AuthLevel.ANONYMOUS); the page has no key to send.
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
register_stream(app)


# Warm the model client when the instance starts, so it is ready before the first visitor opens the chat page.
# The page's ?warm=1 call still covers an instance that started without this (a new one the platform added).
if cb.BASE_URL:
    threading.Thread(target=cb.warm, name="chat-warm-start", daemon=True).start()

# Copy ontology.json onto this instance now, and again every night (see ontology_copy.py for why this is not a timer function).
ontology_copy.start()
