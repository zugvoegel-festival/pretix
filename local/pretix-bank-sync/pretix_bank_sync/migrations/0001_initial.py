# Generated migration for Bank Sync plugin

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('pretixbase', '0296_invoice_invoice_from_state'),  # Latest pretixbase migration
    ]

    operations = [
        migrations.CreateModel(
            name='BankConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requisition_id', models.CharField(db_index=True, help_text='GoCardless requisition ID', max_length=255, unique=True)),
                ('access_token', models.TextField(blank=True, help_text='GoCardless access token (encrypted)')),
                ('status', models.CharField(choices=[('pending', 'Pending authorization'), ('active', 'Active'), ('expired', 'Expired - reauthorization required'), ('error', 'Error'), ('revoked', 'Revoked')], db_index=True, default='pending', max_length=20)),
                ('last_sync', models.DateTimeField(blank=True, help_text='Last successful sync timestamp', null=True)),
                ('sync_count_today', models.IntegerField(default=0, help_text='Number of syncs performed today')),
                ('last_sync_date', models.DateField(blank=True, help_text='Date of last sync (for resetting daily counter)', null=True)),
                ('consent_expires_at', models.DateTimeField(blank=True, help_text='When the bank consent expires', null=True)),
                ('consent_id', models.CharField(blank=True, help_text='GoCardless consent ID', max_length=255)),
                ('last_error', models.TextField(blank=True, help_text='Last error message if status is error')),
                ('last_error_at', models.DateTimeField(blank=True, help_text='When the last error occurred', null=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('organizer', models.ForeignKey(help_text='The organizer this bank connection belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='bank_connections', to='pretixbase.Organizer')),
            ],
            options={
                'verbose_name': 'Bank Connection',
                'verbose_name_plural': 'Bank Connections',
                'ordering': ('-created',),
            },
        ),
        migrations.CreateModel(
            name='BankTransaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transaction_id', models.CharField(db_index=True, help_text='GoCardless transaction ID', max_length=255, unique=True)),
                ('account_id', models.CharField(db_index=True, help_text='GoCardless account ID', max_length=255)),
                ('amount', models.DecimalField(decimal_places=2, help_text='Transaction amount', max_digits=13)),
                ('currency', models.CharField(help_text='Transaction currency', max_length=10)),
                ('date', models.DateField(help_text='Transaction date')),
                ('booking_date', models.DateField(blank=True, help_text='Booking date', null=True)),
                ('remittance_information_unstructured', models.TextField(blank=True, help_text='Unstructured remittance information')),
                ('remittance_information_structured', models.TextField(blank=True, help_text='Structured remittance information')),
                ('reference', models.TextField(blank=True, help_text='Combined reference information for matching')),
                ('debtor_name', models.CharField(blank=True, help_text='Debtor name', max_length=255)),
                ('debtor_account_iban', models.CharField(blank=True, help_text='Debtor IBAN', max_length=34)),
                ('creditor_name', models.CharField(blank=True, help_text='Creditor name', max_length=255)),
                ('creditor_account_iban', models.CharField(blank=True, help_text='Creditor IBAN', max_length=34)),
                ('state', models.CharField(choices=[('unchecked', 'Unchecked'), ('nomatch', 'No match found'), ('matched', 'Matched to order'), ('error', 'Error'), ('duplicate', 'Duplicate transaction'), ('discarded', 'Manually discarded')], db_index=True, default='unchecked', max_length=20)),
                ('error_message', models.TextField(blank=True, help_text='Error message if matching failed')),
                ('raw_data', models.JSONField(default=dict, help_text='Raw transaction data from GoCardless')),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('connection', models.ForeignKey(help_text='The bank connection this transaction belongs to', on_delete=django.db.models.deletion.CASCADE, related_name='transactions', to='pretix_bank_sync.BankConnection')),
                ('order', models.ForeignKey(blank=True, help_text='Matched order', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bank_transactions', to='pretixbase.Order')),
                ('payment', models.ForeignKey(blank=True, help_text='Created payment object', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bank_transactions', to='pretixbase.OrderPayment')),
            ],
            options={
                'verbose_name': 'Bank Transaction',
                'verbose_name_plural': 'Bank Transactions',
                'ordering': ('-date', '-created'),
            },
        ),
        migrations.AddIndex(
            model_name='banktransaction',
            index=models.Index(fields=['connection', 'state'], name='pretix_bank_connect_0_idx'),
        ),
        migrations.AddIndex(
            model_name='banktransaction',
            index=models.Index(fields=['date', 'state'], name='pretix_bank_date_st_idx'),
        ),
    ]
