"""Owner advertising queue: direct multi-photo upload and Excel import."""
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html_join

from app.advertising_product_files import read_product_workbook, store_photo, validate_photo, photo_path
from .models import AdvertisingProduct, AdvertisingQueueSchedule


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class PhotosField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        if not data:
            return []
        values = data if isinstance(data, (tuple, list)) else [data]
        if len(values) > 8:
            raise ValidationError("Максимум 8 фотографий.")
        cleaned = []
        for upload in values:
            upload = super().clean(upload, initial)
            try:
                cleaned.append(validate_photo(upload.read(30 * 1024 * 1024 + 1)))
            except Exception as exc:
                raise ValidationError("Не удалось прочитать фотографию: %(error)s", params={"error": str(exc)}) from exc
        return cleaned


class ProductForm(forms.ModelForm):
    photos = PhotosField(label="Картинки", required=False,
                        help_text="Выберите до 8 файлов. Новые фотографии заменяют предыдущий набор.")

    class Meta:
        model = AdvertisingProduct
        fields = ("title", "photos", "comment", "position")

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("photos") and not self.instance.images:
            self.add_error("photos", "Нужна хотя бы одна фотография товара.")
        status = self.instance.status
        if not self.instance._state.adding:
            # Admin change forms run in a transaction; lock against the bot
            # claiming this product between validation and save.
            status = AdvertisingProduct.objects.select_for_update().get(pk=self.instance.pk).status
        if status not in {"pending", "failed"}:
            raise ValidationError("Редактировать можно товары в очереди или с ошибкой подготовки.")
        return cleaned


class ImportForm(forms.Form):
    file = forms.FileField(label="Excel .xlsx")


@admin.register(AdvertisingProduct)
class AdvertisingProductAdmin(admin.ModelAdmin):
    form = ProductForm
    list_display = ("title", "position", "status", "created_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("title", "comment")
    readonly_fields = ("status", "preview", "error", "created_at", "completed_at")
    fields = ("title", "photos", "preview", "comment", "position", "status", "error", "created_at", "completed_at")
    change_list_template = "admin/pipeline_admin/advertising_queue.html"
    actions = ("retry_preparation",)

    def get_fields(self, request, obj=None):
        fields = super().get_fields(request, obj)
        if obj is not None and not self.has_change_permission(request, obj):
            # Uploads are form-only: a read-only detail page uses the preview.
            return tuple(field for field in fields if field != "photos")
        return fields

    def get_urls(self):
        return [path("import/", self.admin_site.admin_view(self.import_view), name="advertising_product_import"),
                path("<uuid:pk>/photo/<int:index>/", self.admin_site.admin_view(self.photo_view), name="advertising_product_photo")] + super().get_urls()

    def save_model(self, request, obj, form, change):
        if form.cleaned_data.get("photos"):
            obj.images = [store_photo(data) for data in form.cleaned_data["photos"]]
        if change:
            obj.prepared = {}
        super().save_model(request, obj, form, change)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (obj is None or obj.status in {"pending", "failed"})

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or obj.status in {"pending", "failed", "completed"})

    def delete_queryset(self, request, queryset):
        with transaction.atomic():
            rows = list(queryset.select_for_update())
            if any(row.status not in {"pending", "failed", "completed"} for row in rows):
                raise PermissionDenied("Товар уже в работе; удаление недоступно.")
            super().delete_queryset(request, queryset)

    def delete_model(self, request, obj):
        self.delete_queryset(request, AdvertisingProduct.objects.filter(pk=obj.pk))

    @admin.display(description="Фотографии")
    def preview(self, obj):
        return format_html_join(" ", '<img src="{}" style="max-height:160px;max-width:200px" alt="Фото товара">',
                                ((reverse("admin:advertising_product_photo", args=[obj.pk, index]),)
                                 for index in range(len(obj.images or []))))

    def photo_view(self, request, pk, index):
        obj = self.get_object(request, str(pk))
        if not obj or not self.has_view_permission(request, obj):
            raise PermissionDenied
        try:
            return FileResponse(photo_path(obj.images[index]).open("rb"), content_type="image/jpeg")
        except (IndexError, ValueError, OSError):
            raise Http404("Фотография не найдена")

    @admin.action(description="Повторить подготовку товаров с ошибкой")
    def retry_preparation(self, request, queryset):
        if not self.has_change_permission(request):
            raise PermissionDenied
        from django.utils import timezone
        from datetime import timedelta
        # Keep the assigned cadence slot: a retry must not consume another slot.
        count = queryset.filter(status="failed").update(status="preparing", error="", claimed_at=timezone.now() - timedelta(hours=1))
        self.message_user(request, f"На повторную подготовку: {count}")

    def import_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied
        form = ImportForm(request.POST or None, request.FILES or None)
        if request.method == "POST" and form.is_valid():
            try:
                rows = read_product_workbook(form.cleaned_data["file"].read(30 * 1024 * 1024 + 1))
                with transaction.atomic():
                    position = AdvertisingProduct.objects.aggregate(value=Max("position"))["value"] or 0
                    for index, row in enumerate(rows, 1):
                        AdvertisingProduct.objects.create(title=row["title"], comment=row["comment"], position=position + index,
                                                          images=[store_photo(data) for data in row["photos"]])
                self.message_user(request, f"Добавлено товаров: {len(rows)}", messages.SUCCESS)
                return HttpResponseRedirect(reverse("admin:pipeline_admin_advertisingproduct_changelist"))
            except Exception as exc:
                form.add_error("file", str(exc))
        return TemplateResponse(request, "admin/pipeline_admin/advertising_import.html", {
            **self.admin_site.each_context(request), "title": "Импорт рекламных товаров", "form": form,
            "opts": self.model._meta,
        })


@admin.register(AdvertisingQueueSchedule)
class AdvertisingQueueScheduleAdmin(admin.ModelAdmin):
    fields = ("enabled", "next_run_at")
    readonly_fields = ("next_run_at",)
    list_display = ("__str__", "enabled", "next_run_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
