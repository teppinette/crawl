"""
Load secrets from Azure Key Vault using managed identity.
Falls back to environment variables if Key Vault is unreachable (e.g. local dev).
"""

import logging
import os

# Suppress verbose Azure SDK HTTP logging
for _name in ("azure.core.pipeline.policies.http_logging_policy",
              "azure.identity", "azure.identity._credentials",
              "msal", "urllib3"):
    logging.getLogger(_name).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

VAULT_URL = os.environ.get("CRAWL_KV_URL", "https://crawlkeyvault.vault.azure.net/")

_client = None
_cache = {}


def _get_client():
    global _client
    if _client is not None:
        return _client
    try:
        from azure.identity import ManagedIdentityCredential
        from azure.keyvault.secrets import SecretClient
        # Container Apps + user-assigned MI needs the client_id explicitly.
        # On crawldevvm with system-assigned MI, AZURE_CLIENT_ID is unset and
        # ManagedIdentityCredential() finds the system MI via IMDS — same code
        # path as before, no behavioural change for the VM.
        client_id = os.environ.get("AZURE_CLIENT_ID")
        if client_id:
            cred = ManagedIdentityCredential(client_id=client_id)
        else:
            cred = ManagedIdentityCredential()
        _client = SecretClient(vault_url=VAULT_URL, credential=cred)
        # Test connectivity
        _client.get_secret("cir-api-key")
        logger.info("Key Vault connected: %s", VAULT_URL)
        return _client
    except Exception as e:
        logger.warning("Key Vault unavailable, using env/fallback: %s", e)
        _client = False  # Mark as failed so we don't retry every call
        return False


def get_secret(name: str, fallback: str = "") -> str:
    """Get a secret by name. Checks cache, then Key Vault, then env var, then fallback."""
    if name in _cache:
        return _cache[name]

    # Try Key Vault
    client = _get_client()
    if client:
        try:
            val = client.get_secret(name).value
            _cache[name] = val
            return val
        except Exception as e:
            logger.warning("Failed to read secret '%s' from vault: %s", name, e)

    # Fallback: env var (convert secret-name to SECRET_NAME)
    env_key = name.upper().replace("-", "_")
    val = os.environ.get(env_key, fallback)
    _cache[name] = val
    return val


def load_vm_tokens() -> dict:
    """Load all VM gateway tokens. Returns {region: token}."""
    regions = ["americas", "europe", "gulf", "china", "india"]
    return {r: get_secret(f"vm-token-{r}") for r in regions}


def load_db_config() -> dict:
    """Load database connection config."""
    return {
        "host": get_secret("db-host"),
        "dbname": get_secret("db-name"),
        "user": get_secret("db-user"),
        "password": get_secret("db-password"),
        "port": "5432",
        "sslmode": "require",
    }
