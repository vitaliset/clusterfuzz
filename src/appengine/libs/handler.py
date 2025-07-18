# Copyright 2020 Google LLC
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
"""handler.py provides decorators for POST and GET handlers."""

import datetime
import functools
import json
import re

from flask import g
from flask import make_response
from flask import request
import google.auth
import requests

from clusterfuzz._internal.base import utils
from clusterfuzz._internal.config import local_config
from clusterfuzz._internal.datastore import data_types
from clusterfuzz._internal.google_cloud_utils import pubsub
from clusterfuzz._internal.metrics import monitor
from clusterfuzz._internal.system import environment
from libs import access
from libs import auth
from libs import csp
from libs import helpers

HTTP_GET_TIMEOUT_SECS = 30

JSON = 'json'
FORM = 'form'
HTML = 'html'
TEXT = 'text'

CLUSTERFUZZ_AUTHORIZATION_HEADER = 'x-clusterfuzz-authorization'
CLUSTERFUZZ_AUTHORIZATION_IDENTITY = 'x-clusterfuzz-identity'
BEARER_PREFIX = 'Bearer '

_auth_config_obj = None


def _auth_config():
  """Return a config with auth root."""
  global _auth_config_obj
  if not _auth_config_obj:
    _auth_config_obj = local_config.AuthConfig()

  return _auth_config_obj


def extend_request(req, params):
  """Extends a request."""

  def _iterparams():
    yield from params.items()

  def _get(key, default_value=None):
    """Return the value of the key or the default value."""
    return params.get(key, default_value)

  req.get = _get
  req.iterparams = _iterparams


def extend_json_request(req):
  """Extends a request to support JSON."""
  try:
    params = json.loads(req.data)
  except ValueError as e:
    raise helpers.EarlyExitError(
        'Parsing the JSON request body failed: %s' % req.data, 400) from e

  extend_request(req, params)


def cron():
  """Wrap a handler with cron."""

  def decorator(func):
    """Decorator."""

    @functools.wraps(func)
    def wrapper(self):
      """Wrapper."""
      if not self.is_cron():
        raise helpers.AccessDeniedError('You are not a cron.')

      # Add env vars used by logs context for cleanup/triage.
      environment.set_task_id_vars(self.__module__)

      with monitor.wrap_with_monitoring():
        result = func(self)
        if result is None:
          return 'OK'

        return result

    return wrapper

  return decorator


def check_admin_access(func):
  """Wrap a handler with admin checking.

  This decorator must be below post(..) and get(..) when used.
  """

  @functools.wraps(func)
  def wrapper(self):
    """Wrapper."""
    if not auth.is_current_user_admin():
      raise helpers.AccessDeniedError('Admin access is required.')

    return func(self)

  return wrapper


def check_admin_access_if_oss_fuzz(func):
  """Wrap a handler with an admin check if this is OSS-Fuzz.

  This decorator must be below post(..) and get(..) when used.
  """

  @functools.wraps(func)
  def wrapper(self):
    """Wrapper."""
    if utils.is_oss_fuzz():
      return check_admin_access(func)(self)

    return func(self)

  return wrapper


def unsupported_on_local_server(func):
  """Wrap a handler to raise error when running in local App Engine
  development environment.

  This decorator must be below post(..) and get(..) when used.
  """

  @functools.wraps(func)
  def wrapper(self, *args, **kwargs):
    """Wrapper."""
    if environment.is_running_on_app_engine_development():
      raise helpers.EarlyExitError(
          'This feature is not available in local App Engine Development '
          'environment.', 400)

    return func(self, *args, **kwargs)

  return wrapper


def validate_id_token(access_token):
  """Validates a JWT as an id token."""
  response_id_token = requests.get(
      'https://www.googleapis.com/oauth2/v3/tokeninfo',
      params={'id_token': access_token},
      timeout=HTTP_GET_TIMEOUT_SECS)

  if response_id_token.status_code == 200:
    return response_id_token

  return None


