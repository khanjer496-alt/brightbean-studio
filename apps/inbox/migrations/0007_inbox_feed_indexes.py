from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.operations import AddIndexConcurrently
from django.contrib.postgres.search import SearchVector
from django.db import migrations, models


class AddIndexConcurrentlyIdempotent(AddIndexConcurrently):
    """``AddIndexConcurrently`` that survives being run again after a partial failure.

    This migration is not atomic, so a failure partway leaves the indexes
    already built in place (and the one that failed as an INVALID index, which
    ``IF NOT EXISTS`` would wrongly accept). Re-running then died on "already
    exists". Dropping any leftover of this index first (``DROP INDEX
    CONCURRENTLY IF EXISTS``, a no-op on a first run) and building it again
    ends in the same schema. Reversing is unchanged: it drops the index.
    """

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.remove_index(model, self.index, concurrently=True)
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction. inbox_message
    # is written to by the sync and webhooks, so the indexes are built without
    # taking a write lock on it.
    atomic = False

    dependencies = [
        ("inbox", "0006_inboxmessage_api_refreshed_at"),
    ]

    operations = [
        AddIndexConcurrentlyIdempotent(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "-received_at", "-id"], name="inbox_msg_ws_recv_id"),
        ),
        AddIndexConcurrentlyIdempotent(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "created_at"], name="inbox_msg_ws_created"),
        ),
        AddIndexConcurrentlyIdempotent(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "assigned_to", "-received_at"], name="inbox_msg_ws_assign_recv"),
        ),
        AddIndexConcurrentlyIdempotent(
            model_name="inboxmessage",
            index=models.Index(fields=["workspace", "sentiment", "-received_at"], name="inbox_msg_ws_sent_recv"),
        ),
        AddIndexConcurrentlyIdempotent(
            model_name="inboxmessage",
            index=GinIndex(
                SearchVector("body", "sender_name", "sender_handle", config="simple"),
                name="inbox_msg_search_gin",
            ),
        ),
    ]
