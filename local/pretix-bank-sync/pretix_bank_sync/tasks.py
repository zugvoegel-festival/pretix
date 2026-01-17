"""
Tasks for Bank Sync plugin.

Handles periodic syncing and transaction matching.
"""
import logging
import operator
import re
from decimal import Decimal
from functools import reduce
from typing import List, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Min, Max
from django.db.models.functions import Length
from django.utils.timezone import now
from django.utils.translation import gettext_noop
from django_scopes import scope

from pretix.base.models import Order, Invoice, OrderPayment, OrderRefund, Quota
from pretix.base.payment import PaymentException
from pretix.base.services.mail import SendMailException
from pretix.base.services.orders import change_payment_provider
from pretix.base.services.tasks import TransactionAwareTask
from pretix.celery_app import app

from .models import BankConnection, BankTransaction
from .services.enablebanking_service import EnableBankingService

logger = logging.getLogger(__name__)


def _find_order_for_code(base_qs, code):
    """Find order by code with variations."""
    try_codes = [
        code,
        Order.normalize_code(code, is_fallback=True),
        code[:settings.ENTROPY['order_code']],
        Order.normalize_code(code[:settings.ENTROPY['order_code']], is_fallback=True)
    ]
    for c in try_codes:
        try:
            return base_qs.get(code=c)
        except Order.DoesNotExist:
            pass
    return None


def _find_order_for_invoice_id(base_qs, prefixes, number):
    """Find order by invoice number."""
    try:
        r = [
            Q(
                prefix__istartswith=prefix,
                full_invoice_no__iregex=prefix + r'[\- ]*0*' + number
            )
            for prefix in set(prefixes)
        ]
        return base_qs.select_related('order').get(
            reduce(operator.or_, r)
        ).order
    except (Invoice.DoesNotExist, Invoice.MultipleObjectsReturned):
        return None


