"""Tests du backend de stockage des médias (Vercel Blob / S3 / disque local)."""
import errno
from unittest import mock

from django.core.files.base import ContentFile
from django.test import SimpleTestCase

from config.blob_fakes import FakeBlobSdk
from config.storage import (BLOB_NOT_CONFIGURED, MediaStorageError, VercelBlobStorage,
                            default_media_storage, media_error_message)


class VercelBlobStorageTests(SimpleTestCase):
    def setUp(self):
        self.sdk = FakeBlobSdk()
        self.env = mock.patch.dict('os.environ', {
            'BLOB_READ_WRITE_TOKEN': 'vercel_blob_rw_store123_secret',
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        patcher = self.sdk.patch()
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_uses_the_blob_pathname(self):
        storage = VercelBlobStorage()
        name = storage.save('cours/1/a.pdf', ContentFile(b'PDF-CONTENT'))
        self.assertEqual(name, 'cours/1/a.pdf')
        pathname, body = self.sdk.put_blob.call_args[0][:2]
        self.assertEqual(pathname, 'cours/1/a.pdf')
        self.assertEqual(body, b'PDF-CONTENT')

    def test_open_returns_the_stored_content(self):
        storage = VercelBlobStorage()
        storage.save('cours/1/a.pdf', ContentFile(b'PDF-CONTENT'))
        with storage.open('cours/1/a.pdf') as handle:
            self.assertEqual(handle.read(), b'PDF-CONTENT')

    def test_open_missing_file_raises_file_not_found(self):
        storage = VercelBlobStorage()
        with self.assertRaises(FileNotFoundError):
            storage.open('cours/1/absent.pdf')

    def test_delete_never_raises(self):
        storage = VercelBlobStorage()
        storage.save('cours/1/a.pdf', ContentFile(b'PDF-CONTENT'))
        self.sdk.delete_blob.side_effect = OSError('missing')
        storage.delete('cours/1/a.pdf')  # ne doit pas lever

    def test_delete_removes_the_blob(self):
        storage = VercelBlobStorage()
        storage.save('cours/1/a.pdf', ContentFile(b'PDF-CONTENT'))
        storage.delete('cours/1/a.pdf')
        self.assertFalse(storage.exists('cours/1/a.pdf'))

    def test_exists_and_size(self):
        storage = VercelBlobStorage()
        storage.save('cours/1/a.pdf', ContentFile(b'PDF-CONTENT'))
        self.assertTrue(storage.exists('cours/1/a.pdf'))
        self.assertEqual(storage.size('cours/1/a.pdf'), len(b'PDF-CONTENT'))
        self.assertFalse(storage.exists('cours/1/absent.pdf'))

    def test_url_uses_the_public_store_url(self):
        storage = VercelBlobStorage()
        self.assertEqual(storage.url('cours/1/a.pdf'),
                         'https://store123.public.blob.vercel-storage.com/cours/1/a.pdf')

    def test_save_without_credentials_raises_a_readable_error(self):
        with mock.patch.dict('os.environ', {'BLOB_READ_WRITE_TOKEN': '', 'BLOB_STORE_ID': ''}, clear=False):
            storage = VercelBlobStorage()
            with self.assertRaises(MediaStorageError) as ctx:
                storage.save('cours/1/a.pdf', ContentFile(b'x'))
        self.assertIn(BLOB_NOT_CONFIGURED, str(ctx.exception))


class DefaultMediaStorageTests(SimpleTestCase):
    def test_local_filesystem_when_nothing_is_configured(self):
        with mock.patch.dict('os.environ', {'BLOB_READ_WRITE_TOKEN': '', 'AWS_STORAGE_BUCKET_NAME': '',
                                           'AWS_ACCESS_KEY_ID': ''}, clear=False):
            self.assertEqual(default_media_storage()['BACKEND'],
                             'django.core.files.storage.FileSystemStorage')

    def test_vercel_blob_when_a_token_is_present(self):
        with mock.patch.dict('os.environ', {'BLOB_READ_WRITE_TOKEN': 'vercel_blob_rw_store_secret'}, clear=False):
            backend = default_media_storage()
        self.assertEqual(backend['BACKEND'], 'config.storage.VercelBlobStorage')

    def test_s3_when_a_bucket_is_present(self):
        env = {'AWS_STORAGE_BUCKET_NAME': 'educonnect', 'AWS_ACCESS_KEY_ID': 'key',
               'AWS_S3_ENDPOINT_URL': 'https://example.r2.cloudflarestorage.com'}
        with mock.patch.dict('os.environ', env, clear=False):
            backend = default_media_storage()
        self.assertEqual(backend['BACKEND'], 'storages.backends.s3boto3.S3Boto3Storage')
        self.assertEqual(backend['OPTIONS']['bucket_name'], 'educonnect')
        self.assertEqual(backend['OPTIONS']['endpoint_url'], 'https://example.r2.cloudflarestorage.com')


class MediaErrorMessageTests(SimpleTestCase):
    def test_read_only_file_system_message(self):
        message = media_error_message(OSError(errno.EROFS, 'Read-only file system'))
        self.assertIn('stockage des documents', message)
        self.assertIn('BLOB_READ_WRITE_TOKEN', message)

    def test_media_storage_error_message_is_kept(self):
        self.assertEqual(media_error_message(MediaStorageError('Message précis')), 'Message précis')

    def test_generic_message(self):
        self.assertIn('Réessayez', media_error_message(RuntimeError('boom')))
