"""
Views for Bank Sync plugin.

Control panel views for setup, authorization, and transaction management.
"""
import logging
from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView, View, ListView

from pretix.base.forms import SettingsForm
from pretix.base.models import Organizer
from pretix.control.permissions import OrganizerPermissionRequiredMixin
from pretix.control.views import PaginationMixin
from pretix.helpers.urls import build_absolute_uri

from .models import BankConnection, BankTransaction
from .services.gocardless_service import GoCardlessService

logger = logging.getLogger(__name__)


class BankSyncSettingsForm(SettingsForm):
    """Settings form for GoCardless credentials."""
    pretix_bank_sync_client_id = forms.CharField(
        label=_("GoCardless Client ID"),
        required=False,
        help_text=_("Your GoCardless API client ID")
    )
    pretix_bank_sync_client_secret = forms.CharField(
        label=_("GoCardless Client Secret"),
        required=False,
        widget=forms.PasswordInput(render_value=True),
        help_text=_("Your GoCardless API client secret")
    )
    pretix_bank_sync_redirect_uri = forms.CharField(
        label=_("Redirect URI"),
        required=False,
        help_text=_("OAuth redirect URI (configured in GoCardless dashboard)")
    )
    pretix_bank_sync_sandbox = forms.BooleanField(
        label=_("Use Sandbox"),
        required=False,
        help_text=_("Use GoCardless sandbox environment for testing")
    )
    pretix_bank_sync_auto_confirm = forms.BooleanField(
        label=_("Auto-confirm matched payments"),
        required=False,
        help_text=_("Automatically confirm payments when transactions are matched")
    )


class BankSyncSettingsView(OrganizerPermissionRequiredMixin, TemplateView):
    """Main settings page for bank sync."""
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/settings.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer

        # Get or create bank connection
        # Note: Scope is already set by PermissionMiddleware, so we don't need to wrap again
        connection = BankConnection.objects.filter(organizer=organizer).first()

        # Get recent transactions
        if connection:
            recent_transactions = BankTransaction.objects.filter(
                connection=connection
            ).order_by('-booking_date', '-created')[:10]
        else:
            recent_transactions = []

        ctx.update({
            'organizer': organizer,
            'connection': connection,
            'recent_transactions': recent_transactions,
            'form': BankSyncSettingsForm(obj=organizer),
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Handle settings form submission."""
        organizer = request.organizer
        form = BankSyncSettingsForm(request.POST, obj=organizer)

        if form.is_valid():
            form.save()
            messages.success(request, _('Settings saved successfully.'))
            return redirect(self.get_success_url())
        else:
            messages.error(request, _('Please correct the errors below.'))
            ctx = self.get_context_data()
            ctx['form'] = form
            return self.render_to_response(ctx)

    def get_success_url(self):
        return reverse('plugins:pretix_bank_sync:settings', kwargs={
            'organizer': self.request.organizer.slug
        })


class BankSyncAuthorizeView(OrganizerPermissionRequiredMixin, View):
    """Initiate OAuth flow with GoCardless."""
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer

        # Get settings
        client_id = organizer.settings.get('pretix_bank_sync_client_id', '')
        client_secret = organizer.settings.get('pretix_bank_sync_client_secret', '')
        redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')
        sandbox = organizer.settings.get('pretix_bank_sync_sandbox', True)

        if not client_id or not client_secret:
            messages.error(request, _('GoCardless credentials not configured. Please configure them in settings first.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        if not redirect_uri:
            messages.error(request, _('Redirect URI not configured. Please configure it in settings first.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        try:
            # Build callback URL
            callback_url = build_absolute_uri(
                'plugins:pretix_bank_sync:callback',
                kwargs={'organizer': organizer.slug}
            )

            # Initialize service
            service = GoCardlessService(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                sandbox=sandbox
            )

            # Create requisition
            requisition_data = service.create_requisition_link(callback_url)

            requisition_id = requisition_data.get('id')
            auth_link = requisition_data.get('link')

            if not requisition_id or not auth_link:
                messages.error(request, _('Failed to create authorization link. Please try again.'))
                return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

            # Create or update connection
            connection, created = BankConnection.objects.get_or_create(
                organizer=organizer,
                defaults={
                    'requisition_id': requisition_id,
                    'status': BankConnection.STATUS_PENDING,
                }
            )

            if not created:
                connection.requisition_id = requisition_id
                connection.status = BankConnection.STATUS_PENDING
                connection.save(update_fields=['requisition_id', 'status'])

            # Redirect to GoCardless authorization
            return redirect(auth_link)

        except Exception as e:
            logger.exception("Error initiating GoCardless authorization")
            messages.error(request, _('An error occurred: {}').format(str(e)))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)


class BankSyncCallbackView(OrganizerPermissionRequiredMixin, View):
    """Handle OAuth callback from GoCardless."""
    permission = 'can_change_organizer_settings'

    def get(self, request, *args, **kwargs):
        organizer = request.organizer
        requisition_id = request.GET.get('ref')

        if not requisition_id:
            messages.error(request, _('Invalid callback. Missing requisition ID.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        try:
            # Get connection
            connection = get_object_or_404(
                BankConnection,
                organizer=organizer,
                requisition_id=requisition_id
            )

            # Get settings
            client_id = organizer.settings.get('pretix_bank_sync_client_id', '')
            client_secret = organizer.settings.get('pretix_bank_sync_client_secret', '')
            redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')
            sandbox = organizer.settings.get('pretix_bank_sync_sandbox', True)

            # Initialize service
            service = GoCardlessService(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                sandbox=sandbox
            )

            # Get requisition status
            requisition = service.get_requisition(requisition_id)
            status = requisition.get('status')

            if status == 'LN':
                # Linked successfully
                connection.status = BankConnection.STATUS_ACTIVE
                connection.consent_id = requisition.get('consent_id', '')
                if requisition.get('expires_at'):
                    from datetime import datetime
                    connection.consent_expires_at = datetime.fromisoformat(
                        requisition['expires_at'].replace('Z', '+00:00')
                    )
                connection.save(update_fields=['status', 'consent_id', 'consent_expires_at'])

                messages.success(request, _('Bank account connected successfully!'))
            else:
                # Error or not linked
                connection.status = BankConnection.STATUS_ERROR
                connection.last_error = f"Status: {status}"
                connection.last_error_at = now()
                connection.save(update_fields=['status', 'last_error', 'last_error_at'])

                messages.error(request, _('Bank account connection failed. Status: {}').format(status))

        except Exception as e:
            logger.exception("Error processing GoCardless callback")
            messages.error(request, _('An error occurred: {}').format(str(e)))

        return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)


class BankSyncTransactionsView(OrganizerPermissionRequiredMixin, PaginationMixin, ListView):
    """List synced transactions."""
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/transactions.html'
    context_object_name = 'transactions'
    model = BankTransaction

    def get_queryset(self):
        organizer = self.request.organizer
        connection = BankConnection.objects.filter(organizer=organizer).first()
        
        if not connection:
            return BankTransaction.objects.none()
        
        qs = BankTransaction.objects.filter(connection=connection).select_related('order', 'payment')

        # Filter by state if provided
        state = self.request.GET.get('state')
        if state:
            qs = qs.filter(state=state)

        return qs.order_by('-booking_date', '-created')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        connection = BankConnection.objects.filter(organizer=organizer).first()

        ctx.update({
            'organizer': organizer,
            'connection': connection,
            'state_filter': self.request.GET.get('state', ''),
        })
        return ctx
