from django.db import models


class DataDeletionRequest(models.Model):
    """A platform-initiated deletion (Meta's data deletion callback) and its outcome.

    ``request_key`` identifies the signed request, so Meta retrying or a captured
    request being replayed returns the original confirmation code instead of
    deleting twice.
    """

    class Source(models.TextChoices):
        META = "meta", "Meta"

    code = models.CharField(max_length=32, unique=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    external_user_id = models.CharField(max_length=64)
    request_key = models.CharField(max_length=64, unique=True)
    accounts_removed = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "legal_data_deletion_request"

    def __str__(self):
        return f"{self.source} {self.code}"
