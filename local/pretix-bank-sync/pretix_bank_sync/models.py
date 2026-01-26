"""
Models for Bank Sync plugin.

Stores bank connections and synced transactions from GoCardless.
"""
import hashlib
import re
from decimal import Decimal

from django.db import models
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from pretix.base.models.base import LoggedModel


class BankConnection(LoggedModel):
    """
    Represents a GoCardless bank account connection for an organizer.
    """
    STATUS_PENDING = 'pending'
    STATUS_ACTIVE = 'active'
    STATUS_EXPIRED = 'expired'
    STATUS_ERROR = 'error'
    STATUS_REVOKED = 'revoked'

    STATUS_CHOICES = (
        (STATUS_PENDING, _('Pending authorization')),
        (STATUS_ACTIVE, _('Active')),
        (STATUS_EXPIRED, _('Expired - reauthorization required')),
        (STATUS_ERROR, _('Error')),
        (STATUS_REVOKED, _('Revoked')),
    )

    organizer = models.ForeignKey(
        'pretixbase.Organizer',
        on_delete=models.CASCADE,
        related_name='bank_connections',
        help_text=_("The organizer this bank connection belongs to")
    )

    # GoCardless identifiers
    requisition_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text=_("GoCardless requisition ID")
    )

    # Connection status
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True
    )

    # Sync tracking
    last_sync = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Last successful sync timestamp")
    )
    sync_count_today = models.IntegerField(
        default=0,
        help_text=_("Number of syncs performed today")
    )
    last_sync_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("Date of last sync (for resetting daily counter)")
    )

    # Consent management
    consent_expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the bank consent expires")
    )
    consent_id = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("GoCardless consent ID")
    )

    # Error tracking
    last_error = models.TextField(
        blank=True,
        help_text=_("Last error message if status is error")
    )
    last_error_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When the last error occurred")
    )

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-created',)
        verbose_name = _("Bank Connection")
        verbose_name_plural = _("Bank Connections")

    def __str__(self):
        return f"Bank Connection for {self.organizer.name} ({self.status})"

    def can_sync(self):
        """Check if connection can perform a sync (not expired, under daily limit)."""
        if self.status != self.STATUS_ACTIVE:
            return False

        today = now().date()
        if self.last_sync_date != today:
            # Reset counter for new day
            return True

        return self.sync_count_today < 4

    def reset_daily_counter(self):
        """Reset the daily sync counter."""
        today = now().date()
        if self.last_sync_date != today:
            self.sync_count_today = 0
            self.last_sync_date = today
            self.save(update_fields=['sync_count_today', 'last_sync_date'])