@transaction.atomic
def _handle_transaction(trans: BankTransaction, matches: tuple, regex_match_to_slug, organizer):
    """Handle a matched transaction - create payment and confirm."""
    orders = []

    # Filter by currency first (primary matching criterion)
    qs = Order.objects.filter(
        event__organizer=organizer,
        event__currency=trans.currency
    )

    for slug, code in matches:
        original_slug = regex_match_to_slug.get(slug, slug)
        order = _find_order_for_code(
            qs.filter(Q(event__slug__iexact=slug) | Q(event__slug__iexact=original_slug)),
            code
        )
        if order:
            if order.code not in {o.code for o in orders}:
                orders.append(order)
        else:
            # Try invoice number match
            order = _find_order_for_invoice_id(
                Invoice.objects.filter(event__organizer=organizer),
                (slug, original_slug),
                code
            )
            if order and order.code not in {o.code for o in orders}:
                orders.append(order)

    if not orders:
        # No match
        trans.state = BankTransaction.STATE_NOMATCH
        trans.save()
        return

    trans.order = orders[0]

    if len(orders) > 1:
        # Multi-match! Can we split this automatically?
        order_pending_sum = sum(o.pending_sum for o in orders)
        if order_pending_sum != trans.amount:
            # we can't :( this needs to be dealt with by a human
            trans.state = BankTransaction.STATE_NOMATCH
            trans.error_message = gettext_noop('Automatic split to multiple orders not possible.')
            trans.save()
            return

        # we can!
        splits = [(o, o.pending_sum) for o in orders]
    else:
        splits = [(orders[0], trans.amount)]

    for o in orders:
        if o.status == Order.STATUS_PAID and o.pending_sum <= Decimal('0.00'):
            trans.state = BankTransaction.STATE_DUPLICATE
            trans.save()
            return
        elif o.status == Order.STATUS_CANCELED:
            trans.state = BankTransaction.STATE_ERROR
            trans.error_message = gettext_noop('The order has already been canceled.')
            trans.save()
            return

        if trans.currency != o.event.currency:
            trans.state = BankTransaction.STATE_ERROR
            trans.error_message = gettext_noop('Currencies do not match.')
            trans.save()
            return

    trans.state = BankTransaction.STATE_MATCHED
    for order, amount in splits:
        info_data = {
            'reference': trans.reference,
            'date': trans.date.isoformat() if trans.date else '',
            'debtor_name': trans.debtor_name,
            'creditor_name': trans.creditor_name,
            'iban': trans.debtor_account_iban or trans.creditor_account_iban,
            'full_amount': str(trans.amount),
            'trans_id': trans.pk,
            'transaction_id': trans.transaction_id,
        }

        if amount < Decimal("0.00"):
            # Handle refund
            pending_refund = order.refunds.filter(
                amount=-amount,
                provider__in=('manual', 'banktransfer'),
                state__in=(OrderRefund.REFUND_STATE_CREATED, OrderRefund.REFUND_STATE_TRANSIT),
            ).first()
            existing_payment = order.payments.filter(
                provider='banktransfer',
                state__in=(OrderPayment.PAYMENT_STATE_CONFIRMED,),
            ).first()
            if pending_refund:
                pending_refund.provider = "banktransfer"
                pending_refund.info_data = {
                    **pending_refund.info_data,
                    **info_data,
                }
                pending_refund.done()
            elif existing_payment:
                existing_payment.create_external_refund(
                    amount=-amount,
                    info=str(info_data)
                )
            else:
                r = order.refunds.create(
                    state=OrderRefund.REFUND_STATE_EXTERNAL,
                    source=OrderRefund.REFUND_SOURCE_EXTERNAL,
                    amount=-amount,
                    order=order,
                    execution_date=now(),
                    provider='banktransfer',
                    info=str(info_data)
                )
                order.log_action('pretix.event.order.refund.created.externally', {
                    'local_id': r.local_id,
                    'provider': r.provider,
                })
            continue

        try:
            p, created = order.payments.get_or_create(
                amount=amount,
                provider='banktransfer',
                state__in=(OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING),
                defaults={
                    'state': OrderPayment.PAYMENT_STATE_CREATED,
                }
            )
        except OrderPayment.MultipleObjectsReturned:
            created = False
            p = order.payments.filter(
                amount=amount,
                provider='banktransfer',
                state__in=(OrderPayment.PAYMENT_STATE_CREATED, OrderPayment.PAYMENT_STATE_PENDING),
            ).last()

        p.info_data = {
            **p.info_data,
            **info_data,
        }

        if created:
            # Perform payment method switching on-demand
            old_fee, new_fee, fee, p, new_invoice_created = change_payment_provider(
                order, p.payment_provider, p.amount, new_payment=p, create_log=False
            )
            if fee:
                p.fee = fee
                p.save(update_fields=['fee'])

        try:
            p.confirm()
            trans.payment = p
        except Quota.QuotaExceededException:
            logger.warning(f"Quota exceeded when confirming payment for order {order.code}")
        except SendMailException:
            logger.warning(f"Email send failed when confirming payment for order {order.code}")
        except Exception as e:
            logger.error(f"Error confirming payment for order {order.code}: {e}")
            trans.state = BankTransaction.STATE_ERROR
            trans.error_message = str(e)

    trans.save()


def match_transactions(connection: BankConnection):
    """Match unchecked transactions to orders."""
    # Get all unchecked transactions for this connection
    transactions = BankTransaction.objects.filter(
        connection=connection,
        state=BankTransaction.STATE_UNCHECKED
    )

    if not transactions.exists():
        return

    organizer = connection.organizer

    # Build matching patterns
    regex_match_to_slug = {}
    code_len_agg = Order.objects.filter(
        event__organizer=organizer
    ).annotate(
        clen=Length('code')
    ).aggregate(min=Min('clen'), max=Max('clen'))

    prefixes = set()
    for e in organizer.events.all():
        prefixes.add(e.slug.upper())
        if "-" in e.slug:
            prefixes.add(e.slug.upper().replace("-", ""))
            regex_match_to_slug[e.slug.upper().replace("-", "")] = e.slug

    # Match invoice numbers
    inr_len_agg = Invoice.objects.filter(
        event__organizer=organizer
    ).annotate(
        clen=Length('invoice_no')
    ).aggregate(min=Min('clen'), max=Max('clen'))

    invoice_prefixes = Invoice.objects.filter(event__organizer=organizer)
    for p in invoice_prefixes.order_by().distinct().values_list('prefix', flat=True):
        prefix = p.rstrip(" -")
        prefixes.add(prefix)
        if "-" in prefix:
            prefix_nodash = prefix.replace("-", "")
            prefixes.add(prefix_nodash)
            regex_match_to_slug[prefix_nodash] = prefix

    pattern = re.compile(
        "(%s)[ \\-_]*([A-Z0-9]{%s,%s})" % (
            "|".join(sorted(
                [re.escape(p).replace("\\-", r"[\- ]*") for p in prefixes],
                key=lambda p: len(p), reverse=True
            )),
            min(code_len_agg['min'] or 1, inr_len_agg['min'] or 1),
            max(code_len_agg['max'] or 5, inr_len_agg['max'] or 5)
        )
    )

    for trans in transactions:
        if trans.amount == Decimal("0.00"):
            # Ignore zero-valued transactions
            trans.state = BankTransaction.STATE_DISCARDED
            trans.save()
            continue

        # Get reference text
        reference = trans.get_reference_text().upper()

        # Try matching with and without whitespace
        matches_with_whitespace = pattern.findall(reference.replace("\n", " "))
        matches_without_whitespace = pattern.findall(reference.replace(" ", "").replace("\n", ""))

        if len(matches_without_whitespace) > len(matches_with_whitespace):
            matches = matches_without_whitespace
        else:
            matches = matches_with_whitespace

        if matches:
            _handle_transaction(trans, matches, regex_match_to_slug, organizer)
        else:
            trans.state = BankTransaction.STATE_NOMATCH
            trans.save()


