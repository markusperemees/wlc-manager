from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from wlc_manager import __version__
from wlc_manager.artifacts import PosterGenerator
from wlc_manager.config import ConfigurationError, Settings, load_settings
from wlc_manager.database import (
    LATEST_SCHEMA_VERSION,
    Database,
    DatabaseError,
    HeartbeatRepository,
    LeaseLockRepository,
    NotificationRepository,
    PasswordRepository,
    PasswordState,
)
from wlc_manager.notifications import (
    NotificationOutcome,
    NotificationService,
    SmtpRelay,
)
from wlc_manager.observability import configure_logging, process_run, process_step
from wlc_manager.password_application import (
    PasswordApplicationOutcome,
    PasswordApplicationService,
)
from wlc_manager.passwords import PasswordGenerator
from wlc_manager.scheduler import (
    MONTHLY_LOCK_NAME,
    SCHEDULER_SERVICE_NAME,
    WLC_MUTATION_LOCK_NAME,
    SchedulerRuntime,
)
from wlc_manager.scheduling import YearMonth
from wlc_manager.wlc import AireOsWlcClient


@dataclass(frozen=True, slots=True)
class CliContext:
    config_path: Path


class WlanCliState(StrEnum):
    ENABLED = "enabled"
    DISABLED = "disabled"


app = typer.Typer(
    name="wlc-manager",
    help="Manage the public Wi-Fi lifecycle.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Validate and inspect application configuration.")
database_app = typer.Typer(help="Manage the local database schema.")
password_app = typer.Typer(help="Generate and inspect Wi-Fi passwords.")
artifact_app = typer.Typer(help="Generate the current Wi-Fi poster files.")
notification_app = typer.Typer(help="Send Wi-Fi poster files through Microsoft 365 relay.")
wlc_app = typer.Typer(help="Inspect and reconcile the configured AireOS WLAN.")
app.add_typer(config_app, name="config")
app.add_typer(database_app, name="db")
app.add_typer(password_app, name="password")
app.add_typer(artifact_app, name="artifacts")
app.add_typer(notification_app, name="notifications")
app.add_typer(wlc_app, name="wlc")


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            help="Path to the YAML configuration file.",
            envvar="WLC_MANAGER_CONFIG",
        ),
    ] = Path("config.yaml"),
) -> None:
    ctx.obj = CliContext(config_path=config)


@app.command()
def version() -> None:
    """Print the application version."""
    typer.echo(__version__)


@config_app.command("validate")
def validate_config(ctx: typer.Context) -> None:
    """Validate the complete YAML configuration."""
    cli_context = _context(ctx)
    settings = _load(cli_context.config_path)
    configure_logging(settings.graylog)
    with (
        process_run("config_validate", trigger="cli") as logger,
        process_step(logger, "validate_configuration"),
    ):
        typer.echo(f"Configuration is valid: {cli_context.config_path.resolve()}")


@database_app.command("migrate")
def migrate_database(ctx: typer.Context) -> None:
    """Apply all pending SQLite schema migrations."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    with process_run("database_migrate", trigger="cli") as logger:
        with process_step(logger, "apply_migrations"):
            version_number = database.migrate()
        typer.echo(f"Database schema version: {version_number}")


@app.command()
def status(ctx: typer.Context) -> None:
    """Show configuration, database, and secret-file readiness."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    with process_run("status", trigger="cli") as logger:
        with process_step(logger, "collect_status"):
            report = {
                "application_version": __version__,
                "configuration": "valid",
                "database": {
                    "path": str(settings.database.path),
                    "schema_version": database.current_version(),
                    "expected_schema_version": LATEST_SCHEMA_VERSION,
                },
                "secret_files": {
                    "wlc_username": settings.secrets.wlc_username_file.is_file(),
                    "wlc_password": settings.secrets.wlc_password_file.is_file(),
                },
            }
        typer.echo(json.dumps(report, indent=2))


