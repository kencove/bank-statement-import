# Copyright 2025 Kencove
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import hashlib
import hmac
import logging
import time

from odoo import _, http
from odoo.http import request

_logger = logging.getLogger(__name__)

try:
    from jose import jwt
    from jose.exceptions import JWTError

    JOSE_AVAILABLE = True
except ImportError:
    JOSE_AVAILABLE = False
    _logger.warning("python-jose library not installed. Webhook verification disabled.")

# Cache for Plaid webhook verification keys
# Keys are cached to avoid repeated API calls
_WEBHOOK_KEY_CACHE = {}


class PlaidController(http.Controller):
    def _get_plaid_client_for_webhook(self, item_id):
        """Get a Plaid client for webhook verification.

        Returns the provider and client, or (None, None) if not found.
        """
        provider = (
            request.env["online.bank.statement.provider"]
            .sudo()
            .search([("plaid_item_id", "=", item_id)], limit=1)
        )
        if not provider:
            return None, None

        try:
            client = provider._get_plaid_client()
            return provider, client
        except Exception:
            _logger.exception("Failed to get Plaid client for webhook verification")
            return provider, None

    def _verify_plaid_webhook(self, body, headers, provider):
        """Verify Plaid webhook signature.

        Plaid signs all webhooks with a JWT in the Plaid-Verification header.
        We verify the signature using Plaid's public key (fetched via API).

        Returns True if verification succeeds, False otherwise.
        """
        if not JOSE_AVAILABLE:
            _logger.warning("Skipping webhook verification - python-jose not installed")
            return True  # Allow webhook processing but log warning

        signed_jwt = headers.get("Plaid-Verification") or headers.get(
            "plaid-verification"
        )
        if not signed_jwt:
            _logger.warning("No Plaid-Verification header in webhook request")
            return False

        try:
            # Extract key ID from JWT header without validation
            unverified_header = jwt.get_unverified_header(signed_jwt)
            algorithm = unverified_header.get("alg")
            if algorithm != "ES256":
                _logger.warning(
                    "Unexpected JWT algorithm: %s (expected ES256)", algorithm
                )
                return False

            current_key_id = unverified_header.get("kid")
            if not current_key_id:
                _logger.warning("No key ID in JWT header")
                return False

            # Get verification key from cache or API
            key = self._get_webhook_verification_key(current_key_id, provider)
            if not key:
                _logger.warning(
                    "Could not get verification key for key_id: %s", current_key_id
                )
                return False

            # Check if key has expired
            if key.get("expired_at") is not None:
                _logger.warning("Verification key has expired: %s", current_key_id)
                return False

            # Verify JWT signature
            try:
                claims = jwt.decode(signed_jwt, key, algorithms=["ES256"])
            except JWTError as e:
                _logger.warning("JWT verification failed: %s", e)
                return False

            # Verify webhook is not too old (5 minute window)
            iat = claims.get("iat", 0)
            if iat < time.time() - 5 * 60:
                _logger.warning(
                    "Webhook is too old: iat=%s, current=%s", iat, time.time()
                )
                return False

            # Verify request body hash
            expected_hash = claims.get("request_body_sha256")
            if not expected_hash:
                _logger.warning("No request_body_sha256 in JWT claims")
                return False

            actual_hash = hashlib.sha256(body.encode()).hexdigest()
            if not hmac.compare_digest(actual_hash, expected_hash):
                _logger.warning(
                    "Webhook body hash mismatch: expected=%s, actual=%s",
                    expected_hash,
                    actual_hash,
                )
                return False

            return True

        except Exception:
            _logger.exception("Unexpected error during webhook verification")
            return False

    def _get_webhook_verification_key(self, key_id, provider):
        """Get webhook verification key from cache or Plaid API."""
        global _WEBHOOK_KEY_CACHE

        # Check cache first
        if key_id in _WEBHOOK_KEY_CACHE:
            return _WEBHOOK_KEY_CACHE[key_id]

        # Fetch from Plaid API
        try:
            from plaid.model.webhook_verification_key_get_request import (
                WebhookVerificationKeyGetRequest,
            )

            client = provider._get_plaid_client()
            request_obj = WebhookVerificationKeyGetRequest(key_id=key_id)
            response = client.webhook_verification_key_get(request_obj)
            key = response.key.to_dict()

            # Cache the key
            _WEBHOOK_KEY_CACHE[key_id] = key
            return key

        except Exception:
            _logger.exception("Failed to get webhook verification key: %s", key_id)
            return None

    @http.route("/plaid/exchange_token", type="json", auth="user")
    def exchange_token(self, public_token, provider_id, account_id, institution):
        """Exchange public token for access token after Plaid Link success"""
        provider = request.env["online.bank.statement.provider"].browse(provider_id)

        if not provider.exists():
            return {"success": False, "error": "Provider not found"}

        try:
            # Exchange public token for access token
            access_token, item_id = provider._plaid_exchange_token(public_token)

            # Store connection details
            provider.write(
                {
                    "plaid_access_token": access_token,
                    "plaid_item_id": item_id,
                    "plaid_account_id": account_id,
                    "plaid_institution_id": institution.get("institution_id")
                    if institution
                    else False,
                    "plaid_institution_name": institution.get("name")
                    if institution
                    else False,
                    "plaid_error_message": False,
                    "plaid_sync_cursor": False,  # Reset cursor for new connection
                }
            )

            # Fetch account details (type, subtype, name, etc.)
            provider._plaid_fetch_account_details()

            _logger.info(
                "Plaid connection established for provider %s (institution: %s)",
                provider.id,
                institution.get("name") if institution else "unknown",
            )

            return {"success": True}

        except Exception as e:
            _logger.exception("Failed to exchange Plaid token")
            provider.plaid_error_message = str(e)
            return {"success": False, "error": str(e)}

    @http.route("/plaid/webhook", type="json", auth="public", csrf=False)
    def plaid_webhook(self, **kwargs):
        """Handle Plaid webhooks for status updates

        Plaid can notify us of:
        - ITEM_LOGIN_REQUIRED: User needs to re-authenticate
        - TRANSACTIONS_REMOVED: Historical transactions were removed
        - INITIAL_UPDATE: Initial transaction pull complete
        - HISTORICAL_UPDATE: Historical transactions available
        - DEFAULT_UPDATE: New transactions available
        """
        webhook_type = kwargs.get("webhook_type")
        webhook_code = kwargs.get("webhook_code")
        item_id = kwargs.get("item_id")

        _logger.info(
            "Received Plaid webhook: type=%s, code=%s, item_id=%s",
            webhook_type,
            webhook_code,
            item_id,
        )

        if not item_id:
            return {"status": "ignored", "reason": "no item_id"}

        # Find provider by item_id
        provider = (
            request.env["online.bank.statement.provider"]
            .sudo()
            .search([("plaid_item_id", "=", item_id)], limit=1)
        )

        if not provider:
            _logger.warning("No provider found for Plaid item_id: %s", item_id)
            return {"status": "ignored", "reason": "provider not found"}

        # Verify webhook signature
        raw_body = request.httprequest.get_data(as_text=True)
        headers = dict(request.httprequest.headers)
        if not self._verify_plaid_webhook(raw_body, headers, provider):
            _logger.warning("Webhook verification failed for item_id: %s", item_id)
            return {"status": "rejected", "reason": "verification failed"}

        if webhook_type == "ITEM" and webhook_code == "ERROR":
            # Item has an error - mark for reconnection
            error = kwargs.get("error", {})
            provider.plaid_error_message = error.get("error_message", "Unknown error")
            _logger.warning(
                "Plaid item error for provider %s: %s",
                provider.id,
                provider.plaid_error_message,
            )

        elif webhook_type == "ITEM" and webhook_code == "PENDING_EXPIRATION":
            # Access token expiring soon - notify user
            provider.message_post(
                body=_("Plaid connection will expire soon. Please reconnect."),
                message_type="notification",
            )

        elif webhook_type == "TRANSACTIONS":
            if webhook_code in (
                "INITIAL_UPDATE",
                "HISTORICAL_UPDATE",
                "DEFAULT_UPDATE",
            ):
                # New transactions available - could trigger automatic pull
                _logger.info(
                    "Transactions update for provider %s: %s",
                    provider.id,
                    webhook_code,
                )
            elif webhook_code == "TRANSACTIONS_REMOVED":
                # Transactions were removed - reset cursor
                removed_ids = kwargs.get("removed_transactions", [])
                _logger.info(
                    "Transactions removed for provider %s: %s",
                    provider.id,
                    removed_ids,
                )

        return {"status": "received"}
