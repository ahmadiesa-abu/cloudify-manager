#########
# Copyright (c) 2013 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from cloudify.utils import setup_logger

from integration_tests.tests.utils import get_resource


logger = setup_logger('Flask Utils', logging.INFO)

PREPARE_SCRIPT = '/tmp/prepare_reset_storage.py'
SCRIPT_PATH = '/tmp/reset_storage.py'
CONFIG_PATH = '/tmp/reset_storage_config.json'


def prepare_reset_storage_script(environment):
    reset_script = get_resource('scripts/reset_storage.py')
    prepare = get_resource('scripts/prepare_reset_storage.py')
    environment.copy_file_to_manager(reset_script, SCRIPT_PATH)
    environment.copy_file_to_manager(prepare, PREPARE_SCRIPT)
    environment.execute_python_on_manager(
        [PREPARE_SCRIPT, '--config', CONFIG_PATH],
    )


def reset_storage(environment):
    logger.info('Resetting PostgreSQL DB')
    # reset the storage by calling a script on the manager, to access
    # localhost-only APIs (rabbitmq management api)
    environment.execute_python_on_manager(
        [SCRIPT_PATH, '--config', CONFIG_PATH]
    )