@app.command("run")
def run_service(ctx: typer.Context) -> None:
    """Run the long-lived scheduler service."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    runtime: SchedulerRuntime | None = None
    with process_run("scheduler_service", trigger="scheduler") as logger:
        with process_step(logger, "migrate_database"):
            schema_version = database.migrate()
        with process_step(logger, "initialize_scheduler"):
            runtime = SchedulerRuntime(settings, database)
            runtime.heartbeat_once()
        logger.info(
            "scheduler initialized",
            extra={
                "event": "scheduler_initialized",
                "schema_version": schema_version,
                "scheduled_jobs": ",".join(sorted(runtime.scheduled_job_ids())),
            },
        )
        try:
            runtime.run_forever()
        except (KeyboardInterrupt, SystemExit):
            logger.info("scheduler shutdown requested", extra={"event": "scheduler_stopping"})
        finally:
            runtime.shutdown()


@app.command()
def healthcheck(
    ctx: typer.Context,
    max_age_seconds: Annotated[
        int,
        typer.Option(min=30, max=3600, help="Maximum acceptable scheduler heartbeat age."),
    ] = 90,
) -> None:
    """Exit successfully when the scheduler heartbeat is recent."""
    settings = _settings_from_context(ctx)
    database = _database(settings)
    if database.current_version() != LATEST_SCHEMA_VERSION:
        typer.echo("Database schema is not current", err=True)
        raise typer.Exit(code=1)
    age = HeartbeatRepository(database).age_seconds(SCHEDULER_SERVICE_NAME)
    if age is None or age > max_age_seconds:
        typer.echo("Scheduler heartbeat is missing or stale", err=True)
        raise typer.Exit(code=1)
    typer.echo("healthy")


@password_app.command("generate")
def generate_password(
    ctx: typer.Context,
    month: Annotated[str, typer.Option("--month", help="Target month in YYYY-MM format.")],
) -> None:
    """Generate the idempotent password record for a target month."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    with process_run("password_generate", trigger="cli") as logger:
        owner = str(logger.extra["run_id"])
        with process_step(logger, "validate_target_month"):
            period = YearMonth.parse(month)
        with process_step(logger, "check_database_schema"):
            current_version = database.current_version()
            if current_version != LATEST_SCHEMA_VERSION:
                raise DatabaseError(
                    "database schema is not current; run the 'db migrate' command first"
                )
        locks = LeaseLockRepository(database)
        with process_step(logger, "acquire_process_lock"):
            acquired = locks.acquire(name=MONTHLY_LOCK_NAME, owner=owner, ttl_seconds=300)
            if not acquired:
                raise DatabaseError("another password generation process is already running")
        try:
            with process_step(logger, "generate_password"):
                generator = PasswordGenerator(
                    PasswordRepository(database),
                    prefix=settings.password.prefix,
                    random_digits=settings.password.random_digits,
                    history_size=settings.password.history_size,
                )
                result = generator.generate(
                    period=period,
                    dictionary_path=settings.password.dictionary_path,
                    run_id=owner,
                )
        finally:
            if not locks.release(name=MONTHLY_LOCK_NAME, owner=owner):
                logger.warning(
                    "password generation lock was not owned during release",
                    extra={
                        "event": "process_lock_release_missed",
                        "lock_name": MONTHLY_LOCK_NAME,
                    },
                )

        if result.dictionary_stats is not None:
            logger.info(
                "dictionary processed",
                extra={
                    "event": "dictionary_processed",
                    "total_entries": result.dictionary_stats.total_entries,
                    "valid_entries": result.dictionary_stats.valid_entries,
                    "skipped_entries": result.dictionary_stats.skipped_entries,
                    "invalid_entries": result.dictionary_stats.invalid_entries,
                    "duplicate_entries": result.dictionary_stats.duplicate_entries,
                    "eligible_entries": result.eligible_word_count,
                },
            )
        outcome = "generated" if result.created else "already existed"
        logger.info(
            "password generation resolved",
            extra={
                "event": "password_generation_resolved",
                "target_month": str(period),
                "password_created": result.created,
            },
        )
        typer.echo(f"Password for {period}: {outcome}")


