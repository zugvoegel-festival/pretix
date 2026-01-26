"""
Signals for Bank Sync plugin.

Handles settings, navigation, and periodic tasks.
"""
import logging
from django.dispatch import receiver
from django.urls import resolve, reverse
from django.utils.translation import gettext_lazy as _
from django.utils.timezone import now

from pretix.base.settings import settings_hierarkey
from pretix.base.signals import periodic_task
from pretix.control.signals import nav_organizer, nav_event

from .models import BankConnection

logger = logging.getLogger(__name__)

# Register plugin settings defaults
settings_hierarkey.add_default("pretix_bank_sync_client_id", "", str)
settings_hierarkey.add_default("pretix_bank_sync_client_secret", "", str)
settings_hierarkey.add_default("pretix_bank_sync_redirect_uri", "", str)
settings_hierarkey.add_default("pretix_bank_sync_auto_confirm", True, bool)


@receiver(nav_organizer, dispatch_uid="bank_sync_nav_organizer")
def nav_organizer_settings(sender, request, organizer, **kwargs):
    """Add Bank Sync to organizer settings navigation."""
    if not request.user.has_organizer_permission(organizer, 'can_change_organizer_settings'):
        return []

    url = resolve(request.path_info)
    return [{
        'label': _('Bank Sync'),
        'url': reverse('plugins:pretix_bank_sync:connections_list', kwargs={
            'organizer': organizer.slug
        }),
        'parent': reverse('control:organizer.edit', kwargs={
            'organizer': organizer.slug
        }),
        'active': url.namespace == 'plugins:pretix_bank_sync' and url.url_name in ('connections_list', 'bank_setup_wizard', 'connection_delete', 'connection_renew'),
    }]


@receiver(periodic_task, dispatch_uid="bank_sync_periodic_sync")
def periodic_sync_transactions(sender, **kwargs):
    """Periodic task to sync bank transactions (max 4 times per day)."""
    from .tasks import sync_bank_transactions

    # Get all active connections that can sync
    connections = BankConnection.objects.filter(status=BankConnection.STATUS_ACTIVE)

    for connection in connections:
        # Reset daily counter if needed
        connection.reset_daily_counter()

        # Check if we can sync
        if not connection.can_sync():
            logger.debug(
                f"Skipping sync for connection {connection.id}: "
                f"status={connection.status}, count={connection.sync_count_today}"
            )
            continue

        # Trigger async sync task
        try:
            sync_bank_transactions.apply_async(kwargs={'connection_id': connection.id})
            logger.info(f"Queued sync task for connection {connection.id}")
        except Exception as e:
            logger.error(f"Failed to queue sync task for connection {connection.id}: {e}")
            connection.status = BankConnection.STATUS_ERROR
            connection.last_error = str(e)
            connection.last_error_at = now()
            connection.save(update_fields=['status', 'last_error', 'last_error_at'])


@receiver(periodic_task, dispatch_uid="bank_sync_periodic_expiry_check")
def periodic_check_expiry_warnings(sender, **kwargs):
    """Periodic task to check for connections expiring soon and send warnings."""
    from .tasks import check_connection_expiry_warnings
    
    # Queue expiry check task (runs daily)
    try:
        check_connection_expiry_warnings.apply_async()
        logger.debug("Queued expiry warning check task")
    except Exception as e:
        logger.error(f"Failed to queue expiry warning check task: {e}")


@receiver(nav_event, dispatch_uid="bank_sync_nav_event")
def nav_event_transactions(sender, request, **kwargs):
    """Add Bank Transaction Matches to event navigation."""
    if not request.user.has_event_permission(request.organizer, request.event, 'can_change_orders', request=request):
        return []
    
    url = resolve(request.path_info)
    return [{
        'label': _('Bank Transactions'),
        'url': reverse('plugins:pretix_bank_sync:match_review', kwargs={
            'organizer': request.organizer.slug,
            'event': request.event.slug
        }),
        'parent': reverse('control:event.orders', kwargs={
            'organizer': request.organizer.slug,
            'event': request.event.slug
        }),
        'active': url.namespace == 'plugins:pretix_bank_sync' and url.url_name in ('match_review', 'match_approve', 'match_reject'),
    }]
