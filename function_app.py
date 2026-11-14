"""The sujeeth.org chat API as its own Function App. All the work is in chat_blueprint.py."""
import azure.functions as func

from chat_blueprint import bp

# The chat routes are open to the web on purpose and say so themselves (AuthLevel.ANONYMOUS); the page has no key to send.
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
app.register_functions(bp)
