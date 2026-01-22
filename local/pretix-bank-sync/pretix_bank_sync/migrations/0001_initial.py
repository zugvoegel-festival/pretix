# Generated migration for Bank Sync plugin
# Consolidated migration combining all previous migrations

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import pretix.base.models.base


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('pretixbase', '0296_invoice_invoice_from_state'),  # Latest pretixbase migration
    ]

    operations = [
        # Create BankConnection model
        migrations.CreateModel(
            name='BankConnection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requisition_id', models.CharField(db_index=True, help_text='GoCardless requisition ID', max_length=255, unique=True)),
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
            bases=(models.Model, pretix.base.models.base.LoggingMixin),
        ),
        # Create BankTransaction model
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
                ('state', models.CharField(choices=[('unchecked', 'Unchecked'), ('nomatch', 'No match found'), ('matched', 'Matched to order'), ('pending_approval', 'Pending approval'), ('error', 'Error'), ('duplicate', 'Duplicate transaction'), ('discarded', 'Manually discarded')], db_index=True, default='unchecked', max_length=20)),
                ('error_message', models.TextField(blank=True, help_text='Error message if matching failed')),
                ('is_partial_payment', models.BooleanField(default=False, help_text='Whether this transaction is part of a partial payment')),
                ('payment_group_id', models.CharField(blank=True, db_index=True, help_text='Group ID for transactions that belong to the same payment', max_length=255, null=True)),
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
            bases=(models.Model, pretix.base.models.base.LoggingMixin),
        ),
        # Add indexes for BankTransaction
        migrations.AddIndex(
            model_name='banktransaction',
            index=models.Index(fields=['connection', 'state'], name='pretix_bank_connect_c3c7b9_idx'),
        ),
        migrations.AddIndex(
            model_name='banktransaction',
            index=models.Index(fields=['date', 'state'], name='pretix_bank_date_da004d_idx'),
        ),
        # Create TransactionMatchSuggestion model
        migrations.CreateModel(
            name='TransactionMatchSuggestion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('match_type', models.CharField(choices=[('exact_code', 'Exact order code match'), ('fuzzy_code', 'Order code within other words'), ('sender_name', 'Sender name match'), ('amount_only', 'Amount match only')], help_text='Type of match', max_length=20)),
                ('confidence_score', models.FloatField(help_text='Confidence score (0.0 to 1.0)')),
                ('match_reason', models.TextField(help_text='Explanation of why this match was suggested')),
                ('amount_match', models.BooleanField(default=False, help_text='Whether the amount matches exactly')),
                ('amount_difference', models.DecimalField(blank=True, decimal_places=2, help_text='Difference between transaction amount and order pending amount', max_digits=13, null=True)),
                ('is_approved', models.BooleanField(blank=True, default=None, help_text='Whether this suggestion was approved (None = pending, True = approved, False = rejected)', null=True)),
                ('reviewed_at', models.DateTimeField(blank=True, help_text='When this suggestion was reviewed', null=True)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('updated', models.DateTimeField(auto_now=True)),
                ('order', models.ForeignKey(help_text='The suggested order match', on_delete=django.db.models.deletion.CASCADE, related_name='transaction_match_suggestions', to='pretixbase.Order')),
                ('reviewed_by', models.ForeignKey(blank=True, help_text='User who reviewed this suggestion', null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('transaction', models.ForeignKey(help_text='The transaction this suggestion is for', on_delete=django.db.models.deletion.CASCADE, related_name='match_suggestions', to='pretix_bank_sync.BankTransaction')),
            ],
            options={
                'verbose_name': 'Transaction Match Suggestion',
                'verbose_name_plural': 'Transaction Match Suggestions',
                'ordering': ('-confidence_score', '-created'),
            },
            bases=(models.Model, pretix.base.models.base.LoggingMixin),
        ),
        # Add indexes for TransactionMatchSuggestion
        migrations.AddIndex(
            model_name='transactionmatchsuggestion',
            index=models.Index(fields=['transaction', 'is_approved'], name='pretix_bank_transac_c591ad_idx'),
        ),
        migrations.AddIndex(
            model_name='transactionmatchsuggestion',
            index=models.Index(fields=['order', 'is_approved'], name='pretix_bank_order_i_ac2b5f_idx'),
        ),
    ]