class BankTransaction(LoggedModel):
    """
    Represents a bank transaction synced from GoCardless.
    """
    STATE_UNCHECKED = 'unchecked'
    STATE_NOMATCH = 'nomatch'
    STATE_MATCHED = 'matched'
    STATE_PENDING_APPROVAL = 'pending_approval'
    STATE_ERROR = 'error'
    STATE_DUPLICATE = 'duplicate'
    STATE_DISCARDED = 'discarded'

    STATE_CHOICES = (
        (STATE_UNCHECKED, _('Unchecked')),
        (STATE_NOMATCH, _('No match found')),
        (STATE_MATCHED, _('Matched to order')),
        (STATE_PENDING_APPROVAL, _('Pending approval')),
        (STATE_ERROR, _('Error')),
        (STATE_DUPLICATE, _('Duplicate transaction')),
        (STATE_DISCARDED, _('Manually discarded')),
    )

    connection = models.ForeignKey(
        BankConnection,
        on_delete=models.CASCADE,
        related_name='transactions',
        help_text=_("The bank connection this transaction belongs to")
    )

    # GoCardless transaction data
    transaction_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text=_("GoCardless transaction ID")
    )
    account_id = models.CharField(
        max_length=255,
        db_index=True,
        help_text=_("GoCardless account ID")
    )

    # Transaction details
    amount = models.DecimalField(
        max_digits=13,
        decimal_places=2,
        help_text=_("Transaction amount")
    )
    currency = models.CharField(
        max_length=10,
        help_text=_("Transaction currency")
    )
    date = models.DateField(
        help_text=_("Transaction date")
    )
    booking_date = models.DateField(
        null=True,
        blank=True,
        help_text=_("Booking date")
    )

    # Reference information
    remittance_information_unstructured = models.TextField(
        blank=True,
        help_text=_("Unstructured remittance information")
    )
    remittance_information_structured = models.TextField(
        blank=True,
        help_text=_("Structured remittance information")
    )
    reference = models.TextField(
        blank=True,
        help_text=_("Combined reference information for matching")
    )

    # Additional transaction data
    debtor_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Debtor name")
    )
    debtor_account_iban = models.CharField(
        max_length=34,
        blank=True,
        help_text=_("Debtor IBAN")
    )
    creditor_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_("Creditor name")
    )
    creditor_account_iban = models.CharField(
        max_length=34,
        blank=True,
        help_text=_("Creditor IBAN")
    )

    # Matching state
    state = models.CharField(
        max_length=20,
        choices=STATE_CHOICES,
        default=STATE_UNCHECKED,
        db_index=True
    )
    order = models.ForeignKey(
        'pretixbase.Order',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bank_transactions',
        help_text=_("Matched order")
    )
    payment = models.ForeignKey(
        'pretixbase.OrderPayment',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bank_transactions',
        help_text=_("Created payment object")
    )

    # Error tracking
    error_message = models.TextField(
        blank=True,
        help_text=_("Error message if matching failed")
    )

    # Partial payment tracking
    is_partial_payment = models.BooleanField(
        default=False,
        help_text=_("Whether this transaction is part of a partial payment")
    )
    payment_group_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Group ID for transactions that belong to the same payment")
    )

    # Metadata
    raw_data = models.JSONField(
        default=dict,
        help_text=_("Raw transaction data from GoCardless")
    )

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-date', '-created')
        verbose_name = _("Bank Transaction")
        verbose_name_plural = _("Bank Transactions")
        indexes = [
            models.Index(fields=['connection', 'state']),
            models.Index(fields=['date', 'state']),
        ]

    def __str__(self):
        return f"Transaction {self.transaction_id} - {self.amount} {self.currency}"

    def get_reference_text(self):
        """Get combined reference text for matching."""
        if self.reference:
            return self.reference
        # Combine structured and unstructured remittance info
        parts = []
        if self.remittance_information_structured:
            parts.append(self.remittance_information_structured)
        if self.remittance_information_unstructured:
            parts.append(self.remittance_information_unstructured)
        return " ".join(parts)

    def calculate_checksum(self):
        """Calculate checksum for duplicate detection."""
        clean = re.compile('[^a-zA-Z0-9.-]')
        hasher = hashlib.sha1()
        hasher.update(clean.sub('', str(self.transaction_id)).encode('utf-8'))
        hasher.update(clean.sub('', str(self.amount)).encode('utf-8'))
        hasher.update(clean.sub('', str(self.date)).encode('utf-8'))
        return hasher.hexdigest()

    def get_sender_name(self):
        """Get the sender name (debtor or creditor depending on transaction direction)."""
        # For incoming payments, debtor_name is the sender
        # For outgoing payments, creditor_name is the recipient
        # We'll use debtor_name as default for matching
        return self.debtor_name or self.creditor_name


class TransactionMatchSuggestion(LoggedModel):
    """
    Stores match suggestions for transactions that need human approval.
    """
    MATCH_TYPE_EXACT_CODE = 'exact_code'
    MATCH_TYPE_FUZZY_CODE = 'fuzzy_code'
    MATCH_TYPE_SENDER_NAME = 'sender_name'
    MATCH_TYPE_AMOUNT_ONLY = 'amount_only'

    MATCH_TYPE_CHOICES = (
        (MATCH_TYPE_EXACT_CODE, _('Exact order code match')),
        (MATCH_TYPE_FUZZY_CODE, _('Order code within other words')),
        (MATCH_TYPE_SENDER_NAME, _('Sender name match')),
        (MATCH_TYPE_AMOUNT_ONLY, _('Amount match only')),
    )

    transaction = models.ForeignKey(
        BankTransaction,
        on_delete=models.CASCADE,
        related_name='match_suggestions',
        help_text=_("The transaction this suggestion is for")
    )
    order = models.ForeignKey(
        'pretixbase.Order',
        on_delete=models.CASCADE,
        related_name='transaction_match_suggestions',
        help_text=_("The suggested order match")
    )

    # Match quality metrics
    match_type = models.CharField(
        max_length=20,
        choices=MATCH_TYPE_CHOICES,
        help_text=_("Type of match")
    )
    confidence_score = models.FloatField(
        help_text=_("Confidence score (0.0 to 1.0)")
    )
    match_reason = models.TextField(
        help_text=_("Explanation of why this match was suggested")
    )

    # Amount matching
    amount_match = models.BooleanField(
        default=False,
        help_text=_("Whether the amount matches exactly")
    )
    amount_difference = models.DecimalField(
        max_digits=13,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Difference between transaction amount and order pending amount")
    )

    # Status
    is_approved = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text=_("Whether this suggestion was approved (None = pending, True = approved, False = rejected)")
    )
    reviewed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When this suggestion was reviewed")
    )
    reviewed_by = models.ForeignKey(
        'pretixbase.User',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=_("User who reviewed this suggestion")
    )

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-confidence_score', '-created')
        verbose_name = _("Transaction Match Suggestion")
        verbose_name_plural = _("Transaction Match Suggestions")
        indexes = [
            models.Index(fields=['transaction', 'is_approved']),
            models.Index(fields=['order', 'is_approved']),
        ]

    def __str__(self):
        return f"Match suggestion: {self.transaction.transaction_id} -> {self.order.code} (score: {self.confidence_score:.2f})"
