"""Bearer authentication and bounded audit context for the versioned API."""

import logging
from secrets import compare_digest
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False, scheme_name="BrokerageBearer")
logger = logging.getLogger(__name__)


def authenticate_bearer(
    request: Request,
    credential: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer)
    ],
) -> str:
    if not request.app.state.identities:
        raise HTTPException(503, "API authentication is not configured")
    supplied = (credential.credentials if credential else "").encode()
    for expected, client_id in request.app.state.identities:
        if compare_digest(supplied, expected):
            return client_id
    raise HTTPException(
        401,
        "Invalid or missing API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate(
    request: Request,
    client_id: Annotated[str, Depends(authenticate_bearer)],
    principal: Annotated[
        str,
        Header(
            alias="LMG-Data-Privacy-Access-Principal",
            min_length=1,
            max_length=255,
            pattern=r"^[ -~]+$",
        ),
    ],
    justification: Annotated[
        str,
        Header(
            alias="LMG-Data-Privacy-Access-Justification",
            min_length=1,
            max_length=255,
            pattern=r"^[ -~]+$",
        ),
    ],
) -> str:
    # Audit claims cannot change the identity or account ownership.
    logger.info(
        "api_access authenticated_client_id=%s claimed_principal=%s "
        "claimed_justification=%s "
        "method=%s path=%s",
        client_id,
        principal,
        justification,
        request.method,
        request.url.path,
    )
    return client_id


AuthenticatedV1Client = Annotated[str, Depends(authenticate)]
