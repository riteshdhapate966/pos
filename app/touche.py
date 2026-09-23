"""Thin client for the Touché POS REST API.

Every call is a POST whose JSON goes in a `requeststring` form field (the query-string
form hits IIS's URL length limit and comes back as a 404). The server splits methods
across two prefixes, so callers pass e.g. "ToucheLite/SaveOrder" or "TOUCHE/GetCheckDetails".
"""
import json
import os
import time

import httpx
from fastapi import HTTPException

# Server from data/config.txt; credentials still the staging ones from data/postman_collection.json.
BASE_URL = os.environ.get("TOUCHE_BASE_URL", "https://pms-api.rufescent.com/ToucheAPI/")
AUTH = {
    "PropertyId": os.environ.get("TOUCHE_PROPERTY_ID", "PFS"),
    "AppKey": os.environ.get("TOUCHE_APP_KEY", "cnkjhftRngh23i4"),
    "DeviceId": os.environ.get("TOUCHE_DEVICE_ID", "STAGING"),
}
USER_ID = os.environ.get("TOUCHE_USER_ID", "QLUB")
TIMEOUT = float(os.environ.get("TOUCHE_TIMEOUT", "30"))


async def call(method: str, payload: dict) -> dict:
    body = {**AUTH, "Timestamp": str(int(time.time() * 1000)), **payload}
    try:
        async with httpx.AsyncClient(base_url=BASE_URL.rstrip("/") + "/", timeout=TIMEOUT) as client:
            r = await client.post(method, data={"requeststring": json.dumps(body)})
        r.raise_for_status()
        data = r.json()
    except httpx.TimeoutException:
        raise HTTPException(504, f"Touché {method} timed out")
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"Touché {method} failed: {e}")

    # Success is ResponseCode 0, but some methods send code 0 with flag "FAIL".
    status = data.get("ResponseStatus") or {}
    if str(status.get("ResponseCode")) != "0" or str(status.get("ResponseFlag")).upper() == "FAIL":
        msg = status.get("ResponseMessage") or status.get("ResponseFlag") or "unknown error"
        raise HTTPException(502, f"Touché {method}: {msg} (code {status.get('ResponseCode')})")
    return data
