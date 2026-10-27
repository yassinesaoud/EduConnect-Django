from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.db import transaction
from django.http import FileResponse, JsonResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.views import View
from django.views.generic import FormView
from pathlib import Path
from uuid import uuid4
from copy import copy
from django.urls import reverse_lazy
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from accounts.roles import is_admin
from config.mixins import QueryStringMixin
from config.storage import MediaStorageError, media_error_message

from .forms import CoursForm, ExerciceForm, QuestionQCMFormSet, ChapitreFormSet, ImportCoursForm
from .models import Cours, Exercice, SupportCours, TentativeExercice, ParticipationExercice
from .learning import exercise_questions, exercise_revision
from .grading import save_submission, SubmissionError
from .office import export_course
from .visibility import visible_courses, visible_chapters, visible_exercises, manages_course, can_read_support


def _cleanup_saved_files(saved_files):
    """Supprime les fichiers déjà écrits sans masquer l'erreur d'origine."""
    for file in saved_files:
        try:
            file.delete(save=False)
        except Exception:
            pass


class _MediaSaveError(Exception):
    """Erreur d'enregistrement d'un document, déjà traduite pour l'utilisateur."""


class OwnerOrAdminMixin:
    """Modifier/supprimer : seulement le propriétaire (enseignant du cours) ou un administrateur."""

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if not is_admin(self.request.user) and obj.enseignant != self.request.user:
            raise PermissionDenied
        return obj


class DeleteMessageMixin:
    http_method_names = ['post']

    def form_valid(self, form):
        messages.success(self.request, 'Suppression effectuée.')
        return super().form_valid(form)


# ---------- Cours ----------
class CoursListView(PermissionRequiredMixin, QueryStringMixin, ListView):
    permission_required = 'pedagogie.view_cours'
    template_name = 'pedagogie/cours_list.html'
    paginate_by = 9

    def get_queryset(self):
        qs = visible_courses(self.request.user).select_related('enseignant')
        g = self.request.GET
        if q := g.get('q', '').strip():
            qs = qs.filter(Q(titre__icontains=q) | Q(description__icontains=q) | Q(matiere__icontains=q))
        if g.get('matiere'):
            qs = qs.filter(matiere=g['matiere'])
        if g.get('niveau'):
            qs = qs.filter(niveau=g['niveau'])
        if g.get('mes_cours') == '1' and self.request.user.has_perm('pedagogie.add_cours'):
            qs = qs.filter(enseignant=self.request.user)
        return qs

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx['matieres'] = visible_courses(self.request.user).order_by('matiere').values_list('matiere', flat=True).distinct()
        ctx['niveaux'] = Cours.NIVEAUX
        return ctx


class CoursDetailView(PermissionRequiredMixin, DetailView):
    permission_required = 'pedagogie.view_cours'
    model = Cours
    template_name = 'pedagogie/cours_detail.html'

    def get_queryset(self):
        return visible_courses(self.request.user).select_related('enseignant')

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        chapters = visible_chapters(self.request.user, self.object)
        selected = self.request.GET.get('chapitre', '')
        if selected and selected not in [c['id'] for c in chapters]:
            raise Http404('Chapitre introuvable')
        exercises = list(visible_exercises(self.request.user, self.object.exercices.all()))
        ctx['plan'] = [dict(c, numero=i, exercices=[e for e in exercises if e.chapitre_id == c['id']]) for i, c in enumerate(chapters, 1)]
        ctx['chapitre_actif'] = next((c for c in ctx['plan'] if c['id'] == selected), None)
        ctx['exercices_affiches'] = [e for e in exercises if e.chapitre_id == selected]
        current = next((i for i, c in enumerate(chapters) if c['id'] == selected), -1)
        ctx['precedent'] = chapters[current - 1] if current > 0 else None
        ctx['suivant'] = chapters[current + 1] if current + 1 < len(chapters) else None
        ctx['nombre_exercices'] = len(exercises)
        supports = self.object.supports.all()
        if not manages_course(self.request.user, self.object):
            supports = supports.filter(masque=False)
        ctx['supports_generaux'] = supports.filter(chapitre_id='')
        ctx['supports_chapitre'] = supports.filter(chapitre_id=selected) if selected else []
        from .presentation import reading_pages, content_blocks
        content = ctx['chapitre_actif']['contenu'] if ctx['chapitre_actif'] else self.object.contenu
        ctx['pages_lecture'] = Paginator(reading_pages(content), 1)
        ctx['blocs_lecture'] = content_blocks(content)
        ctx['page_lecture'] = ctx['pages_lecture'].get_page(self.request.GET.get('page', 1))
        ctx['chapitre_selectionne'] = selected
        ctx['numeros_pages'] = list(ctx['pages_lecture'].get_elided_page_range(ctx['page_lecture'].number, on_each_side=2, on_ends=1))
        return ctx


