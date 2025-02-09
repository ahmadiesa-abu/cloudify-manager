import json
import traceback
from datetime import datetime
from os.path import join
from urllib.parse import quote as unquote

from flask import request

from flask_restful.inputs import boolean
from flask_restful.reqparse import Argument

from cloudify.models_states import VisibilityState, BlueprintUploadState

from dsl_parser import constants

from manager_rest.security import SecuredResource
from manager_rest.security.authorization import (
    authorize,
    check_user_action_allowed,
)
from manager_rest.resource_manager import get_resource_manager
from manager_rest import upload_manager, workflow_executor
from manager_rest.rest import (
    rest_utils,
    resources_v1,
    resources_v2,
    rest_decorators,
    swagger,
)
from manager_rest.storage import models, get_storage_manager
from manager_rest.utils import get_formatted_timestamp, remove, current_tenant
from manager_rest.rest.rest_utils import (get_labels_from_plan,
                                          get_labels_list,
                                          get_args_and_verify_arguments)
from manager_rest.rest.responses import Label
from manager_rest.manager_exceptions import (ConflictError,
                                             IllegalActionError,
                                             BadParametersError,
                                             ImportedBlueprintNotFound)
from manager_rest import config
from manager_rest.constants import FILE_SERVER_UPLOADED_BLUEPRINTS_FOLDER


class BlueprintsSetVisibility(SecuredResource):

    @authorize('resource_set_visibility')
    @rest_decorators.marshal_with(models.Blueprint)
    def patch(self, blueprint_id):
        """
        Set the blueprint's visibility
        """
        visibility = rest_utils.get_visibility_parameter()
        blueprint = get_storage_manager().get(models.Blueprint, blueprint_id)
        return get_resource_manager().set_visibility(blueprint, visibility)


class BlueprintsIcon(SecuredResource):

    @authorize('blueprint_upload')
    @rest_decorators.marshal_with(models.Blueprint)
    def patch(self, blueprint_id):
        """
        Set the blueprint's icon
        """
        # Get the blueprint to verify if it exists (in the current context)
        blueprint = get_storage_manager().get(models.Blueprint, blueprint_id)
        if request.data:
            upload_manager.update_blueprint_icon_file(
                blueprint.tenant_name, blueprint_id)
        else:
            upload_manager.remove_blueprint_icon_file(
                blueprint.tenant_name, blueprint_id)
        return blueprint


