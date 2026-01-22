"""
Views for Bank Sync plugin.

Control panel views for setup, authorization, and transaction management.
"""
import json
import logging
from datetime import timedelta
from django import forms
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView, View, ListView, DeleteView
from django.db.models import Count, Q

from pretix.base.models import Organizer, Event
from pretix.control.permissions import OrganizerPermissionRequiredMixin, EventPermissionRequiredMixin
from pretix.control.views import PaginationMixin
from pretix.helpers.urls import build_absolute_uri

from .models import BankConnection, BankTransaction, TransactionMatchSuggestion
from .services.gocardless_service import GoCardlessService
from .tasks import approve_match_suggestion

logger = logging.getLogger(__name__)


def get_bank_setup_steps(current_step_key=None, connection=None):
    """
    Get the steps for the bank setup wizard.
    
    Args:
        current_step_key: The key of the current active step
        connection: Optional BankConnection object to check status
    
    Returns:
        List of step dictionaries with 'key', 'label', 'status', 'description'
    """
    steps = [
        {
            'key': 'credentials',
            'label': _('Credentials'),
            'description': _('Configure GoCardless API credentials and test connection'),
            'status': 'pending'
        },
        {
            'key': 'select_country',
            'label': _('Select Country'),
            'description': _('Choose your country'),
            'status': 'pending'
        },
        {
            'key': 'select_bank',
            'label': _('Select Bank'),
            'description': _('Choose your bank from the list'),
            'status': 'pending'
        },
        {
            'key': 'create_requisition',
            'label': _('Create Requisition'),
            'description': _('Create bank connection consent request'),
            'status': 'pending'
        },
        {
            'key': 'authorize',
            'label': _('Authorize'),
            'description': _('Authorize access to your bank account'),
            'status': 'pending'
        },
        {
            'key': 'verify',
            'label': _('Verify'),
            'description': _('Verify the connection was successful'),
            'status': 'pending'
        },
        {
            'key': 'test',
            'label': _('Test Balances'),
            'description': _('Test fetching account balances'),
            'status': 'pending'
        }
    ]
    
    # Determine step statuses
    if current_step_key:
        found_current = False
        for step in steps:
            if step['key'] == current_step_key:
                step['status'] = 'active'
                found_current = True
            elif not found_current:
                step['status'] = 'completed'
            else:
                step['status'] = 'pending'
    
    # If connection exists and is active, mark verify and test as completed
    if connection and connection.status == BankConnection.STATUS_ACTIVE:
        for step in steps:
            if step['key'] in ['create_requisition', 'authorize', 'verify', 'test']:
                step['status'] = 'completed'
    
    return steps


class BankConnectionsListView(OrganizerPermissionRequiredMixin, ListView):
    """
    Main view showing all bank connections with stats and management options.
    """
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/connections_list.html'
    context_object_name = 'connections'
    model = BankConnection

    def get_queryset(self):
        organizer = self.request.organizer
        return BankConnection.objects.filter(
            organizer=organizer
        ).annotate(
            transaction_count=Count('transactions')
        ).order_by('-created')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        
        # Get GoCardless credentials status
        has_credentials = bool(
            organizer.settings.get('pretix_bank_sync_gocardless_client_id', '') and
            organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
        )
        
        # Get connection stats
        connections = self.get_queryset()
        stats = {
            'total': connections.count(),
            'active': connections.filter(status=BankConnection.STATUS_ACTIVE).count(),
            'expired': connections.filter(status=BankConnection.STATUS_EXPIRED).count(),
            'pending': connections.filter(status=BankConnection.STATUS_PENDING).count(),
            'error': connections.filter(status=BankConnection.STATUS_ERROR).count(),
        }
        
        ctx.update({
            'organizer': organizer,
            'has_credentials': has_credentials,
            'stats': stats,
            'now': now(),
        })
        return ctx


class BankConnectionDeleteView(OrganizerPermissionRequiredMixin, DeleteView):
    """
    Delete a bank connection with confirmation.
    """
    permission = 'can_change_organizer_settings'
    model = BankConnection
    template_name = 'pretix_bank_sync/control/connection_delete.html'
    context_object_name = 'connection'

    def get_queryset(self):
        return BankConnection.objects.filter(organizer=self.request.organizer)

    def get_success_url(self):
        return reverse('plugins:pretix_bank_sync:connections_list', kwargs={
            'organizer': self.request.organizer.slug
        })

    def delete(self, request, *args, **kwargs):
        connection = self.get_object()
        messages.success(request, _('Bank connection deleted successfully.'))
        return super().delete(request, *args, **kwargs)


