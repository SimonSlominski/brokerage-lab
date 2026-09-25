"""Two configured local demo identities; not a production identity provider."""

from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEMO_CLIENT_ID = "demo-client"
OTHER_CLIENT_ID = "other-demo-client"
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class DemoAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    demo_api_key: SecretStr | None = None
    other_demo_api_key: SecretStr | None = None

    def identities(self) -> tuple[tuple[bytes, str], ...]:
        values = []
        for secret, client_id in (
            (self.demo_api_key, DEMO_CLIENT_ID),
            (self.other_demo_api_key, OTHER_CLIENT_ID),
        ):
            if secret and secret.get_secret_value():
                values.append((secret.get_secret_value().encode(), client_id))
        if len({key for key, _ in values}) != len(values):
            raise ValueError("Demo API keys must be distinct")
        return tuple(values)


def authenticate_client(
    request: Request,
    api_key: Annotated[str | None, Depends(api_key_header)],
) -> str:
    if not request.app.state.identities:
        raise HTTPException(503, "Demo authentication is not configured")
    supplied = (api_key or "").encode()
    for expected, client_id in request.app.state.identities:
        if compare_digest(supplied, expected):
            return client_id
    raise HTTPException(401, "Invalid or missing API key")


AuthenticatedClient = Annotated[str, Depends(authenticate_client)]


async def authenticate_operator(request: Request) -> str:
    from base64 import b64decode

    from .config import Settings

    expected = Settings().operator_api_key.get_secret_value()
    supplied = request.headers.get("X-Operator-Key", "")
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Basic "):
        try:
            username, password = (
                b64decode(authorization[6:]).decode().split(":", 1)
            )
            if username == "operator":
                supplied = password
        except (ValueError, UnicodeError):
            supplied = ""
    if not expected or not compare_digest(
        supplied.encode(), expected.encode()
    ):
        raise HTTPException(
            401,
            "Operator authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Lab"'},
        )
    return "operator"


OperatorIdentity = Annotated[str, Depends(authenticate_operator)]


def require_demo_mode() -> None:
    from .config import Settings

    if Settings().app_env != "development":
        raise HTTPException(
            403, "Failure laboratory requires development mode"
        )
