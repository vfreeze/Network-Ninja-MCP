"""Staff App JSON API client. Recaps are drafts only: the final recap-submit endpoint is deliberately absent."""
import base64
import json
import re
import time

import httpx2 as httpx  # the httpx build that ships with mcp 2.x
import keyring
from playwright.sync_api import sync_playwright

from .store import Store

BASE = "https://staff.betprogram.networkninja.com/api/v1/"
Q = ("shifts_days_back=90&shifts_days_forward=null&shifts_exclude_unactionable=false"
     "&claims_days_back=90&claims_days_forward=null&claims_include_all_declined=true")
SVC = "recap-mcp"
RETRY_ERRORS = re.compile(r"IncompleteUpload|EmptyUpload|UploadStorageFailed")
DEF_TTL = 12 * 3600


class SiteError(RuntimeError):
    """Message is read aloud to the user: status + site text, never secrets."""


def _claims(token: str) -> dict:
    seg = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


def login() -> str:
    """Headless SAML login (spec 4.1). Needs the live site; not unit-tested."""
    user, pw = keyring.get_password(SVC, "username"), keyring.get_password(SVC, "password")
    if not user or not pw:
        raise SiteError("No saved Staff App credentials; run the credential setup first.")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                with page.expect_response(lambda r: r.url.endswith("/auth/login-saml"), timeout=90000) as got:
                    page.goto("https://staff.betprogram.networkninja.com/")
                    page.fill("#username", user)
                    page.click("text=Log In")
                    page.wait_for_selector("input[type=password]")
                    page.fill("input[type=password]", pw)
                    page.keyboard.press("Enter")
                token = got.value.json().get("token")
            finally:
                browser.close()
    except Exception as e:  # Playwright call logs can echo the filled password: keep only the type
        raise SiteError(f"Staff App login failed ({type(e).__name__}); check the saved credentials.") from None
    if not token:
        raise SiteError("Staff App login did not return a token (wrong password?).")
    keyring.set_password(SVC, "jwt", token)
    return token


class SiteClient:
    def __init__(self, store: Store, http: httpx.Client | None = None):
        self.store = store
        self.http = http or httpx.Client(timeout=60)
        self._defs: dict[str, tuple[float, dict]] = {}  # in-memory, per process

    # --- auth ---
    def _token(self) -> str:
        t = keyring.get_password(SVC, "jwt")
        if not t or _claims(t)["exp"] - time.time() < 2 * 86400:
            t = login()
        return t

    def uid(self) -> str:
        return str(_claims(self._token())["staffing_users_id"])

    # --- transport ---
    def _send(self, method: str, path: str, retry: bool, **kw):
        relogged = False
        for attempt in range(3 if retry else 1):
            try:
                r = self.http.request(method, BASE + path, headers={"Authorization": "Bearer " + self._token()}, **kw)
                if r.status_code == 401:
                    if relogged:
                        raise SiteError("Staff App rejected the login twice (HTTP 401).")
                    relogged = True
                    keyring.set_password(SVC, "jwt", login())
                    r = self.http.request(method, BASE + path,
                                          headers={"Authorization": "Bearer " + self._token()}, **kw)
                    if r.status_code == 401:
                        raise SiteError("Staff App rejected the login twice (HTTP 401).")
                body = self._body(r)
                err = body.get("error") if isinstance(body, dict) else None
                if retry and attempt < 2 and (r.status_code == 503 or (err and RETRY_ERRORS.search(str(err)))):
                    time.sleep(1 << attempt)
                    continue
                if r.status_code >= 400 or err:
                    raise SiteError(f"Staff App error (HTTP {r.status_code}): {err or r.text[:200]}")
                return body
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if not retry or attempt == 2:
                    raise SiteError(f"Could not reach the Staff App: {e}") from e
                time.sleep(1 << attempt)
            except httpx.TransportError as e:  # ReadTimeout, RemoteProtocolError: the file may be stored, never retry
                raise SiteError(f"Staff App connection failed ({type(e).__name__}).") from e

    @staticmethod
    def _body(r):
        try:
            return r.json()
        except ValueError:
            return None

    def _get(self, path: str):
        return self._send("GET", path, False)

    # --- reads ---
    def positions(self) -> list[dict]:
        return self._get(f"staffing_user/{self.uid()}/positions?{Q}")

    def events(self) -> list[dict]:
        return self._get(f"staffing_user/{self.uid()}/events?{Q}")

    def submitted(self) -> list[dict]:
        return self._get(f"recap_response_set/staffing_user/{self.uid()}?{Q}")

    def drafts(self) -> list[dict]:
        return self._get(f"partial_responses/{self.uid()}")

    def definition(self, recap_id: str) -> dict:
        hit = self._defs.get(str(recap_id))
        if hit and time.time() - hit[0] < DEF_TTL:
            return hit[1]
        d = self._get(f"recap_definition/{recap_id}")
        self._defs[str(recap_id)] = (time.time(), d)
        return d

    def app_version(self) -> str:
        try:
            v = self._get("app_version")
        except SiteError:
            return "5.61.0"
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return str(v.get("version") or v.get("app_version") or "5.61.0")
        return "5.61.0"

    # --- writes ---
    @staticmethod
    def _file(jpeg: bytes) -> dict:
        return {"file": ("photo.jpg", jpeg, "image/jpeg")}

    def upload_recap_photo(self, events_id: str, field_id: str, jpeg: bytes) -> tuple[str, str | None]:
        b = self._send("POST", "recap_response_set/upload", True, files=self._file(jpeg),
                       params={"events_custom_id": field_id, "events_id": events_id})
        return str(b["id"]), b.get("uuid")

    def upload_gallery_photo(self, events_id: str, jpeg: bytes) -> str:
        b = self._send("POST", "document/save", True, files=self._file(jpeg),
                       data={"events_id": events_id, "staffing_users_id": self.uid()})
        return str(b["id"])

    def remove_gallery_photos(self, doc_ids: list[str]) -> None:
        bad = [d for d in doc_ids if not self.store.is_our_document(d)]
        if bad:
            raise SiteError(f"Refusing to delete photos that were not uploaded by this tool: {', '.join(map(str, bad))}")
        self._send("POST", "document/remove", False, json={"ids": [str(d) for d in doc_ids]})

    def save_draft(self, payload: dict) -> dict:
        return self._send("POST", "partial_recap_response_set", False, json=payload)

    def submit_primary_claim(self, position_id: str, payload: dict) -> dict:
        """Files the pay claim for a shift. Only called after the user confirmed the preview."""
        return self._send("POST", f"staffing_user/{self.uid()}/positions/{position_id}/primary_claim", False, json=payload)
