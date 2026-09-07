# Copyright 2015 Google Inc. All rights reserved.
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
"""This module contains the default configuration for testing."""


class TestConfig:
    """Config for the test environment."""

    DEBUG = True
    TESTING = True
    SECRET_KEY = "testing"
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    WTF_CSRF_ENABLED = False
    CELERY_BROKER_URL = "redis://127.0.0.1:6379"
    OPENSEARCH_HOST = "noserver"
    OPENSEARCH_PORT = 4711
    OPENSEARCH_USER = None
    OPENSEARCH_PASSWORD = None
    OPENSEARCH_SSL = False
    OPENSEARCH_VERIFY_CERTS = True
    LABELS_TO_PREVENT_DELETION = ["protected", "magic"]
    UPLOAD_ENABLED = False
    UPLOAD_FOLDER = "/tmp"
    AUTO_SKETCH_ANALYZERS = []
    SIMILARITY_DATA_TYPES = []
    SIGMA_RULES_FOLDERS = ["./data/sigma/rules/"]
    INTELLIGENCE_TAG_METADATA = "./data/intelligence_tag_metadata.yaml"
    CONTEXT_LINKS_CONFIG_PATH = "./tests/test_events/mock_context_links.yaml"
    LLM_PROVIDER = "test"
    LLM_PROVIDER_CONFIGS = {"default": {"test": "test"}}
    DFIQ_ENABLED = False
    DATA_TYPES_PATH = "./tests/test_data/nl2q/test_data_types.csv"
    PROMPT_NL2Q = "./tests/test_data/nl2q/test_prompt_nl2q"
    EXAMPLES_NL2Q = "./tests/test_data/nl2q/test_examples_nl2q"