class CoursChapitresMixin:
    def get_supports(self):
        if not hasattr(self, '_supports'):
            self._supports = list(self.object.supports.all()) if self.object else []
            for support in self._supports:
                support.action_selectionnee = self.request.POST.get(f'support_action_{support.pk}', 'hide' if support.masque else 'show')
        return self._supports

    def get_chapitres(self):
        if not hasattr(self, '_chapitres'):
            self._chapitres = ChapitreFormSet(
                self.request.POST if self.request.method == 'POST' else None,
                self.request.FILES if self.request.method == 'POST' else None,
                initial=self.object.chapitres if self.object else [], prefix='chapitres')
            supports = self.get_supports()
            for item in self._chapitres.forms:
                identity = item.data.get(item.add_prefix('id')) if item.is_bound else item.initial.get('id')
                identity = identity or item.initial.get('id')
                item.supports_existants = [s for s in supports if s.chapitre_id == str(identity)]
        return self._chapitres

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['chapitres_formset'] = self.get_chapitres()
        ctx['supports_generaux_edition'] = [s for s in self.get_supports() if not s.chapitre_id]
        return ctx

    def form_valid(self, form):
        supports = self.get_supports()
        allowed = {f'support_action_{s.pk}' for s in supports}
        if any(key.startswith('support_action_') and key not in allowed for key in self.request.POST) or any(s.action_selectionnee not in ('show', 'hide', 'delete') for s in supports):
            form.add_error(None, 'Action sur un document invalide. Rechargez le cours puis réessayez.')
            return self.form_invalid(form)
        formset = self.get_chapitres()
        if not formset.is_valid():
            return self.form_invalid(form)
        old_ids = {c['id'] for c in self.object.chapitres} if self.object else set()
        chapters = []
        chapter_uploads = []
        for item in formset.forms:
            row = item.cleaned_data
            if not row or row.get('DELETE'):
                continue
            identity = str(row.get('id') or item.initial.get('id') or uuid4())
            if (row.get('id') and identity not in old_ids) or any(c['id'] == identity for c in chapters):
                item.add_error('titre', 'Chapitre invalide. Rechargez le cours puis réessayez.')
                return self.form_invalid(form)
            chapters.append(dict(id=identity, titre=row['titre'], contenu=row['contenu'], masque=row.get('masque', False)))
            chapter_uploads.extend((identity, upload) for upload in row.get('fichiers', []))
        if sum(
                upload.size for key in self.request.FILES for upload in self.request.FILES.getlist(key)) > 50 * 1024 * 1024:
            form.add_error(None, 'L’ensemble des documents du cours et des chapitres ne doit pas dépasser 50 Mo par envoi.')
            return self.form_invalid(form)
        form.instance.chapitres = chapters
        saved_files = []
        try:
            with transaction.atomic():
                response = super().form_valid(form)
                for support in supports:
                    if support.action_selectionnee == 'delete':
                        storage, name = support.fichier.storage, support.fichier.name
                        support.delete()
                        transaction.on_commit(lambda storage=storage, name=name: storage.delete(name), robust=True)
                    else:
                        support.masque = support.action_selectionnee == 'hide'
                        support.save(update_fields=['masque'])
                self.object.exercices.exclude(chapitre_id__in=[c['id'] for c in chapters]).update(chapitre_id='')
                self.object.supports.exclude(chapitre_id__in=[c['id'] for c in chapters]).update(chapitre_id='')
                uploads = [('', upload) for upload in form.cleaned_data.get('fichiers', [])] + chapter_uploads
                try:
                    for identity, upload in uploads:
                        support = SupportCours(cours=self.object, nom=upload.name, chapitre_id=identity)
                        support.fichier.save(upload.name, upload, save=False)
                        saved_files.append(support.fichier)
                        support.save()
                except (MediaStorageError, OSError) as exc:
                    # Le stockage est indisponible (système de fichiers en lecture seule sur
                    # Vercel, blob non configuré, quota dépassé...) : on remonte une erreur
                    # lisible au lieu d'une erreur 500.
                    raise _MediaSaveError(media_error_message(exc)) from exc
        except _MediaSaveError as exc:
            _cleanup_saved_files(saved_files)
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        except Exception:
            _cleanup_saved_files(saved_files)
            raise
        messages.success(self.request, 'Cours enregistré.')
        return response


