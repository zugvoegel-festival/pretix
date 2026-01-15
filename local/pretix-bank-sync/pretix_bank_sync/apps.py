from django.utils.translation import gettext_lazy
from . import __version__

try:
    from pretix.base.plugins import PluginConfig, PLUGIN_LEVEL_ORGANIZER
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    name = "pretix_bank_sync"
    verbose_name = "Bank Sync"
    level = PLUGIN_LEVEL_ORGANIZER

    class PretixPluginMeta:
        name = gettext_lazy("Bank Sync")
        author = "pretix"
        description = gettext_lazy("Sync bank account transactions via GoCardless and automatically match them to orders")
        visible = True
        version = __version__
        category = "INTEGRATION"
        compatibility = "pretix>=2.7.0"

    def ready(self):
        from . import signals  # NOQA