@artifact_app.command("generate")
def generate_artifacts(
    ctx: typer.Context,
    month: Annotated[str, typer.Option("--month", help="Target month in YYYY-MM format.")],
) -> None:
    """Generate and record the PNG and PDF poster for a current managed month."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    with process_run("artifact_generate", trigger="cli") as logger:
        owner = str(logger.extra["run_id"])
        with process_step(logger, "validate_target_month"):
            period = YearMonth.parse(month)
            current_period = YearMonth.from_date(
                datetime.now(ZoneInfo(settings.app.timezone)).date()
            )
            if period not in {current_period, current_period.next()}:
                raise DatabaseError(
                    f"artifacts may only be kept for {current_period} and {current_period.next()}"
                )
        with process_step(logger, "check_database_schema"):
            if database.current_version() != LATEST_SCHEMA_VERSION:
                raise DatabaseError(
                    "database schema is not current; run the 'db migrate' command first"
                )
        locks = LeaseLockRepository(database)
        with process_step(logger, "acquire_process_lock"):
            acquired = locks.acquire(name=MONTHLY_LOCK_NAME, owner=owner, ttl_seconds=300)
            if not acquired:
                raise DatabaseError("another monthly reconciliation process is already running")
        try:
            repository = PasswordRepository(database)
            with process_step(logger, "load_password_record"):
                record = repository.get_by_month(str(period))
                if record is None:
                    raise DatabaseError(
                        f"password record does not exist for {period}; generate it first"
                    )
            with process_step(logger, "generate_poster_files"):
                files = PosterGenerator(settings.artifacts).generate(
                    record,
                    ssid=settings.wlc.ssid,
                    current_period=current_period,
                )
            if record.state in {PasswordState.GENERATED, PasswordState.MATERIALS_CREATED}:
                with process_step(logger, "record_poster_files"):
                    repository.mark_materials_created(
                        str(period),
                        png_path=files.png_path,
                        pdf_path=files.pdf_path,
                    )
        finally:
            if not locks.release(name=MONTHLY_LOCK_NAME, owner=owner):
                logger.warning(
                    "artifact generation lock was not owned during release",
                    extra={
                        "event": "process_lock_release_missed",
                        "lock_name": MONTHLY_LOCK_NAME,
                    },
                )

        logger.info(
            "poster artifacts resolved",
            extra={
                "event": "poster_artifacts_resolved",
                "target_month": str(period),
                "files_created": files.created,
                "png_path": str(files.png_path),
                "pdf_path": str(files.pdf_path),
            },
        )
        outcome = "created" if files.created else "already existed"
        typer.echo(f"Poster files for {period}: {outcome}")


@notification_app.command("send")
def send_notification(
    ctx: typer.Context,
    month: Annotated[str, typer.Option("--month", help="Target month in YYYY-MM format.")],
    retry_uncertain: Annotated[
        bool,
        typer.Option(
            "--retry-uncertain",
            help="Explicitly retry a delivery whose previous SMTP result is ambiguous.",
        ),
    ] = False,
) -> None:
    """Send the period's PNG and PDF files through the configured SMTP relay."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    with process_run("notification_send", trigger="cli") as logger:
        owner = str(logger.extra["run_id"])
        with process_step(logger, "validate_target_month"):
            period = YearMonth.parse(month)
            current_period = YearMonth.from_date(
                datetime.now(ZoneInfo(settings.app.timezone)).date()
            )
            if period not in {current_period, current_period.next()}:
                raise DatabaseError(
                    f"notifications may only be sent for {current_period} "
                    f"and {current_period.next()}"
                )
        with process_step(logger, "check_database_schema"):
            if database.current_version() != LATEST_SCHEMA_VERSION:
                raise DatabaseError(
                    "database schema is not current; run the 'db migrate' command first"
                )
        locks = LeaseLockRepository(database)
        with process_step(logger, "acquire_process_lock"):
            acquired = locks.acquire(name=MONTHLY_LOCK_NAME, owner=owner, ttl_seconds=300)
            if not acquired:
                raise DatabaseError("another monthly reconciliation process is already running")
        try:
            password_repository = PasswordRepository(database)
            service = NotificationService(
                password_repository,
                NotificationRepository(database),
                SmtpRelay(settings.smtp),
                settings.smtp,
                ssid=settings.wlc.ssid,
            )
            with process_step(logger, "send_notification"):
                result = service.send(period, retry_uncertain=retry_uncertain)
        finally:
            if not locks.release(name=MONTHLY_LOCK_NAME, owner=owner):
                logger.warning(
                    "notification lock was not owned during release",
                    extra={
                        "event": "process_lock_release_missed",
                        "lock_name": MONTHLY_LOCK_NAME,
                    },
                )

        logger.info(
            "notification delivery resolved",
            extra={
                "event": "notification_delivery_resolved",
                "target_month": str(period),
                "delivery_outcome": result.outcome.value,
                "message_id": result.message_id,
            },
        )
        if result.outcome is NotificationOutcome.UNCERTAIN:
            typer.echo(
                "SMTP delivery result is uncertain; inspect the recipient mailbox, then use "
                "--retry-uncertain only if a resend is required",
                err=True,
            )
            raise typer.Exit(code=3)
        outcome = "sent" if result.outcome is NotificationOutcome.SENT else "already sent"
        typer.echo(f"Notification for {period}: {outcome}")


