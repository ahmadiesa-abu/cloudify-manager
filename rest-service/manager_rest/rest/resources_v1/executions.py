import uuid
from datetime import datetime

from flask_restful.reqparse import Argument
from flask_restful.inputs import boolean

from cloudify.models_states import ExecutionState
from manager_rest import manager_exceptions, workflow_executor
from manager_rest.maintenance import is_bypass_maintenance_mode
from manager_rest.resource_manager import (
    ResourceManager,
    get_resource_manager,
)
from manager_rest.rest import requests_schema, swagger
from manager_rest.rest.rest_decorators import (
    marshal_with,
    not_while_cancelling
)
from manager_rest.rest.rest_utils import (
    get_args_and_verify_arguments,
    get_json_and_verify_params,
    verify_and_convert_bool,
    parse_datetime_string,
    valid_user,
)
from manager_rest.security import SecuredResource
from manager_rest.security.authorization import (authorize,
                                                 check_user_action_allowed)
from manager_rest.storage import (
    get_storage_manager,
    models,
)


class Executions(SecuredResource):

    @swagger.operation(
        responseClass='List[{0}]'.format(models.Execution.__name__),
        nickname="list",
        notes="Returns a list of executions for the optionally provided "
              "deployment id.",
        parameters=[{'name': 'deployment_id',
                     'description': 'List execution of a specific deployment',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'defaultValue': None,
                     'paramType': 'query'},
                    {'name': 'include_system_workflows',
                     'description': 'Include executions of system workflows',
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'bool',
                     'defaultValue': False,
                     'paramType': 'query'}]
    )
    @authorize('execution_list')
    @marshal_with(models.Execution)
    def get(self, _include=None, **kwargs):
        """List executions"""
        args = get_args_and_verify_arguments(
            [Argument('deployment_id', required=False),
             Argument('include_system_workflows', type=boolean,
                      default=False)]
        )
        deployment_id_filter = ResourceManager.create_filters_dict(
            deployment_id=args.deployment_id)
        return get_resource_manager().list_executions(
            is_include_system_workflows=args.include_system_workflows,
            include=_include,
            filters=deployment_id_filter).items

    @authorize('execution_start')
    @not_while_cancelling
    @marshal_with(models.Execution)
    def post(self, **kwargs):
        """Execute a workflow"""
        request_dict = get_json_and_verify_params({
            'deployment_id': {'type': str},
            'workflow_id': {'type': str},
            'created_by': {'optional': True},
            'created_at': {'optional': True},
            'started_at': {'optional': True},
            'ended_at': {'optional': True},
            'status': {'optional': True},
            'id': {'optional': True},
            'error': {'optional': True},
        })

        allow_custom_parameters = verify_and_convert_bool(
            'allow_custom_parameters',
            request_dict.get('allow_custom_parameters', False))
        force = verify_and_convert_bool(
            'force',
            request_dict.get('force', False))
        dry_run = verify_and_convert_bool(
            'dry_run',
            request_dict.get('dry_run', False))
        queue = verify_and_convert_bool(
            'queue',
            request_dict.get('queue', False))

        deployment_id = request_dict['deployment_id']
        workflow_id = request_dict['workflow_id']
        parameters = request_dict.get('parameters', None)
        wait_after_fail = request_dict.get('wait_after_fail', 600)
        scheduled_time = request_dict.get('scheduled_time', None)
        force_status = request_dict.get('force_status', None)
        creator = request_dict.get('created_by')
        created_at = request_dict.get('created_at')
        started_at = request_dict.get('started_at')
        ended_at = request_dict.get('ended_at')
        execution_id = request_dict.get('id')
        error = request_dict.get('error')

        if creator:
            check_user_action_allowed('set_owner', None, True)
            creator = valid_user(creator)

        if created_at or started_at or ended_at:
            check_user_action_allowed('set_timestamp', None, True)
            if created_at:
                created_at = parse_datetime_string(created_at)
            if started_at:
                started_at = parse_datetime_string(started_at)
            if ended_at:
                ended_at = parse_datetime_string(ended_at)

        if error or force_status or execution_id or not deployment_id:
            check_user_action_allowed('set_execution_details', None, True)

        if force_status and scheduled_time:
            raise manager_exceptions.BadParametersError(
                'A status cannot be forced when scheduling an execution.'
            )

        if force_status and force_status not in ExecutionState.STATES:
            raise manager_exceptions.BadParametersError(
                'Force status was set to invalid state "{state}". '
                'Valid states are: {valid}'.format(
                    state=force_status,
                    valid=','.join(ExecutionState.STATES),
                )
            )

        if scheduled_time:
            sm = get_storage_manager()
            schedule = models.ExecutionSchedule(
                id='{}_{}'.format(workflow_id, uuid.uuid4().hex),
                deployment=sm.get(models.Deployment, deployment_id),
                created_at=datetime.utcnow(),
                since=self._parse_scheduled_time(scheduled_time),
                rule={'count': 1},
                slip=0,
                workflow_id=workflow_id,
                parameters=parameters,
                execution_arguments={
                    'allow_custom_parameters': allow_custom_parameters,
                    'force': force,
                    'is_dry_run': dry_run,
                    'wait_after_fail': wait_after_fail,
                },
                stop_on_fail=False,
            )
            schedule.next_occurrence = schedule.compute_next_occurrence()
            sm.put(schedule)
            return models.Execution(status=ExecutionState.SCHEDULED), 201

        if parameters is not None and not isinstance(parameters, dict):
            raise manager_exceptions.BadParametersError(
                f"request body's 'parameters' field must be a dict but"
                f" is of type {parameters.__class__.__name__}")

        sm = get_storage_manager()
        rm = get_resource_manager()
        with sm.transaction():
            kwargs = {
                'workflow_id': workflow_id,
                'parameters': parameters,
                'is_dry_run': dry_run,
                'status': force_status or ExecutionState.PENDING,
                'allow_custom_parameters': allow_custom_parameters,
                'force': force,
                'error': error,
            }
            deployment = None
            if deployment_id:
                deployment = sm.get(models.Deployment, deployment_id)
                rm.verify_deployment_environment_created_successfully(
                    deployment)
                kwargs['deployment'] = deployment
            execution = models.Execution(**kwargs)
            if creator:
                execution.creator = creator
            if created_at:
                execution.created_at = created_at
            if started_at:
                execution.started_at = started_at
            if ended_at:
                execution.ended_at = ended_at
            if execution_id:
                execution.id = execution_id
            sm.put(execution)
            messages = []
            if (
                force_status in ExecutionState.STATES
                and force_status not in ExecutionState.WAITING_STATES
                and force_status != ExecutionState.PENDING
            ):
                self._process_linked_executions(execution, deployment, sm)

            if not force_status:
                messages = rm.prepare_executions(
                    [execution],
                    bypass_maintenance=is_bypass_maintenance_mode(),
                    force=force,
                    queue=queue,
                    wait_after_fail=wait_after_fail,
                )
        workflow_executor.execute_workflow(messages)
        return execution, 201

    def _process_linked_executions(self, execution, deployment, sm):
        def _should_update(execution, target_entity, target_attr):
            attr = getattr(target_entity, target_attr)
            if attr is None:
                # Always update if the linked exec wasn't set
                return True
            else:
                target_time = parse_datetime_string(attr.started_at)
                if target_time < execution.started_at:
                    return True
            return False

        if deployment:
            exec_relations_set = False
            if _should_update(execution, deployment, 'latest_execution'):
                deployment.latest_execution = execution
                exec_relations_set = True

            if execution.workflow_id == \
                    'create_deployment_environment':
                if _should_update(execution, deployment, 'create_execution'):
                    deployment.create_execution = execution
                    exec_relations_set = True

            if exec_relations_set:
                sm.update(deployment)
        elif execution.workflow_id == 'upload_blueprint':
            blueprint = sm.get(models.Blueprint,
                               execution.parameters['blueprint_id'])
            if _should_update(execution, blueprint, 'upload_execution'):
                blueprint.upload_execution = execution
                sm.update(blueprint)

    def _parse_scheduled_time(self, scheduled_time):
        scheduled_utc = parse_datetime_string(scheduled_time)
        if scheduled_utc <= datetime.utcnow():
            raise manager_exceptions.BadParametersError(
                'Date `{0}` has already passed, please provide'
                ' valid date. \nExpected format: YYYYMMDDHHMM+HHMM or'
                ' YYYYMMDDHHMM-HHMM i.e: 201801012230-0500'
                ' (Jan-01-18 10:30pm EST)'.format(scheduled_time))
        return scheduled_utc


