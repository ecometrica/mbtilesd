# -*- coding: utf-8 -*-

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from collections import OrderedDict
import errno
import os
import json

from flask import Flask, request

from gdal2mbtiles.mbtiles import InvalidFileError, MBTiles, Metadata

from .exceptions import (NotFound, TileNotFound, TilesetNotFound,
                         WerkzeugNotFound)


app = Flask(__name__)


def get_mbtiles(name):
    """
    Returns the MBTiles associated with `name`.

    Searches through config['paths'] and finds the earliest match.
    """
    if 'paths' not in app.config:
        paths = os.environ.get('MBTILESPATH', '')
        if not paths:
            raise RuntimeError('No paths configured. Set MBTILESPATH.')
        app.config['paths'] = paths.split(':')

    name += '.mbtiles'
    for path in app.config['paths']:
        filename = os.path.join(path, name)
        if os.path.exists(filename):
            return MBTiles(filename)
    raise IOError(errno.ENOENT, os.strerror(errno.ENOENT))


def get_servers():
    """Returns a list of servers that host tile images."""
    if app.config.get('servers', None) is None:
        servers = os.environ.get('MBTILES_SERVERS', '')
        if servers:
            app.config['servers'] = servers.split(',')
        else:
            # Dynamically construct the server list based on the host
            return [request.host]

    return app.config['servers']


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

            return (json.dumps(OrderedDict(sorted(result.iteritems()))),
                    None,
                    {b'Content-Type': 'application/json',
                     b'Access-Control-Allow-Origin': '*'})
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

            x, y, z = [int(n) for n in (x, y, z)]
            content = mbtiles.get(x=x,
                                  y=2 ** z - 1 - y,
                                  z=z)
            if content is None:
                raise TileNotFound()
            return bytes(content), 200, {b'Content-Type': content_type}

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
