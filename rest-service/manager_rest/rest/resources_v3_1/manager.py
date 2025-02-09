#########
# Copyright (c) 2016 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#  * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  * See the License for the specific language governing permissions and
#  * limitations under the License.
import json
import os
import shutil
import tempfile
import tarfile
import zipfile
from datetime import datetime
from typing import Any

from flask import request, send_file
from flask_restful import Resource
from flask_restful.reqparse import Argument

from manager_rest import config
from manager_rest.manager_exceptions import (
    ArchiveTypeError,
    UploadFileMissing,
    NoAuthProvided,
    MultipleFilesUploadException,
)
from manager_rest.security import SecuredResource, premium_only
from manager_rest.security.user_handler import get_token_status
from manager_rest.rest import rest_utils
from manager_rest.storage import get_storage_manager, models
from manager_rest.security.authorization import (
    authorize,
    is_user_action_allowed,
)
from manager_rest.rest.rest_decorators import (
    marshal_with,
    paginate
)
from manager_rest.persistent_storage import get_storage_handler
try:
    from cloudify_premium import manager as manager_premium
except ImportError:
    manager_premium = None


INDEX_JSON_FILENAME = '.cloudify-index.json'
RESOURCES_PATH = '/resources/'


# community base classes for the managers and brokers endpoints:
# the http verbs that are implemented in premium, are made to throw a
# "premium-only" exception here, so they throw that in community instead of
# a 404. The method body should never be executed, because the @premium_only
# decorator should prevent it.
class _CommunityManagersBase(SecuredResource):
    @authorize('manager_manage')
    @marshal_with(models.Manager)
    @premium_only
    def post(self):
        raise NotImplementedError('Premium implementation only')


class _CommunityManagersId(SecuredResource):
    @authorize('manager_manage')
    @marshal_with(models.Manager)
    @premium_only
    def put(self):
        raise NotImplementedError('Premium implementation only')

    @authorize('manager_manage')
    @marshal_with(models.Manager)
    @premium_only
    def delete(self):
        raise NotImplementedError('Premium implementation only')


class _CommunityBrokersBase(SecuredResource):
    @authorize('broker_manage')
    @marshal_with(models.RabbitMQBroker)
    @premium_only
    def post(self):
        raise NotImplementedError('Premium implementation only')


class _CommunityDBNodesBase(SecuredResource):
    @authorize('cluster_node_config_update')
    @marshal_with(models.DBNodes)
    @paginate
    @premium_only
    def post(self, pagination=None):
        raise NotImplementedError('Premium implementation only')


class _CommunityBrokersId(SecuredResource):
    @authorize('broker_manage')
    @marshal_with(models.Manager)
    @premium_only
    def put(self):
        raise NotImplementedError('Premium implementation only')

    @authorize('broker_manage')
    @marshal_with(models.Manager)
    @premium_only
    def delete(self):
        raise NotImplementedError('Premium implementation only')


managers_base: Any
brokers_base: Any
dbnodes_base: Any
ManagersId: Any
RabbitMQBrokersId: Any
if manager_premium:
    managers_base = manager_premium.ManagersBase
    brokers_base = manager_premium.RabbitMQBrokersBase
    dbnodes_base = manager_premium.DBNodeBase
    ManagersId = manager_premium.ManagersId
    RabbitMQBrokersId = manager_premium.RabbitMQBrokersId
else:
    managers_base = _CommunityManagersBase
    brokers_base = _CommunityBrokersBase
    dbnodes_base = _CommunityDBNodesBase
    ManagersId = _CommunityManagersId
    RabbitMQBrokersId = _CommunityBrokersId


class Managers(managers_base):
    @marshal_with(models.Manager)
    @paginate
    @authorize('manager_get')
    def get(self, pagination=None, _include=None):
        """
        Get the list of managers in the database
        :param hostname: optional hostname to return only a specific manager
        :param _include: optional, what columns to include in the response
        """
        args = rest_utils.get_args_and_verify_arguments([
            Argument('hostname', type=str, required=False)
        ])
        hostname = args.get('hostname')
        if hostname:
            return get_storage_manager().list(
                models.Manager,
                None,
                filters={'hostname': hostname}
            )
        return get_storage_manager().list(
            models.Manager,
            include=_include
        )


