"""Compatibility entry point for notification services."""
from .repository import (adopt_legacy_block_notices, ensure_schema,
                         configure_session, _enabled, _insert)
from .incidents import (_key, _remote_inspection_suffix, _queue_block,
                        release_block, record_work_start, _materialize_resumption,
                        _checkpoint_link, _project_checkpoint,
                        _project_checkpoint_suffix, _current_block_key,
                        recover_interrupted_starts)
from .events import (enqueue_current_project_events,
                     cancel_pending_persistent_execution_failure_event,
                     suppress_persistent_failure_for_block, record_merged_plan,
                     enqueue_completion_if_ready, mark_work_available,
                     enqueue_waiting_reason, enqueue_progress, record_process_start,
                     record_planning_publication, enqueue_draft_decision,
                     enqueue_review_failure, enqueue_worker_failure)
from .rendering import _message, _pieces, MESSAGE_LIMIT
from .transport import send_telegram
from .delivery import (dispatch_pending, dispatch_new_pr_notifications,
                       immediate_delivery, flush_project_events)
