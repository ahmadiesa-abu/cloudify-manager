# Copyright (c) 2017-2019 Cloudify Platform Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import functools

from cloudify import ctx
from cloudify.utils import exception_to_error_cause
from cloudify_rest_client.exceptions import ForbiddenWhileCancelling
from cloudify.exceptions import NonRecoverableError, OperationRetry


def generate_traceback_exception():
    _, exc_value, exc_traceback = sys.exc_info()
    response = exception_to_error_cause(exc_value, exc_traceback)
    return response


def errors_nonrecoverable(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ForbiddenWhileCancelling:
            raise OperationRetry()
        except Exception as ex:
            response = generate_traceback_exception()

            ctx.logger.error(
                'Error traceback %s with message %s',
                response['traceback'], response['message'])

            raise NonRecoverableError(f'Error in {f.__name__}: {ex}')
    return wrapper


def get_deployment_by_id(client, deployment_id):
    """
    Searching for deployment_id in all deployments in order to differentiate
    not finding the deployment then other kinds of errors, like server
    failure.
    """
    deployments = client.deployments.list(_include=['id'], id=deployment_id)
    return deployments[0] if deployments else None