class BankConnectionRenewView(OrganizerPermissionRequiredMixin, View):
    """
    Renew/reauthorize an expired or invalid connection.
    """
    permission = 'can_change_organizer_settings'

    def post(self, request, *args, **kwargs):
        organizer = request.organizer
        connection_id = kwargs.get('connection_id')
        
        try:
            connection = BankConnection.objects.get(
                pk=connection_id,
                organizer=organizer
            )
        except BankConnection.DoesNotExist:
            messages.error(request, _('Connection not found.'))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
        
        # Check if credentials are configured
        client_id = organizer.settings.get('pretix_bank_sync_gocardless_client_id', '')
        client_secret = organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
        
        if not client_id or not client_secret:
            messages.error(request, _('GoCardless credentials not configured. Please configure them first.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)
        
        # Get requisition to find institution_id
        try:
            redirect_uri = organizer.settings.get('pretix_bank_sync_gocardless_redirect_uri', '')
            callback_url = build_absolute_uri(
                'plugins:pretix_bank_sync:callback',
                kwargs={'organizer': organizer.slug}
            )
            
            service = GoCardlessService(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri or callback_url
            )
            
            # Get existing requisition to find institution
            requisition = service.get_requisition(connection.requisition_id)
            institution_id = requisition.get('institution_id')
            
            if institution_id:
                # Create new requisition with same institution
                requisition_data = service.create_requisition_link(
                    redirect_url=callback_url,
                    institution_id=institution_id,
                    agreement=requisition.get('agreement')  # Use existing agreement if available
                )
                
                # Update connection
                connection.requisition_id = requisition_data.get('id')
                connection.status = BankConnection.STATUS_PENDING
                connection.last_error = ''
                connection.save(update_fields=['requisition_id', 'status', 'last_error'])
                
                # Store auth link in session
                request.session['gocardless_auth_link'] = requisition_data.get('link')
                request.session['gocardless_selected_institution'] = institution_id
                
                messages.success(request, _('Requisition renewed. Please authorize the connection.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=authorize")
            else:
                messages.error(request, _('Could not determine institution. Please create a new connection.'))
                return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
                
        except Exception as e:
            logger.exception("Error renewing connection")
            messages.error(request, _('An error occurred: {}').format(str(e)))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)


class BankSetupWizardView(OrganizerPermissionRequiredMixin, TemplateView):
    """
    Step-by-step wizard for setting up GoCardless bank connection.
    """
    permission = 'can_change_organizer_settings'
    template_name = 'pretix_bank_sync/control/bank_setup_wizard.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        organizer = self.request.organizer
        
        # Get current step from URL parameter or default to 'credentials'
        step = self.request.GET.get('step', 'credentials')
        
        # Get or create bank connection (for existing connections being edited)
        connection_id = self.request.GET.get('connection_id')
        connection = None
        if connection_id:
            try:
                connection = BankConnection.objects.get(
                    pk=connection_id,
                    organizer=organizer
                )
            except BankConnection.DoesNotExist:
                pass
        
        # Get steps for tramline
        steps = get_bank_setup_steps(current_step_key=step, connection=connection)
        current_step = next((s for s in steps if s['status'] == 'active'), steps[0])
        
        # Get GoCardless settings (check session for temporary values from test)
        session_creds = self.request.session.get('wizard_credentials', {})
        client_id = session_creds.get('client_id', organizer.settings.get('pretix_bank_sync_gocardless_client_id', ''))
        # Use session value if available (from test), otherwise use saved setting
        client_secret = session_creds.get('client_secret', organizer.settings.get('pretix_bank_sync_gocardless_client_secret', ''))
        redirect_uri = session_creds.get('redirect_uri', organizer.settings.get('pretix_bank_sync_gocardless_redirect_uri', ''))
        
        # Build callback URL
        callback_url = build_absolute_uri(
            'plugins:pretix_bank_sync:callback',
            kwargs={'organizer': organizer.slug}
        )
        
        ctx.update({
            'organizer': organizer,
            'step': step,
            'steps': steps,
            'current_step': current_step,
            'connection': connection,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'callback_url': callback_url,
            'has_credentials': bool(client_id and client_secret),
        })
        
        # Step-specific context
        if step == 'credentials' and client_id and client_secret:
            # Test connection on credentials step by making a real API call
            try:
                service = GoCardlessService(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri or callback_url
                )
                # Actually test credentials by trying to list institutions
                # This verifies both authentication and API access
                try:
                    institutions = service.list_institutions(country='GB')  # Test with a common country
                    # If we get here, credentials are valid
                    ctx['credentials_valid'] = True
                    ctx['credentials_test_message'] = _('Credentials verified successfully. API connection working.')
                except Exception as api_error:
                    # Check if it's an authentication error
                    error_msg = str(api_error)
                    if 'authenticate' in error_msg.lower() or 'token' in error_msg.lower() or '401' in error_msg or '403' in error_msg:
                        ctx['credentials_valid'] = False
                        ctx['credentials_error'] = _('Authentication failed. Please check your Secret ID and Secret Key.')
                    else:
                        # Other API errors might be network or API issues, but credentials might be valid
                        # Still mark as valid if we got past authentication
                        ctx['credentials_valid'] = True
                        ctx['credentials_test_message'] = _('Credentials appear valid, but API call failed: {}').format(error_msg)
                        logger.warning(f"Credentials test: API call failed but auth succeeded: {api_error}")
            except Exception as e:
                ctx['credentials_valid'] = False
                ctx['credentials_error'] = _('Failed to test credentials: {}').format(str(e))
                logger.exception("Error testing credentials")
        
        elif step == 'select_country':
            # Get selected country from session if available
            selected_country = self.request.session.get('gocardless_selected_country', '')
            ctx['selected_country'] = selected_country
        
        elif step == 'select_bank' and client_id and client_secret:
            # Get selected country from session
            selected_country = self.request.session.get('gocardless_selected_country', '')
            ctx['selected_country'] = selected_country
            
            # Get preloaded institutions from session if available
            preloaded_institutions = self.request.session.get('gocardless_institutions', [])
            logger.info(f"Retrieved {len(preloaded_institutions)} preloaded institutions from session for bank selection step")
            
            # Convert to JSON-serializable format and ensure it's a string
            if preloaded_institutions:
                try:
                    ctx['preloaded_institutions_json'] = json.dumps(preloaded_institutions)
                    logger.debug(f"Serialized {len(preloaded_institutions)} institutions to JSON")
                    logger.debug(f"First institution sample: {preloaded_institutions[0] if preloaded_institutions else 'none'}")
                except Exception as e:
                    logger.error(f"Error serializing institutions to JSON: {e}")
                    ctx['preloaded_institutions_json'] = '[]'
            else:
                ctx['preloaded_institutions_json'] = '[]'
                logger.debug("No preloaded institutions found in session")
        
        elif step == 'create_requisition':
            # Show selected institution from session
            selected_institution = self.request.session.get('gocardless_selected_institution')
            ctx['selected_institution'] = selected_institution
        
        elif step == 'authorize':
            # Check if we have auth link in session
            auth_link = self.request.session.get('gocardless_auth_link')
            ctx['has_auth_link'] = bool(auth_link)
        
        elif step == 'verify' and connection:
            try:
                service = GoCardlessService(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri or callback_url
                )
                requisition = service.get_requisition(connection.requisition_id)
                ctx['requisition'] = requisition
                ctx['requisition_status'] = requisition.get('status', '')
                
                # Get accounts if available
                account_ids = requisition.get('accounts', [])
                if account_ids:
                    accounts = []
                    for account_id in account_ids:
                        account = service.get_account_details(account_id)
                        if account:
                            accounts.append(account)
                    ctx['accounts'] = accounts
            except Exception as e:
                ctx['error'] = str(e)
                logger.exception("Error verifying connection")
        
        elif step == 'test' and connection:
            try:
                service = GoCardlessService(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri or callback_url
                )
                requisition = service.get_requisition(connection.requisition_id)
                account_ids = requisition.get('accounts', [])
                
                if account_ids:
                    # Test balances on first account
                    account_id = account_ids[0]
                    balances = service.get_balances(account_id)
                    account_details = service.get_account_details(account_id)
                    
                    ctx['test_account_id'] = account_id
                    ctx['test_balances'] = balances
                    ctx['test_account'] = account_details
                    ctx['test_success'] = True
            except Exception as e:
                ctx['error'] = str(e)
                ctx['test_success'] = False
                logger.exception("Error testing balances")
        
        return ctx

    def post(self, request, *args, **kwargs):
        """Handle form submissions for each step."""
        organizer = request.organizer
        step = request.POST.get('step', 'credentials')
        action = request.POST.get('action', '')
        
        if step == 'credentials':
            client_id = request.POST.get('client_id', '').strip()
            client_secret = request.POST.get('client_secret', '').strip()
            redirect_uri = request.POST.get('redirect_uri', '').strip()
            
            # Handle test action
            if action == 'test':
                if not client_id or not client_secret:
                    messages.error(request, _('Please enter both Client ID and Client Secret to test.'))
                    return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=credentials")
                
                # Test credentials
                callback_url = build_absolute_uri(
                    'plugins:pretix_bank_sync:callback',
                    kwargs={'organizer': organizer.slug}
                )
                
                try:
                    service = GoCardlessService(
                        client_id=client_id,
                        client_secret=client_secret,
                        redirect_uri=redirect_uri or callback_url,
                    )
                    # Actually test credentials by trying to list institutions
                    institutions = service.list_institutions(country='GB')
                    messages.success(request, _('Credentials test successful! Your credentials are valid and can connect to GoCardless API.'))
                except Exception as e:
                    error_msg = str(e)
                    if 'authenticate' in error_msg.lower() or 'token' in error_msg.lower() or '401' in error_msg or '403' in error_msg:
                        messages.error(request, _('Credentials test failed: Authentication failed. Please check your Secret ID and Secret Key.'))
                    else:
                        messages.warning(request, _('Credentials test failed: {}').format(error_msg))
                    logger.exception("Error testing credentials")
                
                # Store form values in session temporarily so they persist after redirect
                # Note: client_secret is stored temporarily for form preservation only
                request.session['wizard_credentials'] = {
                    'client_id': client_id,
                    'client_secret': client_secret,  # Temporary storage for form preservation
                    'redirect_uri': redirect_uri,
                }
                # Redirect back to credentials step
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=credentials&tested=1")
            
            # Save credentials
            if not client_id or not client_secret:
                messages.error(request, _('Client ID and Client Secret are required.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=credentials")
            
            organizer.settings.set('pretix_bank_sync_gocardless_client_id', client_id)
            organizer.settings.set('pretix_bank_sync_gocardless_client_secret', client_secret)
            organizer.settings.set('pretix_bank_sync_gocardless_redirect_uri', redirect_uri)
            
            # Clear session data after saving
            request.session.pop('wizard_credentials', None)
            
            messages.success(request, _('Credentials saved successfully.'))
            # Continue to select_country step
            return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=select_country")
        
        elif step == 'select_country':
            country = request.POST.get('country', '').strip()
            
            if not country:
                messages.error(request, _('Please select a country.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=select_country")
            
            # Store selected country in session
            request.session['gocardless_selected_country'] = country
            
            # Preload banks for the selected country
            client_id = organizer.settings.get('pretix_bank_sync_gocardless_client_id', '')
            client_secret = organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
            redirect_uri = organizer.settings.get('pretix_bank_sync_gocardless_redirect_uri', '')
            
            if client_id and client_secret:
                try:
                    callback_url = build_absolute_uri(
                        'plugins:pretix_bank_sync:callback',
                        kwargs={'organizer': organizer.slug}
                    )
                    
                    service = GoCardlessService(
                        client_id=client_id,
                        client_secret=client_secret,
                        redirect_uri=redirect_uri or callback_url,
                    )
                    
                    # Fetch institutions for the selected country
                    institutions = service.list_institutions(country=country)
                    logger.info(f"Fetched {len(institutions)} institutions for country {country}")
                    
                    # Ensure institutions are JSON-serializable (convert to list of dicts if needed)
                    institutions_list = []
                    for inst in institutions:
                        if isinstance(inst, dict):
                            institutions_list.append(inst)
                            logger.debug(f"Institution (dict): {inst.get('id', 'no-id')} - {inst.get('name', 'no-name')}")
                        else:
                            # Convert object to dict
                            inst_dict = {
                                'id': getattr(inst, 'id', getattr(inst, 'bic', '')),
                                'name': getattr(inst, 'name', ''),
                                'bic': getattr(inst, 'bic', ''),
                            }
                            institutions_list.append(inst_dict)
                            logger.debug(f"Institution (object): {inst_dict['id']} - {inst_dict['name']}")
                    
                    logger.info(f"Converted {len(institutions_list)} institutions to list format")
                    
                    # Store institutions in session for the next step
                    request.session['gocardless_institutions'] = institutions_list
                    logger.info(f"Stored {len(institutions_list)} institutions in session")
                    messages.success(request, _('Country selected. {} banks found. Now choose your bank...').format(len(institutions_list)))
                except Exception as e:
                    logger.exception("Error loading banks for country")
                    messages.warning(request, _('Country selected, but failed to load banks: {}').format(str(e)))
                    # Still proceed to bank selection - it will load via AJAX
            else:
                messages.success(request, _('Country selected. Now choose your bank...'))
            
            return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=select_bank")
        
        elif step == 'select_bank':
            institution_id = request.POST.get('institution_id', '').strip()
            country = request.session.get('gocardless_selected_country', '')
            
            if not country:
                messages.error(request, _('Please select a country first.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=select_country")
            
            if not institution_id:
                messages.error(request, _('Please select a bank.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=select_bank")
            
            # Store selected institution in session for next step
            request.session['gocardless_selected_institution'] = institution_id
            messages.success(request, _('Bank selected. Proceeding to create requisition...'))
            return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=create_requisition")
        
        elif step == 'create_requisition':
            # Get selected institution from session
            institution_id = request.session.get('gocardless_selected_institution')
            if not institution_id:
                messages.error(request, _('No bank selected. Please go back and select a bank.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=select_bank")
            
            # Create requisition
            client_id = organizer.settings.get('pretix_bank_sync_gocardless_client_id', '')
            client_secret = organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
            redirect_uri = organizer.settings.get('pretix_bank_sync_gocardless_redirect_uri', '')
            callback_url = build_absolute_uri(
                'plugins:pretix_bank_sync:callback',
                kwargs={'organizer': organizer.slug}
            )
            
            try:
                service = GoCardlessService(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri or callback_url
                )
                
                requisition_data = service.create_requisition_link(
                    redirect_url=callback_url,
                    institution_id=institution_id
                )
                
                requisition_id = requisition_data.get('id')
                auth_link = requisition_data.get('link')
                
                if not requisition_id or not auth_link:
                    messages.error(request, _('Failed to create requisition. Please try again.'))
                    return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=create_requisition")
                
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
                
                # Store auth link in session for authorize step
                request.session['gocardless_auth_link'] = auth_link
                messages.success(request, _('Requisition created successfully. Ready to authorize.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=authorize")
                
            except Exception as e:
                logger.exception("Error creating requisition")
                messages.error(request, _('An error occurred: {}').format(str(e)))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=create_requisition")
        
        elif step == 'authorize':
            # Redirect to authorization
            auth_link = request.session.get('gocardless_auth_link')
            if auth_link:
                return redirect(auth_link)
            else:
                messages.error(request, _('No authorization link found. Please start over.'))
                return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=select_bank")
        
        elif step == 'test':
            # Just refresh the page to show test results
            return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=test")
        
        # Clear session data if starting over
        if step == 'credentials':
            request.session.pop('gocardless_selected_institution', None)
            request.session.pop('gocardless_auth_link', None)
        
        return redirect(reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug}))


# Keep existing views below
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
        
        # GoCardless settings
        gocardless_client_id = organizer.settings.get('pretix_bank_sync_gocardless_client_id', '')
        gocardless_client_secret = organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
        
        # Get active tab from request
        active_tab = self.request.GET.get('tab', 'gocardless')  # Default to GoCardless tab
        
        ctx.update({
            'organizer': organizer,
            'connection': connection,
            'recent_transactions': recent_transactions,
            'gocardless_client_id': gocardless_client_id,
            'gocardless_client_secret': gocardless_client_secret,
            'active_tab': active_tab,
        })
        return ctx

    def post(self, request, *args, **kwargs):
        """Handle settings form submission."""
        organizer = request.organizer
        active_tab = request.POST.get('tab', '')
        
        # Handle GoCardless settings
        if active_tab == 'gocardless':
            client_id = request.POST.get('gocardless_client_id', '').strip()
            client_secret = request.POST.get('gocardless_client_secret', '').strip()
            
            organizer.settings.set('pretix_bank_sync_gocardless_client_id', client_id)
            organizer.settings.set('pretix_bank_sync_gocardless_client_secret', client_secret)
            
            messages.success(request, _('GoCardless settings saved successfully.'))
            return redirect(f"{reverse('plugins:pretix_bank_sync:settings', kwargs={'organizer': organizer.slug})}?tab=gocardless")
        
        # If no tab specified, redirect to GoCardless tab
        return redirect(f"{reverse('plugins:pretix_bank_sync:settings', kwargs={'organizer': organizer.slug})}?tab=gocardless")

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


class BankSyncCallbackView(OrganizerPermissionRequiredMixin, View):
    """Handle OAuth callback from GoCardless."""
    permission = 'can_change_organizer_settings'

    def get(self, request, *args, **kwargs):
        organizer = request.organizer
        # GoCardless returns 'ref' parameter with requisition_id
        ref = request.GET.get('ref')
        
        if ref:
            try:
                connection = BankConnection.objects.filter(
                    organizer=organizer,
                    requisition_id=ref
                ).first()
                
                if not connection:
                    messages.error(request, _('No pending connection found.'))
                    return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
                
                # Verify requisition status
                client_id = organizer.settings.get('pretix_bank_sync_gocardless_client_id', '')
                client_secret = organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
                redirect_uri = organizer.settings.get('pretix_bank_sync_gocardless_redirect_uri', '')
                
                callback_url = build_absolute_uri(
                    'plugins:pretix_bank_sync:callback',
                    kwargs={'organizer': organizer.slug}
                )
                
                service = GoCardlessService(
                    client_id=client_id,
                    client_secret=client_secret,
                    redirect_uri=redirect_uri or callback_url
                )
                
                requisition = service.get_requisition(ref)
                status = requisition.get('status', '')
                
                if status == 'LN':  # Linked (authorized)
                    connection.status = BankConnection.STATUS_ACTIVE
                    connection.consent_expires_at = now() + timedelta(days=90)  # Default 90 days
                    connection.save(update_fields=['status', 'consent_expires_at'])
                    messages.success(request, _('Bank account connected successfully!'))
                    # Clear session data
                    request.session.pop('gocardless_auth_link', None)
                    request.session.pop('gocardless_selected_institution', None)
                    # Redirect to connections list instead of wizard
                    return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
                else:
                    connection.status = BankConnection.STATUS_ERROR
                    connection.last_error = f"Requisition status: {status}"
                    connection.last_error_at = now()
                    connection.save(update_fields=['status', 'last_error', 'last_error_at'])
                    messages.error(request, _('Authorization failed. Status: {}').format(status))
                    return redirect(f"{reverse('plugins:pretix_bank_sync:bank_setup_wizard', kwargs={'organizer': organizer.slug})}?step=authorize")
                    
            except Exception as e:
                logger.exception("Error processing GoCardless callback")
                messages.error(request, _('An error occurred: {}').format(str(e)))
                return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
        
        # Handle Enable Banking callback (legacy)
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
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)
        
        # Get GoCardless settings
        client_id = organizer.settings.get('pretix_bank_sync_gocardless_client_id', '')
        client_secret = organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
        redirect_uri = organizer.settings.get('pretix_bank_sync_gocardless_redirect_uri', '')
        
        if not client_id or not client_secret:
            messages.error(request, _('GoCardless credentials not configured.'))
            return redirect('plugins:pretix_bank_sync:settings', organizer=organizer.slug)
        
        try:
            # Use the sync task (it handles GoCardless)
            from .tasks import sync_bank_transactions
            sync_bank_transactions(connection.id)
            messages.success(request, _('Transaction sync initiated. New transactions will be processed shortly.'))
            return redirect('plugins:pretix_bank_sync:transactions', organizer=organizer.slug)
            
        except Exception as e:
            logger.exception("Error manually fetching transactions")
            messages.error(request, _('Error fetching transactions: {}').format(str(e)))
            return redirect('plugins:pretix_bank_sync:connections_list', organizer=organizer.slug)


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


class BankInstitutionsAjaxView(OrganizerPermissionRequiredMixin, View):
    """
    AJAX endpoint to fetch institutions for a country (for Select2).
    """
    permission = 'can_change_organizer_settings'

    def get(self, request, *args, **kwargs):
        organizer = request.organizer
        country = request.GET.get('country', '')
        query = request.GET.get('query', '').lower()
        
        # Get GoCardless settings
        client_id = organizer.settings.get('pretix_bank_sync_gocardless_client_id', '')
        client_secret = organizer.settings.get('pretix_bank_sync_gocardless_client_secret', '')
        redirect_uri = organizer.settings.get('pretix_bank_sync_gocardless_redirect_uri', '')
        
        if not client_id or not client_secret:
            return JsonResponse({
                'results': [],
                'error': 'Credentials not configured'
            }, status=400)
        
        if not country:
            return JsonResponse({
                'results': [],
                'error': 'Country required'
            }, status=400)
        
        try:
            callback_url = build_absolute_uri(
                'plugins:pretix_bank_sync:callback',
                kwargs={'organizer': organizer.slug}
            )
            
            service = GoCardlessService(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri or callback_url
            )
            
            institutions = service.list_institutions(country=country)
            
            # Format for Select2
            results = []
            for inst in institutions:
                # Handle both dict and object-like structures
                if isinstance(inst, dict):
                    inst_id = inst.get('id', inst.get('bic', ''))
                    inst_name = inst.get('name', '')
                    inst_bic = inst.get('bic', '')
                else:
                    # If it's an object with attributes
                    inst_id = getattr(inst, 'id', getattr(inst, 'bic', ''))
                    inst_name = getattr(inst, 'name', '')
                    inst_bic = getattr(inst, 'bic', '')
                
                if not inst_id or not inst_name:
                    continue
                
                # Filter by query if provided
                if query:
                    if query not in inst_name.lower() and query not in (inst_bic or '').lower():
                        continue
                
                display_name = inst_name
                if inst_bic:
                    display_name = f"{inst_name} ({inst_bic})"
                
                results.append({
                    'id': inst_id,
                    'text': display_name,
                    'name': inst_name,
                    'bic': inst_bic,
                })
            
            # Sort by name
            results.sort(key=lambda x: x['name'])
            
            return JsonResponse({
                'results': results,
                'pagination': {
                    'more': False
                }
            })
            
        except Exception as e:
            logger.exception("Error fetching institutions")
            return JsonResponse({
                'results': [],
                'error': str(e)
            }, status=500)


class TransactionMatchReviewView(EventPermissionRequiredMixin, PaginationMixin, ListView):
    """View for reviewing and approving transaction matches (event-level)."""
    permission = 'can_change_orders'
    template_name = 'pretix_bank_sync/control/match_review.html'
    context_object_name = 'transactions'
    model = BankTransaction
    paginate_by = 50

    def get_queryset(self):
        event = get_object_or_404(Event, slug=self.kwargs['event'], organizer__slug=self.kwargs['organizer'])
        
        # Get all transactions that either:
        # 1. Are matched to an order in this event, OR
        # 2. Have pending suggestions for orders in this event, OR
        # 3. Are unmatched but belong to a connection for this organizer (might match later)
        # We'll filter by organizer connection and then show those with orders in this event or pending suggestions
        
        # First get transactions from connections for this organizer
        transactions = BankTransaction.objects.filter(
            connection__organizer=event.organizer
        ).select_related(
            'connection',
            'order',
            'order__event',
            'payment'
        ).prefetch_related(
            'match_suggestions',
            'match_suggestions__order'
        )
        
        # Filter to show:
        # - Transactions matched to orders in this event
        # - Transactions with pending suggestions for orders in this event
        # - Unmatched transactions (no match, no suggestions) that could potentially match this event
        event_transaction_ids = set()
        
        # Matched transactions for this event
        matched = transactions.filter(order__event=event).values_list('id', flat=True)
        event_transaction_ids.update(matched)
        
        # Transactions with pending suggestions for this event
        from .models import TransactionMatchSuggestion
        pending_suggestions = TransactionMatchSuggestion.objects.filter(
            transaction__connection__organizer=event.organizer,
            order__event=event,
            is_approved__isnull=True
        ).values_list('transaction_id', flat=True)
        event_transaction_ids.update(pending_suggestions)
        
        # Unmatched transactions (no match, no suggestions) that could potentially match this event
        # Criteria: same currency as event, state is unchecked or nomatch, no approved suggestions
        unmatched = transactions.filter(
            state__in=[BankTransaction.STATE_UNCHECKED, BankTransaction.STATE_NOMATCH],
            currency=event.currency,
            order__isnull=True  # Not matched to any order
        ).exclude(
            # Exclude transactions that have any approved suggestions (they were reviewed and rejected)
            match_suggestions__is_approved=True
        ).values_list('id', flat=True)
        event_transaction_ids.update(unmatched)
        
        # Return filtered transactions
        return transactions.filter(id__in=event_transaction_ids).order_by('-date', '-created')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        event = get_object_or_404(Event, slug=self.kwargs['event'], organizer__slug=self.kwargs['organizer'])
        
        # Get statistics for this event
        from .models import TransactionMatchSuggestion
        event_transaction_ids = set()
        
        # Matched transactions
        matched = BankTransaction.objects.filter(
            connection__organizer=event.organizer,
            order__event=event
        ).values_list('id', flat=True)
        event_transaction_ids.update(matched)
        
        # Transactions with pending suggestions
        pending_sug = TransactionMatchSuggestion.objects.filter(
            transaction__connection__organizer=event.organizer,
            order__event=event,
            is_approved__isnull=True
        ).values_list('transaction_id', flat=True)
        event_transaction_ids.update(pending_sug)
        
        # Unmatched transactions
        unmatched = BankTransaction.objects.filter(
            connection__organizer=event.organizer,
            state__in=[BankTransaction.STATE_UNCHECKED, BankTransaction.STATE_NOMATCH],
            currency=event.currency,
            order__isnull=True
        ).exclude(
            match_suggestions__is_approved=True
        ).values_list('id', flat=True)
        event_transaction_ids.update(unmatched)
        
        all_transactions = BankTransaction.objects.filter(id__in=event_transaction_ids)
        ctx['total_transactions'] = all_transactions.count()
        ctx['pending_suggestions'] = TransactionMatchSuggestion.objects.filter(
            transaction__connection__organizer=event.organizer,
            order__event=event,
            is_approved__isnull=True
        ).count()
        ctx['matched_transactions'] = all_transactions.filter(state=BankTransaction.STATE_MATCHED).count()
        ctx['pending_approval'] = all_transactions.filter(state=BankTransaction.STATE_PENDING_APPROVAL).count()
        ctx['unmatched_transactions'] = all_transactions.filter(
            state__in=[BankTransaction.STATE_UNCHECKED, BankTransaction.STATE_NOMATCH],
            order__isnull=True
        ).count()
        
        ctx['event'] = event
        ctx['organizer'] = event.organizer
        return ctx


class TransactionMatchApproveView(EventPermissionRequiredMixin, View):
    """Approve a match suggestion."""
    permission = 'can_change_orders'

    def post(self, request, *args, **kwargs):
        event = get_object_or_404(Event, slug=kwargs['event'], organizer__slug=kwargs['organizer'])
        suggestion = get_object_or_404(
            TransactionMatchSuggestion,
            pk=kwargs['suggestion_id'],
            transaction__order__event=event,
            is_approved__isnull=True
        )
        
        try:
            approve_match_suggestion(suggestion.pk, request.user)
            messages.success(request, _('Match approved and transaction processed successfully.'))
        except Exception as e:
            logger.exception("Error approving match suggestion")
            messages.error(request, _('Error approving match: {}').format(str(e)))
        
        return redirect('plugins:pretix_bank_sync:match_review', organizer=event.organizer.slug, event=event.slug)


class TransactionMatchRejectView(EventPermissionRequiredMixin, View):
    """Reject a match suggestion."""
    permission = 'can_change_orders'

    def post(self, request, *args, **kwargs):
        event = get_object_or_404(Event, slug=kwargs['event'], organizer__slug=kwargs['organizer'])
        suggestion = get_object_or_404(
            TransactionMatchSuggestion,
            pk=kwargs['suggestion_id'],
            transaction__order__event=event,
            is_approved__isnull=True
        )
        
        suggestion.is_approved = False
        suggestion.reviewed_at = now()
        suggestion.reviewed_by = request.user
        suggestion.save()
        
        messages.success(request, _('Match suggestion rejected.'))
        return redirect('plugins:pretix_bank_sync:match_review', organizer=event.organizer.slug, event=event.slug)