@wlc_app.command("status")
def wlc_status(ctx: typer.Context) -> None:
    """Read the configured WLAN state from AireOS."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    with process_run("wlc_status", trigger="cli") as logger:
        with process_step(logger, "read_wlan_state"):
            status = _wlc_client(settings).get_wlan_status()
        logger.info(
            "WLAN state read",
            extra={
                "event": "wlan_state_read",
                "wlan_id": status.wlan_id,
                "ssid": status.ssid,
                "enabled": status.enabled,
            },
        )
        state = "enabled" if status.enabled else "disabled"
        typer.echo(f"WLAN {status.wlan_id} ({status.ssid}): {state}")


@wlc_app.command("set-state")
def wlc_set_state(
    ctx: typer.Context,
    state: Annotated[WlanCliState, typer.Argument(help="Desired WLAN state.")],
) -> None:
    """Idempotently set and verify the configured WLAN state."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    desired_enabled = state is WlanCliState.ENABLED
    with process_run("wlc_set_state", trigger="cli") as logger:
        owner = str(logger.extra["run_id"])
        with process_step(logger, "check_database_schema"):
            if database.current_version() != LATEST_SCHEMA_VERSION:
                raise DatabaseError(
                    "database schema is not current; run the 'db migrate' command first"
                )
        locks = LeaseLockRepository(database)
        with process_step(logger, "acquire_wlc_lock"):
            acquired = locks.acquire(
                name=WLC_MUTATION_LOCK_NAME,
                owner=owner,
                ttl_seconds=300,
            )
            if not acquired:
                raise DatabaseError("another WLC mutation process is already running")
        try:
            with process_step(logger, "set_wlan_state"):
                change = _wlc_client(settings).set_wlan_enabled(desired_enabled)
        finally:
            if not locks.release(name=WLC_MUTATION_LOCK_NAME, owner=owner):
                logger.warning(
                    "WLC mutation lock was not owned during release",
                    extra={
                        "event": "process_lock_release_missed",
                        "lock_name": WLC_MUTATION_LOCK_NAME,
                    },
                )
        logger.info(
            "WLAN state resolved",
            extra={
                "event": "wlan_state_resolved",
                "wlan_id": change.after.wlan_id,
                "ssid": change.after.ssid,
                "desired_enabled": desired_enabled,
                "state_changed": change.changed,
            },
        )
        outcome = "changed" if change.changed else "already correct"
        typer.echo(f"WLAN {change.after.wlan_id} ({change.after.ssid}): {state.value}; {outcome}")


