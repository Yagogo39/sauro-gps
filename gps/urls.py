from django.urls import path
from . import views

urlpatterns = [
    # Mapa principal
    path('',                          views.mapa,                  name='mapa'),

    # ESP32
    path('api/gps/',                  views.recibir_gps,           name='recibir_gps'),
    path('api/gps/ultima/',           views.ultima_coordenada,     name='ultima_coord'),
    path('api/gps/historial/',        views.historial_coordenadas, name='historial'),

    # Algoritmos
    path('api/ruta/',                 views.calcular_ruta,         name='calcular_ruta'),
    path('api/ruta/<int:ruta_id>/desvio/', views.verificar_desvio, name='verificar_desvio'),

    # Vehículo
    path('api/vehiculo/',             views.estado_vehiculo,       name='estado_vehiculo'),

    # Capas del mapa
    path('api/casetas/',              views.listar_casetas,        name='casetas'),
    path('api/trafico/',              views.listar_zonas_trafico,  name='trafico'),
    path('api/zonas-riesgo/', views.listar_zonas_riesgo, name='listar_zonas_riesgo'),
]