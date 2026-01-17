# Copyright 2025 Kencove
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import hashlib
import time
from datetime import date, datetime
from unittest import mock

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import common

_module_ns = "odoo.addons.account_statement_import_online_plaid"
_provider_class = (
    _module_ns
    + ".models.online_bank_statement_provider_plaid"
    + ".OnlineBankStatementProviderPlaid"
)
_controller_class = _module_ns + ".controllers.plaid_callback"


class MockPlaidTransaction:
    """Mock Plaid transaction object"""

    def __init__(self, **kwargs):
        self.transaction_id = kwargs.get("transaction_id", "txn-123")
        self.pending = kwargs.get("pending", False)
        self.amount = kwargs.get("amount", 100.0)
        self.date = kwargs.get("date", date(2024, 1, 15))
        self.name = kwargs.get("name", "Test Merchant")
        self.merchant_name = kwargs.get("merchant_name", "Test Merchant")
        self.check_number = kwargs.get("check_number")
        self.personal_finance_category = kwargs.get("personal_finance_category")


class MockPlaidAccount:
    """Mock Plaid account object"""

    def __init__(self, **kwargs):
        self.account_id = kwargs.get("account_id", "acct-123")
        self.name = kwargs.get("name", "Checking Account")
        self.type = kwargs.get("type", "depository")
        self.subtype = kwargs.get("subtype", "checking")
        self.mask = kwargs.get("mask", "1234")
        self.balances = mock.MagicMock()
        self.balances.current = kwargs.get("balance", 5000.0)