class ExecutionsId(SecuredResource):

    @swagger.operation(
        responseClass=models.Execution,
        nickname="getById",
        notes="Returns the execution state by its id.",
    )
    @authorize('execution_get')
    @marshal_with(models.Execution)
    def get(self, execution_id, _include=None, **kwargs):
        """
        Get execution by id
        """
        return get_storage_manager().get(
            models.Execution,
            execution_id,
            include=_include
        )

    @swagger.operation(
        responseClass=models.Execution,
        nickname="modify_state",
        notes="Modifies a running execution state (currently, only cancel"
              " and force-cancel are supported)",
        parameters=[{'name': 'body',
                     'description': 'json with an action key. '
                                    'Legal values for action are: [cancel,'
                                    ' force-cancel]',
                     'required': True,
                     'allowMultiple': False,
                     'dataType': requests_schema.ModifyExecutionRequest.__name__,  # NOQA
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @authorize('execution_cancel')
    @marshal_with(models.Execution)
    def post(self, execution_id, **kwargs):
        """
        Apply execution action (cancel, force-cancel) by id
        """
        request_dict = get_json_and_verify_params({'action'})
        action = request_dict['action']

        valid_actions = ['cancel', 'force-cancel', 'kill', 'resume',
                         'force-resume', 'requeue']

        if action not in valid_actions:
            raise manager_exceptions.BadParametersError(
                'Invalid action: {0}, Valid action values are: {1}'.format(
                    action, valid_actions))

        if action in ('resume', 'force-resume'):
            return get_resource_manager().resume_execution(
                execution_id, force=action == 'force-resume')
        return get_resource_manager().cancel_execution(
            execution_id, action == 'force-cancel', action == 'kill')

    @swagger.operation(
        responseClass=models.Execution,
        nickname="updateExecutionStatus",
        notes="Updates the execution's status",
        parameters=[{'name': 'status',
                     'description': "The execution's new status",
                     'required': True,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'},
                    {'name': 'error',
                     'description': "An error message. If omitted, "
                                    "error will be updated to an empty "
                                    "string",
                     'required': False,
                     'allowMultiple': False,
                     'dataType': 'string',
                     'paramType': 'body'}],
        consumes=[
            "application/json"
        ]
    )
    @authorize('execution_status_update')
    @marshal_with(models.Execution)
    def patch(self, execution_id, **kwargs):
        """
        Update execution status by id
        """
        request_dict = get_json_and_verify_params({'status'})

        return get_resource_manager().update_execution_status(
            execution_id,
            request_dict['status'],
            request_dict.get('error', '')
        )
