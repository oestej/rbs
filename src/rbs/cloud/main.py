"""Entry point for the hosted build: ``rbs-cloud``."""

from __future__ import annotations

from rbs.cloud.config import CloudConfig, ConfigurationError
from rbs.cloud.control import ControlPlane
from rbs.cloud.host import CloudHost
from rbs.cloud.identity import build_identity_adapter
from rbs.cloud.sessions import SessionRegistry
from rbs.cloud.solve_pool import SolvePool
from rbs.logging import LoggingConfig, configure_logging, get_logger

logger = get_logger("cloud")


def build_host(config: CloudConfig) -> CloudHost:
    control = ControlPlane(config.control_db, config)
    control.init()
    logger.info("database.initialized", source="control_plane")
    for subject in config.bootstrap_subjects:
        control.admit(subject)
    registry = SessionRegistry(control, config)
    logger.info("session.registry_initialized")
    return CloudHost(
        config,
        control,
        registry,
        build_identity_adapter(config),
        SolvePool(config),
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="rbs-cloud",
        description="Hosted RBS. Configuration comes from the environment; see CloudConfig.",
    )
    parser.add_argument("--host", default=None, help="bind address (overrides RBS_HOST)")
    parser.add_argument("--port", type=int, default=None, help="port (overrides RBS_PORT)")
    parser.add_argument(
        "--sweep-only",
        action="store_true",
        help="run one retention sweep and exit, for a cron-driven deployment",
    )
    args = parser.parse_args(argv)

    runtime = configure_logging(
        LoggingConfig(runtime="cloud", component="server", destination="stdout")
    )
    logger.info("application.started")
    try:
        try:
            config = CloudConfig.from_env()
        except ConfigurationError:
            logger.error(
                "configuration.invalid",
                error_code="cloud_configuration",
                exc_info=True,
            )
            return 2

        host = build_host(config)

        if args.sweep_only:
            evicted = host.sweep_once()
            logger.info("retention.sweep_completed", evicted_count=evicted)
            return 0

        from nicegui import app

        from rbs.ui.app import serve

        _install_authorization_gate(app, host)
        app.on_startup(lambda: host.start_sweeper())
        app.on_shutdown(host.shutdown)

        serve(
            host,
            host=args.host or config.host,
            port=args.port or config.port,
            show=False,
            storage_secret=config.storage_secret,
            # Behind a proxy the default is tight enough to drop a live session on a
            # brief network hiccup.
            reconnect_timeout=30.0,
            exit_abruptly=False,
        )
        return 0
    except Exception:
        logger.exception("application.failed")
        return 1
    finally:
        logger.info("application.stopped")
        runtime.close()


def _install_authorization_gate(app, host: CloudHost) -> None:
    """Refuse unauthenticated requests before any page is rendered.

    In a correct deployment the proxy has already turned these away; this is the
    backstop for anything that reaches the origin directly. It answers 403
    rather than redirecting, because RBS has no login of its own to send anyone
    to - authentication belongs to the deployment.
    """
    from starlette.responses import PlainTextResponse

    # Static assets carry no workspace data and are needed to render the refusal.
    open_prefixes = ("/_nicegui", "/rbs-static", "/favicon")

    @app.middleware("http")
    async def require_authorization(request, call_next):
        path = request.url.path
        if path.startswith(open_prefixes):
            return await call_next(request)
        if host.principal(request) is None:
            return PlainTextResponse(
                "Not authorized. Access to RBS is granted by your organization, "
                "not by this application.",
                status_code=403,
            )
        return await call_next(request)


if __name__ == "__main__":
    raise SystemExit(main())
