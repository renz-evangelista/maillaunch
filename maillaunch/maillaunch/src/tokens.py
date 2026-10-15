"""Where OAuth tokens live.

The requirement is "encrypted local file or OS keychain; never plaintext". This
tries the OS keychain first and falls back to an encrypted file, because
keychains are the better answer but are not always available:

  - macOS: Keychain, always present
  - Linux desktop: SecretService via GNOME Keyring or KWallet
  - Linux headless / SSH / container: no D-Bus, so no keychain at all

On that last case `keyring` raises, and a tool that only supported keychains
would simply not run on a server. The fallback is a Fernet-encrypted file whose
key comes from MAILLAUNCH_SECRET_KEY, so the key lives in the operator's
environment rather than next to the ciphertext -- an encrypted file with its key
stored beside it is just a slower plaintext file.
"""

import json
import os
import stat

import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.errors import KeyringError

from .config import ENV_ENCRYPTION_KEY, HOME

SERVICE = "maillaunch"


class TokenError(Exception):
    """Raised when tokens cannot be stored or read back."""


def _fallback_path():
    return HOME / "tokens.enc"


def _fernet():
    key = os.environ.get(ENV_ENCRYPTION_KEY)
    if not key:
        raise TokenError(
            "No OS keyring is available on this machine, so tokens need the "
            f"encrypted-file fallback. Generate a key once with:\n"
            f"  python -c \"from cryptography.fernet import Fernet; "
            f'print(Fernet.generate_key().decode())"\n'
            f"then export it as {ENV_ENCRYPTION_KEY}."
        )
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        raise TokenError(
            f"{ENV_ENCRYPTION_KEY} is not a valid Fernet key (44 url-safe base64 chars)."
        ) from None


def _keyring_available():
    """Probe rather than assume: the backend can import and still not work."""
    try:
        keyring.get_password(SERVICE, "__probe__")
        return True
    except (KeyringError, Exception):  # noqa: BLE001 - backends raise freely
        return False


def save(provider, token_data):
    """Persist the token bundle for a provider."""
    blob = json.dumps(token_data)
    if _keyring_available():
        try:
            keyring.set_password(SERVICE, provider, blob)
            return "os-keyring"
        except KeyringError:
            pass  # fall through to the encrypted file

    path = _fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    everything = _read_fallback()
    everything[provider] = token_data
    path.write_bytes(_fernet().encrypt(json.dumps(everything).encode()))
    # Owner-only, so the ciphertext is not world-readable even before decryption.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return f"encrypted file ({path})"


def load(provider):
    """Return the stored token bundle, or None if the provider is not authed."""
    if _keyring_available():
        try:
            blob = keyring.get_password(SERVICE, provider)
            if blob:
                return json.loads(blob)
        except KeyringError:
            pass
    return _read_fallback().get(provider)


def delete(provider):
    if _keyring_available():
        try:
            keyring.delete_password(SERVICE, provider)
        except Exception:  # noqa: BLE001 - "not found" varies by backend
            pass
    everything = _read_fallback()
    if everything.pop(provider, None) is not None:
        _fallback_path().write_bytes(_fernet().encrypt(json.dumps(everything).encode()))


def _read_fallback():
    path = _fallback_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(_fernet().decrypt(path.read_bytes()).decode())
    except InvalidToken:
        raise TokenError(
            f"{path} could not be decrypted. Is {ENV_ENCRYPTION_KEY} the same key "
            "it was written with? Delete the file and re-run `maillaunch auth` to start over."
        ) from None
