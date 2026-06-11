import json
import logging
from django.http                  import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts             import render

from .models import (
    Coordenada, RutaPlaneada, PuntoRuta,
    Vehiculo, Caseta, ZonaTrafico
)
from .algorithms import (
    astar_manhattan, costo_uniforme, algoritmo_genetico,
    detectar_ruta_critica, calcular_combustible, rutas_alternas
)

logger = logging.getLogger(__name__)


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
        data        = json.loads(request.body)
        algoritmo   = data.get('algoritmo', 'astar')
        origen_key  = data.get('origen')
        destino_key = data.get('destino')
        puntos      = data.get('puntos', [])
        pedir_alt   = data.get('rutas_alternas', False)

        logger.error(f"[calcular_ruta] algoritmo={algoritmo} origen={origen_key} "
                     f"destino={destino_key} n_puntos={len(puntos)}")

        # ── Validaciones básicas ──────────────────────────────────
        if not puntos or not origen_key:
            return JsonResponse({'error': 'Faltan puntos u origen'}, status=400)

        for p in puntos:
            if 'lat' not in p or 'lng' not in p:
                return JsonResponse(
                    {'error': f'Punto "{p.get("nombre","?")}" sin lat/lng'}, status=400
                )

        # ── Construir coordenadas ─────────────────────────────────
        coordenadas = {
            p['nombre']: (float(p['lat']), float(p['lng']))
            for p in puntos
        }

        casetas       = _casetas_activas()
        zonas_trafico = _zonas_trafico_activas()
        vehiculo      = _vehiculo_activo()
        rendimiento   = vehiculo.rendimiento_kmL if vehiculo else 12.0

        logger.error(f"[calcular_ruta] coordenadas={coordenadas}")

        # ── Ejecutar algoritmo ────────────────────────────────────
        if algoritmo == 'astar':
            if not destino_key:
                return JsonResponse({'error': 'A* requiere destino'}, status=400)
            camino, dist_km, tiempo_min, polilinea = astar_manhattan(
                coordenadas, origen_key, destino_key,
                casetas, zonas_trafico,
                rendimiento_kmL=rendimiento
            )

        elif algoritmo == 'costo_uniforme':
            if not destino_key:
                return JsonResponse({'error': 'UCS requiere destino'}, status=400)
            camino, dist_km, tiempo_min, polilinea = costo_uniforme(
                coordenadas, origen_key, destino_key,
                casetas, zonas_trafico,
                rendimiento_kmL=rendimiento
            )

        elif algoritmo == 'genetico':
            camino, dist_km, tiempo_min, polilinea = algoritmo_genetico(
                coordenadas, origen=origen_key,
                casetas=casetas, zonas_trafico=zonas_trafico
            )

        else:
            return JsonResponse(
                {'error': f'Algoritmo "{algoritmo}" no válido'}, status=400
            )

        logger.error(f"[calcular_ruta] camino={camino} dist={dist_km}")

        # ── Métricas ──────────────────────────────────────────────
        litros, costo_gasolina = calcular_combustible(dist_km, rendimiento)

        alerta_combustible = None
        if vehiculo:
            disponible = vehiculo.autonomia_km()
            if dist_km > disponible:
                alerta_combustible = (
                    f"⚠️ Combustible insuficiente. "
                    f"Autonomía: {disponible:.0f} km. "
                    f"Ruta: {dist_km:.0f} km."
                )

        # ── Guardar ───────────────────────────────────────────────
        ruta_obj = _guardar_ruta(
            nombre       = f"Ruta {algoritmo} — {origen_key} → {destino_key or 'multi'}",
            algoritmo    = algoritmo,
            camino       = camino,
            coordenadas  = coordenadas,
            distancia_km = dist_km,
            tiempo_min   = tiempo_min,
            vehiculo     = vehiculo,
        )

        respuesta = {
            'ruta_id':            ruta_obj.id,
            'algoritmo':          algoritmo,
            'camino':             camino,
            'distancia_km':       dist_km,
            'tiempo_min':         tiempo_min,
            'polilinea':          polilinea,
            'combustible_L':      litros,
            'costo_gasolina':     costo_gasolina,
            'alerta_combustible': alerta_combustible,
            'alternas':           [],
        }

        # ── Alternas ──────────────────────────────────────────────
        if pedir_alt and algoritmo in ['astar', 'costo_uniforme'] and destino_key:
            alternas = rutas_alternas(
                coordenadas, origen_key, destino_key,
                casetas, zonas_trafico, n_alternas=2
            )
            for i, alt in enumerate(alternas):
                alt_obj = _guardar_ruta(
                    nombre         = f"Alterna {i+1} — {origen_key} → {destino_key}",
                    algoritmo      = algoritmo,
                    camino         = alt['ruta'],
                    coordenadas    = coordenadas,
                    distancia_km   = alt['distancia_km'],
                    tiempo_min     = alt['tiempo_min'],
                    vehiculo       = vehiculo,
                    es_alterna     = True,
                    ruta_principal = ruta_obj,
                )
                respuesta['alternas'].append({
                    'ruta_id':      alt_obj.id,
                    'camino':       alt['ruta'],
                    'distancia_km': alt['distancia_km'],
                    'tiempo_min':   alt['tiempo_min'],
                    'polilinea':    alt['polilinea'],
                })

        return JsonResponse(respuesta)

    except Exception as e:
        logger.exception("[calcular_ruta] ERROR INESPERADO")
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