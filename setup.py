# Copyright (c) 2013 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import setuptools
import configparser
import json

metadata = {
    "commit": "7e29dcf9c1b8a4a96a8e1e6ce21c19f80533cef6",
    "branch": "esj-protected/stable-2025.2/32.0-nordix-5"
}

# Write metadata to a file
with open('/opt/ironic_build_metadata.json', 'w') as f:
    json.dump(metadata, f, indent=2)

setuptools.setup(
    setup_requires=['pbr>=6.0.0'],
    pbr=True,
)