def validate_access_token(access_token):
  """Validates a JWT as an access token."""
  response_access_token = requests.get(
      'https://www.googleapis.com/oauth2/v3/tokeninfo',
      params={'access_token': access_token},
      timeout=HTTP_GET_TIMEOUT_SECS)

  if response_access_token.status_code == 200:
    return response_access_token

  return None


def validate_token(authorization):
  """Validates a JWT as either an access or id token, or raises."""
  access_token = authorization.split(' ')[1]
  id_token_response = validate_id_token(access_token)
  if id_token_response is not None:
    return id_token_response

  access_token_response = validate_access_token(access_token)
  if access_token_response is not None:
    return access_token_response

  raise helpers.UnauthorizedError(
      f'Failed to authorize. The Authorization header ({authorization}) '
      'is neither a valid id or access token.')


def get_email_and_access_token(authorization):
  """Get user email from the request.

    See: https://developers.google.com/identity/protocols/OAuth2InstalledApp
  """
  if not authorization.startswith(BEARER_PREFIX):
    raise helpers.UnauthorizedError(
        'The Authorization header is invalid. It should have been started with'
        " '%s'." % BEARER_PREFIX)

  response = validate_token(authorization)

  try:
    data = json.loads(response.text)

    # Whitelist service accounts. They have different client IDs (or aud).
    # Therefore, we check against their email directly.
    if data.get('email_verified') and data.get('email') in _auth_config().get(
        'whitelisted_oauth_emails', default=[]):
      return data['email'], authorization

    # Validate that this is an explicitly whitelisted client ID.
    whitelisted_client_ids = _auth_config().get(
        'whitelisted_oauth_client_ids', default=[])
    if data.get('aud') not in whitelisted_client_ids:
      raise helpers.UnauthorizedError(
          "The access token doesn't belong to one of the allowed OAuth clients"
          ': %s.' % response.text)

    if not data.get('email_verified'):
      raise helpers.UnauthorizedError('The email (%s) is not verified: %s.' %
                                      (data.get('email'), response.text))

    return data['email'], authorization
  except (KeyError, ValueError) as e:
    raise helpers.EarlyExitError(
        'Parsing the JSON response body failed: %s' % response.text, 500) from e


def oauth(func):
  """Wrap a handler with OAuth authentication by reading the Authorization
    header and getting user email.
  """

  @functools.wraps(func)
  def wrapper(self, *args, **kwargs):
    """Wrapper."""
    auth_header = request.headers.get('Authorization')
    if auth_header:
      email, returned_auth_header = get_email_and_access_token(auth_header)
      setattr(g, '_oauth_email', email)

      response = make_response(func(self, *args, **kwargs))
      response.headers[CLUSTERFUZZ_AUTHORIZATION_HEADER] = str(
          returned_auth_header)
      response.headers[CLUSTERFUZZ_AUTHORIZATION_IDENTITY] = str(email)
      return response

    return func(self, *args, **kwargs)

  return wrapper


def pubsub_push(func):
  """Wrap a handler with pubsub push authentication."""

  @functools.wraps(func)
  def wrapper(self):
    """Wrapper."""
    try:
      email = auth.get_email_from_bearer_token(request)
    except google.auth.exceptions.GoogleAuthError as e:
      raise helpers.UnauthorizedError('Invalid ID token.') from e

    if (not email or email != utils.service_account_email()):
      raise helpers.UnauthorizedError('Invalid ID token.')

    message = pubsub.raw_message_to_message(json.loads(request.data.decode()))
    return func(self, message)

  return wrapper


def check_user_access(need_privileged_access):
  """Wrap a handler with check_user_access.

  This decorator must be below post(..) and get(..) when used.
  """

  def decorator(func):
    """Decorator."""

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
      """Wrapper."""
      if not access.has_access(need_privileged_access=need_privileged_access):
        raise helpers.AccessDeniedError()

      return func(self, *args, **kwargs)

    return wrapper

  return decorator


