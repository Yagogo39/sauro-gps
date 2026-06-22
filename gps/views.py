import json
import logging
from django.http                  import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts             import render
import os
from django.conf import settings
from datetime import datetime, timedelta
import pytz

# Tabla de referencia: Nombre de ciudad o región -> ID de Zona Horaria
ZONAS_MAP = {
    'CDMX': 'America/Mexico_City',
    'Tijuana': 'America/Tijuana',
    'Merida': 'America/Merida',
    'Cancun': 'America/Cancun',
    'Monterrey': 'America/Monterrey',
    'Hermosillo': 'America/Hermosillo',
    'Chihuahua': 'America/Chihuahua'
}

from .models import (
    Coordenada, RutaPlaneada, PuntoRuta,
    Vehiculo, Caseta, ZonaTrafico
)
from .algorithms import (
    astar_manhattan, costo_uniforme, algoritmo_genetico,
    detectar_ruta_critica, calcular_combustible, rutas_alternas
)

logger = logging.getLogger(__name__)


def _zonas_riesgo_json():
    ruta_archivo = os.path.join(settings.BASE_DIR, 'data', 'zonas_riesgo.json')
    if os.path.exists(ruta_archivo):
        with open(ruta_archivo, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

# --- Nueva vista para la API ---
def listar_zonas_riesgo(request):
    return JsonResponse({'zonas': _zonas_riesgo_json()})

# ─── HELPERS INTERNOS ─────────────────────────────────────────────────────────
def _casetas_activas():
    """Devuelve casetas con claves 'lat'/'lng' que espera algorithms.py"""
    casetas = Caseta.objects.filter(activa=True)
    return [
        {
            'lat':   float(c.latitud),
            'lng':   float(c.longitud),
            'costo': float(c.costo),
        }
        for c in casetas
    ]


def _zonas_trafico_activas():
    """Devuelve zonas con claves 'lat'/'lng' que espera algorithms.py"""
    zonas = ZonaTrafico.objects.filter(activa=True)
    result = []
    for z in zonas:
        result.append({
            'lat':         float(z.latitud_centro),
            'lng':         float(z.longitud_centro),
            'radio_km':    float(z.radio_km),
            'nivel':       z.nivel_trafico,
            'hora_inicio': z.hora_inicio,
            'hora_fin':    z.hora_fin,
        })
    return result


def _vehiculo_activo():
    return Vehiculo.objects.filter(activo=True).first()


def _guardar_ruta(nombre, algoritmo, camino, coordenadas,
                  distancia_km, tiempo_min, vehiculo=None,
                  es_alterna=False, ruta_principal=None):
    litros, costo_gasolina = 0.0, 0.0
    if vehiculo and distancia_km:
        litros, costo_gasolina = calcular_combustible(
            distancia_km, vehiculo.rendimiento_kmL
        )

    ruta_obj = RutaPlaneada.objects.create(
        nombre         = nombre,
        algoritmo      = algoritmo,
        distancia_km   = distancia_km,
        tiempo_min     = tiempo_min,
        combustible_L  = litros,
        costo_gasolina = costo_gasolina,
        vehiculo       = vehiculo,
        es_alterna     = es_alterna,
        ruta_principal = ruta_principal,
    )

    tipo_map = {0: 'origen', len(camino) - 1: 'destino'}
    for idx, nombre_punto in enumerate(camino):
        lat, lng = coordenadas[nombre_punto]
        PuntoRuta.objects.create(
            ruta     = ruta_obj,
            nombre   = nombre_punto,
            latitud  = lat,
            longitud = lng,
            orden    = idx,
            tipo     = tipo_map.get(idx, 'intermedio'),
        )

    return ruta_obj


# ══════════════════════════════════════════════════════════════════════════════
# VISTA PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════
def mapa(request):
    vehiculo = _vehiculo_activo()
    return render(request, 'gps/mapa.html', {
        'vehiculo':   vehiculo,
        'algoritmos': [
            {'id': 'astar',          'nombre': 'A* Manhattan'},
            {'id': 'costo_uniforme', 'nombre': 'Costo Uniforme'},
            {'id': 'genetico',       'nombre': 'Genético Evolutivo'},
        ]
    })


# ══════════════════════════════════════════════════════════════════════════════
# ESP32
# ══════════════════════════════════════════════════════════════════════════════
@csrf_exempt
def recibir_gps(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    try:
        data  = json.loads(request.body)
        coord = Coordenada.objects.create(
            latitud   = data['lat'],
            longitud  = data['lng'],
            velocidad = data.get('velocidad'),
        )
        nivel = data.get('nivel_combustible')
        if nivel is not None:
            v = _vehiculo_activo()
            if v:
                v.nivel_combustible = nivel
                v.save(update_fields=['nivel_combustible'])

        return JsonResponse({'status': 'ok', 'id': coord.id,
                             'timestamp': str(coord.timestamp)})
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)


# ══════════════════════════════════════════════════════════════════════════════
# CALCULAR RUTA
# ══════════════════════════════════════════════════════════════════════════════
@csrf_exempt
def calcular_ruta(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Método no permitido'}, status=405)
    
    try:
        data          = json.loads(request.body)
        algoritmo     = data.get('algoritmo', 'astar')
        origen_key    = data.get('origen')
        destino_key   = data.get('destino')
        puntos        = data.get('puntos', [])
        pedir_alt     = data.get('rutas_alternas', False)
        evitar_riesgo = data.get('evitar_riesgo', False)

        if not puntos or not origen_key:
            return JsonResponse({'error': 'Faltan puntos u origen'}, status=400)

        # Diccionario base de coordenadas
        coordenadas_base = {p['nombre']: (float(p['lat']), float(p['lng'])) for p in puntos}
        zonas_riesgo  = _zonas_riesgo_json() if evitar_riesgo else []
        casetas       = _casetas_activas()
        zonas_trafico = _zonas_trafico_activas()
        vehiculo      = _vehiculo_activo()
        rendimiento   = vehiculo.rendimiento_kmL if vehiculo else 12.0

        camino, dist_km, tiempo_min, polilinea = ([], 0.0, 0.0, [])
        litros, costo_gasolina = calcular_combustible(dist_km, rendimiento)
        coords_a_usar = coordenadas_base # Por defecto

        # 1. Ejecución inteligente
        alternas = []
        if (pedir_alt or evitar_riesgo) and algoritmo in ['astar', 'costo_uniforme'] and destino_key:
            alternas = rutas_alternas(
                coordenadas_base, origen_key, destino_key,
                casetas, zonas_trafico, zonas_riesgo, n_alternas=2
            )

        if evitar_riesgo and alternas:
            if algoritmo == 'astar':
                p_camino, p_dist, p_tiempo, p_poly = astar_manhattan(coordenadas_base, origen_key, destino_key, casetas, zonas_trafico, zonas_riesgo, rendimiento_kmL=rendimiento)
            else:
                p_camino, p_dist, p_tiempo, p_poly = costo_uniforme(coordenadas_base, origen_key, destino_key, casetas, zonas_trafico, zonas_riesgo, rendimiento_kmL=rendimiento)
            
            # Comparamos
            todas = [{'ruta': p_camino, 'distancia_km': p_dist, 'tiempo_min': p_tiempo, 'polilinea': p_poly, 'coords': coordenadas_base}] + alternas
            mejor = min(todas, key=lambda x: x['distancia_km'])
            
            camino, dist_km, tiempo_min, polilinea = mejor['ruta'], mejor['distancia_km'], mejor['tiempo_min'], mejor['polilinea']
            
            coords_a_usar = mejor.get('coords', coordenadas_base) # Usamos las coords de la ruta elegida
        else:
            if algoritmo == 'astar':
                camino, dist_km, tiempo_min, polilinea = astar_manhattan(coordenadas_base, origen_key, destino_key, casetas, zonas_trafico, zonas_riesgo, rendimiento_kmL=rendimiento)
            elif algoritmo == 'costo_uniforme':
                camino, dist_km, tiempo_min, polilinea = costo_uniforme(coordenadas_base, origen_key, destino_key, casetas, zonas_trafico, zonas_riesgo, rendimiento_kmL=rendimiento)
            elif algoritmo == 'genetico':
                camino, dist_km, tiempo_min, polilinea = algoritmo_genetico(coordenadas_base, origen=origen_key, casetas=casetas, zonas_trafico=zonas_trafico)

        # 2. Guardado usando las coordenadas correctas (base o expandidas con desvíos)
        ruta_obj = _guardar_ruta(
            nombre=f"Ruta {algoritmo} — {origen_key} → {destino_key}",
            algoritmo=algoritmo, 
            camino=camino, 
            coordenadas=coords_a_usar, # <-- AQUI ESTA LA CORRECCION
            distancia_km=dist_km, 
            tiempo_min=tiempo_min, 
            vehiculo=vehiculo
        )

        # 1. Identificar zonas (usamos el destino_key para buscar en la tabla)
        # Si el destino no está en la tabla, usamos la de CDMX por defecto
        zona_destino_id = ZONAS_MAP.get(destino_key, 'America/Mexico_City')
        tz_destino = pytz.timezone(zona_destino_id)
        
        # 2. Calcular tiempos
        tz_origen = pytz.timezone('America/Mexico_City') # Ajusta según donde esté el servidor
        hora_salida = datetime.now(tz_origen)
        
        # Calculamos la llegada sumando los minutos del algoritmo
        # Aseguramos que tiempo_min sea un float para evitar errores
        minutos_viaje = float(tiempo_min) if tiempo_min else 0.0
        hora_llegada = hora_salida + timedelta(minutes=minutos_viaje)
        
        # Convertimos la llegada a la zona horaria del destino
        hora_llegada_destino = hora_llegada.astimezone(tz_destino)

        return JsonResponse({
            'ruta_id':        ruta_obj.id,
            'camino':         camino,
            'distancia_km':   dist_km,
            'tiempo_min':     tiempo_min if tiempo_min is not None else 0, # Protege contra NaN
            'combustible_L':  litros,                                       # ¡Esto es lo que falta!
            'costo_gasolina': costo_gasolina,                               # ¡Esto también!
            'polilinea':      polilinea,
            'hora_salida': hora_salida.strftime("%H:%M %p"),
            'hora_llegada': hora_llegada_destino.strftime("%H:%M %p"),
            'zona_llegada': hora_llegada_destino.tzname(),
            'alternas':       alternas if pedir_alt else []
        })

    except Exception as e:
        logger.exception("[calcular_ruta] Error")
        return JsonResponse({'error': str(e)}, status=500)
# ══════════════════════════════════════════════════════════════════════════════
# COORDENADAS Y DESVÍOS
# ══════════════════════════════════════════════════════════════════════════════
def ultima_coordenada(request):
    try:
        coord = Coordenada.objects.latest('timestamp')
        return JsonResponse({
            'lat':       coord.latitud,
            'lng':       coord.longitud,
            'velocidad': coord.velocidad,
            'timestamp': str(coord.timestamp),
        })
    except Coordenada.DoesNotExist:
        return JsonResponse({'error': 'Sin datos aún'}, status=404)


def historial_coordenadas(request):
    coords = Coordenada.objects.all()[:100]
    return JsonResponse({'coordenadas': [
        {'lat': c.latitud, 'lng': c.longitud, 'timestamp': str(c.timestamp)}
        for c in coords
    ]})


def verificar_desvio(request, ruta_id):
    try:
        coord_actual = Coordenada.objects.latest('timestamp')
        ruta         = RutaPlaneada.objects.get(id=ruta_id)
        puntos       = list(ruta.puntos.values('latitud', 'longitud'))
        desviado     = detectar_ruta_critica(
            (coord_actual.latitud, coord_actual.longitud), puntos
        )
        return JsonResponse({
            'desviado':   desviado,
            'lat_actual': coord_actual.latitud,
            'lng_actual': coord_actual.longitud,
            'ruta_id':    ruta_id,
        })
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


# ══════════════════════════════════════════════════════════════════════════════
# VEHÍCULO
# ══════════════════════════════════════════════════════════════════════════════
def estado_vehiculo(request):
    vehiculo = _vehiculo_activo()
    if not vehiculo:
        return JsonResponse({'error': 'No hay vehículo registrado'}, status=404)
    return JsonResponse({
        'nombre':            vehiculo.nombre,
        'tipo_combustible':  vehiculo.tipo_combustible,
        'nivel_combustible': vehiculo.nivel_combustible,
        'autonomia_km':      round(vehiculo.autonomia_km(), 1),
        'rendimiento_kmL':   vehiculo.rendimiento_kmL,
        'tanque_litros':     vehiculo.tanque_litros,
    })


# ══════════════════════════════════════════════════════════════════════════════
# CASETAS Y TRÁFICO
# ══════════════════════════════════════════════════════════════════════════════
def listar_casetas(request):
    casetas = Caseta.objects.filter(activa=True).values(
        'id', 'nombre', 'latitud', 'longitud', 'costo'
    )
    return JsonResponse({'casetas': list(casetas)})


def listar_zonas_trafico(request):
    zonas = ZonaTrafico.objects.filter(activa=True).values(
        'id', 'nombre', 'latitud_centro', 'longitud_centro',
        'radio_km', 'nivel_trafico'
    )
    return JsonResponse({'zonas': list(zonas)})