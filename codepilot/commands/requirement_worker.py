"""Hidden requirement-planning worker command."""

from __future__ import annotations

import click

from codepilot.storage import database as db


@click.command("requirement-worker", hidden=True)
@click.argument("job_id", type=int)
def requirement_worker(job_id: int) -> None:
    """Run one persisted requirement-planning job."""
    db.init_db()
    from codepilot.webapp import actions as _web_actions  # noqa: F401 - initializes fallback UI job shell
    from codepilot.webapp.action_requirements import run_requirement_job_worker

    try:
        run_requirement_job_worker(int(job_id))
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
