from __future__ import absolute_import, print_function, unicode_literals

import collections
import hashlib
import logging
import os
import platform
import xml.etree.ElementTree as ET

import requests
from pkg_resources import parse_version as pv
from six import raise_from
from six.moves.urllib.parse import quote
from tqdm import tqdm
from tqdm.utils import CallbackIOWrapper

from . import errors
from .__about__ import __version__
# For backwards compatibility
from .errors import *
from .mixins.channels import ChannelsMixin
from .mixins.organizations import OrgMixin
from .mixins.package import PackageMixin
from .requests_ext import NullAuth
from .utils import compute_hash, jencode
from .utils.http_codes import STATUS_CODES

logger = logging.getLogger('binstar')


class Binstar(OrgMixin, ChannelsMixin, PackageMixin):
    """
    An object that represents interfaces with the Anaconda repository restful API.

    :param token: a token generated by Binstar.authenticate or None for
                  an anonymous user.
    """

    def __init__(self, token=None, domain='https://api.anaconda.org', verify=True, **kwargs):
        self._session = requests.Session()
        self._session.headers['x-binstar-api-version'] = __version__
        self.session.verify = verify
        self.session.auth = NullAuth()
        self.token = token
        self._token_warning_sent = False

        user_agent = 'Anaconda-Client/{} (+https://anaconda.org)'.format(__version__)
        self._session.headers.update({
            'User-Agent': user_agent,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        })

        if token:
            self._session.headers.update({'Authorization': 'token {}'.format(token)})

        if domain.endswith('/'):
            domain = domain[:-1]
        if not domain.startswith(('http://', 'https://')):
            domain = 'https://' + domain
        self.domain = domain

    @property
    def session(self):
        return self._session

    def check_server(self):
        """
        Checks if the server is reachable and throws
        and exception if it isn't
        """
        msg = 'API server not found. Please check your API url configuration.'

        try:
            response = self.session.head(self.domain)
        except Exception as e:
            raise_from(errors.ServerError(msg), e)

        try:
            self._check_response(response)
        except errors.NotFound as e:
            raise raise_from(errors.ServerError(msg), e)

    def authentication_type(self):
        url = '%s/authentication-type' % self.domain
        res = self.session.get(url)
        try:
            self._check_response(res)
            res = res.json()
            return res['authentication_type']
        except BinstarError:
            return 'password'

    def krb_authenticate(self, *args, **kwargs):
        try:
            from requests_kerberos import HTTPKerberosAuth
            return self._authenticate(HTTPKerberosAuth(), *args, **kwargs)
        except ImportError:
            raise BinstarError(
                'Kerberos authentication requires the requests-kerberos '
                'package to be installed:\n'
                '    conda install requests-kerberos\n'
                'or: \n'
                '    pip install requests-kerberos'
            )

    def authenticate(self, username, password, *args, **kwargs):
        return self._authenticate((username, password), *args, **kwargs)

    def _authenticate(self,
                      auth,
                      application,
                      application_url=None,
                      for_user=None,
                      scopes=None,
                      created_with=None,
                      max_age=None,
                      strength='strong',
                      fail_if_already_exists=False,
                      hostname=platform.node()):
        '''
        Use basic authentication to create an authentication token using the interface below.
        With this technique, a username and password need not be stored permanently, and the user can
        revoke access at any time.

        :param username: The users name
        :param password: The users password
        :param application: The application that is requesting access
        :param application_url: The application's home page
        :param scopes: Scopes let you specify exactly what type of access you need. Scopes limit access for the tokens.
        '''

        url = '%s/authentications' % (self.domain)
        payload = {"scopes": scopes, "note": application, "note_url": application_url,
                   'hostname': hostname,
                   'user': for_user,
                   'max-age': max_age,
                   'created_with': None,
                   'strength': strength,
                   'fail-if-exists': fail_if_already_exists}

        data, headers = jencode(payload)
        res = self.session.post(url, auth=auth, data=data, headers=headers)
        self._check_response(res)
        res = res.json()
        token = res['token']
        self.session.headers.update({'Authorization': 'token %s' % (token)})
        return token

    def list_scopes(self):
        url = '%s/scopes' % (self.domain)
        res = requests.get(url)
        self._check_response(res)
        return res.json()

    def authentication(self):
        '''
        Retrieve information on the current authentication token
        '''
        url = '%s/authentication' % (self.domain)
        res = self.session.get(url)
        self._check_response(res)
        return res.json()

    def authentications(self):
        '''
        Get a list of the current authentication tokens
        '''

        url = '%s/authentications' % (self.domain)
        res = self.session.get(url)
        self._check_response(res)
        return res.json()

    def remove_authentication(self, auth_name=None, organization=None):
        """
        Remove the current authentication or the one given by `auth_name`
        """
        if auth_name:
            if organization:
                url = '%s/authentications/org/%s/name/%s' % (self.domain, organization, auth_name)
            else:
                url = '%s/authentications/name/%s' % (self.domain, auth_name)
        else:
            url = '%s/authentications' % (self.domain,)

        res = self.session.delete(url)
        self._check_response(res, [201])

    def _check_response(self, res, allowed=[200]):
        api_version = res.headers.get('x-binstar-api-version', '0.2.1')
        if pv(api_version) > pv(__version__):
            logger.warning('The api server is running the binstar-api version %s. you are using %s\nPlease update your '
                           'client with pip install -U binstar or conda update binstar' % (api_version, __version__))

        if not self._token_warning_sent and 'Conda-Token-Warning' in res.headers:
            logger.warning('Token warning: {}'.format(res.headers['Conda-Token-Warning']))
            self._token_warning_sent = True

        if 'X-Anaconda-Lockdown' in res.headers:
            logger.warning('Anaconda repository is currently in LOCKDOWN mode.')

        if 'X-Anaconda-Read-Only' in res.headers:
            logger.warning('Anaconda repository is currently in READ ONLY mode.')

        if not res.status_code in allowed:
            short, long = STATUS_CODES.get(res.status_code, ('?', 'Undefined error'))
            msg = '%s: %s ([%s] %s -> %s)' % (short, long, res.request.method, res.request.url, res.status_code)

            try:
                data = res.json()
            except:
                pass
            else:
                msg = data.get('error', msg)

            ErrCls = errors.BinstarError
            if res.status_code == 401:
                ErrCls = errors.Unauthorized
            elif res.status_code == 404:
                ErrCls = errors.NotFound
            elif res.status_code == 409:
                ErrCls = errors.Conflict
            elif res.status_code >= 500:
                ErrCls = errors.ServerError

            raise ErrCls(msg, res.status_code)

    def user(self, login=None):
        '''
        Get user information.

        :param login: (optional) the login name of the user or None. If login is None
                      this method will return the information of the authenticated user.
        '''
        if login:
            url = '%s/user/%s' % (self.domain, login)
        else:
            url = '%s/user' % (self.domain)

        res = self.session.get(url, verify=self.session.verify)
        self._check_response(res)

        return res.json()

    def user_packages(
            self,
            login=None,
            platform=None,
            package_type=None,
            type_=None,
            access=None):
        '''
        Returns a list of packages for a given user and optionally filter
        by `platform`, `package_type` and `type_`.

        :param login: (optional) the login name of the user or None. If login
                      is None this method will return the packages for the
                      authenticated user.
        :param platform: only find packages that include files for this platform.
           (e.g. 'linux-64', 'osx-64', 'win-32')
        :param package_type: only find packages that have this kind of file
           (e.g. 'env', 'conda', 'pypi')
        :param type_: only find packages that have this conda `type`
           (i.e. 'app')
        :param access: only find packages that have this access level
           (e.g. 'private', 'authenticated', 'public')
        '''
        if login:
            url = '{0}/packages/{1}'.format(self.domain, login)
        else:
            url = '{0}/packages'.format(self.domain)

        arguments = collections.OrderedDict()

        if platform:
            arguments['platform'] = platform
        if package_type:
            arguments['package_type'] = package_type
        if type_:
            arguments['type'] = type_
        if access:
            arguments['access'] = access

        res = self.session.get(url, params=arguments)
        self._check_response(res)

        return res.json()

    def package(self, login, package_name):
        '''
        Get information about a specific package

        :param login: the login of the package owner
        :param package_name: the name of the package
        '''
        url = '%s/package/%s/%s' % (self.domain, login, package_name)
        res = self.session.get(url)
        self._check_response(res)
        return res.json()

    def package_add_collaborator(self, owner, package_name, collaborator):
        url = '%s/packages/%s/%s/collaborators/%s' % (self.domain, owner, package_name, collaborator)
        res = self.session.put(url)
        self._check_response(res, [201])
        return

    def package_remove_collaborator(self, owner, package_name, collaborator):
        url = '%s/packages/%s/%s/collaborators/%s' % (self.domain, owner, package_name, collaborator)
        res = self.session.delete(url)
        self._check_response(res, [201])
        return

    def package_collaborators(self, owner, package_name):

        url = '%s/packages/%s/%s/collaborators' % (self.domain, owner, package_name)
        res = self.session.get(url)
        self._check_response(res, [200])
        return res.json()

    def all_packages(self, modified_after=None):
        '''
        '''
        url = '%s/package_listing' % (self.domain)
        data = {'modified_after': modified_after or ''}
        res = self.session.get(url, data=data)
        self._check_response(res)
        return res.json()

    def add_package(
            self,
            login,
            package_name,
            summary=None,
            license=None,
            public=True,
            license_url=None,
            license_family=None,
            attrs=None,
            package_type=None,
    ):
        """
        Add a new package to a users account

        :param login: the login of the package owner
        :param package_name: the name of the package to be created
        :param package_type: A type identifier for the package (eg. 'pypi' or 'conda', etc.)
        :param summary: A short summary about the package
        :param license: the name of the package license
        :param license_url: the url of the package license
        :param public: if true then the package will be hosted publicly
        :param attrs: A dictionary of extra attributes for this package
        """
        if package_type is not None:
            package_type = package_type.value

        url = '%s/package/%s/%s' % (self.domain, login, package_name)

        attrs = attrs or {}
        attrs['summary'] = summary
        attrs['package_types'] = [package_type]
        attrs['license'] = {
            'name': license,
            'url': license_url,
            'family': license_family,
        }

        payload = dict(public=bool(public),
                       publish=False,
                       public_attrs=dict(attrs or {})
                       )

        data, headers = jencode(payload)
        res = self.session.post(url, data=data, headers=headers)
        self._check_response(res)
        return res.json()

    def update_package(self, login, package_name, attrs):
        """
        Update public_attrs of the package on a users account

        :param login: the login of the package owner
        :param package_name: the name of the package to be updated
        :param attrs: A dictionary of attributes to update
        """
        url = '{}/package/{}/{}'.format(self.domain, login, package_name)

        payload = dict(public_attrs=dict(attrs))
        data, headers = jencode(payload)
        res = self.session.patch(url, data=data, headers=headers)
        self._check_response(res)
        return res.json()

    def update_release(self, login, package_name, version, attrs):
        """
        Update release public_attrs of the package on a users account

        :param login: the login of the package owner
        :param package_name: the name of the package to be updated
        :param version: version of the package to update
        :param attrs: A dictionary of attributes to update
        """
        url = '{}/release/{}/{}/{}'.format(self.domain, login, package_name, version)
        payload = dict(public_attrs=dict(attrs))
        data, headers = jencode(payload)
        res = self.session.patch(url, data=data, headers=headers)
        self._check_response(res)
        return res.json()

    def remove_package(self, username, package_name):

        url = '%s/package/%s/%s' % (self.domain, username, package_name)

        res = self.session.delete(url)
        self._check_response(res, [201])
        return

    def release(self, login, package_name, version):
        '''
        Get information about a specific release

        :param login: the login of the package owner
        :param package_name: the name of the package
        :param version: the name of the package
        '''
        url = '%s/release/%s/%s/%s' % (self.domain, login, package_name, version)
        res = self.session.get(url)
        self._check_response(res)
        return res.json()

    def remove_release(self, username, package_name, version):
        '''
        remove a release and all files under it

        :param username: the login of the package owner
        :param package_name: the name of the package
        :param version: the name of the package
        '''
        url = '%s/release/%s/%s/%s' % (self.domain, username, package_name, version)
        res = self.session.delete(url)
        self._check_response(res, [201])
        return

    def add_release(self, login, package_name, version, requirements, announce, release_attrs):
        '''
        Add a new release to a package.

        :param login: the login of the package owner
        :param package_name: the name of the package
        :param version: the version string of the release
        :param requirements: A dict of requirements TODO: describe
        :param announce: An announcement that will be posted to all package watchers
        '''

        url = '%s/release/%s/%s/%s' % (self.domain, login, package_name, version)

        if not release_attrs:
            release_attrs = {}

        payload = {
            'requirements': requirements,
            'announce': announce,
            'description': None,  # Will be updated with the one on release_attrs
        }
        payload.update(release_attrs)

        data, headers = jencode(payload)
        res = self.session.post(url, data=data, headers=headers)
        self._check_response(res)
        return res.json()

    def distribution(self, login, package_name, release, basename=None):

        url = '%s/dist/%s/%s/%s/%s' % (self.domain, login, package_name, release, basename)

        res = self.session.get(url)
        self._check_response(res)
        return res.json()

    def remove_dist(self, login, package_name, release, basename=None, _id=None):

        if basename:
            url = '%s/dist/%s/%s/%s/%s' % (self.domain, login, package_name, release, basename)
        elif _id:
            url = '%s/dist/%s/%s/%s/-/%s' % (self.domain, login, package_name, release, _id)
        else:
            raise TypeError("method remove_dist expects either 'basename' or '_id' arguments")

        res = self.session.delete(url)
        self._check_response(res)
        return res.json()

    def download(self, login, package_name, release, basename, md5=None):
        """
        Download a package distribution

        :param login: the login of the package owner
        :param package_name: the name of the package
        :param version: the version string of the release
        :param basename: the basename of the distribution to download
        :param md5: (optional) an md5 hash of the download if given and the package has not changed
                    None will be returned

        :returns: a file like object or None
        """

        url = '%s/download/%s/%s/%s/%s' % (self.domain, login, package_name, release, basename)
        if md5:
            headers = {'ETag': md5, }
        else:
            headers = {}

        res = self.session.get(url, headers=headers, allow_redirects=False)
        self._check_response(res, allowed=[200, 302, 304])

        if res.status_code == 200:
            # We received the content directly from anaconda.org
            return res
        elif res.status_code == 304:
            # The content has not changed
            return None
        elif res.status_code == 302:
            # Download from s3:
            # We need to create a new request (without using session) to avoid
            # sending the custom headers set on our session to S3 (which causes
            # a failure).
            res2 = requests.get(res.headers['location'], stream=True)
            return res2

    def upload(self, login, package_name, release, basename, fd, distribution_type,
               description='', md5=None, sha256=None, size=None, dependencies=None, attrs=None,
               channels=('main',)):
        """
        Upload a new distribution to a package release.

        :param login: the login of the package owner
        :param package_name: the name of the package
        :param release: the version string of the release
        :param basename: the basename of the distribution to download
        :param fd: a file like object to upload
        :param distribution_type: pypi or conda or ipynb, etc.
        :param description: (optional) a short description about the file
        :param md5: (optional) base64 encoded md5 hash calculated from package file
        :param sha256: (optional) base64 encoded sha256 hash calculated from package file
        :param size: (optional) size of package file in bytes
        :param dependencies: (optional) list package dependencies
        :param attrs: any extra attributes about the file (eg. build=1, pyversion='2.7', os='osx')
        :param channels: list of labels package will be available from
        """
        url = '%s/stage/%s/%s/%s/%s' % (self.domain, login, package_name, release, quote(basename))
        if attrs is None:
            attrs = {}
        if not isinstance(attrs, dict):
            raise TypeError('argument attrs must be a dictionary')

        sha256 = sha256 if sha256 is not None else compute_hash(fd, size=size, hash_algorithm=hashlib.sha256)[0]

        if not isinstance(distribution_type, str):
            distribution_type = distribution_type.value

        payload = dict(
            distribution_type=distribution_type,
            description=description,
            attrs=attrs,
            dependencies=dependencies,
            channels=channels,
            sha256=sha256
        )

        data, headers = jencode(payload)
        res = self.session.post(url, data=data, headers=headers)
        self._check_response(res)
        obj = res.json()

        s3url = obj['post_url']
        s3data = obj['form_data']

        if md5 is None:
            _hexmd5, b64md5, size = compute_hash(fd, size=size)
        elif size is None:
            spos = fd.tell()
            fd.seek(0, os.SEEK_END)
            size = fd.tell() - spos
            fd.seek(spos)

        s3data['Content-Length'] = size
        s3data['Content-MD5'] = b64md5

        request_method = self.session if s3url.startswith(self.domain) else requests

        file_size = os.fstat(fd.fileno()).st_size
        with tqdm(total=file_size, unit="B", unit_scale=True, unit_divisor=1024) as t:
            wrapped_file = CallbackIOWrapper(t.update, fd, "read")
            s3res = request_method.post(
                s3url, data=s3data, files={'file': (basename, wrapped_file)},
                verify=self.session.verify, timeout=10 * 60 * 60)

        if s3res.status_code != 201:
            logger.info(s3res.text)
            xml_error = ET.fromstring(s3res.text)
            msg_tail = ''
            if xml_error.find('Code').text == 'InvalidDigest':
                msg_tail = ' The Content-MD5 or checksum value is not valid.'
            raise errors.BinstarError('Error uploading package!%s' % msg_tail, s3res.status_code)

        url = '%s/commit/%s/%s/%s/%s' % (self.domain, login, package_name, release, quote(basename))
        payload = dict(dist_id=obj['dist_id'])
        data, headers = jencode(payload)
        res = self.session.post(url, data=data, headers=headers)
        self._check_response(res)

        return res.json()

    def search(self, query, package_type=None, platform=None):
        if package_type is not None:
            package_type = package_type.value

        url = '%s/search' % self.domain
        res = self.session.get(url, params={
            'name': query,
            'type': package_type,
            'platform': platform,
        })
        self._check_response(res)
        return res.json()

    def user_licenses(self):
        """Download the user current trial/paid licenses."""
        url = '{domain}/license'.format(domain=self.domain)
        res = self.session.get(url)
        self._check_response(res)
        return res.json()
