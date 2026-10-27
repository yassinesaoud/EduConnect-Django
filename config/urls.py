from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('', include('dashboard.urls')),
    path('comptes/', include('accounts.urls')),
    path('pedagogie/', include('pedagogie.urls')),
    path('coaching/', include('coaching.urls')),
    path('ressources/', include('ressources.urls')),
    path('objectifs/', include('objectifs.urls')),
    path('admin/', admin.site.urls),
]

# Les documents de cours sont servis par le stockage objet en production
# (Vercel Blob / S3) ; en local on les expose depuis MEDIA_ROOT.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