@app.task(base=TransactionAwareTask, bind=True, max_retries=3, default_retry_delay=60)
def sync_bank_transactions(self, connection_id: int):
    """Sync transactions from Enable Banking for a bank connection."""
    with scope(organizer=None):
        try:
            connection = BankConnection.objects.get(pk=connection_id)
        except BankConnection.DoesNotExist:
            logger.error(f"Bank connection {connection_id} not found")
            return

    with scope(organizer=connection.organizer):
        # Reset daily counter if needed
        connection.reset_daily_counter()

        # Check if we can sync
        if not connection.can_sync():
            logger.warning(f"Cannot sync connection {connection_id}: limit reached or not active")
            return

        # Get settings
        organizer = connection.organizer
        application_id = organizer.settings.get('pretix_bank_sync_application_id', '')
        private_key_path = organizer.settings.get('pretix_bank_sync_private_key_path', '')
        redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')

        if not application_id or not private_key_path:
            logger.error(f"Enable Banking credentials not configured for organizer {organizer.slug}")
            connection.status = BankConnection.STATUS_ERROR
            connection.last_error = "Enable Banking credentials not configured"
            connection.last_error_at = now()
            connection.save(update_fields=['status', 'last_error', 'last_error_at'])
            return

        try:
            # Initialize service
            service = EnableBankingService(
                application_id=application_id,
                private_key_path=private_key_path,
                redirect_uri=redirect_uri
            )

            # Get account_uid (stored in requisition_id field)
            account_uid = connection.requisition_id
            if not account_uid:
                logger.error(f"No account UID found for connection {connection_id}")
                return

            # For Enable Banking, we work with individual accounts
            # The account_uid is stored in requisition_id
            accounts = [{"uid": account_uid}]
            if not accounts:
                logger.warning(f"No accounts found for connection {connection_id}")
                return

            # Get transactions for each account
            new_transactions = []
            for account in accounts:
                account_uid = account.get('uid', '')
                if not account_uid:
                    continue

                try:
                    transactions = service.get_transactions(account_uid)
                    for tx_data in transactions:
                        # Normalize transaction
                        normalized = service.normalize_transaction(tx_data, account_uid)

                        # Check if transaction already exists
                        transaction_id = normalized.get('transaction_id')
                        if not transaction_id:
                            continue

                        if BankTransaction.objects.filter(
                            transaction_id=transaction_id
                        ).exists():
                            continue

                        # Create transaction record
                        trans = BankTransaction(
                            connection=connection,
                            **normalized
                        )
                        trans.save()
                        new_transactions.append(trans)

                except Exception as e:
                    logger.error(f"Error fetching transactions for account {account_id}: {e}")
                    continue

            # Update connection
            connection.last_sync = now()
            connection.sync_count_today += 1
            connection.save(update_fields=['last_sync', 'sync_count_today'])

            # Match new transactions
            if new_transactions:
                logger.info(f"Synced {len(new_transactions)} new transactions for connection {connection_id}")
                match_transactions(connection)

            # Check if consent has expired
            if connection.consent_expires_at and connection.consent_expires_at < now():
                connection.status = BankConnection.STATUS_EXPIRED
                connection.save(update_fields=['status'])
                logger.warning(f"Connection {connection_id} consent has expired")

        except Exception as e:
            logger.error(f"Error syncing transactions for connection {connection_id}: {e}")
            connection.status = BankConnection.STATUS_ERROR
            connection.last_error = str(e)
            connection.last_error_at = now()
            connection.save(update_fields=['status', 'last_error', 'last_error_at'])
            raise
