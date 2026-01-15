pretix Bank Sync Plugin
========================

This plugin integrates with GoCardless Bank Account Data API to:

- Sync bank account transactions up to 4 times per day
- Automatically match transactions to orders/payments
- Manage bank connection lifecycle (setup, authorization, reauthorization)

Installation
------------

Install the plugin using pip::

    pip install pretix-bank-sync

Then enable it in your pretix configuration.

Configuration
-------------

Configure your GoCardless API credentials in the plugin settings.

License
-------

Copyright 2024 pretix

Licensed under the Apache License, Version 2.0
