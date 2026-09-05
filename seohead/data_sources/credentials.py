"""Read external-provider secrets from ``~/.config``, with environment overrides.

The providers intentionally do not share one variable or a proprietary configuration format.
Existing secrets already have stable locations, including ``~/.config/arsenkin/token`` and
``~/.config/yandex-wordstat/{api_key,folder_id}``. Moving them would break working scripts and
could leave forgotten copies behind. Therefore:

* the canonical persistent source is a user-readable file on disk;
* environment variables take precedence for CI, containers, and one-off runs with another key;
* **a secret value is never printed or included in an exception.** Messages identify only the
  missing path or variable. This is more important than debugging convenience because logs may
  be copied into session transcripts.
"""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_ROOT = Path(os.path.expanduser("~/.config"))


class MissingCredential(RuntimeError):
    """A credential is missing; the message names sources but never exposes a value."""


def read(path: str, env_var: str, *, hint: str = "") -> str:
    """Read a secret from ``$env_var`` or ``~/.config/<path>``.

    ``path`` is relative to ``~/.config``, for example ``arsenkin/token``.
    """
    from_env = os.environ.get(env_var)
    if from_env and from_env.strip():
        return from_env.strip()

    file_path = CONFIG_ROOT / path
    if not file_path.exists():
        raise MissingCredential(
            f"credential not found: store it in {file_path} or set ${env_var}"
            + (f". {hint}" if hint else "")
        )
    value = file_path.read_text(encoding="utf-8").strip()
    if not value:
        raise MissingCredential(f"{file_path} is empty; store a value there or set ${env_var}")
    return value


def available(path: str, env_var: str) -> bool:
    """Return whether a credential exists, for diagnostics and graceful provider skips."""
    try:
        read(path, env_var)
    except MissingCredential:
        return False
    return True


# --- provider-specific credentials -----------------------------------------


def arsenkin_token() -> str:
    return read(
        "arsenkin/token", "ARSENKIN_TOKEN", hint="Create the token in your arsenkin.ru account."
    )


def yandex_cloud_api_key() -> str:
    return read(
        "yandex-wordstat/api_key",
        "YANDEX_CLOUD_API_KEY",
        hint="Create it in Yandex AI Studio with the search-api.webSearch.user role.",
    )


def yandex_cloud_folder_id() -> str:
    return read(
        "yandex-wordstat/folder_id",
        "YANDEX_CLOUD_FOLDER_ID",
        hint="Use the folder ID shown in the Yandex Cloud console.",
    )


def metrika_token() -> str:
    return read(
        "yandex-metrika/token",
        "YANDEX_METRIKA_TOKEN",
        hint="Create a Yandex Metrica OAuth token at oauth.yandex.ru.",
    )


def dataforseo_login() -> str:
    return read("dataforseo/login", "DATAFORSEO_LOGIN")


def dataforseo_password() -> str:
    return read("dataforseo/password", "DATAFORSEO_PASSWORD")


def dataforseo_ready() -> tuple[bool, dict[str, bool]]:
    """Return DataForSEO readiness and its per-component status, without exposing secret values.

    DataForSEO authenticates with HTTP Basic auth over login *and* password (see
    ``DataForSEOClient._auth``); either one alone cannot authenticate a request. This is the
    single readiness definition shared by provider wrappers and ``sources_doctor`` so the two
    never drift back out of sync with what the client actually requires.
    """
    components = {
        "login": available("dataforseo/login", "DATAFORSEO_LOGIN"),
        "password": available("dataforseo/password", "DATAFORSEO_PASSWORD"),
    }
    return all(components.values()), components


def gsc_access_token() -> str:
    """Search Console has no long-lived API key; this reads a short-lived OAuth2 bearer token.

    Generate one at https://developers.google.com/oauthplayground (authorize the
    ``webmasters.readonly`` scope) or with ``gcloud auth application-default print-access-token``
    after ``gcloud auth application-default login --scopes=...webmasters.readonly``. A token
    expires roughly hourly; this client does not refresh one, so callers re-supply it per session.
    """
    return read(
        "gsc/access_token",
        "GSC_ACCESS_TOKEN",
        hint="See docs/SETUP.md for how to obtain a Search Console OAuth token.",
    )


def crux_api_key() -> str:
    return read(
        "crux/api_key",
        "CRUX_API_KEY",
        hint="Create an API key in Google Cloud Console and enable the Chrome UX Report API.",
    )


def indexnow_key() -> str:
    """IndexNow needs a self-generated key, not a provider-issued secret.

    Any random string works (for example ``openssl rand -hex 16``); publish it unmodified at
    ``https://<your-domain>/<key>.txt`` before submitting, so a receiving search engine can
    verify the submitter controls the host.
    """
    return read(
        "indexnow/key",
        "INDEXNOW_KEY",
        hint="Generate one yourself and publish it at https://<host>/<key>.txt first.",
    )
