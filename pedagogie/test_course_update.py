"""Tests d'enregistrement d'un cours avec documents.

Ces tests couvrent la régression « POST /pedagogie/cours/<pk>/modifier/ renvoie
une erreur 500 sur Vercel » : le système de fichiers des fonctions Vercel est en
lecture seule, toute écriture de document échouait avec
``OSError: [Errno 30] Read-only file system``.
"""
from io import BytesIO
from unittest import mock
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from reportlab.pdfgen import canvas

from accounts.roles import ensure_system_roles, TEACHER
from config.blob_fakes import FakeBlobSdk
from .models import Cours, SupportCours


class ReadOnlyMediaStorage(FileSystemStorage):
    """Simule le système de fichiers en lecture seule des fonctions Vercel."""

    def _save(self, name, content):
        raise OSError(30, 'Read-only file system')


def build_pdf():
    """Construit un PDF valide (les documents sont validés à l'envoi)."""
    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    document.drawString(100, 750, 'Document EduConnect')
    document.save()
    return buffer.getvalue()


READONLY_STORAGES = {
    'default': {'BACKEND': 'pedagogie.test_course_update.ReadOnlyMediaStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class CourseUpdateDocumentTests(TestCase):
    def setUp(self):
        ensure_system_roles()
        self.teacher = get_user_model().objects.create_user(username='teacher')
        self.teacher.groups.add(Group.objects.get(name=TEACHER))
        self.chapter_id = str(uuid4())
        self.course = Cours.objects.create(
            titre='Mon cours', description='Description', matiere='Maths', niveau='debutant',
            contenu='Introduction', enseignant=self.teacher,
            chapitres=[{'id': self.chapter_id, 'titre': 'Chapitre 1',
                        'contenu': '<p>Contenu</p>', 'masque': False}])
        self.client.force_login(self.teacher)

    def payload(self, **extra):
        data = {
            'titre': 'Mon cours', 'description': 'Description', 'matiere': 'Maths',
            'niveau': 'debutant', 'contenu': 'Introduction',
            'chapitres-TOTAL_FORMS': '1', 'chapitres-INITIAL_FORMS': '1',
            'chapitres-MIN_NUM_FORMS': '0', 'chapitres-MAX_NUM_FORMS': '100',
            'chapitres-0-id': self.chapter_id,
            'chapitres-0-titre': 'Chapitre 1', 'chapitres-0-contenu': '<p>Contenu</p>',
        }
        data.update(extra)
        return data

    def update_url(self):
        return reverse('pedagogie:cours_update', args=[self.course.pk])

    # ------------------------------------------------------------------ tests
    @override_settings(STORAGES=READONLY_STORAGES)
    def test_read_only_media_returns_a_readable_error_instead_of_a_500(self):
        """Régression : le POST ne doit plus renvoyer une erreur 500."""
        document = SimpleUploadedFile('support.pdf', build_pdf(), content_type='application/pdf')
        response = self.client.post(self.update_url(), self.payload(fichiers=document))
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'stockage des documents')
        # Aucun document ne doit avoir été enregistré à moitié.
        self.assertEqual(SupportCours.objects.count(), 0)

    @override_settings(STORAGES=READONLY_STORAGES)
    def test_read_only_media_rolls_back_the_course_changes(self):
        """L'échec d'enregistrement ne doit pas laisser le cours modifié."""
        document = SimpleUploadedFile('support.pdf', build_pdf(), content_type='application/pdf')
        self.client.post(self.update_url(), self.payload(titre='Titre modifié', fichiers=document))
        self.course.refresh_from_db()
        self.assertEqual(self.course.titre, 'Mon cours')

    def test_course_update_with_document_succeeds(self):
        document = SimpleUploadedFile('support.pdf', build_pdf(), content_type='application/pdf')
        response = self.client.post(self.update_url(), self.payload(fichiers=document))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SupportCours.objects.count(), 1)
        support = SupportCours.objects.get()
        self.assertTrue(support.fichier.name)

    def test_chapter_document_upload_succeeds(self):
        document = SimpleUploadedFile('support.pdf', build_pdf(), content_type='application/pdf')
        response = self.client.post(self.update_url(), self.payload(**{'chapitres-0-fichiers': document}))
        self.assertEqual(response.status_code, 302)
        support = SupportCours.objects.get()
        self.assertEqual(support.chapitre_id, self.chapter_id)

    def test_course_update_without_document_still_succeeds(self):
        response = self.client.post(self.update_url(), self.payload(titre='Nouveau titre'))
        self.assertEqual(response.status_code, 302)
        self.course.refresh_from_db()
        self.assertEqual(self.course.titre, 'Nouveau titre')


BLOB_STORAGES = {
    'default': {'BACKEND': 'config.storage.VercelBlobStorage', 'OPTIONS': {'access': 'public'}},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class CourseUpdateBlobStorageTests(TestCase):
    """Enregistrement d'un cours avec document via Vercel Blob (chemin nominal)."""

    def setUp(self):
        ensure_system_roles()
        self.teacher = get_user_model().objects.create_user(username='teacher')
        self.teacher.groups.add(Group.objects.get(name=TEACHER))
        self.chapter_id = str(uuid4())
        self.course = Cours.objects.create(
            titre='Mon cours', description='Description', matiere='Maths', niveau='debutant',
            contenu='Introduction', enseignant=self.teacher,
            chapitres=[{'id': self.chapter_id, 'titre': 'Chapitre 1',
                        'contenu': '<p>Contenu</p>', 'masque': False}])
        self.client.force_login(self.teacher)
        self.blob = FakeBlobSdk()
        self.env = mock.patch.dict('os.environ', {
            'BLOB_READ_WRITE_TOKEN': 'vercel_blob_rw_fakestore_secret',
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def payload(self, **extra):
        data = {
            'titre': 'Mon cours', 'description': 'Description', 'matiere': 'Maths',
            'niveau': 'debutant', 'contenu': 'Introduction',
            'chapitres-TOTAL_FORMS': '1', 'chapitres-INITIAL_FORMS': '1',
            'chapitres-MIN_NUM_FORMS': '0', 'chapitres-MAX_NUM_FORMS': '100',
            'chapitres-0-id': self.chapter_id,
            'chapitres-0-titre': 'Chapitre 1', 'chapitres-0-contenu': '<p>Contenu</p>',
        }
        data.update(extra)
        return data

    @override_settings(STORAGES=BLOB_STORAGES)
    def test_document_is_stored_in_the_blob_store(self):
        document = SimpleUploadedFile('support.pdf', build_pdf(), content_type='application/pdf')
        with self.blob.patch():
            response = self.client.post(
                reverse('pedagogie:cours_update', args=[self.course.pk]),
                self.payload(fichiers=document))
        self.assertEqual(response.status_code, 302)
        support = SupportCours.objects.get()
        self.assertTrue(support.fichier.name.startswith(f'cours/{self.course.pk}/'))
        self.assertTrue(support.fichier.name.endswith('.pdf'))
        self.assertIn(support.fichier.name, self.blob.stored)

    @override_settings(STORAGES=BLOB_STORAGES)
    def test_stored_document_can_be_downloaded_again(self):
        content = build_pdf()
        document = SimpleUploadedFile('support.pdf', content, content_type='application/pdf')
        with self.blob.patch():
            self.client.post(reverse('pedagogie:cours_update', args=[self.course.pk]),
                             self.payload(fichiers=document))
            support = SupportCours.objects.get()
            response = self.client.get(reverse('pedagogie:support_download', args=[support.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), content)
