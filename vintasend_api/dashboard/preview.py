"""Builds a rendered preview of a notification's templates.

Templates are fetched from the template source at the commit SHA persisted with the
notification, so a preview always shows the template as it was when the notification was
created. Notifications that are still pending fall back to the current ``main`` commit,
since they have not been rendered yet.
"""

from typing import Any

from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    EmailTemplateContent,
)

from .contract import NotificationPreviewOut
from .errors import ApiError
from .service import AnyNotification, ServiceCaller
from .template_source import TemplateSourceClient, TemplateSourceError


def build_notification_preview(
    service: ServiceCaller,
    template_client: TemplateSourceClient,
    notification: AnyNotification,
) -> NotificationPreviewOut:
    git_commit_sha = notification.git_commit_sha

    if not git_commit_sha and notification.status == "PENDING_SEND":
        git_commit_sha = _fetch(template_client.get_latest_main_commit_sha)

    if not git_commit_sha:
        raise ApiError(
            "PREVIEW_UNAVAILABLE",
            "This notification does not have a tracked git commit SHA and is not pending "
            "send, so preview is unavailable.",
        )

    body_template_content = _fetch(
        template_client.get_template_content_by_commit,
        notification.body_template,
        git_commit_sha,
    )

    subject_template_content = ""
    if notification.subject_template:
        subject_template_content = _fetch(
            template_client.get_template_content_by_commit,
            notification.subject_template,
            git_commit_sha,
        )

    rendered = service.render_email_template_from_content(
        notification,
        # `EmailTemplateContent.subject_template` is a plain `str`, so a notification
        # with no subject template renders an empty subject rather than passing `None`
        # into a renderer that does not accept it.
        EmailTemplateContent(
            subject_template=subject_template_content,
            body_template=body_template_content,
        ),
        _context_for(service, notification),
    )

    return NotificationPreviewOut(
        gitCommitSha=git_commit_sha,
        bodyTemplatePath=notification.body_template,
        subjectTemplatePath=notification.subject_template or None,
        renderedBodyHtml=rendered.body,
        renderedSubjectHtml=rendered.subject,
    )


def _context_for(service: ServiceCaller, notification: AnyNotification) -> dict[str, Any]:
    """Reuse the stored context, or regenerate it from the notification's generator.

    ``render_email_template_from_content`` takes a materialised context and generates
    none of its own, so resolving which context to render with happens here. The
    TypeScript sibling passes either shape down into its service instead; the decision
    and its outcome are the same.
    """
    if notification.context_used is not None:
        return dict(notification.context_used)
    return service.get_notification_context(notification)


def _fetch(operation: Any, *args: Any) -> Any:
    """Run a template-source call, reporting a source failure as the contract's 502."""
    try:
        return operation(*args)
    except TemplateSourceError as error:
        raise ApiError.upstream(str(error)) from error
