"""
WebVPN URL encoding and authentication for Fudan University.

Handles:
- AES-128-CFB URL encoding/decoding for WebVPN proxy URLs
- Full 7-step IDP authentication flow against id.fudan.edu.cn
"""

import html as html_mod
import hmac
import re
import time
from binascii import Error as Base32Error, hexlify, unhexlify
from urllib.parse import urlparse, urlencode, quote, urljoin

import requests
from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
import base64

from src.runtime import config


class SecondFactorError(RuntimeError):
    """Stop automatic password retries when a second factor needs attention."""


def totp_code(secret: str, timestamp: float | None = None) -> str:
    """Generate a six-digit, 30-second HMAC-SHA1 TOTP (RFC 6238)."""
    normalized = "".join(secret.split()).upper().rstrip("=")
    if not normalized:
        raise SecondFactorError(
            "Set UIS_TOTP_SECRET to the account's bound TOTP secret "
            "(a repository secret when running in GitHub Actions)"
        )
    try:
        key = base64.b32decode(normalized + "=" * (-len(normalized) % 8))
    except (Base32Error, ValueError):
        raise SecondFactorError("UIS_TOTP_SECRET must be a Base32 TOTP secret") from None
    counter = int((time.time() if timestamp is None else timestamp) // 30)
    digest = hmac.digest(key, counter.to_bytes(8, "big"), "sha1")
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset:offset + 4], "big") & 0x7FFFFFFF
    return f"{number % 1_000_000:06d}"


def encrypt_host(hostname: str) -> str:
    """Encrypt hostname using AES-128-CFB for WebVPN URL encoding.

    Returns the hex-encoded ciphertext of the hostname.
    """
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    plaintext = hostname.encode("utf-8")
    encrypted = cipher.encrypt(plaintext)
    return hexlify(encrypted).decode("ascii")


def decrypt_host(ciphertext_hex: str) -> str:
    """Decrypt a WebVPN-encoded hostname."""
    key = config.WEBVPN_AES_KEY
    iv = config.WEBVPN_AES_IV
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    decrypted = cipher.decrypt(unhexlify(ciphertext_hex))
    return decrypted.decode("utf-8")


def get_vpn_url(url: str) -> str:
    """Convert a regular URL to its WebVPN proxy URL.

    Example:
        https://icourse.fudan.edu.cn/courseapi/v3/...
        ->
        https://webvpn.fudan.edu.cn/https/77726476706e69737468656265737421f9f44e.../courseapi/v3/...
    """
    parsed = urlparse(url)
    protocol = parsed.scheme
    hostname = parsed.hostname
    port = parsed.port
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query
    if parsed.fragment:
        path += "#" + parsed.fragment

    # Remove leading slash from path for concatenation
    path = path.lstrip("/")

    encrypted = encrypt_host(hostname)
    iv_hex = hexlify(config.WEBVPN_AES_IV).decode("ascii")

    # Include non-standard port
    port_suffix = ""
    if port and not (
        (protocol == "http" and port == 80)
        or (protocol == "https" and port == 443)
    ):
        port_suffix = f"-{port}"

    vpn_url = f"{config.WEBVPN_BASE}/{protocol}{port_suffix}/{iv_hex}{encrypted}"
    if path:
        vpn_url += f"/{path}"
    return vpn_url


def get_ordinary_url(vpn_url: str) -> str:
    """Convert a WebVPN URL back to the original URL."""
    parsed = urlparse(vpn_url)
    path_parts = parsed.path.strip("/").split("/", 2)
    if len(path_parts) < 2:
        raise ValueError(f"Invalid WebVPN URL: {vpn_url}")

    protocol_part = path_parts[0]  # e.g. "https" or "https-8080"
    encoded_host = path_parts[1]  # IV + ciphertext
    rest = path_parts[2] if len(path_parts) > 2 else ""

    # Parse protocol and optional port
    if "-" in protocol_part:
        protocol, port_str = protocol_part.rsplit("-", 1)
        port = f":{port_str}"
    else:
        protocol = protocol_part
        port = ""

    # Strip the 32-char IV hex prefix
    iv_hex_len = 32
    ciphertext_hex = encoded_host[iv_hex_len:]
    hostname = decrypt_host(ciphertext_hex)

    original = f"{protocol}://{hostname}{port}"
    if rest:
        original += f"/{rest}"
    if parsed.query:
        original += f"?{parsed.query}"
    return original