@wlc_app.command("apply-password")
def wlc_apply_password(
    ctx: typer.Context,
    month: Annotated[str, typer.Option("--month", help="Target month in YYYY-MM format.")],
    allow_early: Annotated[
        bool,
        typer.Option(
            "--allow-early",
            help="Explicitly permit applying next month's notified password before its first day.",
        ),
    ] = False,
) -> None:
    """Apply and persist a notified monthly password on the configured WLAN."""
    settings = _settings_from_context(ctx)
    configure_logging(settings.graylog)
    database = _database(settings)
    with process_run("wlc_password_apply", trigger="cli") as logger:
        owner = str(logger.extra["run_id"])
        with process_step(logger, "validate_target_month"):
            period = YearMonth.parse(month)
            today = datetime.now(ZoneInfo(settings.app.timezone)).date()
            current_period = YearMonth.from_date(today)
            if period not in {current_period, current_period.next()}:
                raise DatabaseError(
                    f"WLC passwords may only be applied for {current_period} "
                    f"and {current_period.next()}"
                )
        with process_step(logger, "check_database_schema"):
            if database.current_version() != LATEST_SCHEMA_VERSION:
                raise DatabaseError(
                    "database schema is not current; run the 'db migrate' command first"
                )
        locks = LeaseLockRepository(database)
        with process_step(logger, "acquire_wlc_lock"):
            acquired = locks.acquire(
                name=WLC_MUTATION_LOCK_NAME,
                owner=owner,
                ttl_seconds=300,
            )
            if not acquired:
                raise DatabaseError("another WLC mutation process is already running")
        try:
            service = PasswordApplicationService(
                PasswordRepository(database),
                lambda: _wlc_client(settings),
            )
            with process_step(logger, "apply_wlc_password"):
                result = service.apply(
                    period,
                    today=today,
                    allow_early=allow_early,
                )
        finally:
            if not locks.release(name=WLC_MUTATION_LOCK_NAME, owner=owner):
                logger.warning(
                    "WLC mutation lock was not owned during release",
                    extra={
                        "event": "process_lock_release_missed",
                        "lock_name": WLC_MUTATION_LOCK_NAME,
                    },
                )

        logger.info(
            "WLC password application resolved",
            extra={
                "event": "wlc_password_application_resolved",
                "target_month": str(period),
                "application_outcome": result.outcome.value,
                "wlan_id": result.wlan_id,
                "wlan_was_enabled": result.wlan_was_enabled,
                "wlan_is_enabled": result.wlan_is_enabled,
            },
        )
        labels = {
            PasswordApplicationOutcome.APPLIED: "applied",
            PasswordApplicationOutcome.ALREADY_APPLIED: "already applied",
            PasswordApplicationOutcome.NOT_DUE: "not due",
            PasswordApplicationOutcome.NOT_READY: "not ready",
        }
        typer.echo(f"WLC password for {period}: {labels[result.outcome]}")


def _settings_from_context(ctx: typer.Context) -> Settings:
    return _load(_context(ctx).config_path)


def _context(ctx: typer.Context) -> CliContext:
    context = ctx.find_root().obj
    if not isinstance(context, CliContext):
        raise RuntimeError("CLI context was not initialized")
    return context


def _load(path: Path) -> Settings:
    try:
        return load_settings(path)
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc


def _database(settings: Settings) -> Database:
    return Database(
        settings.database.path,
        busy_timeout_seconds=settings.database.busy_timeout_seconds,
    )


def _wlc_client(settings: Settings) -> AireOsWlcClient:
    return AireOsWlcClient.from_secret_files(settings.wlc, settings.secrets)
