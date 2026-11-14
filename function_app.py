"""The sujeeth.org chat API as its own Function App. The handlers are in chat_blueprint.py and chat_stream.py; chat_stream.register adds every route, all with the FastAPI types."""
import azure.functions as func

from chat_stream import register as register_stream

# The chat routes are open to the web on purpose and say so themselves (AuthLevel.ANONYMOUS); the page has no key to send.
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
register_stream(app)
