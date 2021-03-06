"""
Core objects for accessing platform API data.

"""
import base64
import cgi
from copy import deepcopy
import httplib
import logging
from posixpath import join
import simplejson as json
import urllib

from django.utils.encoding import StrAndUnicode
import remoteobjects
from remoteobjects.http import userAgent

from . import conf
from . import fields
from . import util
from .. import __version__



log = logging.getLogger('tcmui.core.api')



class Credentials(object):
    def __init__(self, userid, password):
        self.userid, self.password = userid, password


    def __repr__(self):
        return "<Credentials: %s>" % self.userid



admin = Credentials(conf.TCM_ADMIN_USER, conf.TCM_ADMIN_PASS)



class ObjectMixin(StrAndUnicode):
    api_base_url = conf.TCM_API_BASE


    def __init__(self, **kwargs):
        """
        Rather than updating __dict__ directly with **kwargs, go through the
        field descriptors for setting initial data.

        """
        super(ObjectMixin, self).__init__()
        self.auth = None
        for k, v in kwargs.items():
            setattr(self, k, v)

    def get_request(self, *args, **kwargs):
        """
        Add authorization and user-agent headers, request a JSON-formatted
        response, and prepend TCM_API_BASE to relative URL paths.

        """
        auth = kwargs.pop("auth", self.auth)

        request = super(ObjectMixin, self).get_request(*args, **kwargs)

        # Add API base URL to relative paths.
        if "://" not in request["uri"]:
            request["uri"] = join(self.api_base_url, request["uri"])

        # Request a JSON response.
        request["uri"] = util.add_to_querystring(request["uri"], _type="json")

        # Add Authorization header.
        if auth is not None:
            request["headers"]["authorization"] = (
                "Basic %s"
                % base64.encodestring(
                    "%s:%s" % (auth.userid, auth.password)
                    )[:-1]
                )

        # Add User-Agent header.
        request["headers"]["user-agent"] = "TCMui/%s" % __version__

        return request


    class Conflict(httplib.HTTPException):
        """An HTTPException thrown when the server reports that the requested
        action cannot be taken because it conflicts with the current state of
        the resource.

        This could be due to trying to update an out-of-date version of a
        resource, or because a resource can't be deleted because it is
        referenced by other resources, or because supplied data for a new
        resource violates unique constraints on that type of resource, or for
        other reasons. It's up to the catching code to determine the meaning of
        this exception, based on context and the error string from the server
        (which will be available as the ``response_error`` attribute).

        This exception corresponds to the HTTP status code 409.

        """
        pass


    @classmethod
    def exception_classes(cls):
        """
        Maps httplib exceptional status codes to Python exception classes.

        """
        return {
            httplib.CONFLICT: cls.Conflict,
            httplib.NOT_FOUND: cls.NotFound,
            httplib.UNAUTHORIZED: cls.Unauthorized,
            httplib.PRECONDITION_FAILED: cls.PreconditionFailed,
            httplib.FORBIDDEN: cls.Forbidden,
            httplib.BAD_REQUEST: cls.RequestError,
            httplib.INTERNAL_SERVER_ERROR: cls.ServerError,
            }


    @classmethod
    def raise_for_response(cls, url, response, content):
        """
        Raises exceptions corresponding to invalid HTTP responses that
        instances of this class can't be updated from.

        """
        # Turn exceptional httplib2 responses into exceptions.
        classname = cls.__name__
        exception_classes = cls.exception_classes()
        if response.status in exception_classes:
            exc_cls = exception_classes[response.status]

            # try to pull out an error
            content_type = cgi.parse_header(response.get("content-type", ""))[0]
            if content_type == "application/json":
                data = json.loads(content)
                # error format is e.g. {"errors":[{"error":"email.in.use"}]}
                # currently only one error is returned at a time
                error = data.get("errors", [{}])[0].get("error", "")
            else:
                error = ""

            exc = exc_cls(
                '%d %s requesting %s %s: %s'
                % (response.status, response.reason, classname, url, error)
                )
            exc.response_error = error
            raise exc

        try:
            response_has_content = cls.response_has_content[response.status]
        except KeyError:
            # we only expect the statuses that we know do or don't have content
            raise cls.BadResponse('Unexpected response requesting %s %s: %d %s'
                % (classname, url, response.status, response.reason))

        try:
            location_header = cls.location_headers[response.status]
        except KeyError:
            pass
        else:
            if cls.location_header_required.get(response.status) and location_header.lower() not in response:
                raise cls.BadResponse(
                    "%r header missing from %d %s response requesting %s %s"
                    % (location_header, response.status, response.reason,
                       classname, url))

        if not response_has_content:
            # then there's no content-type either, so we're done
            return

        # check that the response body was json
        content_type = response.get('content-type', '').split(';', 1)[0].strip()
        if content_type not in cls.content_types:
            raise cls.BadResponse(
                'Bad response fetching %s %s: content-type %s is not an expected type'
                % (classname, url, response.get('content-type')))


    def update_from_response(self, url, response, content):
        if isinstance(content, str):
            try:
                charset = cgi.parse_header(
                    response["content-type"])[1]["charset"]
            except KeyError:
                charset = "utf-8"
            content = content.decode(charset)
        return super(ObjectMixin, self).update_from_response(
            url, response, content)


    @classmethod
    def get(cls, url, **kwargs):
        obj = super(ObjectMixin, cls).get(url, **kwargs)
        obj.auth = kwargs.get("auth")
        return obj


    def _put(self, **kwargs):
        self._request("PUT", **kwargs)


    def _post(self, **kwargs):
        self._request("POST", **kwargs)


    def _delete(self, **kwargs):
        kwargs.setdefault("update_from_response", False)
        return self._request("DELETE", **kwargs)


    def _request(self, method, relative_url=None, full_payload=False,
                 version_payload=True, update_from_response=False,
                 extra_payload=None,
                 default_content_type="application/x-www-form-urlencoded",
                 **kw):
        """
        Swiss army knife utility method to make HTTP requests relative to this
        object.

        ``method``
            HTTP method to use.

        ``relative_url``
            URL relative to this object's location; if None, this object's
            location is used.

        ``full_payload``
            If True, send the full contents of this object's to_dict() as the
            payload. If set to another object, send the contents of that
            object's to_dict() instead.

        ``version_payload``
            If True, send this object's resource version as originalVersionId
            in payload. If set to another object, use that object's resource
            version instead. Mutually exclusive with full_payload.

        ``extra_payload``
            A dictionary of extra payload data to send.

        ``update_from_response``
            If True, update this object from the response. If set to another
            object, update that object instead.

        ``default_content_type``
            Send the body payload as this content type. Supported content types
            are "application/json" and "application/x-www-form-urlencoded".

        All other keyword arguments are passed along directly to get_request
        (possibly modified as dictated by the other arguments)

        """
        if getattr(self, "_location", None) is None:
            raise ValueError("Cannot %s %r with no URL" % (method, self))

        kw["method"] = method

        if "url" not in kw:
            if relative_url is not None:
                kw["url"] = join(self._location, relative_url)
            else:
                kw["url"] = self._location

        payload = {}
        if full_payload:
            if not hasattr(full_payload, "to_dict"):
                full_payload = self
            payload.update(full_payload.to_dict())
        elif version_payload:
            if not hasattr(version_payload, "identity"):
                version_payload = self
            payload.update({
                "originalVersionId": version_payload.identity["@version"]})
        if extra_payload:
            payload.update(extra_payload)

        headers = kw.setdefault("headers", {})
        headers.setdefault("content-type", default_content_type)

        if payload:
            if headers["content-type"] == "application/json":
                kw["body"] = json.dumps(payload)
            elif headers["content-type"] == "application/x-www-form-urlencoded":
                kw["body"] = urllib.urlencode(payload, doseq=True)
            else:
                raise ValueError("content type '%s' is not supported"
                                 % headers["content-type"])

        request = self.get_request(**kw)

        log.debug("Sending request %r", request)

        response, content = userAgent.request(**request)

        if update_from_response:
            log.debug("Got response %r, updating", response)

            if not hasattr(update_from_response, "update_from_response"):
                update_from_response = self

            update_from_response.update_from_response(None, response, content)
        else:
            log.debug("Got response %r, raising", response)
            self.raise_for_response(self._location, response, content)


    def post(self, obj, **kwargs):
        """Add another `RemoteObject` to this remote resource through an HTTP
        ``POST`` request.

        Parameter `obj` is a `RemoteObject` instance to save to this
        instance's resource. For example, this (`self`) may be a collection to
        which you want to post an asset (`obj`).

        """
        self._request(
            "POST",
            full_payload=obj,
            update_from_response=obj,
            **kwargs)


    def put(self, **kwargs):
        """Save a previously requested `RemoteObject` back to its remote
        resource through an HTTP ``PUT`` request.

        Optional `http` parameter is the user agent object to use. `http`
        objects should be compatible with `httplib2.Http` objects.

        """
        self._put(full_payload=True, update_from_response=True, **kwargs)


    def delete(self, **kwargs):
        """Delete the remote resource represented by the `RemoteObject`
        instance through an HTTP ``DELETE`` request.

        Optional parameter `http` is the user agent object to use. `http`
        objects should be compatible with `httplib2.Http` objects.

        """
        self._delete()

        # No more resource.
        self._location = None
        self.identity = None


    def to_dict(self):
        """Encodes the DataObject to a dictionary."""
        data = {}
        for field_name, field in self.fields.iteritems():
            data.update(field.submit_data(self))
        return data


    def filter(self, **kwargs):
        """
        Returns a new instance with filter parameters added as parameters to
        the instance's query string.

        """
        auth = kwargs.pop("auth", self.auth)

        newurl = util.add_to_querystring(self._location, **kwargs)

        return self.get(newurl, auth=auth)


    def __repr__(self):
        return "<%s: %s>" % (self.__class__.__name__, self)



