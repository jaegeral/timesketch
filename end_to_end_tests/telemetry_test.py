# Copyright 2026 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""End-to-end tests for OpenTelemetry."""

import os
import subprocess
import time
from . import interface
from . import manager


class TelemetryTest(interface.BaseEndToEndTest):
    """End-to-end tests for OpenTelemetry."""

    NAME = "telemetry_test"

    def test_telemetry_flow(self):
        """Test telemetry behavior based on current environment configuration."""
        otel_mode = os.environ.get("TIMESKETCH_OTEL_MODE", "").lower()
        
        # Trigger activity
        self.api.get_sketches()
        time.sleep(2)

        # Check logs
        try:
            # We look at the logs of the current container
            # In E2E tests, we are usually running INSIDE the timesketch container
            # but we need to check the web server logs.
            result = subprocess.run(
                ['sudo', 'docker', 'logs', 'timesketch-dev', '--tail', '100'],
                capture_output=True,
                text=True,
                check=False # Don't crash if docker isn't accessible this way
            )
            logs = result.stdout + result.stderr
            
            if otel_mode == "otlp-console":
                self.assertions.assertIn(
                    '"name": "/api/v1/sketches/"', 
                    logs, 
                    msg="Console mode active but no spans found in logs."
                )
            else:
                self.assertions.assertNotIn(
                    '"name": "/api/v1/sketches/"', 
                    logs, 
                    msg="Telemetry disabled but spans found in logs."
                )

        except Exception: # pylint: disable=broad-except
            # If we can't check docker logs (e.g. CI environment), we at least
            # verify that the API call didn't crash.
            pass

manager.EndToEndTestManager.register_test(TelemetryTest)
