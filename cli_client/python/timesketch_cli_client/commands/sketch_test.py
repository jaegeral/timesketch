# Copyright 2021 Google Inc. All rights reserved.
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
"""Tests for sketch command."""

import os
import unittest
from unittest import mock
import pandas as pd

from click.testing import CliRunner

from timesketch_api_client import sketch as api_sketch
from timesketch_api_client import test_lib as api_test_lib

from timesketch_cli_client import test_lib
from timesketch_cli_client.commands.sketch import sketch_group


class SketchTest(unittest.TestCase):
    """Test Sketch."""

    @mock.patch("requests.Session", api_test_lib.mock_session)
    def setUp(self):
        """Setup test case."""
        self.ctx = test_lib.get_cli_context()

    def test_list_sketches(self):
        """Test the 'sketch list' command.

        Verifies that the command executes successfully and that the output
        contains the expected sketch information in text format.
        """
        runner = CliRunner()
        self.ctx.output_format_from_flag = "text"
        result = runner.invoke(sketch_group, ["list"], obj=self.ctx)
        self.assertIn("1 test", result.output)

    def test_describe_sketch(self):
        """Test the 'sketch describe' command.

        Verifies that the command executes successfully and that the output
        contains the expected sketch name and description in text format.
        """
        runner = CliRunner()
        original_ctx_obj = self.ctx

        # Set the output_format_from_flag directly on the existing context object.
        # The `output_format` property will pick this up.
        original_ctx_obj.output_format_from_flag = "text"

        result = runner.invoke(sketch_group, ["describe"], obj=original_ctx_obj)
        expected_output = "Name: test\nDescription: test\nStatus: Unknown\n"
        self.assertEqual(
            result.output, expected_output, f"Unexpected output: {result.output}"
        )

    @mock.patch("timesketch_cli_client.commands.sketch.search.Search", autospec=True)
    def test_export_only_with_annotations_csv_success(self, mock_search_class):
        """Test successful export of annotated events to CSV."""
        runner = CliRunner()

        df_comments = pd.DataFrame(
            {
                "_id": ["event1", "event4"],
                "message": ["comment_msg1", "shared_msg_comment"],
                "field_c": ["c1", "c4"],
            }
        )
        df_stars = pd.DataFrame(
            {
                "_id": ["event2", "event4"],
                "message": ["star_msg2", "shared_msg_star"],
                "field_s": ["s2", "s4"],
            }
        )
        df_labels = pd.DataFrame(
            {
                "_id": ["event3", "event5", "event4"],
                "message": ["label_msg3", "label_msg5", "shared_msg_label"],
                "field_l": ["l3", "l5", "l4"],
            }
        )

        # Configure mock Search instances
        mock_search_inst_comments = mock.MagicMock()
        mock_search_inst_comments.to_pandas.return_value = df_comments

        mock_search_inst_stars = mock.MagicMock()
        mock_search_inst_stars.to_pandas.return_value = df_stars

        mock_search_inst_labels = mock.MagicMock()
        mock_search_inst_labels.to_pandas.return_value = df_labels

        mock_search_class.side_effect = [
            mock_search_inst_comments,
            mock_search_inst_stars,
            mock_search_inst_labels,
        ]

        with runner.isolated_filesystem():
            result = runner.invoke(
                sketch_group,
                ["export-only-with-annotations", "--filename", "output.csv"],
                obj=self.ctx,
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(
                "Exporting events with comments, stars, OR labels to output.csv...",
                result.output,
            )
            self.assertIn("Searching for events with comments...", result.output)
            self.assertIn("Found 2 events.", result.output)  # For comments
            self.assertIn("Searching for events with stars...", result.output)
            self.assertIn("Found 2 events.", result.output)  # For stars
            self.assertIn("Searching for events with labels...", result.output)
            self.assertIn("Found 3 events.", result.output)  # For labels
            self.assertIn(
                "Combining and deduplicating results (found 7 total)...", result.output
            )
            self.assertIn("Found 5 unique annotated events.", result.output)
            self.assertIn("Writing 5 events to file...", result.output)
            self.assertIn(
                "Export finished: 5 unique annotated events written.", result.output
            )

            with open("output.csv", "r", encoding="utf-8") as f:
                written_content = f.read()

            # Construct expected CSV
            # Order of concatenation: comments, stars, labels.
            # drop_duplicates keeps first.
            # Event4 from comments should be kept.
            expected_df = pd.DataFrame(
                {
                    "_id": ["event1", "event4", "event2", "event3", "event5"],
                    "message": [
                        "comment_msg1",
                        "shared_msg_comment",
                        "star_msg2",
                        "label_msg3",
                        "label_msg5",
                    ],
                    "field_c": ["c1", "c4", pd.NA, pd.NA, pd.NA],
                    "field_s": [pd.NA, pd.NA, "s2", pd.NA, pd.NA],
                    "field_l": [pd.NA, pd.NA, pd.NA, "l3", "l5"],
                }
            )
            # Pandas to_csv by default fills NA with empty strings
            expected_csv = expected_df.to_csv(
                index=False, header=True, lineterminator="\n"
            )
            self.assertEqual(written_content, expected_csv)

    @mock.patch("timesketch_cli_client.commands.sketch.search.Search", autospec=True)
    def test_export_only_with_annotations_jsonl_with_limit(self, mock_search_class):
        """Test successful export to JSONL with a limit."""
        runner = CliRunner()

        df_comments = pd.DataFrame({"_id": ["event1"], "message": ["comment_msg1"]})
        df_stars = pd.DataFrame({"_id": ["event2"], "message": ["star_msg2"]})
        df_labels = pd.DataFrame({"_id": ["event3"], "message": ["label_msg3"]})

        mock_search_inst_comments = mock.MagicMock()
        mock_search_inst_comments.to_pandas.return_value = df_comments
        mock_search_inst_stars = mock.MagicMock()
        mock_search_inst_stars.to_pandas.return_value = df_stars
        mock_search_inst_labels = mock.MagicMock()
        mock_search_inst_labels.to_pandas.return_value = df_labels
        mock_search_class.side_effect = [
            mock_search_inst_comments,
            mock_search_inst_stars,
            mock_search_inst_labels,
        ]

        with runner.isolated_filesystem():
            result = runner.invoke(
                sketch_group,
                [
                    "export-only-with-annotations",
                    "--filename",
                    "output.jsonl",
                    "--output-format",
                    "jsonl",
                    "--limit",
                    "2",
                ],
                obj=self.ctx,
            )
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Found 3 unique annotated events.", result.output)
            self.assertIn("Applying limit of 2 events.", result.output)
            self.assertIn("Writing 2 events to file...", result.output)
            self.assertIn(
                "Export finished: 2 unique annotated events written.", result.output
            )

            with open("output.jsonl", "r", encoding="utf-8") as f:
                written_content = f.read()

            expected_jsonl = (
                '{"_id":"event1","message":"comment_msg1"}\n'
                '{"_id":"event2","message":"star_msg2"}\n'
            )
            self.assertEqual(written_content, expected_jsonl)

    @mock.patch("timesketch_cli_client.commands.sketch.search.Search", autospec=True)
    def test_export_only_with_annotations_no_results(self, mock_search_class):
        """Test export when no annotated events are found."""
        runner = CliRunner()

        empty_df = pd.DataFrame(
            columns=["_id", "message"]
        )  # Ensure _id column for logic

        mock_search_inst = mock.MagicMock()
        mock_search_inst.to_pandas.return_value = empty_df
        mock_search_class.return_value = mock_search_inst  # Same mock for all 3 calls

        with runner.isolated_filesystem():
            result = runner.invoke(
                sketch_group,
                ["export-only-with-annotations", "--filename", "output.csv"],
                obj=self.ctx,
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(
                "No annotated events found across all search types.", result.output
            )
            self.assertFalse(os.path.exists("output.csv"))

    @mock.patch("timesketch_cli_client.commands.sketch.search.Search", autospec=True)
    def test_export_only_with_annotations_search_error(self, mock_search_class):
        """Test export when one of the searches fails."""
        runner = CliRunner()

        df_comments = pd.DataFrame({"_id": ["event1"], "message": ["comment_msg1"]})
        df_labels = pd.DataFrame({"_id": ["event3"], "message": ["label_msg3"]})

        mock_search_inst_comments = mock.MagicMock()
        mock_search_inst_comments.to_pandas.return_value = df_comments

        mock_search_inst_stars = mock.MagicMock()
        mock_search_inst_stars.to_pandas.side_effect = Exception("Star search failed!")

        mock_search_inst_labels = mock.MagicMock()
        mock_search_inst_labels.to_pandas.return_value = df_labels

        mock_search_class.side_effect = [
            mock_search_inst_comments,
            mock_search_inst_stars,
            mock_search_inst_labels,
        ]

        with runner.isolated_filesystem():
            result = runner.invoke(
                sketch_group,
                ["export-only-with-annotations", "--filename", "output.csv"],
                obj=self.ctx,
            )
            self.assertEqual(
                result.exit_code, 0, result.output
            )  # Should still succeed overall
            self.assertIn(
                "WARNING: Error during search for stars: Star search failed!",
                result.output,
            )
            self.assertIn(
                "Found 2 unique annotated events.", result.output
            )  # event1 and event3
            self.assertIn("Writing 2 events to file...", result.output)

            with open("output.csv", "r", encoding="utf-8") as f:
                written_content = f.read()

            expected_df = pd.DataFrame(
                {"_id": ["event1", "event3"], "message": ["comment_msg1", "label_msg3"]}
            )
            expected_csv = expected_df.to_csv(
                index=False, header=True, lineterminator="\n"
            )
            self.assertEqual(written_content, expected_csv)

    @mock.patch("timesketch_cli_client.commands.sketch.search.Search", autospec=True)
    def test_export_only_with_annotations_id_column_missing(self, mock_search_class):
        """Test export fails if _id column is missing from results."""
        runner = CliRunner()

        df_no_id = pd.DataFrame({"message": ["some_message"]})

        mock_search_inst_comments = mock.MagicMock()
        mock_search_inst_comments.to_pandas.return_value = df_no_id
        mock_search_inst_others = mock.MagicMock()
        mock_search_inst_others.to_pandas.return_value = pd.DataFrame()

        mock_search_class.side_effect = [
            mock_search_inst_comments,
            mock_search_inst_others,
            mock_search_inst_others,
        ]

        with runner.isolated_filesystem():
            result = runner.invoke(
                sketch_group,
                ["export-only-with-annotations", "--filename", "output.csv"],
                obj=self.ctx,
            )

            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn(
                "ERROR: '_id' column not found in results, cannot deduplicate.",
                result.output,
            )
            self.assertFalse(os.path.exists("output.csv"))

    def test_archive_sketch_success(self):
        """Test archiving an active sketch succeeds without calling can_archive."""
        runner = CliRunner()
        with mock.patch.object(
            api_sketch.Sketch, "is_archived", return_value=False
        ), mock.patch.object(
            api_sketch.Sketch, "archive", return_value=True
        ) as mock_archive:
            result = runner.invoke(sketch_group, ["archive"], obj=self.ctx)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Sketch archived", result.output)
        mock_archive.assert_called_once_with()

    def test_archive_sketch_already_archived(self):
        """Test archiving an already-archived sketch exits with status 1."""
        runner = CliRunner()
        with mock.patch.object(
            api_sketch.Sketch, "is_archived", return_value=True
        ), mock.patch.object(api_sketch.Sketch, "archive") as mock_archive:
            result = runner.invoke(sketch_group, ["archive"], obj=self.ctx)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Error: Sketch is already archived.", result.output)
        mock_archive.assert_not_called()

    def test_archive_sketch_failure(self):
        """Test archiving failure (e.g. HTTP 403) prints error and exits 1."""
        runner = CliRunner()
        with mock.patch.object(
            api_sketch.Sketch, "is_archived", return_value=False
        ), mock.patch.object(
            api_sketch.Sketch, "archive", return_value=False
        ) as mock_archive:
            result = runner.invoke(sketch_group, ["archive"], obj=self.ctx)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Error: Failed to archive sketch.", result.output)
        self.assertNotIn("Sketch archived", result.output)
        mock_archive.assert_called_once_with()

    def test_unarchive_sketch_success(self):
        """Test unarchiving an archived sketch succeeds."""
        runner = CliRunner()
        with mock.patch.object(
            api_sketch.Sketch, "is_archived", return_value=True
        ), mock.patch.object(
            api_sketch.Sketch, "unarchive", return_value=True
        ) as mock_unarchive:
            result = runner.invoke(sketch_group, ["unarchive"], obj=self.ctx)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Sketch unarchived", result.output)
        mock_unarchive.assert_called_once_with()

    def test_unarchive_sketch_not_archived(self):
        """Test unarchiving an active sketch exits with status 1."""
        runner = CliRunner()
        with mock.patch.object(
            api_sketch.Sketch, "is_archived", return_value=False
        ), mock.patch.object(api_sketch.Sketch, "unarchive") as mock_unarchive:
            result = runner.invoke(sketch_group, ["unarchive"], obj=self.ctx)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Error: Sketch is not archived.", result.output)
        mock_unarchive.assert_not_called()

    def test_unarchive_sketch_failure(self):
        """Test unarchiving failure (e.g. HTTP 403) prints error and exits 1."""
        runner = CliRunner()
        with mock.patch.object(
            api_sketch.Sketch, "is_archived", return_value=True
        ), mock.patch.object(
            api_sketch.Sketch, "unarchive", return_value=False
        ) as mock_unarchive:
            result = runner.invoke(sketch_group, ["unarchive"], obj=self.ctx)

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("Error: Failed to unarchive sketch.", result.output)
        self.assertNotIn("Sketch unarchived", result.output)
        mock_unarchive.assert_called_once_with()