class RemoteObject(ObjectMixin, remoteobjects.RemoteObject):
    identity = fields.ResourceIdentity()


    @property
    def api_name(self):
        return self.__class__.__name__.lower()


    def _set_location(self, val):
        self._location_override = val


    def _get_location(self):
        if self._location_override:
            return self._location_override
        # Avoid infinite loopage; take care to not trigger delivery
        try:
            if self._delivered and "@url" in self.identity:
                return self.identity["@url"]
        except TypeError:
            pass
        return None


    _location = property(_get_location, _set_location)


    def update_from_response(self, url, response, content):
        super(RemoteObject, self).update_from_response(url, response, content)
        # If updated from a response, we should have a resourceIdentity.url;
        # use that canonical URL rather than any previously-set URL.
        self._location_override = None

    def update_from_dict(self, data):
        """
        Unwrap the JSON data.

        We expect to get data in a form like this:

        {
           "ns1.user":[
              {
                 "ns1.screenName":"userName1",
                 "ns1.userStatusId":1
                 ... more user data ...
              }
           ]
        }

        We pass on just the inner-most dictionary.

        In order to also support data passed in from ListObject instances,
        the unwrapping is optional; if we get just a straight data dictionary,
        we'll pass that on untouched.

        """
        wrapper_key = "ns1.%s" % self.api_name
        if wrapper_key in data:
            data = data[wrapper_key][0]
        return super(RemoteObject, self).update_from_dict(data)



