from typing import List
from uuid import UUID
import logging
import sqlalchemy as sa
from fastapi import Body, Depends, HTTPException, Path, Response, status

from prefect.orion import models, schemas
from prefect.orion.api import dependencies
from prefect.orion.utilities.server import OrionRouter
from prefect.utilities.logging import get_logger

logger = get_logger("orion.api")

router = OrionRouter(prefix="/flow_runs", tags=["Flow Runs"])


@router.post("/")
async def create_flow_run(
    flow_run: schemas.actions.FlowRunCreate,
    session: sa.orm.Session = Depends(dependencies.get_session),
    response: Response = None,
) -> schemas.core.FlowRun:
    """
    Create a flow run
    """
    nested = await session.begin_nested()
    try:
        result = await models.flow_runs.create_flow_run(
            session=session, flow_run=flow_run
        )
        response.status_code = status.HTTP_201_CREATED
        return result
    except sa.exc.IntegrityError as exc:
        # try to load a flow run with the same idempotency key
        await nested.rollback()
        stmt = await session.execute(
            sa.select(models.orm.FlowRun).filter_by(
                flow_id=flow_run.flow_id,
                idempotency_key=flow_run.idempotency_key,
            )
        )
        result = stmt.scalar()

        # if nothing was returned, then the integrity error was caused by violating
        # a constraint other than the idempotency key. The most probable one is
        # that a primary key was provided that already exists in the database.
        if not result:
            msg = "Could not create flow run due to database constraint violations."
            logger.error(msg)
            logger.error(exc)
            raise ValueError(msg)
        return result


@router.get("/{id}")
async def read_flow_run(
    flow_run_id: UUID = Path(..., description="The flow run id", alias="id"),
    session: sa.orm.Session = Depends(dependencies.get_session),
) -> schemas.core.FlowRun:
    """
    Get a flow run by id
    """
    flow_run = await models.flow_runs.read_flow_run(
        session=session, flow_run_id=flow_run_id
    )
    if not flow_run:
        raise HTTPException(status_code=404, detail="Flow run not found")
    return flow_run


@router.get("/")
async def read_flow_runs(
    offset: int = 0,
    limit: int = 10,
    session: sa.orm.Session = Depends(dependencies.get_session),
) -> List[schemas.core.FlowRun]:
    """
    Query for flow runs
    """
    return await models.flow_runs.read_flow_runs(
        session=session, offset=offset, limit=limit
    )


@router.delete("/{id}", status_code=204)
async def delete_flow_run(
    flow_run_id: UUID = Path(..., description="The flow run id", alias="id"),
    session: sa.orm.Session = Depends(dependencies.get_session),
):
    """
    Delete a flow run by id
    """
    result = await models.flow_runs.delete_flow_run(
        session=session, flow_run_id=flow_run_id
    )
    if not result:
        raise HTTPException(status_code=404, detail="Flow run not found")
    return result


@router.post("/{id}/set_state")
async def set_flow_run_state(
    flow_run_id: UUID = Path(..., description="The flow run id", alias="id"),
    state: schemas.actions.StateCreate = Body(..., description="The intended state."),
    session: sa.orm.Session = Depends(dependencies.get_session),
    response: Response = None,
) -> schemas.responses.SetStateResponse:
    """Set a flow run state, invoking any orchestration rules."""

    # create the state
    new_state = await models.flow_run_states.create_flow_run_state(
        session=session, flow_run_id=flow_run_id, state=state
    )
    # set the 201 because a new state was created
    response.status_code = status.HTTP_201_CREATED

    # if the set state has the same type as the provided state, it was accepted,
    # though its details may have been updated
    if new_state.type == state.type:

        # indicate the state was accepted
        return schemas.responses.SetStateResponse(
            status=schemas.responses.SetStateStatus.ACCEPT,
            details=dict(
                run_details=new_state.run_details,
                state_details=new_state.state_details,
            ),
        )

    # otherwise the requested transition was rejected
    else:

        # send the new state
        return schemas.responses.SetStateResponse(
            status=schemas.responses.SetStateStatus.REJECT,
            details=dict(state=new_state),
        )
