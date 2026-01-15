from django.urls import re_path

from .views import (
    BankSyncSettingsView,
    BankSyncAuthorizeView,
    BankSyncCallbackView,
    BankSyncTransactionsView,
)

urlpatterns = [
    re_path(
        r'^control/organizer/(?P<organizer>[^/]+)/bank-sync/settings$',
        BankSyncSettingsView.as_view(),
        name='settings'
    ),
    re_path(
        r'^control/organizer/(?P<organizer>[^/]+)/bank-sync/authorize$',
        BankSyncAuthorizeView.as_view(),
        name='authorize'
    ),
    re_path(
        r'^control/organizer/(?P<organizer>[^/]+)/bank-sync/callback$',
        BankSyncCallbackView.as_view(),
        name='callback'
    ),
    re_path(
        r'^control/organizer/(?P<organizer>[^/]+)/bank-sync/transactions$',
        BankSyncTransactionsView.as_view(),
        name='transactions'
    ),
]