class ListObject(ObjectMixin, remoteobjects.ListObject):
    def update_from_dict(self, data):
        """
        Unwrap the JSON data.

        We expect to get data in a form like this:

        {
           "ns1.searchResult":[
              {
                 "@xsi.type":"ns1:searchResult",
                 "ns1.companies":{
                    "ns1.company":[
                       {
                          "@xsi.type":"ns1:company",
                          ... company data ...
                       },
                       {
                          "@xsi.type":"ns1:company",
                          ... company data ...
                       }
                    ]
                 },
                 "ns1.totalResults":2
              }
           ]
        }

        We pass on the inner list of data dictionaries.

        """
        outer_key = None
        for candidate_key in [
            "ns1.ArrayOf%s" % self.entryclass.__name__,
            "ns1.searchResult"
            ]:
            if candidate_key in data:
                outer_key = candidate_key
                break
        if outer_key is not None:
            data = data[outer_key][0]
            if outer_key == "ns1.searchResult":
                data = data["ns1.%s" % self.api_name]
            try:
                data = data["ns1.%s" % self.entryclass().api_name]
            except (KeyError, TypeError):
                data = []
            # Because this JSON is BadgerFish-translated XML
            # (http://ajaxian.com/archives/badgerfish-translating-xml-to-json)
            # length-1 lists are not sent as lists, so we re-listify.
            if not isinstance(data, list):
                data = [data]
        return super(ListObject, self).update_from_dict(data)


    @classmethod
    def get(cls, url=None, **kwargs):
        if url is None:
            try:
                url = cls.default_url
            except AttributeError:
                raise ValueError("%s has no default URL; .get() requires url."
                                 % cls)
        obj = super(ListObject, cls).get(url, **kwargs)
        obj.auth = kwargs.get("auth", None)
        return obj


    def __getitem__(self, *args, **kwargs):
        obj = super(ListObject, self).__getitem__(*args, **kwargs)
        obj.auth = self.auth
        return obj


    def __iter__(self, *args, **kwargs):
        for obj in super(ListObject, self).__iter__(*args, **kwargs):
            obj.auth = self.auth
            yield obj
