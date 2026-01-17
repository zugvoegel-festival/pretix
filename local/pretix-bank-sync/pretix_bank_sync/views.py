"""
Views for Bank Sync plugin.

Control panel views for setup, authorization, and transaction management.
"""
import logging
import os
from datetime import timedelta
from django import forms
from django.conf import settings as django_settings
from django.contrib import messages
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile
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
from .services.enablebanking_service import EnableBankingService

logger = logging.getLogger(__name__)


class BankSyncSettingsForm(SettingsForm):
    """Settings form for Enable Banking credentials."""
    pretix_bank_sync_application_id = forms.CharField(
        label=_("Application ID"),
        required=False,
        help_text=_("Your Enable Banking application ID (from the PEM filename, e.g., aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee)")
    )
    pretix_bank_sync_private_key_file = forms.FileField(
        label=_("Private Key File (PEM)"),
        required=False,
        help_text=_("Upload the private key PEM file downloaded from Enable Banking. Leave empty to keep existing file.")
    )
    pretix_bank_sync_private_key_path = forms.CharField(
        label=_("Private Key File Path"),
        required=False,
        widget=forms.HiddenInput(),  # Hide from form, auto-managed
        help_text=_("Path to the stored private key file (automatically set when uploading)")
    )
    pretix_bank_sync_redirect_uri = forms.CharField(
        label=_("Redirect URI"),
        required=False,
        help_text=_("OAuth redirect URI - must match exactly what's configured in Enable Banking dashboard. The actual callback URL will be automatically generated based on your pretix installation URL.")
    )
    pretix_bank_sync_auto_confirm = forms.BooleanField(
        label=_("Auto-confirm matched payments"),
        required=False,
        help_text=_("Automatically confirm payments when transactions are matched")
    )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Exclude private_key_file from SettingsForm's automatic handling
        # We'll handle it manually in the view
        if 'pretix_bank_sync_private_key_file' in self.fields:
            # Don't let SettingsForm try to save this field
            self.fields['pretix_bank_sync_private_key_file'].widget.attrs['data-exclude-from-save'] = 'true'


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

        # Create form with current values
        form = BankSyncSettingsForm(obj=organizer)
        # Set the private_key_path initial value for display
        if organizer.settings.get('pretix_bank_sync_private_key_path'):
            form.fields['pretix_bank_sync_private_key_path'].initial = organizer.settings.get('pretix_bank_sync_private_key_path')
        
        # Build the actual callback URL that will be used
        callback_url = build_absolute_uri(
            'plugins:pretix_bank_sync:callback',
            kwargs={'organizer': organizer.slug}
        )
        
        # If connection is pending, try to regenerate authorization URL
        pending_auth_url = None
        if connection and connection.status == BankConnection.STATUS_PENDING:
            try:
                application_id = organizer.settings.get('pretix_bank_sync_application_id', '')
                private_key_path = organizer.settings.get('pretix_bank_sync_private_key_path', '')
                redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')
                
                if application_id and private_key_path:
                    service = EnableBankingService(
                        application_id=application_id,
                        private_key_path=private_key_path,
                        redirect_uri=redirect_uri
                    )
                    # Try to get the bank info from the connection if stored
                    # For now, we'll need to regenerate - but we need bank info
                    # Store this info or allow user to click "Continue Authorization"
                    pending_auth_url = None  # Will be generated on demand
            except Exception as e:
                logger.warning(f"Could not prepare auth URL for pending connection: {e}")
        
        ctx.update({
            'organizer': organizer,
            'connection': connection,
            'recent_transactions': recent_transactions,
            'form': form,
            'has_private_key': bool(organizer.settings.get('pretix_bank_sync_private_key_path')),
            'actual_callback_url': callback_url,
            'pending_auth_url': pending_auth_url,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Handle settings form submission."""
        organizer = request.organizer
        form = BankSyncSettingsForm(request.POST, request.FILES, obj=organizer)

        if form.is_valid():
            # Handle file upload (before saving other fields)
            private_key_file = form.cleaned_data.get('pretix_bank_sync_private_key_file')
            if private_key_file and isinstance(private_key_file, UploadedFile):
                # Validate file extension
                if not private_key_file.name.lower().endswith('.pem'):
                    messages.error(request, _('Invalid file type. Please upload a .pem file.'))
                    ctx = self.get_context_data()
                    ctx['form'] = form
                    return self.render_to_response(ctx)
                
                # Validate file content (should start with BEGIN PRIVATE KEY or BEGIN RSA PRIVATE KEY)
                private_key_file.seek(0)
                content = private_key_file.read(100).decode('utf-8', errors='ignore')
                private_key_file.seek(0)
                if 'BEGIN' not in content or 'PRIVATE KEY' not in content:
                    messages.error(request, _('Invalid PEM file format. Please upload a valid private key file.'))
                    ctx = self.get_context_data()
                    ctx['form'] = form
                    return self.render_to_response(ctx)
                
                # Save file to secure location
                # Store in organizer-specific directory
                file_path = f'pretix_bank_sync/{organizer.slug}/{organizer.slug}-enablebanking-key.pem'
                
                try:
                    # Delete old file if exists
                    old_path = organizer.settings.get('pretix_bank_sync_private_key_path', '')
                    if old_path and default_storage.exists(old_path):
                        default_storage.delete(old_path)
                    
                    # Save new file
                    saved_path = default_storage.save(file_path, private_key_file)
                    
                    # Update settings with the stored path (before form.save())
                    organizer.settings.set('pretix_bank_sync_private_key_path', saved_path)
                    
                    messages.success(request, _('Private key file uploaded successfully.'))
                except Exception as e:
                    logger.exception("Error saving private key file")
                    messages.error(request, _('Error saving private key file: {}').format(str(e)))
                    ctx = self.get_context_data()
                    ctx['form'] = form
                    return self.render_to_response(ctx)
            
            # Save other form fields (excluding private_key_file which we handled manually)
            # Remove private_key_file from cleaned_data so SettingsForm doesn't try to save it
            cleaned_data = form.cleaned_data.copy()
            cleaned_data.pop('pretix_bank_sync_private_key_file', None)
            
            # Manually save each field except the file field
            for field_name, value in cleaned_data.items():
                if field_name != 'pretix_bank_sync_private_key_path':  # Already saved above if file uploaded
                    organizer.settings.set(field_name, value)
            
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


class BankSelectionForm(forms.Form):
    """Form for selecting country and bank."""
    bank_name = forms.ChoiceField(
        label=_("Bank"),
        required=True,
        help_text=_("Select a bank from the list")
    )
    
    def __init__(self, *args, banks=None, **kwargs):
        super().__init__(*args, **kwargs)
        if banks:
            # Create choices from banks list
            choices = [('', _('-- Select a bank --'))]
            for bank in banks:
                bank_name = bank.get('name', '')
                bank_display = bank_name
                if bank.get('bic'):
                    bank_display = f"{bank_name} ({bank['bic']})"
                choices.append((bank_name, bank_display))
            self.fields['bank_name'].choices = choices


class BankSyncAuthorizeView(OrganizerPermissionRequiredMixin, TemplateView):
    """Initiate OAuth flow with Enable Banking - shows bank selection first."""
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/select_bank.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer

        # Get settings
        application_id = organizer.settings.get('pretix_bank_sync_application_id', '')
        private_key_path = organizer.settings.get('pretix_bank_sync_private_key_path', '')
        redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')

        # Check if credentials are configured
        if not application_id or not private_key_path or not redirect_uri:
            ctx['error'] = _('Please configure Enable Banking credentials in settings first.')
            return ctx

        # Check if there's a pending connection
        pending_connection = BankConnection.objects.filter(
            organizer=organizer,
            status=BankConnection.STATUS_PENDING
        ).first()
        
        # Get country from request or default to FI
        country = self.request.GET.get('country', 'FI')
        logger.info(f"Fetching banks for country: {country}")
        
        # Try to fetch available banks
        banks = []
        try:
            service = EnableBankingService(
                application_id=application_id,
                private_key_path=private_key_path,
                redirect_uri=redirect_uri
            )
            banks = service.get_aspsps(country=country)
            logger.info(f"Found {len(banks)} banks for country {country}")
        except Exception as e:
            logger.exception(f"Error fetching banks for country {country}")
            ctx['error'] = _('Failed to fetch available banks: {}').format(str(e))
            ctx['country'] = country
            ctx['form'] = BankSelectionForm(banks=[])
            ctx['banks'] = []
            return ctx

        # Create form with banks
        form = BankSelectionForm(banks=banks)

        ctx.update({
            'organizer': organizer,
            'form': form,
            'banks': banks,
            'country': country,
            'pending_connection': pending_connection,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Handle bank selection and initiate authorization."""
        organizer = request.organizer

        # Get settings
        application_id = organizer.settings.get('pretix_bank_sync_application_id', '')
        private_key_path = organizer.settings.get('pretix_bank_sync_private_key_path', '')
        redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')

        if not application_id:
            messages.error(request, _('Application ID not configured. Please configure it in settings first.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        if not private_key_path:
            messages.error(request, _('Private key file path not configured. Please configure it in settings first.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        if not redirect_uri:
            messages.error(request, _('Redirect URI not configured. Please configure it in settings first.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        # Get country from POST or default to FI
        country = request.POST.get('country', 'FI')
        form = BankSelectionForm(request.POST)
        
        # Re-fetch banks to populate form choices
        try:
            service = EnableBankingService(
                application_id=application_id,
                private_key_path=private_key_path,
                redirect_uri=redirect_uri
            )
            banks = service.get_aspsps(country=country)
            form = BankSelectionForm(request.POST, banks=banks)
        except Exception as e:
            logger.exception("Error fetching banks for validation")
            messages.error(request, _('Failed to fetch banks: {}').format(str(e)))
            return redirect('plugins:pretix_bank_sync:authorize', organizer=organizer.slug)
        
        if not form.is_valid():
            messages.error(request, _('Please select a bank.'))
            return redirect('plugins:pretix_bank_sync:authorize', organizer=organizer.slug)

        bank_name = form.cleaned_data['bank_name']

        try:
            # Build callback URL - this must match exactly what's configured in Enable Banking dashboard
            callback_url = build_absolute_uri(
                'plugins:pretix_bank_sync:callback',
                kwargs={'organizer': organizer.slug}
            )
            
            logger.info(f"Using callback URL: {callback_url}")
            logger.info(f"Configured redirect URI in settings: {redirect_uri}")
            
            # Warn if they don't match (but use the built callback_url as that's the actual endpoint)
            if redirect_uri and redirect_uri != callback_url:
                logger.warning(f"Redirect URI mismatch: settings has '{redirect_uri}' but callback is '{callback_url}'. Using callback URL.")

            # Initialize service
            service = EnableBankingService(
                application_id=application_id,
                private_key_path=private_key_path,
                redirect_uri=redirect_uri
            )

            # Create authorization with selected bank
            # Use the actual callback URL, not the configured one (which might be outdated)
            auth_data = service.create_authorization(
                redirect_url=callback_url,
                aspsp_name=bank_name,
                aspsp_country=country
            )

            auth_url = auth_data.get('url')
            state = auth_data.get('state', application_id)

            if not auth_url:
                messages.error(request, _('Failed to create authorization link. Please try again.'))
                return redirect('plugins:pretix_bank_sync:authorize', organizer=organizer.slug)

            # Create or update connection
            connection, created = BankConnection.objects.get_or_create(
                organizer=organizer,
                defaults={
                    'requisition_id': state,  # Store state for later verification
                    'status': BankConnection.STATUS_PENDING,
                }
            )

            if not created:
                connection.requisition_id = state
                connection.status = BankConnection.STATUS_PENDING
                connection.save(update_fields=['requisition_id', 'status'])

            # Redirect to Enable Banking authorization
            return redirect(auth_url)

        except Exception as e:
            logger.exception("Error initiating Enable Banking authorization")
            messages.error(request, _('An error occurred: {}').format(str(e)))
            return redirect('plugins:pretix_bank_sync:authorize', organizer=organizer.slug)


class BankSyncCallbackView(OrganizerPermissionRequiredMixin, View):
    """Handle OAuth callback from Enable Banking."""
    permission = 'can_change_organizer_settings'

    def get(self, request, *args, **kwargs):
        organizer = request.organizer
        # Enable Banking returns 'code' in the redirect URL
        code = request.GET.get('code')
        state = request.GET.get('state')

        if not code:
            messages.error(request, _('Invalid callback. Missing authorization code.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

        try:
            # Get connection (verify state matches)
            connection = BankConnection.objects.filter(organizer=organizer).first()
            if not connection:
                messages.error(request, _('No pending connection found.'))
                return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

            # Get settings
            application_id = organizer.settings.get('pretix_bank_sync_application_id', '')
            private_key_path = organizer.settings.get('pretix_bank_sync_private_key_path', '')
            redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')

            if not application_id or not private_key_path:
                messages.error(request, _('Enable Banking credentials not configured.'))
                return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

            # Initialize service
            service = EnableBankingService(
                application_id=application_id,
                private_key_path=private_key_path,
                redirect_uri=redirect_uri
            )

            # Create session using the authorization code
            session = service.create_session(code)
            
            # Session contains accounts list
            accounts = session.get('accounts', [])
            if not accounts:
                connection.status = BankConnection.STATUS_ERROR
                connection.last_error = "No accounts found in session"
                connection.last_error_at = now()
                connection.save(update_fields=['status', 'last_error', 'last_error_at'])
                messages.error(request, _('No accounts found in the session.'))
                return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)

            # Store session information
            # Note: Enable Banking sessions are temporary, we'll need to re-authorize periodically
            # For now, we'll store the first account UID in requisition_id
            first_account = accounts[0]
            account_uid = first_account.get('uid', '')
            
            connection.status = BankConnection.STATUS_ACTIVE
            connection.requisition_id = account_uid  # Store account UID
            connection.consent_id = session.get('session_id', '')  # Store session ID if available
            # Enable Banking access is valid for the period specified in authorization
            # We'll set expiration to 10 days from now (default)
            connection.consent_expires_at = now() + timedelta(days=10)
            connection.save(update_fields=['status', 'requisition_id', 'consent_id', 'consent_expires_at'])

            messages.success(request, _('Bank account connected successfully!'))

        except Exception as e:
            logger.exception("Error processing Enable Banking callback")
            messages.error(request, _('An error occurred: {}').format(str(e)))

        return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)


class BankSyncFetchTransactionsView(OrganizerPermissionRequiredMixin, View):
    """Manually fetch transactions from connected bank account."""
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        
        # Get connection
        connection = BankConnection.objects.filter(organizer=organizer).first()
        if not connection or connection.status != BankConnection.STATUS_ACTIVE:
            messages.error(request, _('No active bank connection found.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)
        
        # Get settings
        application_id = organizer.settings.get('pretix_bank_sync_application_id', '')
        private_key_path = organizer.settings.get('pretix_bank_sync_private_key_path', '')
        redirect_uri = organizer.settings.get('pretix_bank_sync_redirect_uri', '')
        
        if not application_id or not private_key_path:
            messages.error(request, _('Enable Banking credentials not configured.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)
        
        try:
            # Import the sync function from tasks
            from .tasks import sync_bank_transactions
            
            # Call the sync function directly (synchronously for manual trigger)
            # Get account UID from connection
            account_uid = connection.requisition_id
            if not account_uid:
                messages.error(request, _('No account ID found in connection.'))
                return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)
            
            # Initialize service
            from .services.enablebanking_service import EnableBankingService
            service = EnableBankingService(
                application_id=application_id,
                private_key_path=private_key_path,
                redirect_uri=redirect_uri
            )
            
            # Fetch transactions
            logger.info(f"Manually fetching transactions for account {account_uid}")
            transactions = service.get_transactions(account_uid)
            
            # Process and save transactions (similar to tasks.py)
            new_transactions = []
            from .models import BankTransaction
            
            for tx_data in transactions:
                normalized = service.normalize_transaction(tx_data, account_uid)
                transaction_id = normalized.get('transaction_id', '')
                
                if not transaction_id:
                    continue
                
                # Check if transaction already exists
                existing = BankTransaction.objects.filter(
                    connection=connection,
                    transaction_id=transaction_id
                ).first()
                
                if existing:
                    continue
                
                # Create new transaction using the normalized data
                trans = BankTransaction(
                    connection=connection,
                    **normalized
                )
                trans.save()
                new_transactions.append(trans)
            
            # Update connection
            connection.last_sync = now()
            connection.save(update_fields=['last_sync'])
            
            # Match new transactions with orders
            if new_transactions:
                from .tasks import match_transactions
                match_transactions(connection)
                messages.success(request, _('Successfully fetched {} new transaction(s) and matched them with orders.').format(len(new_transactions)))
            else:
                messages.info(request, _('No new transactions found.'))
            
            # Redirect to transactions view
            return redirect('plugins:pretix_bank_sync:transactions', organizer=organizer.slug)
            
        except Exception as e:
            logger.exception("Error manually fetching transactions")
            messages.error(request, _('Error fetching transactions: {}').format(str(e)))
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