class RabbitMQBrokers(brokers_base):
    @marshal_with(models.RabbitMQBroker)
    @paginate
    @authorize('broker_get')
    def get(self, pagination=None):
        """List brokers from the database."""
        brokers = get_storage_manager().list(models.RabbitMQBroker)
        if not is_user_action_allowed('broker_credentials'):
            for broker in brokers:
                broker.username = None
                broker.password = None
        return brokers


class DBNodes(dbnodes_base):
    @marshal_with(models.DBNodes)
    @paginate
    @authorize('db_nodes_get')
    def get(self, pagination=None):
        """List DB nodes from database"""
        return get_storage_manager().list(models.DBNodes)


class FileServerProxy(SecuredResource):
    def __init__(self):
        self.storage_handler = get_storage_handler()

    def delete(self, path=None, **_):
        rel_path = _resource_relative_path(path)

        if not path:
            return {}, 404

        if not _is_resource_path_directory(rel_path):
            return self.storage_handler.delete(rel_path)

        raise NotImplementedError('Removing directories is not supported')

    def get(self, path=None, **_):
        rel_path = _resource_relative_path(path)

        if not path:
            return {}, 404

        args = rest_utils.get_args_and_verify_arguments([
            Argument('archive', type=bool, required=False)
        ])
        as_archive = args.get('archive', False)

        if not _is_resource_path_directory(rel_path):
            return self.storage_handler.proxy(rel_path)
        elif not as_archive:
            files_metadata = {
                f_path: f_mtime
                for f_info in self.storage_handler.list(rel_path)
                for f_path, f_mtime in f_info.serialize(rel_path).items()
            }
            return files_metadata, 200
        else:
            archive_file_name, download_file_name =\
                self._prepare_directory_archive(rel_path)

            result = send_file(
                archive_file_name,
                download_name=download_file_name,
                as_attachment=True,
            )

            os.remove(archive_file_name)
            return result

    def _prepare_directory_archive(self, path):
        tmp_dir_name = tempfile.mkdtemp()
        metadata = {}
        for file_info in self.storage_handler.list(path):
            src_path = os.path.join(
                path,
                os.path.relpath(file_info.filepath, path),
            )
            dst_path = os.path.join(
                tmp_dir_name,
                os.path.relpath(src_path, path),
            )
            dst_dir = os.path.dirname(dst_path)
            if not os.path.isdir(dst_dir):
                os.makedirs(dst_dir)
            with self.storage_handler.get(src_path) as tmp_file_name:
                shutil.copy2(tmp_file_name, dst_path)
            metadata.update(file_info.serialize(path))

        with open(os.path.join(tmp_dir_name, INDEX_JSON_FILENAME), 'wt',
                  encoding='utf-8') as fp:
            json.dump(metadata, fp)

        archive_file_name = _create_archive(tmp_dir_name)
        shutil.rmtree(tmp_dir_name)

        if stripped := path.rstrip('/'):
            download_file_name = f"{os.path.basename(stripped)}.tar.gz"
        else:
            download_file_name = 'resource.tar.gz'

        return archive_file_name, download_file_name

    def put(self, path=None):
        args = rest_utils.get_args_and_verify_arguments([
            Argument('extract', type=bool, default=False, required=False)
        ])
        extract = args.get('extract', False)
        file_mtime = None

        if request.files:
            if len(request.files) > 1:
                raise MultipleFilesUploadException(
                    'Multiple files upload is not supported')
            _, tmp_file_name = tempfile.mkstemp()
            for uploaded_file in request.files.values():
                with open(tmp_file_name, 'wb') as tmp_file:
                    tmp_file.write(uploaded_file.stream.read())
            try:
                file_mtime = request.form.get('mtime')
            except AttributeError:
                pass
        else:
            _, tmp_file_name = tempfile.mkstemp()
            with open(tmp_file_name, 'wb') as tmp_file:
                tmp_file.write(request.data)

        if file_mtime:
            file_timestamp = datetime.fromisoformat(file_mtime).timestamp()
            os.utime(tmp_file_name, (file_timestamp, file_timestamp))

        if not extract:
            return self.storage_handler.move(tmp_file_name, path)

        try:
            tmp_dir_name = _extract_archive(tmp_file_name)
        finally:
            os.remove(tmp_file_name)

        try:
            os.remove(os.path.join(tmp_dir_name, INDEX_JSON_FILENAME))
        except FileNotFoundError:
            pass

        for dir_path, _, file_names in os.walk(tmp_dir_name):
            for file_name in file_names:
                src = os.path.join(tmp_dir_name, dir_path, file_name)
                dst = os.path.join(
                    os.path.dirname(path),
                    os.path.relpath(
                        os.path.join(dir_path, file_name),
                        tmp_dir_name,
                    ),
                )

                self.storage_handler.move(src, dst)
        shutil.rmtree(tmp_dir_name)
        return "", 200

    def post(self, path=None):
        if not request.files:
            raise UploadFileMissing('File upload error: no files provided')

        for _, file in request.files.items():
            _, tmp_file_name = tempfile.mkstemp()
            with open(tmp_file_name, 'wb') as tmp_file:
                tmp_file.write(file.stream.read())
                tmp_file.close()
            self.storage_handler.move(
                tmp_file_name,
                os.path.join(path or '', file.filename)
            )
        return "", 200


