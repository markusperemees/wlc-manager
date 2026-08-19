from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from wlc_manager.artifacts import MonthlyArtifactReconciler, PosterGenerator
from wlc_manager.config import Settings
from wlc_manager.database import (
    Database,
    HeartbeatRepository,
    LeaseLockRepository,
    NotificationRepository,
    PasswordRepository,
)
from wlc_manager.notifications import (
    MessageRelay,
    MonthlyNotificationReconciler,
    NotificationOutcome,
    NotificationService,
    SmtpRelay,
)
from wlc_manager.observability import process_run, process_step
from wlc_manager.password_application import (
    PasswordApplicationOutcome,
    PasswordApplicationService,
)
from wlc_manager.passwords import PasswordGenerator
from wlc_manager.reconciliation import MonthlyPasswordReconciler
from wlc_manager.scheduling import YearMonth, wlan_should_be_enabled
from wlc_manager.wlc import AireOsWlcClient, ManagedWlc

SCHEDULER_SERVICE_NAME = "wlc-manager-scheduler"
MONTHLY_LOCK_NAME = "monthly-password-reconciliation"
WLC_MUTATION_LOCK_NAME = "wlc-mutation"


class SchedulerRuntime:
    """Long-running in-process scheduler with restart reconciliation."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        now: Callable[[ZoneInfo], datetime] | None = None,
        wlc_client_factory: Callable[[], ManagedWlc] | None = None,
        poster_generator: PosterGenerator | None = None,
        notification_relay: MessageRelay | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.timezone = ZoneInfo(settings.app.timezone)
        self.now = now or (lambda timezone: datetime.now(timezone))
        self.scheduler = BlockingScheduler(timezone=self.timezone)
        self.heartbeat_repository = HeartbeatRepository(database)
        self.lock_repository = LeaseLockRepository(database)
        self.wlc_client_factory = wlc_client_factory or (
            lambda: AireOsWlcClient.from_secret_files(settings.wlc, settings.secrets)
        )
        password_repository = PasswordRepository(database)
        self.reconciler = MonthlyPasswordReconciler(
            password_repository,
            PasswordGenerator(
                password_repository,
                prefix=settings.password.prefix,
                random_digits=settings.password.random_digits,
                history_size=settings.password.history_size,
            ),
            dictionary_path=settings.password.dictionary_path,
        )
        self.artifact_reconciler = MonthlyArtifactReconciler(
            password_repository,
            poster_generator or PosterGenerator(settings.artifacts),
            ssid=settings.wlc.ssid,
        )
        self.notification_reconciler = MonthlyNotificationReconciler(
            NotificationService(
                password_repository,
                NotificationRepository(database),
                notification_relay or SmtpRelay(settings.smtp),
                settings.smtp,
                ssid=settings.wlc.ssid,
            )
        )
        self.password_application_service = PasswordApplicationService(
            password_repository,
            self.wlc_client_factory,
        )
        self._register_jobs()

    def _register_jobs(self) -> None:
        now = self.now(self.timezone)
        self.scheduler.add_job(
            self._heartbeat_job,
            trigger="interval",
            seconds=30,
            id="scheduler-heartbeat",
            name="Scheduler heartbeat",
            next_run_time=now,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._wlan_reconciliation_job,
            trigger="interval",
            seconds=self.settings.scheduler.wlan_check_seconds,
            id="wlan-state-reconciliation",
            name="WLAN desired-state reconciliation",
            next_run_time=now,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self.settings.scheduler.wlan_check_seconds,
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._monthly_reconciliation_job,
            trigger="interval",
            seconds=self.settings.scheduler.monthly_check_seconds,
            id="monthly-password-reconciliation",
            name="Monthly password reconciliation",
            next_run_time=now,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=self.settings.scheduler.monthly_check_seconds,
            replace_existing=True,
        )

    def run_forever(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)

    def scheduled_job_ids(self) -> set[str]:
        return {job.id for job in self.scheduler.get_jobs()}

    def heartbeat_once(self) -> None:
        self._heartbeat_job()

    def reconcile_monthly_once(self) -> None:
        self._monthly_reconciliation_job()

    def reconcile_wlan_once(self) -> None:
        self._wlan_reconciliation_job()

    def _heartbeat_job(self) -> None:
        self.heartbeat_repository.beat(SCHEDULER_SERVICE_NAME)

    def _wlan_reconciliation_job(self) -> None:
        with process_run("wlan_state_reconciliation", trigger="scheduler") as logger:
            owner = str(logger.extra["run_id"])
            with process_step(logger, "acquire_wlc_lock"):
                acquired = self.lock_repository.acquire(
                    name=WLC_MUTATION_LOCK_NAME,
                    owner=owner,
                    ttl_seconds=max(120, min(self.settings.scheduler.wlan_check_seconds * 2, 600)),
                )
            if not acquired:
                logger.info(
                    "WLAN reconciliation skipped because another run owns the WLC lock",
                    extra={
                        "event": "process_lock_contended",
                        "lock_name": WLC_MUTATION_LOCK_NAME,
                    },
                )
                return

            try:
                moment = self.now(self.timezone)
                desired_enabled = wlan_should_be_enabled(
                    moment,
                    timezone=self.settings.app.timezone,
                    schedule=self.settings.scheduler,
                )
                with process_step(logger, "reconcile_wlan_state"):
                    change = self.wlc_client_factory().set_wlan_enabled(desired_enabled)
                logger.info(
                    "WLAN state reconciled",
                    extra={
                        "event": "wlan_state_reconciled",
                        "wlan_id": change.after.wlan_id,
                        "ssid": change.after.ssid,
                        "desired_enabled": desired_enabled,
                        "before_enabled": change.before.enabled,
                        "after_enabled": change.after.enabled,
                        "state_changed": change.changed,
                    },
                )
            finally:
                released = self.lock_repository.release(name=WLC_MUTATION_LOCK_NAME, owner=owner)
                if not released:
                    logger.warning(
                        "WLC mutation lock was not owned during release",
                        extra={
                            "event": "process_lock_release_missed",
                            "lock_name": WLC_MUTATION_LOCK_NAME,
                        },
                    )

    def _monthly_reconciliation_job(self) -> None:
        with process_run("monthly_password_reconciliation", trigger="scheduler") as logger:
            owner = str(logger.extra["run_id"])
            with process_step(logger, "acquire_process_lock"):
                acquired = self.lock_repository.acquire(
                    name=MONTHLY_LOCK_NAME,
                    owner=owner,
                    ttl_seconds=max(300, min(self.settings.scheduler.monthly_check_seconds, 1800)),
                )
            if not acquired:
                logger.info(
                    "monthly reconciliation skipped because another run owns the lock",
                    extra={"event": "process_lock_contended", "lock_name": MONTHLY_LOCK_NAME},
                )
                return

            try:
                today = self.now(self.timezone).date()
                with process_step(logger, "reconcile_password_periods"):
                    result = self.reconciler.reconcile(
                        today=today,
                        run_id=owner,
                    )
                logger.info(
                    "monthly password periods reconciled",
                    extra={
                        "event": "password_periods_reconciled",
                        "checked_periods": ",".join(str(item) for item in result.checked_periods),
                        "generated_periods": ",".join(
                            str(item.period) for item in result.reconciled_periods
                        ),
                    },
                )
                for item in result.reconciled_periods:
                    stats = item.result.dictionary_stats
                    if stats is not None:
                        logger.info(
                            "dictionary processed",
                            extra={
                                "event": "dictionary_processed",
                                "target_month": str(item.period),
                                "total_entries": stats.total_entries,
                                "valid_entries": stats.valid_entries,
                                "skipped_entries": stats.skipped_entries,
                                "invalid_entries": stats.invalid_entries,
                                "duplicate_entries": stats.duplicate_entries,
                                "eligible_entries": item.result.eligible_word_count,
                            },
                        )
                with process_step(logger, "reconcile_poster_artifacts"):
                    artifact_result = self.artifact_reconciler.reconcile(
                        current_period=YearMonth.from_date(today)
                    )
                logger.info(
                    "poster artifacts reconciled",
                    extra={
                        "event": "poster_artifacts_reconciled",
                        "resolved_periods": ",".join(
                            str(item.period) for item in artifact_result.resolved_files
                        ),
                        "created_periods": ",".join(
                            str(item.period)
                            for item in artifact_result.resolved_files
                            if item.created
                        ),
                    },
                )
                with process_step(logger, "reconcile_notifications"):
                    notification_result = self.notification_reconciler.reconcile(
                        current_period=YearMonth.from_date(today)
                    )
                logger.info(
                    "notification deliveries reconciled",
                    extra={
                        "event": "notification_deliveries_reconciled",
                        "sent_periods": ",".join(
                            str(item.period)
                            for item in notification_result.results
                            if item.outcome is NotificationOutcome.SENT
                        ),
                        "already_sent_periods": ",".join(
                            str(item.period)
                            for item in notification_result.results
                            if item.outcome is NotificationOutcome.ALREADY_SENT
                        ),
                        "uncertain_periods": ",".join(
                            str(item.period)
                            for item in notification_result.results
                            if item.outcome is NotificationOutcome.UNCERTAIN
                        ),
                    },
                )
                for item in notification_result.results:
                    if item.outcome is NotificationOutcome.UNCERTAIN:
                        logger.warning(
                            "notification delivery requires operator review",
                            extra={
                                "event": "notification_delivery_uncertain",
                                "target_month": str(item.period),
                                "message_id": item.message_id,
                            },
                        )
                with process_step(logger, "acquire_wlc_application_lock"):
                    wlc_acquired = self.lock_repository.acquire(
                        name=WLC_MUTATION_LOCK_NAME,
                        owner=owner,
                        ttl_seconds=300,
                    )
                if not wlc_acquired:
                    logger.info(
                        "WLC password application skipped because another run owns the lock",
                        extra={
                            "event": "process_lock_contended",
                            "lock_name": WLC_MUTATION_LOCK_NAME,
                        },
                    )
                else:
                    try:
                        with process_step(logger, "reconcile_wlc_password"):
                            application_result = self.password_application_service.reconcile(
                                today=today
                            )
                        logger.info(
                            "WLC password application reconciled",
                            extra={
                                "event": "wlc_password_application_reconciled",
                                "target_month": str(application_result.period),
                                "application_outcome": application_result.outcome.value,
                                "wlan_id": application_result.wlan_id,
                                "wlan_was_enabled": application_result.wlan_was_enabled,
                                "wlan_is_enabled": application_result.wlan_is_enabled,
                                "application_ready": application_result.outcome
                                is not PasswordApplicationOutcome.NOT_READY,
                            },
                        )
                    finally:
                        released = self.lock_repository.release(
                            name=WLC_MUTATION_LOCK_NAME,
                            owner=owner,
                        )
                        if not released:
                            logger.warning(
                                "WLC mutation lock was not owned during release",
                                extra={
                                    "event": "process_lock_release_missed",
                                    "lock_name": WLC_MUTATION_LOCK_NAME,
                                },
                            )
            finally:
                released = self.lock_repository.release(name=MONTHLY_LOCK_NAME, owner=owner)
                if not released:
                    logger.warning(
                        "monthly reconciliation lock was not owned during release",
                        extra={
                            "event": "process_lock_release_missed",
                            "lock_name": MONTHLY_LOCK_NAME,
                        },
                    )
