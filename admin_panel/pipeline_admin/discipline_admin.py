"""Editable, ordered Discipline cards and a durable morning schedule."""
from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from app.discipline_modes import SHORT_FORMATS
from .models import DisciplineIdea, DisciplineQueueSchedule

EDITABLE = {"pending", "skipped", "paused", "ready", "offered", "deferred"}


class IdeaForm(forms.ModelForm):
    mode = forms.ChoiceField(label='Режим', choices=[(key, value.label) for key, value in SHORT_FORMATS.items()])
    class Meta:
        model = DisciplineIdea
        fields = '__all__'
    def clean_sources(self):
        sources = self.cleaned_data.get('sources') or []
        if not isinstance(sources, list) or any(not isinstance(s, dict) or not str(s.get('url', '')).startswith('https://') for s in sources):
            raise ValidationError('Нужен список источников с https-ссылкой в поле url и пояснением в note.')
        return sources
    def clean(self):
        cleaned = super().clean()
        if not self.instance._state.adding:
            row = DisciplineIdea.objects.select_for_update().get(pk=self.instance.pk)
            if row.status not in EDITABLE or row.run_id:
                raise ValidationError('Генерация уже начата. Правки текста делайте в диалоге генерации.')
        series = cleaned.get('content_series')
        if series:
            from .models import Project
            if not Project.objects.filter(pk=series.project_id, slug='discipline').exists():
                raise ValidationError('Выберите серию проекта Дисциплина.')
            cleaned['series'] = series.title_ru
        return cleaned


@admin.register(DisciplineIdea)
class DisciplineIdeaAdmin(admin.ModelAdmin):
    form = IdeaForm
    list_display = ('position', 'title', 'series', 'episode', 'mode_label', 'status')
    list_filter = ('status', 'series', 'mode')
    search_fields = ('title', 'script', 'problem')
    readonly_fields = ('status', 'offered_at', 'completed_at', 'run_id', 'created_at')
    fields = ('position', 'title', 'slug', 'content_series', 'series', 'episode', 'mode', 'problem', 'hook', 'script', 'direction', 'payoff', 'review_plan', 'sources', 'status', 'offered_at', 'completed_at', 'run_id', 'created_at')
    actions = ('skip', 'return_to_queue')

    @admin.display(description='Режим')
    def mode_label(self, obj):
        fmt = SHORT_FORMATS.get(obj.mode)
        return fmt.label if fmt else obj.mode

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or (obj.status in EDITABLE and not obj.run_id))

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or obj.status in {'pending','skipped'})

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            rows = list(queryset.select_for_update())
            if any(r.status not in {'pending','skipped'} for r in rows):
                raise PermissionDenied('Сценарий уже предложен или в работе.')
            super().delete_queryset(request, queryset)

    def delete_model(self, request, obj):
        self.delete_queryset(request, DisciplineIdea.objects.filter(pk=obj.pk))

    @admin.action(description='Отклонить выбранные идеи')
    def skip(self, request, queryset):
        if not self.has_change_permission(request):
            raise PermissionDenied
        with transaction.atomic():
            DisciplineQueueSchedule.objects.select_for_update().filter(pk=1).first()
            # A running manual generation must be cancelled in its dialogue first.
            rows = list(queryset.select_for_update().filter(status__in=['pending','paused','ready','offered','deferred']))
            advance = any(row.status != 'pending' for row in rows)
            for row in rows:
                row.status = 'skipped'
                row.save(update_fields=['status'])
            if advance and not DisciplineIdea.objects.filter(status__in=['ready','offered','deferred','in_progress','paused']).exists():
                following = DisciplineIdea.objects.select_for_update().filter(status='pending').first()
                if following:
                    following.status = 'ready'
                    following.save(update_fields=['status'])
        self.message_user(request, f'Отклонено: {len(rows)}.')

    @admin.action(description='Вернуть отклонённые идеи в очередь')
    def return_to_queue(self, request, queryset):
        if not self.has_change_permission(request):
            raise PermissionDenied
        count = queryset.filter(status='skipped').update(status='pending', delivery_index=0, offered_at=None)
        self.message_user(request, f'Возвращено: {count}.')


@admin.register(DisciplineQueueSchedule)
class DisciplineQueueScheduleAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'enabled', 'next_run_at')
    fields = ('enabled', 'next_run_at')
    def has_add_permission(self, request):
        return super().has_add_permission(request) and not DisciplineQueueSchedule.objects.exists()
    def has_delete_permission(self, request, obj=None):
        return False
