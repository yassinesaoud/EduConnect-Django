"""Fausse implémentation de l'API Vercel Blob, pour les tests.

Reproduit le comportement utile du SDK : ``put_blob`` enregistre le contenu,
``head_blob`` lève une erreur pour un chemin inconnu, ``get_blob`` renvoie les
octets stockés et ``delete_blob`` retire l'objet.
"""

from unittest import mock


class FakeBlobSdk:
    """SDK Vercel Blob simulé, en mémoire.

    Les méthodes sont des ``mock.Mock`` afin de pouvoir en inspecter les
    appels, tout en conservant un état cohérent entre elles.
    """

    def __init__(self):
        self.stored = {}
        self.put_blob = mock.Mock(side_effect=self._put_blob)
        self.get_blob = mock.Mock(side_effect=self._get_blob)
        self.head_blob = mock.Mock(side_effect=self._head_blob)
        self.delete_blob = mock.Mock(side_effect=self._delete_blob)

    # -- API ---------------------------------------------------------------
    def _put_blob(self, pathname, body, **kwargs):
        self.stored[pathname] = body
        return mock.Mock(
            url=f'https://fake.public.blob.vercel-storage.com/{pathname}',
            download_url=f'https://fake.public.blob.vercel-storage.com/{pathname}?download=1',
            pathname=pathname)

    def _get_blob(self, pathname, **kwargs):
        if pathname not in self.stored:
            raise OSError('Blob not found')
        return mock.Mock(body=self.stored[pathname])

    def _head_blob(self, pathname, **kwargs):
        if pathname not in self.stored:
            raise OSError('Blob not found')
        return mock.Mock(size=len(self.stored[pathname]))

    def _delete_blob(self, pathname, **kwargs):
        self.stored.pop(pathname, None)

    # -- utilitaire --------------------------------------------------------
    def patch(self):
        """Retourne un context manager qui remplace le SDK par cette instance."""
        return mock.patch('vercel.blob.sync', self)