class WebVPNSession:
    """Manages a WebVPN session with full IDP authentication."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.logged_in = False

    def login(self, student_id: str = None, password: str = None) -> bool:
        """Execute the full 7-step IDP authentication flow.

        Returns True on success, raises on failure.
        """
        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        if not student_id or not password:
            raise ValueError(
                "Student ID and password are required. "
                "Set STUID and UISPsw environment variables."
            )

        print("[1/7] Getting authentication context...")
        lck, entity_id = self._get_auth_context()

        print("[2/7] Querying authentication methods...")
        auth_chain_code, request_type = self._query_auth_methods(lck, entity_id)

        print("[3/7] Getting RSA public key...")
        pub_key_pem = self._get_public_key()

        print("[4/7] Encrypting password...")
        encrypted_password = self._encrypt_password(password, pub_key_pem)

        print("[5/7] Executing authentication...")
        login_token = self._auth_execute(
            student_id,
            encrypted_password,
            lck,
            entity_id,
            auth_chain_code,
            request_type,
        )

        print("[6/7] Getting CAS ticket...")
        ticket_url = self._get_cas_ticket(login_token)

        print("[7/7] Establishing WebVPN session...")
        self._establish_session(ticket_url)

        self.logged_in = True
        print("[*] WebVPN login successful!")
        return True

    def authenticate_icourse(
        self, student_id: str = None, password: str = None
    ) -> bool:
        """Authenticate to iCourse via CAS/IDP through WebVPN.

        Mimics the browser flow:
        1. Access casapi login URL (like clicking "校内用户登录")
        2. Follow redirect to IDP authenticate (casapi generates correct
           service URL with forward param and r=auth/login)
        3. IDP auth steps through WebVPN
        4. Follow ticket back to iCourse through WebVPN
        """
        student_id = student_id or config.STUDENT_ID
        password = password or config.PASSWORD

        print("[*] Starting iCourse CAS authentication through WebVPN...")

        # Pre-flight: probe the WebVPN portal.  A cold session redirects to
        # /login instantly (status 302); a hot session returns 200.  Fail
        # fast on cold — login_with_retry() in main.py will re-login.
        warmup = self.session.get(
            config.WEBVPN_BASE + "/", allow_redirects=False, timeout=5,
        )
        if warmup.status_code != 200:
            raise RuntimeError("WebVPN session cold — re-login needed")

        idp_vpn_base = get_vpn_url(config.IDP_BASE)

        # Step 1: Initiate CAS login via casapi.  Use allow_redirects=True
        # so requests follows the full redirect chain like a browser.
        print("[1/7] Initiating CAS login via casapi...")
        casapi_url = (
            f"{config.ICOURSE_BASE}/casapi/index.php"
            f"?r=auth/login&school_login=1"
            f"&tenant_code={config.TENANT_CODE}"
            f"&forward={quote(config.ICOURSE_BASE + '/', safe='')}"
        )
        vpn_url = get_vpn_url(casapi_url)

        resp = self.session.get(vpn_url, allow_redirects=True, timeout=60)
        lck = None
        for source in [resp.url] + [
            h.headers.get("Location", "") for h in (resp.history or [])
        ]:
            m = re.search(r'lck=([^&#"]+)', source)
            if m:
                lck = m.group(1)
                break
        if not lck:
            m = re.search(r'lck=([^&#"]+)', resp.text[:5000])
            if m:
                lck = m.group(1)
        if not lck:
            raise RuntimeError(
                f"Failed to extract lck from CAS redirect chain "
                f"(final URL: {resp.url[:120]})"
            )

        entity_id = config.ICOURSE_BASE
        print("    lck: OK")

        # Step 2: Query auth methods (through WebVPN)
        print("[2/7] Querying auth methods (via WebVPN)...")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authn/queryAuthMethods")
        resp = self.session.post(
            url,
            json={"lck": lck, "entityId": entity_id},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE,
            },
            timeout=60,
        )
        data = resp.json()
        auth_method_list = data.get("data", [])
        request_type = data.get("requestType", "chain_type")

        auth_chain_code = ""
        for method in auth_method_list:
            if method.get("moduleCode") == "userAndPwd":
                auth_chain_code = method.get("authChainCode", "")
                break
        if not auth_chain_code:
            raise RuntimeError("No authChainCode found in response")
        print("    authChainCode: OK")

        # Step 3: Get RSA public key (through WebVPN)
        print("[3/7] Getting RSA public key (via WebVPN)...")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authn/getJsPublicKey")
        resp = self.session.get(
            url,
            headers={"Referer": f"{idp_vpn_base}/ac/"},
            timeout=60,
        )
        data = resp.json()
        pub_key_b64 = data.get("data", "")
        if not pub_key_b64:
            raise RuntimeError("Failed to get public key via WebVPN")
        print("    Got RSA public key")

        # Step 4: Encrypt password
        print("[4/7] Encrypting password...")
        encrypted_password = self._encrypt_password(password, pub_key_b64)

        # Step 5: Execute authentication (through WebVPN)
        print("[5/7] Executing authentication (via WebVPN)...")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authn/authExecute")
        payload = {
            "authModuleCode": "userAndPwd",
            "authChainCode": auth_chain_code,
            "entityId": entity_id,
            "requestType": request_type,
            "lck": lck,
            "authPara": {
                "loginName": student_id,
                "password": encrypted_password,
                "verifyCode": "",
            },
        }
        resp = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE,
            },
            timeout=60,
        )
        data = resp.json()

        if str(data.get("code")) != "200":
            raise RuntimeError(
                f"iCourse CAS auth failed (code={data.get('code')})"
            )

        if data.get("second"):
            data = self._authenticate_totp(
                data, student_id, lck, entity_id, request_type, via_vpn=True
            )
        login_token = data.get("loginToken", "")
        if not login_token:
            raise RuntimeError("No loginToken in iCourse CAS response")
        print("    loginToken: OK")

        # Step 6: Get CAS ticket (through WebVPN)
        print("[6/7] Getting CAS ticket (via WebVPN)...")
        url = get_vpn_url(f"{config.IDP_BASE}/idp/authCenter/authnEngine")
        resp = self.session.post(
            url,
            data={"loginToken": login_token},
            headers={
                "Referer": f"{idp_vpn_base}/ac/",
                "Origin": config.WEBVPN_BASE,
            },
            timeout=60,
        )
        html = resp.text

        # Extract ticket URL from the authnEngine response
        # The URL may already be rewritten to a WebVPN URL by the proxy
        ticket_match = re.search(
            r'locationValue\s*=\s*"([^"]*ticket=[^"]*)"', html
        )
        if not ticket_match:
            ticket_match = re.search(
                r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)', html
            )
        if not ticket_match:
            raise RuntimeError(
                f"Failed to extract iCourse ticket URL (response length: {len(html)})"
            )

        ticket_url = html_mod.unescape(ticket_match.group(1))
        print("    Ticket extracted.")

        # Step 7: Follow ticket to iCourse (through WebVPN)
        print("[7/7] Following ticket to iCourse (via WebVPN)...")
        if not ticket_url.startswith(config.WEBVPN_BASE):
            ticket_url = get_vpn_url(ticket_url)

        resp = self.session.get(
            ticket_url, allow_redirects=True, timeout=90
        )
        print(f"    Status: {resp.status_code}")

        # Verify by making a test API call
        test_url = get_vpn_url(
            f"{config.ICOURSE_BASE}/userapi/v1/infosimple"
        )
        resp = self.session.get(test_url, timeout=60)
        if resp.status_code == 200:
            try:
                user_data = resp.json()
                if user_data.get("code") in (0, 200):
                    print("    Verified: login OK")
                    print("[*] iCourse authentication successful!")
                    return True
            except Exception:
                pass

        print("    Could not verify iCourse auth via API, proceeding...")
        return True

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET request through WebVPN. Converts URL automatically."""
        vpn_url = get_vpn_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.get(vpn_url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        """POST request through WebVPN. Converts URL automatically."""
        vpn_url = get_vpn_url(url)
        kwargs.setdefault("timeout", 60)
        return self.session.post(vpn_url, **kwargs)

    def get_raw(self, url: str, **kwargs) -> requests.Response:
        """GET request without URL conversion (for already-converted URLs)."""
        kwargs.setdefault("timeout", 30)
        return self.session.get(url, **kwargs)

    def post_raw(self, url: str, **kwargs) -> requests.Response:
        """POST request without URL conversion."""
        kwargs.setdefault("timeout", 30)
        return self.session.post(url, **kwargs)

    # --- Private authentication steps ---

    def _get_auth_context(self) -> tuple[str, str]:
        """Step 1: GET authenticate endpoint, extract lck from redirect."""
        service_url = f"{config.WEBVPN_BASE}/login?cas_login=true"
        url = (
            f"{config.IDP_BASE}/idp/authCenter/authenticate"
            f"?service={quote(service_url, safe='')}"
        )
        resp = self.session.get(url, allow_redirects=False, timeout=60)

        # Follow redirects manually to extract lck
        location = resp.headers.get("Location", "")
        while resp.status_code in (301, 302) and "lck=" not in location:
            resp = self.session.get(location, allow_redirects=False, timeout=60)
            location = resp.headers.get("Location", "")

        if resp.status_code in (301, 302):
            location = resp.headers.get("Location", "")

        # Extract lck parameter
        lck_match = re.search(r"[?&]lck=([^&]+)", location)
        if not lck_match:
            raise RuntimeError(
                f"Failed to extract lck from redirect (status={resp.status_code})"
            )

        lck = lck_match.group(1)
        entity_id = config.WEBVPN_BASE
        print("    lck: OK")
        return lck, entity_id

    def _query_auth_methods(
        self, lck: str, entity_id: str
    ) -> tuple[str, str]:
        """Step 2: Query available authentication methods."""
        url = f"{config.IDP_BASE}/idp/authn/queryAuthMethods"
        resp = self.session.post(
            url,
            json={"lck": lck, "entityId": entity_id},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=60,
        )
        data = resp.json()

        # data["data"] is a list of auth methods; pick the userAndPwd one
        # authChainCode for userAndPwd is in the list items;
        # requestType is at the top level
        auth_method_list = data.get("data", [])
        request_type = data.get("requestType", "chain_type")

        auth_chain_code = ""
        for method in auth_method_list:
            if method.get("moduleCode") == "userAndPwd":
                auth_chain_code = method.get("authChainCode", "")
                break

        if not auth_chain_code:
            raise RuntimeError("Failed to get authChainCode")

        print("    authChainCode: OK")
        return auth_chain_code, request_type

    def _get_public_key(self) -> str:
        """Step 3: Get RSA public key for password encryption."""
        url = f"{config.IDP_BASE}/idp/authn/getJsPublicKey"
        resp = self.session.get(
            url,
            headers={
                "Referer": f"{config.IDP_BASE}/ac/",
            },
            timeout=60,
        )
        data = resp.json()
        pub_key_b64 = data.get("data", "")
        if not pub_key_b64:
            raise RuntimeError("Failed to get public key")

        print("    Got RSA public key")
        return pub_key_b64

    def _encrypt_password(self, password: str, pub_key_b64: str) -> str:
        """Step 4: RSA-encrypt the password with PKCS1_v1_5."""
        # Construct PEM format
        pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            + pub_key_b64
            + "\n-----END PUBLIC KEY-----"
        )
        rsa_key = RSA.import_key(pem)
        cipher = PKCS1_v1_5.new(rsa_key)
        encrypted = cipher.encrypt(password.encode("utf-8"))
        return base64.b64encode(encrypted).decode("ascii")

    def _auth_execute(
        self,
        student_id: str,
        encrypted_password: str,
        lck: str,
        entity_id: str,
        auth_chain_code: str,
        request_type: str,
    ) -> str:
        """Step 5: Execute authentication and get loginToken."""
        url = f"{config.IDP_BASE}/idp/authn/authExecute"
        payload = {
            "authModuleCode": "userAndPwd",
            "authChainCode": auth_chain_code,
            "entityId": entity_id,
            "requestType": request_type,
            "lck": lck,
            "authPara": {
                "loginName": student_id,
                "password": encrypted_password,
                "verifyCode": "",
            },
        }
        resp = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=60,
        )
        data = resp.json()

        if str(data.get("code")) != "200":
            raise RuntimeError(
                f"Authentication failed (code={data.get('code')})"
            )

        # loginToken is at top level, not nested under "data"
        if data.get("second"):
            data = self._authenticate_totp(
                data, student_id, lck, entity_id, request_type
            )
        login_token = data.get("loginToken", "")
        if not login_token:
            raise RuntimeError("No loginToken in response")

        print("    loginToken: OK")
        return login_token

    def _authenticate_totp(
        self,
        challenge: dict,
        student_id: str,
        lck: str,
        entity_id: str,
        request_type: str,
        *,
        via_vpn: bool = False,
    ) -> dict:
        """Continue a password challenge using the offered OTP module."""
        if "userAndOtp" not in (challenge.get("moduleCodes") or []):
            raise SecondFactorError(
                "Unified authentication requires a second factor but does not offer TOTP"
            )
        if not challenge.get("requestNumber") or not challenge.get("authChainCode"):
            raise SecondFactorError("TOTP challenge is missing its authentication context")
        code = totp_code(config.TOTP_SECRET)
        base = get_vpn_url(config.IDP_BASE) if via_vpn else config.IDP_BASE
        origin = config.WEBVPN_BASE if via_vpn else config.IDP_BASE
        print("[Login] TOTP second factor...")
        try:
            resp = self.session.post(
                f"{base}/idp/authn/authExecute",
                json={
                    "authModuleCode": "userAndOtp",
                    "authChainCode": challenge["authChainCode"],
                    "entityId": entity_id,
                    "requestNumber": challenge["requestNumber"],
                    "requestType": request_type,
                    "lck": lck,
                    "authPara": {
                        "loginName": challenge.get("userName") or student_id,
                        "otpCode": code,
                    },
                },
                headers={
                    "Content-Type": "application/json",
                    "Referer": f"{base}/ac/",
                    "Origin": origin,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError):
            raise SecondFactorError(
                "TOTP request failed; automatic login retries stopped"
            ) from None
        if not isinstance(data, dict) or str(data.get("code")) != "200":
            raise SecondFactorError(
                "TOTP authentication failed; check the bound secret and system clock"
            )
        if data.get("second") or not data.get("loginToken"):
            raise SecondFactorError(
                "TOTP did not finish authentication; an additional verification step is required"
            )
        print("[Login] TOTP OK")
        return data

    def _get_cas_ticket(self, login_token: str) -> str:
        """Step 6: Exchange loginToken for a CAS ticket URL."""
        url = f"{config.IDP_BASE}/idp/authCenter/authnEngine"
        resp = self.session.post(
            url,
            data={"loginToken": login_token},
            headers={
                "Referer": f"{config.IDP_BASE}/ac/",
                "Origin": config.IDP_BASE,
            },
            timeout=60,
        )

        # The response is HTML containing a JS redirect with the ticket URL
        html = resp.text

        # Extract the locationValue from the JavaScript
        ticket_match = re.search(
            r'locationValue\s*=\s*"([^"]*ticket=[^"]*)"', html
        )
        if not ticket_match:
            # Fallback: any URL with ticket= parameter
            ticket_match = re.search(
                r'(https?://[^\s"\'<>]*ticket=[^\s"\'<>]*)', html
            )

        if not ticket_match:
            raise RuntimeError(
                f"Failed to extract ticket URL (response length: {len(html)})"
            )

        ticket_url = ticket_match.group(1)
        # Unescape HTML entities (e.g., &amp; -> &)
        ticket_url = html_mod.unescape(ticket_url)
        print("    Ticket extracted.")
        return ticket_url

    def _establish_session(self, ticket_url: str):
        """Step 7: Follow the ticket URL to establish WebVPN session."""
        for attempt in range(3):
            try:
                resp = self.session.get(
                    ticket_url, allow_redirects=True, timeout=20
                )
                if resp.status_code == 200:
                    print("    Session established.")
                    return
                raise RuntimeError(
                    f"Failed to establish WebVPN session (status={resp.status_code})"
                )
            except requests.exceptions.Timeout:
                has_ticket = any(
                    "wengine_vpn_ticket" in c.name
                    for c in self.session.cookies
                )
                if has_ticket:
                    print("    Session cookie set despite timeout.")
                    return
                if attempt < 2:
                    print(f"    Timeout, retrying ({attempt + 2}/3)...")
                    continue
                raise