def check_testcase_access(func):
  """Wrap a handler with check_testcase_access.

  It expects the param
    `testcaseId`. And it expects func to have testcase as its first argument.

  This decorator must be below post(..) and get(..) when used.
  """

  @functools.wraps(func)
  def wrapper(self):
    """Wrapper."""
    testcase_id = helpers.cast(
        request.get('testcaseId'), int,
        "The param 'testcaseId' is not a number.")

    testcase = access.check_access_and_get_testcase(testcase_id)
    return func(self, testcase)

  return wrapper


def allowed_cors(func):
  """Wrap a handler with 'Access-Control-Allow-Origin to allow cross-domain
  AJAX calls."""

  @functools.wraps(func)
  def wrapper(self):
    """Wrapper."""
    origin = request.headers.get('Origin')
    whitelisted_cors_urls = _auth_config().get('whitelisted_cors_urls')
    response = make_response(func(self))

    if origin and whitelisted_cors_urls:
      for domain_regex in whitelisted_cors_urls:
        if re.match(domain_regex, origin):
          response.headers['Access-Control-Allow-Origin'] = origin
          response.headers['Vary'] = 'Origin'
          response.headers['Access-Control-Allow-Credentials'] = 'true'
          response.headers['Access-Control-Allow-Methods'] = (
              'GET,OPTIONS,POST')
          response.headers['Access-Control-Allow-Headers'] = (
              'Accept,Authorization,Content-Type')
          response.headers['Access-Control-Max-Age'] = '3600'
          break

    return response

  return wrapper


def post(request_content_type, response_content_type):
  """Wrap a POST handler, parse request, and set response's content type."""

  def decorator(func):
    """Decorator."""

    @functools.wraps(func)
    def wrapper(self):
      """Wrapper."""
      if response_content_type == JSON:
        self.is_json = True

      if request_content_type == JSON:
        extend_json_request(request)
      elif request_content_type == FORM:
        extend_request(request, request.form)
      else:
        extend_request(request, request.args)

      response = make_response(func(self))
      if response_content_type == JSON:
        response.headers['Content-Type'] = 'application/json'
      elif response_content_type == TEXT:
        response.headers['Content-Type'] = 'text/plain'
      elif response_content_type == HTML:
        # Don't enforce content security policies in local development mode.
        if not environment.is_running_on_app_engine_development():
          response.headers['Content-Security-Policy'] = csp.get_default()

      return response

    return wrapper

  return decorator


def get(response_content_type):
  """Wrap a GET handler and set response's content type."""

  def decorator(func):
    """Decorator."""

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
      """Wrapper."""
      if response_content_type == JSON:
        self.is_json = True

      extend_request(request, request.args)
      response = make_response(func(self, *args, **kwargs))
      if response_content_type == JSON:
        response.headers['Content-Type'] = 'application/json'
      elif response_content_type == TEXT:
        response.headers['Content-Type'] = 'text/plain'
      elif response_content_type == HTML:
        # Don't enforce content security policies in local development mode.
        if not environment.is_running_on_app_engine_development():
          response.headers['Content-Security-Policy'] = csp.get_default()

      return response

    return wrapper

  return decorator


def require_csrf_token(func):
  """Wrap a handler to require a valid CSRF token."""

  def wrapper(self, *args, **kwargs):
    """Check to see if this handler has a valid CSRF token provided to it."""
    token_value = request.get('csrf_token')
    user = auth.get_current_user()
    if not user:
      raise helpers.AccessDeniedError('Not logged in.')

    query = data_types.CSRFToken.query(
        data_types.CSRFToken.value == token_value,
        data_types.CSRFToken.user_email == user.email)
    token = query.get()
    if not token:
      raise helpers.AccessDeniedError('Invalid CSRF token.')

    # Make sure that the token is not expired.
    if token.expiration_time < datetime.datetime.utcnow():
      token.key.delete()
      raise helpers.AccessDeniedError('Expired CSRF token.')

    return func(self, *args, **kwargs)

  return wrapper