class MonitoringAuth(Resource):
    """Auth endpoint for monitoring.

    Users who access /monitoring need to first pass through auth_request
    proxying to here. If this returns 200, the user has full access
    to local prometheus.

    Note: This is a subclass of Resource, not SecuredResource, because this
    does authentication in a special way.
    """
    def get(self, **_):
        # this request checks for stage's auth cookie and authenticates that
        # way, so that users can access monitoring once they've logged in
        # to stage.
        # Only this endpoint allows cookie login, because other endpoints
        # don't have any CSRF protection, and this one is read-only anyway.
        if token := request.cookies.get('XSRF-TOKEN'):
            user = get_token_status(token)
            monitoring_allowed_roles = set(
                config.instance.authorization_permissions
                .get('monitoring', [])
            )
            if (
                user.is_bootstrap_admin
                # only check system roles, and not tenant roles, because
                # monitoring is not a tenant-specific action
                or set(user.system_roles) & monitoring_allowed_roles
            ):
                return "", 200
        raise NoAuthProvided()


def _extract_archive(file_name, dst_dir=None):
    archive_type = _archive_type(file_name).lower()
    if dst_dir is None:
        dst_dir = tempfile.mkdtemp()
    match archive_type:
        case 'tar':
            with tarfile.open(file_name, 'r:*') as archive:
                archive.extractall(path=dst_dir)
            return dst_dir
        case 'zip':
            with zipfile.ZipFile(file_name) as archive:
                archive.extractall(path=dst_dir)
            return dst_dir
    raise ArchiveTypeError(f'Unknown archive type {archive_type}')


def _create_archive(dir_name):
    _, tmp_file_name = tempfile.mkstemp(suffix='.tar.gz')
    with tarfile.open(tmp_file_name, 'w:gz') as archive:
        archive.add(dir_name, arcname='./')
    return tmp_file_name


def _archive_type(file_name):
    if tarfile.is_tarfile(file_name):
        return 'tar'
    if zipfile.is_zipfile(file_name):
        return 'zip'


def _is_resource_path_directory(path):
    return path.endswith('/')


def _resource_relative_path(uri=None):
    if not uri:
        uri = request.headers['X-Original-Uri']
        if not uri.startswith(RESOURCES_PATH):
            return None

    return uri
