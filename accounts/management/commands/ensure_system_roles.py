from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from accounts.roles import SYSTEM_ROLES, ensure_system_roles


class Command(BaseCommand):
    help = 'Ensure the fixed system roles exist without creating or changing users.'

    def handle(self, *args, **options):
        ensure_system_roles()
        existing = set(Group.objects.filter(name__in=SYSTEM_ROLES).values_list('name', flat=True))
        missing = set(SYSTEM_ROLES) - existing
        if missing:
            raise CommandError('System role initialization did not complete.')
        self.stdout.write(self.style.SUCCESS('System roles are initialized.'))
