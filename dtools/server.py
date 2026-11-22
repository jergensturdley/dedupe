"""Instant HTTP file sharing with a scannable QR link."""

from __future__ import annotations

import base64
import functools
import hmac
import http.server
import os
import secrets
import socket
import sys
from pathlib import Path
from urllib.parse import quote

AUTH_ENV = "DEDUPE_SHARE_PASSWORD"
AUTH_REALM = "dedupe"


def _lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def resolve_password(args) -> tuple[str | None, str]:
    """Return the shared password and where it came from."""
    if getattr(args, "no_auth", False):
        return None, "none"
    password = getattr(args, "password", None)
    if password:
        return password, "argument"
    password = os.environ.get(AUTH_ENV)
    if password:
        return password, "environment"
    if getattr(args, "auth", False):
        return secrets.token_urlsafe(12), "generated"
    return None, "none"


def build_handler(root: Path, password: str | None = None):
    """Return a request handler class bound to root, optionally password-gated."""
    base = http.server.SimpleHTTPRequestHandler

    class Handler(base):
        def do_GET(self) -> None:
            if not self._allowed():
                self._challenge()
                return
            base.do_GET(self)

        def do_HEAD(self) -> None:
            if not self._allowed():
                self._challenge()
                return
            base.do_HEAD(self)

        def _allowed(self) -> bool:
            if password is None:
                return True
            header = self.headers.get("Authorization", "")
            scheme, _, credential = header.partition(" ")
            if scheme.lower() != "basic" or not credential:
                return False
            try:
                decoded = base64.b64decode(credential, validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            user, separator, supplied = decoded.partition(":")
            if not separator or not user:
                return False
            return hmac.compare_digest(supplied, password)

        def _challenge(self) -> None:
            body = b"Password required.\n"
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate", f'Basic realm="{AUTH_REALM}", charset="UTF-8"'
            )
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return functools.partial(Handler, directory=str(root))


def cmd_serve(args) -> int:
    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        print(f"serve: no such path: {target}", file=sys.stderr)
        return 1
    root = target.parent if target.is_file() else target
    direct = quote(target.name, safe="") if target.is_file() else ""
    password, source = resolve_password(args)
    handler = build_handler(root, password)
    try:
        server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as error:
        print(f"serve: cannot bind {args.host}:{args.port}: {error}", file=sys.stderr)
        return 1
    address = server.server_address
    ip = _lan_ip() if args.host in ("0.0.0.0", "") else args.host
    url = f"http://{ip}:{address[1]}/" + direct

    print(f"Serving {target} at {url}")
    if password:
        if source == "generated":
            print(f"Generated password: {password}")
        print(f"Password required ({source}); browsers will prompt for it.")
        print("HTTP is unencrypted; share the password only with trusted devices.")
    if args.host in ("0.0.0.0", ""):
        print("LAN access is enabled; share only with trusted devices.")
    import qrcode

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    if sys.stdout.isatty():
        qr.print_tty()
    else:
        qr.print_ascii(tty=False)
    if args.png:
        qr.make_image(fill_color="black", back_color="white").save(args.png)
        print(f"QR image saved to {args.png}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
    return 0
