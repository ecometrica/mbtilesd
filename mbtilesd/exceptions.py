# -*- coding: utf-8 -*-

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

from werkzeug.exceptions import NotFound as WerkzeugNotFound


class NotFound(WerkzeugNotFound):
    description = 'Not Found'

    def get_body(self, environ, *args, **kwargs):
        return '{description}'.format(
            description=self.get_description(environ)
        )

    def get_headers(self, environ, *args, **kwargs):
        return [(b'Content-Type', b'text/plain')]


class TileNotFound(NotFound):
    description = 'Tile does not exist'


class TilesetNotFound(NotFound):
    description = 'Tileset does not exist'