class CoursImportView(PermissionRequiredMixin, FormView):
    permission_required = 'pedagogie.add_cours'
    form_class = ImportCoursForm
    template_name = 'pedagogie/cours_import.html'
    success_url = reverse_lazy('pedagogie:cours_list')

    def form_valid(self, form):
        saved_files = []
        try:
            with transaction.atomic():
                for upload, intro, chapters in form.documents:
                    course = Cours.objects.create(titre=Path(upload.name).stem[:200],
                        description=f'Cours importé depuis {upload.name}'[:500],
                        matiere=form.cleaned_data['matiere'], niveau=form.cleaned_data['niveau'],
                        contenu=intro, chapitres=chapters, enseignant=self.request.user)
                    support = SupportCours(cours=course, nom=upload.name)
                    try:
                        support.fichier.save(upload.name, upload, save=False)
                    except (MediaStorageError, OSError) as exc:
                        raise _MediaSaveError(media_error_message(exc)) from exc
                    saved_files.append(support.fichier)
                    support.save()
        except _MediaSaveError as exc:
            _cleanup_saved_files(saved_files)
            form.add_error(None, str(exc))
            return self.form_invalid(form)
        except Exception:
            _cleanup_saved_files(saved_files)
            raise
        messages.success(self.request, f'{len(form.documents)} cours importé(s). Vous pouvez maintenant les modifier et ajouter des exercices par chapitre.')
        return super().form_valid(form)


class CoursExportView(PermissionRequiredMixin, View):
    permission_required = 'pedagogie.view_cours'

    def get(self, request, pk, kind):
        if kind not in ('docx', 'pptx', 'pdf'):
            raise Http404
        course = copy(get_object_or_404(visible_courses(request.user), pk=pk))
        course.chapitres = visible_chapters(request.user, course)
        response = FileResponse(export_course(course, kind), as_attachment=True, filename=f'cours-{pk}.{kind}')
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class SupportDownloadView(PermissionRequiredMixin, View):
    permission_required = 'pedagogie.view_cours'

    def get(self, request, pk):
        support = get_object_or_404(SupportCours.objects.select_related('cours'), pk=pk)
        if not can_read_support(request.user, support):
            raise Http404
        try:
            file = support.fichier.open('rb')
        except FileNotFoundError as exc:
            raise Http404 from exc
        response = FileResponse(file, as_attachment=True, filename=support.nom)
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class ChapitresChoicesView(PermissionRequiredMixin, View):
    permission_required = 'pedagogie.add_exercice'

    def get(self, request, pk):
        qs = Cours.objects.all() if is_admin(request.user) else Cours.objects.filter(enseignant=request.user)
        course = get_object_or_404(qs, pk=pk)
        return JsonResponse({'chapitres': [{'id': c['id'], 'titre': c['titre']} for c in course.chapitres]})


class CoursCreateView(PermissionRequiredMixin, CoursChapitresMixin, CreateView):
    permission_required = 'pedagogie.add_cours'
    form_class = CoursForm
    template_name = 'pedagogie/cours_form.html'

    def form_valid(self, form):
        form.instance.enseignant = self.request.user
        return super().form_valid(form)


