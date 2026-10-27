from django.apps import apps
from django.contrib.auth.management import create_permissions
from django.contrib.auth.models import Group, Permission
from django.db import transaction

ADMIN = 'Administrateur'
TEACHER = 'Enseignant / Coach'
LEARNER = 'Apprenant'
SYSTEM_ROLES = (ADMIN, TEACHER, LEARNER)


def is_admin(user):
    return user.is_superuser or user.groups.filter(name=ADMIN).exists()


def _perms(models, actions):
    codes = [f'{a}_{m}' for m in models for a in actions]
    return Permission.objects.filter(codename__in=codes, content_type__app_label__in=['auth', 'pedagogie', 'objectifs', 'ressources'])


@transaction.atomic
def ensure_system_roles():
    """Crée les 3 rôles système (une seule fois) avec leurs permissions par défaut."""
    # Also support callers outside `migrate`, such as registration and the
    # explicit initialization command. Permission creation is idempotent.
    for label in ('auth', 'pedagogie', 'objectifs', 'coaching', 'ressources'):
        create_permissions(apps.get_app_config(label), verbosity=0)

    crud = ('add', 'change', 'delete', 'view')
    defaults = {
        ADMIN: _perms(['user', 'group', 'cours', 'exercice', 'objectifapprentissage', 'tacheapprentissage', 'categorie', 'ressource'], crud),
        TEACHER: _perms(['cours', 'exercice', 'objectifapprentissage', 'tacheapprentissage', 'categorie', 'ressource'], crud),
        LEARNER: _perms(['cours', 'exercice'], ('view',)) | _perms(['objectifapprentissage', 'tacheapprentissage'], ('view',)) | _perms(['categorie', 'ressource'], ('view',)) | Permission.objects.filter(content_type__app_label='objectifs', codename='update_statut_tacheapprentissage'),
    }
    for name, perms in defaults.items():
        group, _ = Group.objects.get_or_create(name=name)
        group.permissions.add(*perms)
        # Ajout ciblé et idempotent des permissions Coaching, sans remplacer
        # les permissions existantes ou personnalisées.
        if name == ADMIN:
            coaching_actions = {
                model: crud for model in (
                    'sessioncoaching', 'conseilpedagogique', 'coachavailability',
                    'coachavailabilityexception', 'bookingrequest', 'coachingaction',
                )
            }
            extra_codes = []
        elif name == TEACHER:
            coaching_actions = {
                'sessioncoaching': crud, 'conseilpedagogique': crud,
                'coachavailability': crud, 'coachavailabilityexception': crud,
                'bookingrequest': ('view', 'change'), 'coachingaction': crud,
            }
            extra_codes = []
        else:
            coaching_actions = {
                'sessioncoaching': ('view',), 'conseilpedagogique': ('view',),
                'bookingrequest': ('add', 'view', 'change'),
                'coachingaction': ('view', 'change'),
            }
            extra_codes = ['view_own_coachingaction']
        codes = [f'{action}_{model}' for model, actions in coaching_actions.items() for action in actions]
        codes.extend(extra_codes)
        coaching = Permission.objects.filter(content_type__app_label='coaching', codename__in=codes)
        group.permissions.add(*coaching)
