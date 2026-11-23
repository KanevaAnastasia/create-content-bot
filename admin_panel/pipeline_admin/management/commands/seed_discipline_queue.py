"""Import reviewed JSON cards without resetting existing workflow state."""
import json
from datetime import datetime
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from app.discipline_modes import SHORT_FORMATS
from pipeline_admin.models import DisciplineIdea, DisciplineQueueSchedule

FIELDS = ('slug','title','position','mode','series','episode','script','direction','problem','hook','payoff','sources','review_plan')

class Command(BaseCommand):
    help = 'Добавить проверенные карточки в очередь Дисциплины. Существующие не изменяются.'
    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--start', required=True, help='ISO timestamp with timezone')
        parser.add_argument('--enable', action='store_true')
        parser.add_argument('--update-pending', action='store_true', help='Обновить только ещё не предложенные карточки; при активной карточке отменить весь импорт.')
    def handle(self, *args, **options):
        try:
            rows=json.loads(Path(options['path']).read_text())
            start=datetime.fromisoformat(options['start'])
        except (OSError, ValueError) as exc:
            raise CommandError('Не удалось прочитать JSON или время запуска.') from exc
        if not isinstance(rows, list) or not rows:
            raise CommandError('Нужен непустой список карточек.')
        if start.tzinfo is None:
            raise CommandError('Укажите часовой пояс времени запуска.')
        slugs=set(); positions=set()
        for row in rows:
            if not isinstance(row, dict) or not all(key in row for key in FIELDS) or row['mode'] not in SHORT_FORMATS:
                raise CommandError('Неполная карточка или неизвестный режим.')
            if row['slug'] in slugs:
                raise CommandError('Повтор ключа в файле.')
            slugs.add(row['slug'])
            if not isinstance(row['position'], int) or row['position'] < 1 or row['position'] in positions:
                raise CommandError('Порядок должен быть положительным и уникальным.')
            positions.add(row['position'])
            if not isinstance(row['sources'], list) or not row['sources'] or any(
                not isinstance(source, dict) or not str(source.get('url','')).startswith('https://') for source in row['sources']):
                raise CommandError('Нужен список проверяемых https-источников.')
            if not all(str(row[k]).strip() for k in ('title','script','direction','problem','hook','payoff')):
                raise CommandError('Пустой текст карточки.')
        created=0; updated=0
        with transaction.atomic():
            DisciplineQueueSchedule.objects.select_for_update().filter(pk=1).first()
            for row in rows:
                obj, added=DisciplineIdea.objects.select_for_update().get_or_create(slug=row['slug'], defaults={k:row[k] for k in FIELDS if k!='slug'})
                created+=int(added)
                if not added and options['update_pending']:
                    if obj.status != 'pending':
                        raise CommandError('Есть уже предложенная или изменённая по статусу карточка. Импорт отменён.')
                    for key in FIELDS:
                        setattr(obj,key,row[key])
                    obj.save(update_fields=FIELDS)
                    updated+=1
            schedule, added=DisciplineQueueSchedule.objects.get_or_create(id=1, defaults={'enabled':options['enable'],'next_run_at':start})
        self.stdout.write(f'Добавлено {created}; обновлено {updated}; уже существовало {len(rows)-created}; расписание {schedule.next_run_at.isoformat()}; включено {schedule.enabled}.')
