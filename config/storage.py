"""Stockage des médias (documents de cours).

Sur Vercel, le système de fichiers des fonctions est **en lecture seule** : écrire
dans ``MEDIA_ROOT`` échoue avec ``OSError: [Errno 30] Read-only file system`` et
transforme chaque enregistrement de cours qui joint un document en erreur 500.

Ce module fournit :

* ``VercelBlobStorage`` : backend Django basé sur Vercel Blob (stockage objet natif Vercel) ;
* ``default_media_storage()`` : sélection automatique du backend selon l'environnement ;
* ``media_error_message()`` : message utilisateur pour une erreur de stockage.

Priorité de sélection :

1. ``BLOB_READ_WRITE_TOKEN`` (ou ``VERCEL_BLOB_READ_WRITE_TOKEN``) → Vercel Blob ;
2. ``AWS_STORAGE_BUCKET_NAME`` + ``AWS_ACCESS_KEY_ID`` → stockage S3 compatible ;
3. sinon → ``FileSystemStorage`` (développement local).
"""

from __future__ import annotations

import errno
import os
import re

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

#: Format des jetons Vercel Blob : ``vercel_blob_rw_<store_id>_<secret>``.
_BLOB_TOKEN_RE = re.compile(r'^vercel_blob_rw_(?P<store>[A-Za-z0-9]+)_')

#: Message affiché quand le stockage objet n'est pas configuré sur l'hébergeur.
BLOB_NOT_CONFIGURED = (
    "Le stockage des documents n'est pas configuré sur cet environnement. "
    "Définissez BLOB_READ_WRITE_TOKEN (Vercel Blob) pour pouvoir joindre des documents."
)


class MediaStorageError(Exception):
    """Erreur de stockage à présenter à l'utilisateur (et non une erreur 500)."""


@deconstructible
class VercelBlobStorage(Storage):
    """Backend de stockage Django adossé à Vercel Blob.

    Les noms de fichiers conservent leur forme habituelle (``cours/12/abc.pdf``) ;
    les URL publiques sont construites à partir de l'identifiant du store.
    """

    def __init__(self, access='public', location='', base_url=None, **kwargs):
        self.access = access
        self.location = (location or '').strip('/')
        self._base_url = base_url

    # ------------------------------------------------------------------ config
    @staticmethod
    def _token():
        return (os.environ.get('BLOB_READ_WRITE_TOKEN')
                or os.environ.get('VERCEL_BLOB_READ_WRITE_TOKEN') or '').strip()

    def _store_id(self):
        store = (os.environ.get('BLOB_STORE_ID') or '').strip()
        if store:
            return store
        match = _BLOB_TOKEN_RE.match(self._token())
        if not match:
            raise MediaStorageError(BLOB_NOT_CONFIGURED)
        return match.group('store')

    @property
    def base_url(self):
        if self._base_url:
            return self._base_url.rstrip('/')
        return f'https://{self._store_id()}.public.blob.vercel-storage.com'

    @staticmethod
    def _sdk():
        try:
            from vercel.blob import sync
        except ImportError as exc:  # pragma: no cover - dépend de l'installation
            raise MediaStorageError(
                "Le SDK Vercel Blob n'est pas installé sur ce serveur.") from exc
        return sync

    def _pathname(self, name):
        name = (name or '').lstrip('/')
        return f'{self.location}/{name}' if self.location else name

    # ------------------------------------------------------------ Storage API
    def _open(self, name, mode='rb'):
        sdk = self._sdk()
        try:
            result = sdk.get_blob(self._pathname(name), access=self.access)
        except MediaStorageError:
            raise
        except Exception as exc:
            raise FileNotFoundError(f"Document introuvable : {name}") from exc
        return ContentFile(result.body)

    def _save(self, name, content):
        sdk = self._sdk()
        self._store_id()  # échoue explicitement si le stockage n'est pas configuré
        data = content.read() if hasattr(content, 'read') else content
        if isinstance(data, str):
            data = data.encode('utf-8')
        content_type = getattr(content, 'content_type', None) or 'application/octet-stream'
        try:
            sdk.put_blob(self._pathname(name), data, access=self.access,
                        content_type=content_type, allow_overwrite=True)
        except MediaStorageError:
            raise
        except Exception as exc:
            raise MediaStorageError(f"Vercel Blob a refusé l'enregistrement du document : {exc}") from exc
        return name

    def delete(self, name):
        sdk = self._sdk()
        try:
            sdk.delete_blob(self._pathname(name))
        except Exception:
            # Supprimer un document déjà absent ne doit jamais faire échouer l'enregistrement.
            pass

    def exists(self, name):
        sdk = self._sdk()
        try:
            sdk.head_blob(self._pathname(name))
        except Exception:
            return False
        return True

    def size(self, name):
        sdk = self._sdk()
        return sdk.head_blob(self._pathname(name)).size

    def url(self, name):
        return f'{self.base_url}/{self._pathname(name)}'


def default_media_storage():
    """Renvoie la configuration ``STORAGES['default']`` adaptée à l'environnement."""
    if os.environ.get('BLOB_READ_WRITE_TOKEN') or os.environ.get('VERCEL_BLOB_READ_WRITE_TOKEN'):
        return {'BACKEND': 'config.storage.VercelBlobStorage', 'OPTIONS': {'access': 'public'}}
    if os.environ.get('AWS_STORAGE_BUCKET_NAME') and os.environ.get('AWS_ACCESS_KEY_ID'):
        options = {
            'bucket_name': os.environ['AWS_STORAGE_BUCKET_NAME'],
            'region_name': os.environ.get('AWS_S3_REGION_NAME', 'auto'),
        }
        if os.environ.get('AWS_S3_ENDPOINT_URL'):
            options['endpoint_url'] = os.environ['AWS_S3_ENDPOINT_URL']
        return {'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage', 'OPTIONS': options}
    return {'BACKEND': 'django.core.files.storage.FileSystemStorage'}


def media_error_message(exc):
    """Transforme une erreur technique de stockage en message lisible."""
    if isinstance(exc, MediaStorageError):
        return str(exc)
    if isinstance(exc, OSError):
        if exc.errno in (errno.EROFS, errno.EACCES, errno.EPERM, errno.ENOENT):
            return (
                "Le stockage des documents n'est pas disponible sur cet environnement "
                "(système de fichiers en lecture seule). Configurez Vercel Blob "
                "(BLOB_READ_WRITE_TOKEN) pour pouvoir joindre des documents."
            )
        return f"Erreur de stockage du document : {exc.strerror or exc}."
    return "Le document n'a pas pu être enregistré. Réessayez plus tard."
