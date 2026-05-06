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

import subprocess
import time
from . import interface
from . import manager


class TelemetryTest(interface.BaseEndToEndTest):
    """End-to-end tests for OpenTelemetry."""

    NAME = "telemetry_test"

    def _set_otel_mode(self, mode):
        """Helper to set telemetry mode in the container."""
        # Note: This assumes gunicorn is running with --reload
        subprocess.run(
            ['sudo', 'docker', 'exec', 'timesketch-dev', 'export', f'TIMESKETCH_OTEL_MODE={mode}'],
            check=True
        )
        # We also need to restart gunicorn to pick up the env change reliably
        # if the export doesn't stick to the parent process.
        # In a real environment, we'd restart the container or service.
        subprocess.run(
            ['sudo', 'docker', 'exec', 'timesketch-dev', 'pkill', '-9', 'gunicorn'],
            check=False
        )
        time.sleep(3) # Wait for reload

    def test_console_telemetry(self):
        """Test that spans are printed to stdout in console mode."""
        self._set_otel_mode('otlp-console')
        
        # 1. Trigger an API call
...
            self.assertions.assertIn('"user.name"', logs)

        except subprocess.CalledProcessError as e:
            self.assertions.fail(f"Failed to fetch docker logs: {e}")

    def _unset_otel_mode(self):
        """Helper to completely remove telemetry mode from the container."""
        subprocess.run(
            ['sudo', 'docker', 'exec', 'timesketch-dev', 'unset', 'TIMESKETCH_OTEL_MODE'],
            check=False
        )
        # Restart to ensure clean process state
        subprocess.run(
            ['sudo', 'docker', 'exec', 'timesketch-dev', 'pkill', '-9', 'gunicorn'],
            check=False
        )
        time.sleep(3)

    def test_missing_config_telemetry(self):
        """Test that the system is stable when the config is completely missing."""
        # 1. Completely unset the variable
        self._unset_otel_mode()

        # 2. Trigger an API call
        sketches = self.api.get_sketches()
        self.assertions.assertIsNotNone(sketches)

        # 3. Check logs for total silence
        try:
            result = subprocess.run(
                ['sudo', 'docker', 'logs', 'timesketch-dev', '--tail', '50'],
                capture_output=True,
                text=True,
                check=True
            )
            logs = result.stdout + result.stderr
            
            # Verify NO spans and NO errors
            self.assertions.assertNotIn('"name": "/api/v1/sketches/"', logs)
            self.assertions.assertNotIn('opentelemetry', logs)
            self.assertions.assertNotIn('Telemetry operation failed', logs)

        except subprocess.CalledProcessError as e:
            self.assertions.fail(f"Failed to fetch docker logs: {e}")


manager.EndToEndTestManager.register_test(TelemetryTest)
