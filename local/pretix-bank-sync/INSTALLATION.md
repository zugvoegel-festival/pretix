# Installation Instructions

## Development Mode Installation (Recommended)

If you're developing or testing the plugin locally:

1. **Activate your pretix virtual environment** (if you have one):
   ```bash
   source /path/to/pretix/venv/bin/activate
   # or
   source env/bin/activate
   ```

2. **Install the plugin in development mode**:
   ```bash
   cd /Users/manuel.huettel/Repos/privat/pretix/local/pretix-bank-sync
   pip install -e .
   ```

   This will:
   - Install the plugin and its dependencies (including `gocardless-pro`)
   - Make the plugin discoverable by pretix
   - Allow you to edit the code without reinstalling

## Production Installation

For production use:

1. **Install from the directory**:
   ```bash
   cd /Users/manuel.huettel/Repos/privat/pretix/local/pretix-bank-sync
   pip install .
   ```

2. **Or install from a wheel** (if you've built one):
   ```bash
   pip install pretix-bank-sync-*.whl
   ```

## Enable the Plugin

After installation:

1. **Run migrations** (if needed):
   ```bash
   python src/manage.py migrate pretix_bank_sync
   ```

2. **Enable the plugin in pretix**:
   - Go to your organizer settings
   - Navigate to "Plugins"
   - Find "Bank Sync" in the list
   - Click "Enable"

3. **Configure the plugin**:
   - Go to Organizer Settings → Bank Sync
   - Enter your GoCardless API credentials:
     - Client ID
     - Client Secret
     - Redirect URI (configured in GoCardless dashboard)
   - Choose Sandbox or Production environment
   - Save settings

4. **Connect a bank account**:
   - Click "Connect Bank Account" in the Bank Sync settings
   - You'll be redirected to GoCardless to authorize access
   - After authorization, you'll be redirected back

## Dependencies

The plugin requires:
- `gocardless-pro>=1.0.0` (will be installed automatically)
- `requests` (usually already installed with pretix)

## Troubleshooting

### Plugin not appearing in the list

- Make sure you've installed it: `pip list | grep pretix-bank-sync`
- Restart your pretix web server and celery workers
- Check that the entry point is correct in `pyproject.toml`

### Import errors

- Make sure all dependencies are installed: `pip install -r requirements.txt` (if you have one)
- Verify the plugin is in your Python path

### Migration errors

- Run: `python src/manage.py migrate pretix_bank_sync`
- If tables already exist, you might need to fake the migration: `python src/manage.py migrate pretix_bank_sync --fake`

## Next Steps

After installation and configuration:

1. The plugin will automatically sync transactions up to 4 times per day
2. Transactions will be automatically matched to orders based on:
   - Currency (primary filter)
   - Order codes or invoice numbers in transaction references
3. Matched transactions will create and confirm payments automatically
4. View all transactions in Organizer Settings → Bank Sync → Transactions