class CoursUpdateView(PermissionRequiredMixin, OwnerOrAdminMixin, CoursChapitresMixin, UpdateView):
    permission_required = 'pedagogie.change_cours'
    model = Cours
    form_class = CoursForm
    template_name = 'pedagogie/cours_form.html'

    def form_valid(self, form):
        return super().form_valid(form)


class CoursDeleteView(PermissionRequiredMixin, OwnerOrAdminMixin, DeleteMessageMixin, DeleteView):
    permission_required = 'pedagogie.delete_cours'
    model = Cours
    success_url = reverse_lazy('pedagogie:cours_list')

    def form_valid(self, form):
        if self.object.exercices.filter(tentatives__isnull=False).exists():
            messages.warning(self.request, 'Ce cours contient des copies et des notes. Masquez-le pour conserver les résultats.')
            return redirect(self.object)
        files = [(support.fichier.storage, support.fichier.name) for support in self.object.supports.all()]
        with transaction.atomic():
            response = super().form_valid(form)
            for storage, name in files:
                transaction.on_commit(lambda storage=storage, name=name: storage.delete(name), robust=True)
        return response


# ---------- Exercices ----------
class ExerciceListView(PermissionRequiredMixin, QueryStringMixin, ListView):
    permission_required = 'pedagogie.view_exercice'
    template_name = 'pedagogie/exercice_list.html'
    paginate_by = 10

    def get_queryset(self):
        qs = visible_exercises(self.request.user).select_related('cours')
        g = self.request.GET
        if q := g.get('q', '').strip():
            qs = qs.filter(Q(titre__icontains=q) | Q(question__icontains=q))
        for field in ('type_exercice', 'difficulte'):
            if g.get(field):
                qs = qs.filter(**{field: g[field]})
        if g.get('cours', '').isdigit():
            qs = qs.filter(cours_id=g['cours'])
        return qs

    def get_context_data(self, **kw):
        ctx = super().get_context_data(**kw)
        ctx.update(types=Exercice.TYPES, difficultes=Exercice.DIFFICULTES, cours_all=visible_courses(self.request.user).order_by('titre'))
        return ctx


class ExerciceDetailView(PermissionRequiredMixin, DetailView):
    permission_required = 'pedagogie.view_exercice'
    model = Exercice
    queryset = Exercice.objects.select_related('cours__enseignant')
    template_name = 'pedagogie/exercice_detail.html'

    def get_queryset(self):
        return visible_exercises(self.request.user, super().get_queryset())

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        revision = exercise_revision(self.object)
        manager = manages_course(self.request.user, self.object.cours)
        state = ParticipationExercice.objects.filter(eleve=self.request.user, exercice=self.object).first()
        can_submit = not manager and (state is None or state.tentatives_utilisees < state.tentatives_autorisees)
        latest = self.object.tentatives.filter(eleve=self.request.user).first()
        attempt = getattr(self, 'tentative', None)
        if self.request.method == 'GET' and self.request.GET.get('tentative', '').isdigit():
            attempt = get_object_or_404(TentativeExercice, pk=self.request.GET['tentative'], eleve=self.request.user, exercice=self.object)
        if attempt is None and not can_submit and not manager:
            attempt = latest
        questions = exercise_questions(self.object)
        kind = self.object.type_exercice
        if attempt:
            if attempt.enonce:
                questions = attempt.enonce['questions']
                kind = attempt.enonce['type']
            elif attempt.revision != revision:
                questions = [dict(question=f'Question {i + 1} — ancienne version', propositions='', bonne_reponse='', explication='', bareme='') for i in range(len(attempt.reponses))]
        rows = []
        for index, question in enumerate(questions):
            answer = attempt.reponses[index] if attempt else (self.request.POST.get(f'reponse_{index}', '').strip() if self.request.method == 'POST' else '')
            rows.append(dict(question=question['question'], propositions=(['Vrai', 'Faux'] if kind == 'vf' else question['propositions'].splitlines()), bonne_reponse=question['bonne_reponse'], explication=question.get('explication', ''), reponse=answer, juste=attempt.resultats[index] if attempt else False, bareme=question.get('bareme', ''), correction=attempt.corrections.get(str(index)) if attempt else None))
        ctx['questions_qcm'] = rows
        ctx['exercise_revision'] = revision
        ctx['question_type'] = kind
        ctx['can_submit'] = can_submit
        ctx['readonly'] = manager or attempt is not None
        ctx['teacher_preview'] = manager
        ctx['attempt_number'] = state.tentatives_utilisees + 1 if state else 1
        ctx['correction_available'] = attempt is not None and attempt.revision == revision
        ctx['tentative'] = attempt
        ctx['derniere_tentative'] = latest
        if attempt:
            ctx['corrige'] = True
            ctx['score'] = None if kind == 'ouverte' else sum(result is True for result in attempt.resultats)
        ctx['erreur_reponse'] = getattr(self, 'erreur_reponse', '')
        return ctx

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if manages_course(request.user, self.object.cours):
            raise PermissionDenied('Le propriétaire du cours dispose d’un aperçu, sans tentative notée.')
        questions = exercise_questions(self.object)
        answers = [request.POST.get(f'reponse_{i}', '').strip() for i in range(len(questions))]
        revision = exercise_revision(self.object)
        try:
            number = int(request.POST.get('attempt_number', '1'))
            self.tentative = save_submission(request.user, self.object, answers,
                request.POST.get('exercise_revision', revision), number)
        except (SubmissionError, ValueError) as error:
            self.erreur_reponse = str(error) if isinstance(error, SubmissionError) else 'Numéro de tentative invalide. Rechargez la page.'
        return self.render_to_response(self.get_context_data(object=self.object))


