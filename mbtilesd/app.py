# -*- coding: utf-8 -*-

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from collections import OrderedDict
from datetime import datetime
from email.utils import formatdate
import errno
import json
import os
import re

from flask import abort, Flask, request

from gdal2mbtiles.mbtiles import InvalidFileError, MBTiles, Metadata

from .exceptions import (NotFound, TileNotFound, TilesetNotFound,
                         WerkzeugNotFound)


class App(Flask):
    def make_default_options_response(self, *args, **kwargs):
        # Allow XHR requests with a preflight OPTIONS request
        rv = super(App, self).make_default_options_response(*args, **kwargs)
        allowed_headers = request.headers.get(b'Access-Control-Request-Headers',
                                              None)
        if allowed_headers is not None:
            rv.headers.add(b'Access-Control-Allow-Headers', allowed_headers)
        return rv

    def make_response(self, rv, *args, **kwargs):
        # Allow requests from any origin
        rv = super(App, self).make_response(rv, *args, **kwargs)
        rv.headers.add(b'Access-Control-Allow-Origin', '*')
        return rv


app = App(__name__)


def load_config(filename=None):
    if hasattr(app, 'config_filename'):
        # Already configured
        return

    if filename is None:
        filename = '/etc/mbtilesd/settings.py'
        silent = True
    else:
        silent = False
    app.config_filename = filename

    # Default configuration
    app.config.update(dict(
        CACHE_MAX_AGE=86400,    # 1 day
        PATHS=[os.path.abspath(os.path.curdir)],
        SERVERS=[],
    ))
    app.config.from_pyfile(filename, silent=silent)

app.before_first_request(load_config)


def get_mbtiles(name):
    """
    Returns the MBTiles associated with `name`.

    Searches through config['PATHS'] and finds the earliest match.
    """
    name += '.mbtiles'
    for path in app.config['PATHS']:
        filename = os.path.join(path, name)
        if os.path.exists(filename):
            return MBTiles(filename)
    raise IOError(errno.ENOENT, os.strerror(errno.ENOENT))


def get_servers():
    """Returns a list of servers that host tile images."""
    servers = app.config['SERVERS']
    if servers:
        return servers

    # Dynamically construct the server list based on the host
    return [request.host]


@app.errorhandler(404)
def http_not_found(error):
    """Responds with a plain-text HTTP Not Found (404)."""
    if type(error) == WerkzeugNotFound:
        error = NotFound()
    return error.get_response(environ={})


@app.route('/v3/<name>.json')
def tilejson(name):
    """Responds with TileJSON for `name`."""
    try:
        with get_mbtiles(name=name) as mbtiles:
            metadata = mbtiles.metadata

            result = dict(
                description=metadata['description'],
                filesize=os.path.getsize(mbtiles.filename),
                id=name,
                legend=None,
                name=metadata['name'],
                private=True,
                scheme='xyz',
                tilejson='2.0.0',
                tiles=[
                    '{http}://{host}/v3/{name}/{{z}}/{{x}}/{{y}}.{ext}'.format(
                        http=request.scheme,
                        host=host,
                        name=name,
                        ext=metadata['format']
                    )
                    for host in get_servers()
                ],
                type=metadata['type'],
                version=metadata['version'],
                webpage=None,
            )

            maxzoom = metadata.get('x-maxzoom', None)
            minzoom = metadata.get('x-minzoom', None)
            if maxzoom is None or minzoom is None:
                cursor = mbtiles._conn.execute(
                    "SELECT MIN(zoom_level), MAX(zoom_level) FROM tiles"
                )
                minzoom, maxzoom = cursor.fetchone()
                metadata['x-minzoom'] = minzoom
                metadata['x-maxzoom'] = maxzoom
            minzoom = int(minzoom)
            maxzoom = int(maxzoom)
            result.update(dict(minzoom=minzoom, maxzoom=maxzoom))

            if metadata.get('bounds', None):
                bounds = [float(b) for b in metadata['bounds'].split(',')]
                result['bounds'] = bounds
                result['center'] = [(bounds[2] + bounds[0]) / 2,
                                    (bounds[3] + bounds[1]) / 2,
                                    (maxzoom + minzoom) / 2]

            results = json.dumps(OrderedDict(sorted(result.iteritems())))
            jsonp = request.args.get('callback')
            if jsonp is not None:
                if not re.match('[A-Za-z_]\w*$', jsonp):
                    # This is not a valid jsonp identifier.
                    abort(400)

                return (
                    '{callback}({results})'.format(
                        callback=jsonp, results=results
                    ),
                    None,
                    {b'Content-Type': 'application/javascript; charset=utf-8'}
                )
            return (results,
                    None,
                    {b'Content-Type': 'application/json; charset=utf-8'})
    except (InvalidFileError, IOError):
        raise TilesetNotFound()


def tile(name, x, y, z, format, content_type):
    """
    Responds with raw tile data for `name` at (`x`, `y``, `z``).

    format: Must match the MBTiles format metadata.
    content_type: Used to determine the Content-Type of the response.
    """
    try:
        with get_mbtiles(name=name) as mbtiles:
            if mbtiles.metadata['format'] != format:
                raise TileNotFound()

            # mtime needs to be an int, because Last-Modified headers aren't
            # millisecond-precise.
            mtime = int(os.path.getmtime(mbtiles.filename))
            mdatetime = datetime.fromtimestamp(mtime)
            modified_since = request.if_modified_since
            unmodified_since = request.if_unmodified_since
            if (modified_since is not None and mdatetime <= modified_since) or \
               (unmodified_since is not None and mdatetime > unmodified_since):
                return b'', 304

            x, y, z = [int(n) for n in (x, y, z)]
            content = mbtiles.get(x=x,
                                  y=2 ** z - 1 - y,
                                  z=z)
            if content is None:
                raise TileNotFound()
            return (
                bytes(content),
                200,
                {
                    b'Content-Type': content_type,
                    b'Cache-Control': 'max-age={0}'.format(
                        app.config['CACHE_MAX_AGE']
                    ),
                    b'Last-Modified': formatdate(mtime, localtime=False,
                                                 usegmt=True),
                }
            )

    except (InvalidFileError, IOError):
        raise TilesetNotFound()


@app.route('/v3/<name>/<z>/<x>/<y>.png')
def tile_png(name, x, y, z):
    """Responds with a PNG for `name` at (`x`, `y``, `z``)."""
    return tile(name=name, x=x, y=y, z=z,
                format=Metadata.latest().FORMATS.PNG,
                content_type='image/png')


@app.route('/v3/<name>/<z>/<x>/<y>.jpg')
def jpgtile(name, x, y, z):
    """Responds with a JPEG for `name` at (`x`, `y``, `z``)."""
    return tile(name=name, x=x, y=y, z=z,
                format=Metadata.latest().FORMATS.JPG,
                content_type='image/jpeg')
