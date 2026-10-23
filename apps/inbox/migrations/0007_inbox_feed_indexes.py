from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.contrib.postgres.search import SearchVector
from django.db import migrations, models


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction. inbox_message
    # is written to by the sync and webhooks, so the indexes are built without
    # taking a write lock on it.
    atomic = False

    dependencies = [
        ("inbox", "0006_inboxmessage_api_refreshed_at"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "-received_at", "-id"], name="inbox_msg_ws_recv_id"),
        ),
        AddIndexConcurrently(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "assigned_to", "-received_at"], name="inbox_msg_ws_assign_recv"),
        ),
        AddIndexConcurrently(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "sentiment", "-received_at"], name="inbox_msg_ws_sent_recv"),
        ),
        AddIndexConcurrently(
            model_name="inboxmessage",
            index=GinIndex(
                SearchVector("body", "sender_name", "sender_handle", config="simple"),
                name="inbox_msg_search_gin",
            ),
        ),
    ]
