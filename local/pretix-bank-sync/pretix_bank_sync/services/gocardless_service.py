"""
GoCardless Bank Account Data API service.

Handles OAuth flow, API calls, and transaction fetching.
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.utils.timezone import now

logger = logging.getLogger(__name__)


class GoCardlessService:
    """
    Service for interacting with GoCardless Bank Account Data API.
    """

    # API endpoints
    SANDBOX_BASE_URL = "https://bankaccountdata.gocardless.com/api/v2"
    PRODUCTION_BASE_URL = "https://bankaccountdata.gocardless.com/api/v2"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, sandbox: bool = True):
        """
        Initialize GoCardless service.

        Args:
            client_id: GoCardless client ID
            client_secret: GoCardless client secret
            redirect_uri: OAuth redirect URI
            sandbox: Use sandbox environment (default: True)
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.sandbox = sandbox
        self.base_url = self.SANDBOX_BASE_URL if sandbox else self.PRODUCTION_BASE_URL

    def _get_access_token(self) -> Optional[str]:
        """
        Get access token using client credentials.

        Returns:
            Access token or None if authentication fails
        """
        url = f"{self.base_url}/token/new/"
        data = {
            "secret_id": self.client_id,
            "secret_key": self.client_secret,
        }

        try:
            response = requests.post(url, json=data, timeout=30)
            response.raise_for_status()
            token_data = response.json()
            return token_data.get("access")
        except requests.RequestException as e:
            logger.error(f"Failed to get GoCardless access token: {e}")
            return None

    def create_requisition_link(
        self,
        redirect_url: str,
        institution_id: Optional[str] = None,
        agreement: Optional[str] = None
    ) -> Dict:
        """
        Create a requisition link for bank authorization.

        Args:
            redirect_url: URL to redirect to after authorization
            institution_id: Optional institution ID (if user already selected bank)
            agreement: Optional agreement ID for reauthorization

        Returns:
            Dict with 'id' (requisition_id) and 'link' (authorization URL)
        """
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Failed to authenticate with GoCardless")

        url = f"{self.base_url}/requisitions/"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        data = {
            "redirect": redirect_url,
        }
        if institution_id:
            data["institution_id"] = institution_id
        if agreement:
            data["agreement"] = agreement

        try:
            response = requests.post(url, json=data, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to create requisition: {e}")
            raise Exception(f"Failed to create requisition: {e}")

    def get_requisition(self, requisition_id: str) -> Dict:
        """
        Get requisition details.

        Args:
            requisition_id: The requisition ID

        Returns:
            Requisition data
        """
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Failed to authenticate with GoCardless")

        url = f"{self.base_url}/requisitions/{requisition_id}/"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to get requisition {requisition_id}: {e}")
            raise Exception(f"Failed to get requisition: {e}")

    def get_accounts(self, requisition_id: str) -> List[Dict]:
        """
        Get accounts for a requisition.

        Args:
            requisition_id: The requisition ID

        Returns:
            List of account data
        """
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Failed to authenticate with GoCardless")

        url = f"{self.base_url}/requisitions/{requisition_id}/"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            requisition_data = response.json()
            account_ids = requisition_data.get("accounts", [])

            accounts = []
            for account_id in account_ids:
                account_data = self.get_account_details(account_id)
                if account_data:
                    accounts.append(account_data)

            return accounts
        except requests.RequestException as e:
            logger.error(f"Failed to get accounts for requisition {requisition_id}: {e}")
            raise Exception(f"Failed to get accounts: {e}")

    def get_account_details(self, account_id: str) -> Optional[Dict]:
        """
        Get account details.

        Args:
            account_id: The account ID

        Returns:
            Account data or None
        """
        access_token = self._get_access_token()
        if not access_token:
            return None

        url = f"{self.base_url}/accounts/{account_id}/"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to get account {account_id}: {e}")
            return None

    def get_transactions(
        self,
        account_id: str,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None
    ) -> List[Dict]:
        """
        Get transactions for an account.

        Args:
            account_id: The account ID
            date_from: Start date (default: 90 days ago)
            date_to: End date (default: today)

        Returns:
            List of transaction data
        """
        access_token = self._get_access_token()
        if not access_token:
            raise Exception("Failed to authenticate with GoCardless")

        if date_from is None:
            date_from = now() - timedelta(days=90)
        if date_to is None:
            date_to = now()

        url = f"{self.base_url}/accounts/{account_id}/transactions/"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }
        params = {
            "date_from": date_from.strftime("%Y-%m-%d"),
            "date_to": date_to.strftime("%Y-%m-%d"),
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get("transactions", {}).get("booked", [])
        except requests.RequestException as e:
            logger.error(f"Failed to get transactions for account {account_id}: {e}")
            raise Exception(f"Failed to get transactions: {e}")

    def get_consent_status(self, requisition_id: str) -> Dict:
        """
        Get consent status for a requisition.

        Args:
            requisition_id: The requisition ID

        Returns:
            Consent status data
        """
        requisition = self.get_requisition(requisition_id)
        return {
            "status": requisition.get("status"),
            "consent_id": requisition.get("consent_id"),
            "expires_at": requisition.get("expires_at"),
        }

    def normalize_transaction(self, transaction_data: Dict, account_id: str) -> Dict:
        """
        Normalize transaction data from GoCardless format.

        Args:
            transaction_data: Raw transaction data from GoCardless
            account_id: Account ID

        Returns:
            Normalized transaction data
        """
        # Extract amount
        amount_str = transaction_data.get("transactionAmount", {}).get("amount", "0")
        amount = Decimal(amount_str)

        # Extract currency
        currency = transaction_data.get("transactionAmount", {}).get("currency", "")

        # Extract dates
        date_str = transaction_data.get("bookingDate") or transaction_data.get("valueDate", "")
        date = None
        if date_str:
            try:
                date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass

        booking_date_str = transaction_data.get("bookingDate", "")
        booking_date = None
        if booking_date_str:
            try:
                booking_date = datetime.strptime(booking_date_str, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                pass

        # Extract remittance information
        remittance_unstructured = transaction_data.get("remittanceInformationUnstructured", "")
        remittance_structured = transaction_data.get("remittanceInformationStructured", "")
        reference = " ".join(filter(None, [remittance_structured, remittance_unstructured]))

        # Extract party information
        debtor_account = transaction_data.get("debtorAccount", {})
        creditor_account = transaction_data.get("creditorAccount", {})

        return {
            "transaction_id": transaction_data.get("transactionId", ""),
            "account_id": account_id,
            "amount": amount,
            "currency": currency,
            "date": date,
            "booking_date": booking_date,
            "remittance_information_unstructured": remittance_unstructured,
            "remittance_information_structured": remittance_structured,
            "reference": reference,
            "debtor_name": transaction_data.get("debtorName", ""),
            "debtor_account_iban": debtor_account.get("iban", ""),
            "creditor_name": transaction_data.get("creditorName", ""),
            "creditor_account_iban": creditor_account.get("iban", ""),
            "raw_data": transaction_data,
        }