class BlueprintsId(resources_v2.BlueprintsId):
    @authorize('blueprint_upload')
    @rest_decorators.marshal_with(models.Blueprint)
    def put(self, blueprint_id, **kwargs):
        """
        Upload a blueprint (id specified)
        """
        rm = get_resource_manager()
        sm = get_storage_manager()
        rest_utils.validate_inputs({'blueprint_id': blueprint_id})

        args = None
        form_params = request.form.get('params')
        if form_params:
            args = json.loads(form_params)
        if not args:
            args = request.args.to_dict(flat=False)

        async_upload = args.get('async_upload', False)
        created_at = args.get('created_at')
        created_by = args.get('created_by')
        labels = args.get('labels', [])
        visibility = args.get('visibility')
        private_resource = args.get('private_resource')
        application_file_name = args.get('application_file_name', '')
        skip_execution = args.get('skip_execution', False)
        state = args.get('state')
        blueprint_url = args.get('blueprint_archive_url')

        if blueprint_url:
            if (
                request.data or  # blueprint in body (pre-7.0 client)
                'blueprint_archive' in request.files  # multipart
            ):
                raise BadParametersError(
                    "Can pass blueprint as only one of: URL via query "
                    "parameters, multi-form or chunked.")

        if created_at:
            check_user_action_allowed('set_timestamp', None, True)
            created_at = rest_utils.parse_datetime_string(created_at)

        if created_by:
            check_user_action_allowed('set_owner', None, True)
            created_by = rest_utils.valid_user(created_by)

        if visibility is not None:
            rest_utils.validate_visibility(
                visibility, valid_values=VisibilityState.STATES)

        unique_labels_check = []
        for label in labels:
            parsed_key, parsed_value = rest_utils.parse_label(label['key'],
                                                              label['value'])
            label['key'] = parsed_key
            label['value'] = parsed_value
            unique_labels_check.append((parsed_key, parsed_value))
        rest_utils.test_unique_labels(unique_labels_check)

        visibility = rm.get_resource_visibility(
            models.Blueprint, blueprint_id, visibility, private_resource)

        with sm.transaction():
            if failed_bp := self._failed_blueprint(
                sm, blueprint_id, visibility,
            ):
                sm.delete(failed_bp)

        with sm.transaction():
            blueprint = models.Blueprint(
                plan=None,
                id=blueprint_id,
                description=None,
                created_at=created_at or datetime.now(),
                updated_at=datetime.now(),
                main_file_name=unquote(application_file_name),
                visibility=visibility,
                state=state or BlueprintUploadState.PENDING,
                creator=created_by,
            )
            sm.put(blueprint)

            if not blueprint_url:
                blueprint.state = BlueprintUploadState.UPLOADING

            if not skip_execution:
                blueprint.upload_execution, messages = rm.upload_blueprint(
                    blueprint_id,
                    application_file_name,
                    blueprint_url,
                    labels=labels,
                    # for the import resolver
                    file_server_root=config.instance.file_server_root,
                    marketplace_api_url=config.instance.marketplace_api_url,
                )
            else:
                messages = []
                async_upload = True

        try:
            if not blueprint_url:
                upload_manager.upload_blueprint_archive_to_file_server(
                    blueprint_id)
            if messages:
                workflow_executor.execute_workflow(messages)
        except Exception as e:
            blueprint.state = BlueprintUploadState.FAILED_UPLOADING
            blueprint.error = str(e)
            blueprint.error_traceback = traceback.format_exc()
            sm.update(blueprint)
            upload_manager.cleanup_blueprint_archive_from_file_server(
                blueprint_id, current_tenant.name)
            raise

        if not async_upload:
            return rest_utils.get_uploaded_blueprint(sm, blueprint)
        return blueprint, 201

    def _failed_blueprint(self, sm, blueprint_id, visibility):
        current_tenant = request.headers.get('tenant')
        if visibility == VisibilityState.GLOBAL:
            # TODO shouldn't this need an all_tenants=True or the like?
            existing_duplicates = sm.list(
                models.Blueprint, filters={'id': blueprint_id})
            if existing_duplicates:
                if existing_duplicates[0].state in \
                        BlueprintUploadState.FAILED_STATES:
                    return existing_duplicates[0]
                raise ConflictError(
                    f"Can't set or create the resource `{blueprint_id}`, its "
                    "visibility can't be global because it also exists in "
                    "other tenants"
                )
        else:
            existing_duplicates = sm.list(
                models.Blueprint, filters={'id': blueprint_id,
                                           'tenant_name': current_tenant})
            if existing_duplicates:
                if existing_duplicates[0].state in \
                        BlueprintUploadState.FAILED_STATES:
                    return existing_duplicates[0]
                raise ConflictError(
                    f'blueprint with id={blueprint_id} already exists on '
                    f'tenant {current_tenant} or with global visibility'
                )

    @swagger.operation(
        responseClass=models.Blueprint,
        nickname="deleteById",
        notes="deletes a blueprint by its id."
    )
    @authorize('blueprint_delete')
    def delete(self, blueprint_id, **kwargs):
        """
        Delete blueprint by id
        """
        query_args = get_args_and_verify_arguments(
            [Argument('force', type=boolean, default=False)])
        get_resource_manager().delete_blueprint(
            blueprint_id,
            force=query_args.force)
        return None, 204

    @authorize('blueprint_upload')
    @rest_decorators.marshal_with(models.Blueprint)
    def patch(self, blueprint_id, **kwargs):
        """
        Update a blueprint.

        Used for updating the blueprint's state (and error) while uploading,
        and updating the blueprint's other attributes upon a successful upload.
        This method is for internal use only.
        """
        if not request.json:
            raise IllegalActionError('Update a blueprint request must include '
                                     'at least one parameter to update')

        request_schema = {
            'plan': {'type': dict, 'optional': True},
            'requirements': {'type': dict, 'optional': True},
            'description': {'type': str, 'optional': True},
            'main_file_name': {'type': str, 'optional': True},
            'visibility': {'type': str, 'optional': True},
            'state': {'type': str, 'optional': True},
            'error': {'type': str, 'optional': True},
            'error_traceback': {'type': str, 'optional': True},
            'labels': {'type': list, 'optional': True},
            'creator': {'type': str, 'optional': True},
            'created_at': {'type': str, 'optional': True},
            'upload_execution': {'type': str, 'optional': True},
        }
        request_dict = rest_utils.get_json_and_verify_params(request_schema)
        created_at = creator = None
        if request_dict.get('created_at'):
            check_user_action_allowed('set_timestamp', None, True)
            created_at = rest_utils.parse_datetime_string(
                request_dict['created_at'])

        if request_dict.get('creator'):
            check_user_action_allowed('set_owner', None, True)
            creator = rest_utils.valid_user(request_dict['creator'])

        invalid_params = set(request_dict.keys()) - set(request_schema.keys())
        if invalid_params:
            raise BadParametersError(
                "Unknown parameters: {}".format(','.join(invalid_params))
            )
        sm = get_storage_manager()
        blueprint = sm.get(models.Blueprint, blueprint_id)
        # if finished blueprint validation - cleanup DB entry
        # and uploaded blueprints folder
        if blueprint.state == BlueprintUploadState.VALIDATING:
            # unattach .upload_execution from the blueprint before deleting it
            # so that the execution is not deleted via cascade (the user still
            # needs to be able to fetch the execution & logs to view the
            # results of validation)
            blueprint.upload_execution = None
            sm.update(blueprint)

            uploaded_blueprint_path = join(
                config.instance.file_server_root,
                FILE_SERVER_UPLOADED_BLUEPRINTS_FOLDER,
                blueprint.tenant.name,
                blueprint.id)
            remove(uploaded_blueprint_path)
            sm.delete(blueprint)
            return blueprint

        # set blueprint visibility
        visibility = request_dict.get('visibility')
        if visibility:
            if visibility not in VisibilityState.STATES:
                raise BadParametersError(
                    "Invalid visibility: `{0}`. Valid visibility's values "
                    "are: {1}".format(visibility, VisibilityState.STATES)
                )
            blueprint.visibility = visibility

        # set other blueprint attributes.
        if 'plan' in request_dict:
            blueprint.plan = request_dict['plan']
        if 'requirements' in request_dict:
            blueprint.requirements = request_dict['requirements']
        if 'description' in request_dict:
            blueprint.description = request_dict['description']
        if 'main_file_name' in request_dict:
            blueprint.main_file_name = request_dict['main_file_name']
        if 'creator' in request_dict:
            blueprint.creator = creator
        if 'upload_execution' in request_dict:
            blueprint.upload_execution = sm.get(
                models.Execution, request_dict['upload_execution'])

        if request_dict.get('plan'):
            imported_blueprints = request_dict['plan']\
                .get(constants.IMPORTED_BLUEPRINTS, {})
            _validate_imported_blueprints(sm, blueprint, imported_blueprints)
        # set blueprint state
        state = request_dict.get('state')
        if state:
            if state not in BlueprintUploadState.STATES:
                raise BadParametersError(
                    "Invalid state: `{0}`. Valid blueprint state values are: "
                    "{1}".format(state, BlueprintUploadState.STATES)
                )

            blueprint.state = state
            blueprint.error = request_dict.get('error')
            blueprint.error_traceback = request_dict.get('error_traceback')

            # On finalizing the blueprint upload, extract archive to file
            # server
            if state == BlueprintUploadState.UPLOADED:
                upload_manager. \
                    extract_blueprint_archive_to_file_server(
                        blueprint_id=blueprint_id,
                        tenant=blueprint.tenant.name)

            # If failed for any reason, cleanup the blueprint archive from
            # server
            elif state in BlueprintUploadState.FAILED_STATES:
                upload_manager. \
                    cleanup_blueprint_archive_from_file_server(
                        blueprint_id=blueprint_id,
                        tenant=blueprint.tenant.name)

        labels_list = None
        if request_dict.get('labels') is not None:
            raw_list = request_dict['labels']
            if all(
                'key' in label and 'value' in label
                for label in raw_list
            ):
                labels_list = [Label(**label)
                               for label in raw_list]
            else:
                labels_list = get_labels_list(raw_list)
        if state == BlueprintUploadState.UPLOADED and labels_list is None:
            labels_list = get_labels_from_plan(blueprint.plan,
                                               constants.BLUEPRINT_LABELS)

        if labels_list is not None:
            check_state = state or blueprint.state
            if check_state != BlueprintUploadState.UPLOADED:
                raise ConflictError(
                    'Blueprint labels can be created only if the '
                    'blueprint state is {0}'.format(
                        BlueprintUploadState.UPLOADED))
            rm = get_resource_manager()
            rm.update_resource_labels(models.BlueprintLabel,
                                      blueprint,
                                      labels_list,
                                      creator=creator,
                                      created_at=created_at)

        blueprint.updated_at = get_formatted_timestamp()
        return sm.update(blueprint)


