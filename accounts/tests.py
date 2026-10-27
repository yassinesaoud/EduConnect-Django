from unittest.mock import patch

from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import Group, User
from django.test import TestCase

from .forms import RegisterForm
from .roles import LEARNER


class RegistrationRoleTests(TestCase):
    def registration_data(self, username):
        return {
            'first_name': 'Test',
            'last_name': 'Learner',
            'username': username,
            'email': f'{username}@example.test',
            'password1': 'PreviewSafePassword!483',
            'password2': 'PreviewSafePassword!483',
        }

    def test_registration_creates_user_with_learner_role(self):
        response = self.client.post(
            '/comptes/inscription/', self.registration_data('new_learner'),
        )

        self.assertRedirects(response, '/comptes/connexion/')
        user = User.objects.get(username='new_learner')
        self.assertTrue(user.groups.filter(name=LEARNER).exists())

    def test_registration_restores_missing_system_roles(self):
        Group.objects.filter(name=LEARNER).delete()

        response = self.client.post(
            '/comptes/inscription/', self.registration_data('restored_learner'),
        )

        self.assertRedirects(response, '/comptes/connexion/')
        user = User.objects.get(username='restored_learner')
        self.assertTrue(user.groups.filter(name=LEARNER).exists())
        self.assertTrue(Group.objects.filter(name=LEARNER).exists())

    def test_registration_rolls_back_user_if_assignment_flow_fails(self):
        form = RegisterForm(data=self.registration_data('rolled_back_learner'))
        self.assertTrue(form.is_valid(), form.errors)
        original_save = UserCreationForm.save

        def save_then_fail(parent_form, commit=True):
            original_save(parent_form, commit=commit)
            raise RuntimeError('simulated failure after user insert')

        with patch.object(UserCreationForm, 'save', save_then_fail):
            with self.assertRaisesRegex(RuntimeError, 'simulated failure'):
                form.save()

        self.assertFalse(User.objects.filter(username='rolled_back_learner').exists())
