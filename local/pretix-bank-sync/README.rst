pretix Bank Sync Plugin
========================

This plugin integrates with Enable Banking Open Banking API to:

- Sync bank account transactions up to 4 times per day
- Automatically match transactions to orders/payments
- Manage bank connection lifecycle (setup, authorization, reauthorization)

Enable Banking is an EU-based (Finland) provider offering PSD2-compliant
Account Information Services (AIS) across 2,500+ banks in 29 European countries.

Installation
------------

Install the plugin using pip::

    pip install pretix-bank-sync

Then enable it in your pretix configuration.

Configuration
-------------

Configure your Enable Banking API credentials in the plugin settings.

You can sign up for Enable Banking at https://enablebanking.com/

License
-------

Copyright 2024 pretix

Licensed under the Apache License, Version 2.0
