#
# This file is part of pretix (Community Edition).
#
# Copyright (C) 2014-2020 Raphael Michel and contributors
# Copyright (C) 2020-2021 rami.io GmbH and contributors
#
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
# Public License as published by the Free Software Foundation in version 3 of the License.
#
# ADDITIONAL TERMS APPLY: Pursuant to Section 7 of the GNU Affero General Public License, additional terms are
# applicable granting you additional permissions and placing additional restrictions on your usage of this software.
# Please refer to the pretix LICENSE file to obtain the full terms applicable to this work. If you did not receive
# this file, see <https://pretix.eu/about/en/license>.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
# warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License along with this program.  If not, see
# <https://www.gnu.org/licenses/>.
#
from django.http import (
    HttpResponseForbidden, HttpResponseNotFound, HttpResponseServerError,
)
from django.middleware.csrf import REASON_NO_CSRF_COOKIE, REASON_NO_REFERER
from django.template import TemplateDoesNotExist, loader
from django.template.loader import get_template
from django.utils.functional import Promise
from django.utils.translation import gettext as _
from django.views.decorators.csrf import requires_csrf_token
from sentry_sdk import last_event_id

from pretix.base.i18n import language
from pretix.base.middleware import get_language_from_request


def csrf_failure(request, reason=""):
    try:
        locale = get_language_from_request(request)
    except:
        locale = "en"
    with language(locale):  # Middleware might not have run, need to do this manually
        t = get_template('csrffail.html')
        c = {
            'reason': reason,
            'no_referer': reason == REASON_NO_REFERER,
            'no_referer1': _(
                "You are seeing this message because this HTTPS site requires a "
                "'Referer header' to be sent by your Web browser, but none was "
                "sent. This header is required for security reasons, to ensure "
                "that your browser is not being hijacked by third parties."),
            'no_referer2': _(
                "If you have configured your browser to disable 'Referer' headers, "
                "please re-enable them, at least for this site, or for HTTPS "
                "connections, or for 'same-origin' requests."),
            'no_cookie': reason == REASON_NO_CSRF_COOKIE,
            'no_cookie1': _(
                "You are seeing this message because this site requires a CSRF "
                "cookie when submitting forms. This cookie is required for "
                "security reasons, to ensure that your browser is not being "
                "hijacked by third parties."),
            'no_cookie2': _(
                "If you have configured your browser to disable cookies, please "
                "re-enable them, at least for this site, or for 'same-origin' "
                "requests."),
        }
        return HttpResponseForbidden(t.render(c), content_type='text/html')


@requires_csrf_token
def page_not_found(request, exception):
    try:
        locale = get_language_from_request(request)
    except:
        locale = "en"
    with language(locale):  # Middleware might not have run, need to do this manually
        exception_repr = exception.__class__.__name__
        # Try to get an "interesting" exception message, if any (and not the ugly
        # Resolver404 dictionary)
        try:
            message = exception.args[0]
        except (AttributeError, IndexError):
            pass
        else:
            if isinstance(message, (str, Promise)):
                exception_repr = str(message)
        context = {
            'request_path': request.path,
            'exception': exception_repr,
        }
        template = get_template('404.html')
        body = template.render(context, request)
        r = HttpResponseNotFound(body)
        r.xframe_options_exempt = True
        return r


@requires_csrf_token
def server_error(request):
    try:
        locale = get_language_from_request(request)
    except:
        locale = "en"
    with language(locale):  # Middleware might not have run, need to do this manually
        try:
            template = loader.get_template('500.html')
        except TemplateDoesNotExist:
            return HttpResponseServerError('<h1>Server Error (500)</h1>', content_type='text/html')
        r = HttpResponseServerError(template.render({
            'request': request,
            'sentry_event_id': last_event_id(),
        }))
        r.xframe_options_exempt = True
        return r