class ExerciceFormMixin:
    form_class = ExerciceForm
    template_name = 'pedagogie/exercice_form.html'

    def get_form_kwargs(self):
        kw = super().get_form_kwargs()
        kw['user'] = self.request.user
        return kw


    def get_questions_formset(self):
        if not hasattr(self, '_questions_formset'):
            initial = getattr(self.object, 'questions_supplementaires', []) if self.object else []
            self._questions_formset = QuestionQCMFormSet(self.request.POST if self.request.method == 'POST' else None, initial=initial, prefix='questions', form_kwargs={'type_exercice': self.request.POST.get('type_exercice', 'qcm') if self.request.method == 'POST' else getattr(self.object, 'type_exercice', 'qcm')})
        return self._questions_formset

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['questions_formset'] = self.get_questions_formset()
        return ctx

    def form_valid(self, form):
        formset = self.get_questions_formset()
        if not formset.is_valid():
            return self.form_invalid(form)
        form.instance.questions_supplementaires = [
            {key: row[key] for key in ('question', 'propositions', 'bonne_reponse', 'explication', 'bareme')}
            for row in formset.cleaned_data if row and not row.get('DELETE')
        ]
        messages.success(self.request, 'Exercice enregistré.')
        return super().form_valid(form)


class ExerciceCreateView(PermissionRequiredMixin, ExerciceFormMixin, CreateView):
    permission_required = 'pedagogie.add_exercice'

    def get_initial(self):
        cid = self.request.GET.get('cours', '')
        return {'cours': cid, 'chapitre_id': self.request.GET.get('chapitre', '')} if cid.isdigit() else {}

    def form_valid(self, form):
        return super().form_valid(form)


class ExerciceUpdateView(PermissionRequiredMixin, OwnerOrAdminMixin, ExerciceFormMixin, UpdateView):
    permission_required = 'pedagogie.change_exercice'
    model = Exercice

    def form_valid(self, form):
        return super().form_valid(form)


class ExerciceDeleteView(PermissionRequiredMixin, OwnerOrAdminMixin, DeleteMessageMixin, DeleteView):
    permission_required = 'pedagogie.delete_exercice'
    model = Exercice
    success_url = reverse_lazy('pedagogie:exercice_list')

    def form_valid(self, form):
        if self.object.tentatives.exists():
            messages.warning(self.request, 'Cet exercice contient des copies enregistrées. Sa suppression est bloquée pour conserver les notes.')
            return redirect(self.object)
        return super().form_valid(form)
