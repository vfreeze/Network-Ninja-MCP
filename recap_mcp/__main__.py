"""CLI: set-credentials | check | serve."""
import argparse
import getpass
import json
import logging
import re
import secrets

import keyring
from mcp.server.transport_security import TransportSecuritySettings

from . import server as srv
from .server import build_server
from .site import SVC, SiteClient
from .store import Store
from .upload import data_dir

SECRET_KEY = "connector_secret"


class RedactBearer(logging.Filter):
    PAT = re.compile(r"Bearer\s+\S+", re.I)

    def filter(self, record):
        record.msg = self.PAT.sub("Bearer ***", record.getMessage())
        record.args = ()
        return True


def setup_logging(path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    for h in (logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        h.addFilter(RedactBearer())  # on the handlers that write, so every logger is covered
        root.addHandler(h)
    root.setLevel(logging.INFO)


def make_app(store, site, public_host: str, secret: str):
    server = build_server(store, site, public_url=f"https://{public_host}")
    return server.streamable_http_app(
        streamable_http_path=f"/{secret}/mcp",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[public_host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=["https://claude.ai", f"https://{public_host}"]))


def _store() -> Store:
    data_dir().mkdir(parents=True, exist_ok=True)
    return Store(data_dir() / "recap.db")


def set_credentials(a):
    keyring.set_password(SVC, "username", input("Staff App username: ").strip())
    keyring.set_password(SVC, "password", getpass.getpass("Staff App password: "))
    secret = keyring.get_password(SVC, SECRET_KEY)
    if not secret:
        secret = secrets.token_urlsafe(32)
        keyring.set_password(SVC, SECRET_KEY, secret)
    print("Saved. Your connector URL (paste into Claude; keep it private):")
    print(f"https://{a.public_host or '<your-ngrok-domain>'}/{secret}/mcp")


def check(a):
    store = _store()
    build_server(store, SiteClient(store), "https://unused")  # sets srv.Deps
    srv._refresh(force=True)
    print(json.dumps(srv.find_recaps(), indent=2))


def serve(a):
    import uvicorn
    secret = keyring.get_password(SVC, SECRET_KEY)
    if not secret:
        raise SystemExit("No connector secret yet. Run: python -m recap_mcp set-credentials")
    data_dir().mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir() / "server.log")
    store = _store()
    uvicorn.run(make_app(store, SiteClient(store), a.public_host, secret), host="127.0.0.1", port=a.port,
                log_config=None, access_log=False)  # access log would print the secret path


def main(argv=None):
    p = argparse.ArgumentParser(prog="recap_mcp", description="Recap MCP server for the Staff App.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set-credentials", help="save Staff App login and create the connector secret")
    s.add_argument("--public-host", help="your ngrok domain, to print the real connector URL")
    s.set_defaults(fn=set_credentials)
    sub.add_parser("check", help="log in and list pending recaps").set_defaults(fn=check)
    s = sub.add_parser("serve", help="run the MCP server on 127.0.0.1")
    s.add_argument("--public-host", required=True, help="your ngrok domain")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=serve)
    a = p.parse_args(argv)
    if getattr(a, "public_host", None):  # accept a pasted URL: keep only the domain
        a.public_host = a.public_host.split("://")[-1].split("/")[0]
    a.fn(a)


if __name__ == "__main__":
    main()