class TestAccountBankStatementImportOnlinePlaid(common.TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.currency_usd = cls.env.ref("base.USD")
        cls.currency_usd.write({"active": True})

        cls.now = fields.Datetime.now()

        # Create bank account
        cls.bank_account = cls.env["res.partner.bank"].create(
            {
                "acc_number": "123456789",
                "partner_id": cls.env.company.partner_id.id,
            }
        )

        # Create journal
        cls.journal = cls.env["account.journal"].create(
            {
                "name": "Plaid Bank Test",
                "type": "bank",
                "code": "PLDT",
                "currency_id": cls.currency_usd.id,
                "bank_statements_source": "online",
                "online_bank_statement_provider": "plaid",
                "bank_account_id": cls.bank_account.id,
            }
        )

        cls.provider = cls.journal.online_bank_statement_provider_id
        cls.provider.write(
            {
                "plaid_client_id": "test_client_id",
                "plaid_secret": "test_secret",
                "plaid_environment": "sandbox",
                "plaid_access_token": "test_access_token",
                "plaid_item_id": "test_item_id",
                "plaid_account_id": "acct-123",
                "plaid_account_type": "depository",
            }
        )

    def test_service_registration(self):
        """Test that Plaid is registered as an available service"""
        services = self.provider._get_available_services()
        service_codes = [s[0] for s in services]
        self.assertIn("plaid", service_codes)

    def test_connection_status_disconnected(self):
        """Test connection status when not connected"""
        # Clear connection fields to test disconnected status
        self.provider.write(
            {
                "plaid_access_token": False,
                "plaid_account_id": False,
            }
        )
        self.assertEqual(self.provider.plaid_connection_status, "disconnected")
        # Restore for other tests
        self.provider.write(
            {
                "plaid_access_token": "test_access_token",
                "plaid_account_id": "acct-123",
            }
        )

    def test_connection_status_connected(self):
        """Test connection status when connected"""
        self.assertEqual(self.provider.plaid_connection_status, "connected")

    def test_connection_status_error(self):
        """Test connection status when there's an error"""
        self.provider.plaid_error_message = "Test error"
        self.assertEqual(self.provider.plaid_connection_status, "error")

    def test_transaction_sign_inversion(self):
        """Test that Plaid amounts are inverted (positive -> negative for expenses)"""
        transaction = MockPlaidTransaction(
            transaction_id="txn-456",
            amount=100.0,  # Plaid: positive = expense
            name="Coffee Shop",
            merchant_name="Starbucks",
        )

        line = self.provider._plaid_transaction_to_line(transaction)

        # Odoo: negative = expense
        self.assertEqual(line["amount"], -100.0)
        self.assertEqual(line["unique_import_id"], "txn-456")
        self.assertEqual(line["payment_ref"], "Starbucks")

    def test_pending_transaction_unique_id(self):
        """Test that pending transactions get a different unique_import_id"""
        transaction = MockPlaidTransaction(
            transaction_id="txn-789",
            pending=True,
            amount=50.0,
        )

        line = self.provider._plaid_transaction_to_line(transaction)

        self.assertEqual(line["unique_import_id"], "pending-txn-789")

    def test_check_number_preserved(self):
        """Test that check numbers are preserved in payment_ref and ref"""
        transaction = MockPlaidTransaction(
            transaction_id="txn-check",
            amount=500.0,
            name="Check Payment",
            merchant_name=None,
            check_number="1234",
        )

        line = self.provider._plaid_transaction_to_line(transaction)

        self.assertIn("#1234", line["payment_ref"])
        self.assertEqual(line["ref"], "1234")

    def test_category_mapping(self):
        """Test that Plaid categories are mapped to accounts"""
        # Find an expense account to use for mapping
        account = self.env["account.account"].search(
            [
                ("account_type", "=", "expense"),
                ("company_id", "=", self.env.company.id),
            ],
            limit=1,
        )

        if account:
            # Update the existing FOOD_AND_DRINK mapping (created via data XML)
            # instead of creating a new one to avoid unique constraint violation
            mapping = self.env["plaid.category.mapping"].search(
                [("plaid_category", "=", "FOOD_AND_DRINK")],
                limit=1,
            )
            if mapping:
                mapping.account_id = account
            else:
                # Fallback: create if somehow it doesn't exist
                mapping = self.env["plaid.category.mapping"].create(
                    {
                        "name": "Test Category",
                        "plaid_category": "FOOD_AND_DRINK",
                        "account_id": account.id,
                        "company_id": False,
                    }
                )

            # Create a proper mock for personal_finance_category
            # Note: MagicMock(attr=value) doesn't set attributes, we need to do it explicitly
            category_mock = mock.MagicMock()
            category_mock.primary = "FOOD_AND_DRINK"
            category_mock.detailed = "FOOD_AND_DRINK_RESTAURANTS"

            transaction = MockPlaidTransaction(
                transaction_id="txn-food",
                amount=25.0,
                name="Restaurant",
                personal_finance_category=category_mock,
            )

            line = self.provider._plaid_transaction_to_line(transaction)

            self.assertEqual(line.get("counterpart_account_id"), account.id)

    def test_obtain_statement_data_not_connected(self):
        """Test error when trying to sync without connection"""
        # Temporarily clear access token
        original_token = self.provider.plaid_access_token
        self.provider.plaid_access_token = False

        with self.assertRaises(UserError):
            self.provider._plaid_obtain_statement_data(
                datetime(2024, 1, 1),
                datetime(2024, 1, 31),
            )

        # Restore for other tests
        self.provider.plaid_access_token = original_token

    def mock_plaid_get_transactions(self):
        """Create mock for _plaid_get_transactions"""
        transactions = [
            MockPlaidTransaction(
                transaction_id="txn-001",
                amount=100.0,
                date=date(2024, 1, 15),
                name="Purchase 1",
                merchant_name="Store A",
            ),
            MockPlaidTransaction(
                transaction_id="txn-002",
                amount=-500.0,  # Credit/deposit
                date=date(2024, 1, 16),
                name="Deposit",
                merchant_name=None,
            ),
        ]
        return mock.patch(
            _provider_class + "._plaid_get_transactions",
            return_value=transactions,
        )

    def mock_plaid_get_balance(self):
        """Create mock for _plaid_get_balance"""
        return mock.patch(
            _provider_class + "._plaid_get_balance",
            return_value=5000.0,
        )

    def test_obtain_statement_data(self):
        """Test obtaining statement data from mocked Plaid API"""
        with self.mock_plaid_get_transactions(), self.mock_plaid_get_balance():
            lines, statement_values = self.provider._plaid_obtain_statement_data(
                datetime(2024, 1, 1),
                datetime(2024, 1, 31),
            )

        self.assertEqual(len(lines), 2)
        self.assertEqual(statement_values.get("balance_end_real"), 5000.0)

        # Check first transaction (expense)
        self.assertEqual(lines[0]["amount"], -100.0)
        self.assertEqual(lines[0]["unique_import_id"], "txn-001")

        # Check second transaction (deposit - sign inverted)
        self.assertEqual(lines[1]["amount"], 500.0)  # -(-500) = 500
        self.assertEqual(lines[1]["unique_import_id"], "txn-002")

    def test_credit_card_balance_inversion(self):
        """Test that credit card balances are inverted"""
        self.provider.plaid_account_type = "credit"

        with mock.patch(_provider_class + "._get_plaid_client") as mock_client:
            mock_api = mock.MagicMock()
            mock_client.return_value = mock_api

            mock_account = MockPlaidAccount(
                account_id="acct-123",
                balance=1500.0,  # Plaid: positive = amount owed
            )
            mock_response = mock.MagicMock()
            mock_response.accounts = [mock_account]
            mock_api.accounts_balance_get.return_value = mock_response

            balance = self.provider._plaid_get_balance()

            # Odoo: negative = credit card debt
            self.assertEqual(balance, -1500.0)

    def test_disconnect(self):
        """Test disconnecting Plaid connection"""
        self.provider.action_plaid_disconnect()

        self.assertFalse(self.provider.plaid_access_token)
        self.assertFalse(self.provider.plaid_item_id)
        self.assertFalse(self.provider.plaid_account_id)
        self.assertEqual(self.provider.plaid_connection_status, "disconnected")

    def test_reset_cursor(self):
        """Test resetting sync cursor"""
        self.provider.plaid_sync_cursor = "test-cursor"
        self.provider.action_plaid_reset_cursor()

        self.assertFalse(self.provider.plaid_sync_cursor)


class TestPlaidCategoryMapping(common.TransactionCase):
    def test_category_mapping_creation(self):
        """Test creating a category mapping"""
        mapping = self.env["plaid.category.mapping"].create(
            {
                "name": "Food and Drink",
                "plaid_category": "FOOD_AND_DRINK",
            }
        )

        self.assertTrue(mapping.exists())
        self.assertTrue(mapping.active)

    def test_category_mapping_unique_constraint(self):
        """Test that duplicate mappings are prevented"""
        # Unique constraint: (plaid_category, plaid_category_detailed, company_id)
        # PostgreSQL doesn't consider two NULLs equal, so set plaid_category_detailed
        self.env["plaid.category.mapping"].create(
            {
                "name": "Travel 1",
                "plaid_category": "TRAVEL",
                "plaid_category_detailed": "TRAVEL_FLIGHTS",
            }
        )

        with self.assertRaises(IntegrityError):
            self.env["plaid.category.mapping"].create(
                {
                    "name": "Travel 2",
                    "plaid_category": "TRAVEL",
                    "plaid_category_detailed": "TRAVEL_FLIGHTS",
                }
            )


class TestPlaidWebhookVerification(common.TransactionCase):
    """Tests for Plaid webhook signature verification"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Import controller for testing (use relative import per OCA guidelines)
        from ..controllers.plaid_callback import PlaidController

        cls.controller = PlaidController()

        # Create a provider for testing
        cls.currency_usd = cls.env.ref("base.USD")
        cls.currency_usd.write({"active": True})

        cls.bank_account = cls.env["res.partner.bank"].create(
            {
                "acc_number": "987654321",
                "partner_id": cls.env.company.partner_id.id,
            }
        )

        cls.journal = cls.env["account.journal"].create(
            {
                "name": "Plaid Webhook Test",
                "type": "bank",
                "code": "PLWH",
                "currency_id": cls.currency_usd.id,
                "bank_statements_source": "online",
                "online_bank_statement_provider": "plaid",
                "bank_account_id": cls.bank_account.id,
            }
        )

        cls.provider = cls.journal.online_bank_statement_provider_id
        cls.provider.write(
            {
                "plaid_client_id": "test_client_id",
                "plaid_secret": "test_secret",
                "plaid_environment": "sandbox",
                "plaid_access_token": "test_access_token",
                "plaid_item_id": "webhook_test_item",
                "plaid_account_id": "acct-webhook",
            }
        )

    def test_webhook_verification_missing_header(self):
        """Test that webhook is rejected when Plaid-Verification header is missing"""
        body = '{"webhook_type": "TRANSACTIONS", "item_id": "test"}'
        headers = {}  # No Plaid-Verification header

        # Mock JOSE_AVAILABLE to True to test verification logic
        with mock.patch(_controller_class + ".JOSE_AVAILABLE", True):
            result = self.controller._verify_plaid_webhook(body, headers, self.provider)

        self.assertFalse(result)

    def test_webhook_verification_invalid_algorithm(self):
        """Test that webhook is rejected with non-ES256 algorithm"""
        body = '{"webhook_type": "TRANSACTIONS", "item_id": "test"}'

        # Mock JWT with wrong algorithm
        with mock.patch(_controller_class + ".JOSE_AVAILABLE", True), mock.patch(
            _controller_class + ".jwt"
        ) as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {
                "alg": "RS256",  # Wrong algorithm
                "kid": "test-key-id",
            }

            headers = {"Plaid-Verification": "fake.jwt.token"}
            result = self.controller._verify_plaid_webhook(body, headers, self.provider)

        self.assertFalse(result)

    def test_webhook_verification_expired(self):
        """Test that webhook is rejected when too old (> 5 minutes)"""
        body = '{"webhook_type": "TRANSACTIONS", "item_id": "test"}'
        body_hash = hashlib.sha256(body.encode()).hexdigest()

        # Mock JWT with old timestamp
        with mock.patch(_controller_class + ".JOSE_AVAILABLE", True), mock.patch(
            _controller_class + ".jwt"
        ) as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {
                "alg": "ES256",
                "kid": "test-key-id",
            }
            # IAT is 10 minutes ago (too old)
            mock_jwt.decode.return_value = {
                "iat": time.time() - 600,
                "request_body_sha256": body_hash,
            }

            # Mock key retrieval
            with mock.patch.object(
                self.controller,
                "_get_webhook_verification_key",
                return_value={"expired_at": None, "key": "fake-key"},
            ):
                headers = {"Plaid-Verification": "fake.jwt.token"}
                result = self.controller._verify_plaid_webhook(
                    body, headers, self.provider
                )

        self.assertFalse(result)

    def test_webhook_verification_body_hash_mismatch(self):
        """Test that webhook is rejected when body hash doesn't match"""
        body = '{"webhook_type": "TRANSACTIONS", "item_id": "test"}'

        with mock.patch(_controller_class + ".JOSE_AVAILABLE", True), mock.patch(
            _controller_class + ".jwt"
        ) as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {
                "alg": "ES256",
                "kid": "test-key-id",
            }
            # Return wrong hash
            mock_jwt.decode.return_value = {
                "iat": time.time(),
                "request_body_sha256": "wrong_hash_value",
            }

            with mock.patch.object(
                self.controller,
                "_get_webhook_verification_key",
                return_value={"expired_at": None, "key": "fake-key"},
            ):
                headers = {"Plaid-Verification": "fake.jwt.token"}
                result = self.controller._verify_plaid_webhook(
                    body, headers, self.provider
                )

        self.assertFalse(result)

    def test_webhook_verification_success(self):
        """Test successful webhook verification"""
        body = '{"webhook_type": "TRANSACTIONS", "item_id": "test"}'
        body_hash = hashlib.sha256(body.encode()).hexdigest()

        with mock.patch(_controller_class + ".JOSE_AVAILABLE", True), mock.patch(
            _controller_class + ".jwt"
        ) as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {
                "alg": "ES256",
                "kid": "test-key-id",
            }
            mock_jwt.decode.return_value = {
                "iat": time.time(),  # Current time (valid)
                "request_body_sha256": body_hash,  # Correct hash
            }

            with mock.patch.object(
                self.controller,
                "_get_webhook_verification_key",
                return_value={"expired_at": None, "key": "fake-key"},
            ):
                headers = {"Plaid-Verification": "fake.jwt.token"}
                result = self.controller._verify_plaid_webhook(
                    body, headers, self.provider
                )

        self.assertTrue(result)

    def test_webhook_verification_expired_key(self):
        """Test that webhook is rejected when verification key has expired"""
        body = '{"webhook_type": "TRANSACTIONS", "item_id": "test"}'

        with mock.patch(_controller_class + ".JOSE_AVAILABLE", True), mock.patch(
            _controller_class + ".jwt"
        ) as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {
                "alg": "ES256",
                "kid": "test-key-id",
            }

            # Key has expired_at set (not None means expired)
            with mock.patch.object(
                self.controller,
                "_get_webhook_verification_key",
                return_value={"expired_at": "2024-01-01T00:00:00Z", "key": "fake-key"},
            ):
                headers = {"Plaid-Verification": "fake.jwt.token"}
                result = self.controller._verify_plaid_webhook(
                    body, headers, self.provider
                )

        self.assertFalse(result)

    def test_webhook_verification_skipped_when_jose_unavailable(self):
        """Test that webhook verification is skipped when jose library is unavailable"""
        body = '{"webhook_type": "TRANSACTIONS", "item_id": "test"}'
        headers = {}  # No verification header needed

        with mock.patch(_controller_class + ".JOSE_AVAILABLE", False):
            result = self.controller._verify_plaid_webhook(body, headers, self.provider)

        # Should return True (allow) when jose is unavailable
        self.assertTrue(result)
