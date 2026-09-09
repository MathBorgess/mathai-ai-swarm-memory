"""Public, machine-readable A2A discovery contract.

The card deliberately contains no instructions telling a model to call a broker.
OAuth is advertised as metadata; the broker must validate GitHub identity and
mint its own short-lived session before proxying to Hermes.
"""

from typing import Any

OAUTH_SCOPES = {
    "a2a:discover": "Discover the agent and its capabilities",
    "a2a:message": "Send messages to the agent",
    "a2a:history": "Read conversation history",
}


def build_agent_card(public_url: str = "https://a2a.mathai.com.br") -> dict[str, Any]:
    return {
        "name": "Hermes",
        "description": "Math AI remote agent",
        "url": public_url,
        "version": "0.0.1",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": [{"id": "hermes-message", "name": "Hermes messaging", "description": "Exchange text messages with Hermes"}],
        "securitySchemes": {
            "githubOAuth": {
                "type": "oauth2",
                "flows": {
                    "authorizationCode": {
                        "authorizationUrl": "https://github.com/login/oauth/authorize",
                        "tokenUrl": "https://github.com/login/oauth/access_token",
                        "scopes": OAUTH_SCOPES,
                    }
                },
                "x-provider": "github",
                "x-token-validation": "https://api.github.com/user",
                "x-session": "broker-minted-short-lived",
            }
        },
        "security": [{"githubOAuth": list(OAUTH_SCOPES)}],
    }