def _validate_imported_blueprints(sm, blueprint, imported_blueprints):
    for imported_blueprint in imported_blueprints:
        if not sm.get(models.Blueprint, imported_blueprint, include=['id']):
            raise ImportedBlueprintNotFound(
                f'Blueprint {blueprint.id} uses an imported blueprint which '
                f'is unavailable: {imported_blueprint}')


class BlueprintsIdValidate(BlueprintsId):
    @authorize('blueprint_upload')
    @rest_decorators.marshal_with(models.Blueprint)
    def put(self, blueprint_id, **kwargs):
        """
        Validate a blueprint (id specified)
        """
        rm = get_resource_manager()
        sm = get_storage_manager()
        args = None
        form_params = request.form.get('params')
        if form_params:
            args = json.loads(form_params)
        if not args:
            args = request.args.to_dict(flat=False)

        rest_utils.validate_inputs({'blueprint_id': blueprint_id})
        visibility = args.pop('visibility')
        if visibility is not None:
            rest_utils.validate_visibility(
                visibility, valid_values=VisibilityState.STATES)
        application_file_name = args.pop('application_file_name', '')
        blueprint_url = args.pop('blueprint_archive_url', None)

        with sm.transaction():
            blueprint = models.Blueprint(
                plan=None,
                id=blueprint_id,
                description=None,
                main_file_name=None,
                visibility=None,
                state=BlueprintUploadState.VALIDATING,
            )

            sm.put(blueprint)
            blueprint.upload_execution, messages = rm.upload_blueprint(
                blueprint_id,
                application_file_name,
                blueprint_url,
                config.instance.file_server_root,     # for the import resolver
                config.instance.marketplace_api_url,  # for the import resolver
                validate_only=True,
            )

        try:
            if not blueprint_url:
                upload_manager.upload_blueprint_archive_to_file_server(
                    blueprint_id)
            workflow_executor.execute_workflow(messages)
        except Exception:
            sm.sm.delete(blueprint)
            upload_manager.cleanup_blueprint_archive_from_file_server(
                blueprint_id, current_tenant.name)
            raise
        return blueprint, 201


class BlueprintsIdArchive(resources_v1.BlueprintsIdArchive):
    @authorize('blueprint_upload')
    def put(self, blueprint_id, **kwargs):
        """
        Upload an archive for an existing a blueprint.

        Used for uploading the blueprint's archive, downloaded from a URL using
        a system workflow, to the manager's file server
        This method is for internal use only.
        """
        upload_manager. \
            upload_blueprint_archive_to_file_server(blueprint_id=blueprint_id)
