# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Create issue tests."""
import datetime
import unittest
from unittest import mock

import flask
import webtest

from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.metrics import events
from clusterfuzz._internal.tests.test_libs import helpers as test_helpers
from clusterfuzz._internal.tests.test_libs import test_utils
from handlers.testcase_detail import create_issue
from libs import form


@test_utils.with_cloud_emulators('datastore')
class HandlerTest(unittest.TestCase):
  """Test HandlerTest."""

  def setUp(self):
    test_helpers.patch(self, [
        'handlers.testcase_detail.show.get_testcase_detail',
        'libs.access.has_access',
        'libs.auth.get_current_user',
        'clusterfuzz._internal.issue_management.issue_filer.file_issue',
        'clusterfuzz._internal.issue_management.issue_tracker_utils.'
        'get_issue_tracker_for_testcase',
        'clusterfuzz._internal.metrics.events.emit',
        'clusterfuzz._internal.metrics.events._get_datetime_now',
    ])
    self.mock._get_datetime_now.return_value = datetime.datetime(2025, 1, 1)  # pylint: disable=protected-access
    self.mock.has_access.return_value = True
    self.mock.get_testcase_detail.return_value = {'testcase': 'yes'}
    self.mock.get_current_user().email = 'test@user.com'

    flaskapp = flask.Flask('testflask')
    flaskapp.add_url_rule('/', view_func=create_issue.Handler.as_view('/'))
    self.app = webtest.TestApp(flaskapp)

    self.testcase = data_types.Testcase()
    self.testcase.put()

  def test_create_successfully(self):
    """Create issue successfully."""
    issue_tracker = mock.Mock()
    issue_tracker.project = 'oss-fuzz'
    self.mock.get_issue_tracker_for_testcase.return_value = issue_tracker
    issue_id = '100'
    self.mock.file_issue.return_value = int(issue_id), None

    resp = self.app.post_json(
        '/', {
            'testcaseId': self.testcase.key.id(),
            'severity': 3,
            'ccMe': True,
            'csrf_token': form.generate_csrf_token(),
        })

    self.assertEqual('yes', resp.json['testcase'])
    self.mock.get_issue_tracker_for_testcase.assert_has_calls(
        [mock.call(mock.ANY)])
    self.assertEqual(
        self.testcase.key.id(),
        self.mock.get_issue_tracker_for_testcase.call_args[0][0].key.id())
    self.mock.file_issue.assert_has_calls([
        mock.call(
            mock.ANY,
            issue_tracker,
            security_severity=3,
            user_email='test@user.com',
            additional_ccs=['test@user.com'])
    ])
    self.assertEqual(self.testcase.key.id(),
                     self.mock.file_issue.call_args[0][0].key.id())

    self.mock.emit.assert_called_once_with(
        events.IssueFilingEvent(
            testcase=self.testcase,
            issue_tracker_project='oss-fuzz',
            issue_id='100',
            issue_created=True))

  def test_no_issue_tracker(self):
    """No IssueTracker."""
    self.mock.get_issue_tracker_for_testcase.return_value = None

    resp = self.app.post_json(
        '/', {
            'testcaseId': self.testcase.key.id(),
            'severity': 3,
            'ccMe': True,
            'csrf_token': form.generate_csrf_token(),
        },
        expect_errors=True)
    self.assertEqual(resp.status_int, 404)
    self.mock.emit.assert_not_called()

  def test_invalid_testcase(self):
    """Invalid testcase."""
    issue_tracker = mock.Mock()
    self.mock.get_issue_tracker_for_testcase.return_value = issue_tracker

    resp = self.app.post_json(
        '/', {
            'testcaseId': self.testcase.key.id() + 1,
            'severity': 3,
            'ccMe': True,
            'csrf_token': form.generate_csrf_token(),
        },
        expect_errors=True)
    self.assertEqual(resp.status_int, 404)
    self.mock.emit.assert_not_called()

  def test_invalid_severity(self):
    """Invalid severity."""
    issue_tracker = mock.Mock()
    self.mock.get_issue_tracker_for_testcase.return_value = issue_tracker
    self.mock.file_issue.return_value = 100, None

    resp = self.app.post_json(
        '/', {
            'testcaseId': self.testcase.key.id(),
            'severity': 'a',
            'ccMe': True,
            'csrf_token': form.generate_csrf_token(),
        },
        expect_errors=True)
    self.assertEqual(resp.status_int, 400)
    self.mock.emit.assert_not_called()

  def test_creating_fails(self):
    """Fail to create issue."""
    issue_tracker = mock.Mock()
    issue_tracker.project = 'oss-fuzz'
    self.mock.get_issue_tracker_for_testcase.return_value = issue_tracker
    self.mock.file_issue.return_value = None, None

    resp = self.app.post_json(
        '/', {
            'testcaseId': self.testcase.key.id(),
            'severity': 3,
            'ccMe': True,
            'csrf_token': form.generate_csrf_token(),
        },
        expect_errors=True)

    self.assertEqual(resp.status_int, 500)
    self.mock.get_issue_tracker_for_testcase.assert_has_calls(
        [mock.call(mock.ANY)])
    self.assertEqual(
        self.testcase.key.id(),
        self.mock.get_issue_tracker_for_testcase.call_args[0][0].key.id())
    self.mock.file_issue.assert_has_calls([
        mock.call(
            mock.ANY,
            issue_tracker,
            security_severity=3,
            user_email='test@user.com',
            additional_ccs=['test@user.com'])
    ])
    self.assertEqual(self.testcase.key.id(),
                     self.mock.file_issue.call_args[0][0].key.id())
    self.mock.emit.assert_called_once_with(
        events.IssueFilingEvent(
            testcase=self.testcase,
            issue_tracker_project='oss-fuzz',
            issue_id=None,
            issue_created=False))
