"""Structured human-readable event intelligence for live and replay modes."""

from kdtb.alerts.service import (
    ResearchAlertBuilder,
    ResearchAlertConsumer,
    historical_replay_assessment_time,
    render_alert,
)

__all__ = [
    "ResearchAlertBuilder",
    "ResearchAlertConsumer",
    "historical_replay_assessment_time",
    "render_alert",
]
