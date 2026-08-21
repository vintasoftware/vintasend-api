"""Converts VintaSend notification dataclasses into the wire contract.

List payloads drop the potentially large context and attachment payloads; detail
payloads keep them. Dates always become ISO-8601 UTC strings, and absent dates are
normalised to ``null`` (never omitted) so JSON responses are uniform.

Several field names differ between the Python dataclasses and the wire contract. The
mapping is small enough to keep in one place, which is here:

===================  ======================================
wire                 ``Notification`` / ``OneOffNotification``
===================  ======================================
``createdAt``        ``created``
``updatedAt``        ``modified``
``contextParameters````context_kwargs``
``extraParams``      ``adapter_extra_parameters``
===================  ======================================

Everything else is the same field in snake_case.
"""

import datetime
from typing import Any

from vintasend.services.dataclasses import (
    Notification,
    OneOffNotification,
    StoredAttachment,
)

from .contract import (
    NotificationAttachmentOut,
    OneOffNotificationDetailOut,
    OneOffNotificationOut,
    UserNotificationDetailOut,
    UserNotificationOut,
)


AnyNotification = Notification | OneOffNotification

ListNotificationOut = UserNotificationOut | OneOffNotificationOut

DetailNotificationOut = UserNotificationDetailOut | OneOffNotificationDetailOut


def is_one_off(notification: AnyNotification) -> bool:
    """Discriminate the two notification variants.

    The Python library has no ``isOneOffNotification`` helper like its TypeScript
    sibling, and the two dataclasses are unrelated types rather than a tagged union, so
    an ``isinstance`` check is the discriminator.
    """
    return isinstance(notification, OneOffNotification)


def to_iso(value: datetime.datetime | None) -> str | None:
    """Render a timestamp as an ISO-8601 UTC string, or ``null`` when unset.

    Formatted with exactly three fractional digits and a ``Z`` suffix, which is what
    JavaScript's ``Date.prototype.toISOString`` produces. Python's ``isoformat`` would
    give ``+00:00`` and drop the fraction on a whole second, so the same notification
    would serialise differently through this API than through the TypeScript one.

    Naive datetimes are read as UTC rather than rejected: a backend that stores
    timestamps without a timezone would otherwise make every response unserialisable,
    and UTC is what every VintaSend backend writes.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    utc = value.astimezone(datetime.timezone.utc)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S')}.{utc.microsecond // 1000:03d}Z"


def to_json_value(value: Any) -> Any | None:
    """Normalise an absent payload to ``null``.

    ``NotificationContextDict`` is a ``dict`` subclass and attachment-free contexts are
    plain dicts, so nothing further is needed to make these JSON-serialisable.
    """
    return None if value is None else value


def serialize_attachment(attachment: StoredAttachment) -> NotificationAttachmentOut:
    return NotificationAttachmentOut(
        id=str(attachment.id),
        filename=attachment.filename,
        contentType=attachment.content_type,
        size=attachment.size,
        description=attachment.description,
    )


def serialize_attachments(
    attachments: list[StoredAttachment] | None,
) -> list[NotificationAttachmentOut]:
    if not attachments:
        return []
    return [serialize_attachment(attachment) for attachment in attachments]


def _shared_fields(notification: AnyNotification) -> dict[str, Any]:
    return {
        "id": str(notification.id),
        "notificationType": notification.notification_type,
        "title": notification.title,
        "contextName": notification.context_name,
        "status": notification.status,
        "sendAfter": to_iso(notification.send_after),
        "sentAt": to_iso(notification.sent_at),
        "readAt": to_iso(notification.read_at),
        "createdAt": to_iso(notification.created),
        "updatedAt": to_iso(notification.modified),
        "adapterUsed": notification.adapter_used,
        "bodyTemplate": notification.body_template,
        "subjectTemplate": notification.subject_template,
        "gitCommitSha": notification.git_commit_sha,
        "requestedTemplateVersion": notification.requested_template_version,
        "usedTemplateVersion": notification.used_template_version,
        "tenant": notification.tenant,
    }


def _detail_fields(notification: AnyNotification) -> dict[str, Any]:
    return {
        "contextUsed": to_json_value(notification.context_used),
        "contextParameters": to_json_value(notification.context_kwargs),
        "extraParams": to_json_value(notification.adapter_extra_parameters),
        "attachments": serialize_attachments(notification.attachments),
    }


def serialize_user_notification(notification: Notification) -> UserNotificationOut:
    return UserNotificationOut(
        userId=str(notification.user_id),
        **_shared_fields(notification),
    )


def serialize_one_off_notification(notification: OneOffNotification) -> OneOffNotificationOut:
    return OneOffNotificationOut(
        emailOrPhone=notification.email_or_phone,
        firstName=notification.first_name,
        lastName=notification.last_name,
        **_shared_fields(notification),
    )


def serialize_notification(notification: AnyNotification) -> ListNotificationOut:
    """Serialize either notification variant for list responses."""
    if isinstance(notification, OneOffNotification):
        return serialize_one_off_notification(notification)
    return serialize_user_notification(notification)


def serialize_notification_detail(notification: AnyNotification) -> DetailNotificationOut:
    """Serialize either notification variant for detail responses."""
    if isinstance(notification, OneOffNotification):
        return OneOffNotificationDetailOut(
            emailOrPhone=notification.email_or_phone,
            firstName=notification.first_name,
            lastName=notification.last_name,
            **_shared_fields(notification),
            **_detail_fields(notification),
        )

    return UserNotificationDetailOut(
        userId=str(notification.user_id),
        **_shared_fields(notification),
        **_detail_fields(notification),
    )
