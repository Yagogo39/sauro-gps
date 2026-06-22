import heapq
import math
import random
import requests
from datetime import datetime


# ─── OSRM: DISTANCIA REAL POR CARRETERA ───────────────────────────────────────
def osrm_distancia(lat1, lon1, lat2, lon2):
    """
    Consulta el servidor público de OSRM para obtener
    distancia real (km) y duración (minutos) entre dos puntos.
    Devuelve (distancia_km, duracion_min) o None si falla.
    """
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{lon1},{lat1};{lon2},{lat2}"
        f"?overview=false"
    )
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get("code") == "Ok":
            ruta = data["routes"][0]
            distancia_km  = ruta["distance"] / 1000.0
            duracion_min  = ruta["duration"] / 60.0
            return distancia_km, duracion_min
    except Exception:
        pass
    # Fallback: distancia euclidiana si OSRM no responde
    return _distancia_euclidiana(lat1, lon1, lat2, lon2), None


def osrm_geometria(lat1, lon1, lat2, lon2):
    """
    Igual que osrm_distancia pero también devuelve la geometría
    (lista de [lat, lng]) para dibujar la ruta real en el mapa.
    """
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{lon1},{lat1};{lon2},{lat2}"
        f"?overview=full&geometries=geojson"
    )
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get("code") == "Ok":
            ruta      = data["routes"][0]
            dist_km   = ruta["distance"] / 1000.0
            dur_min   = ruta["duration"] / 60.0
            coords    = ruta["geometry"]["coordinates"]  # [[lon,lat], ...]
            # OSRM devuelve [lon, lat] → invertimos a [lat, lon] para Leaflet
            polilinea = [[c[1], c[0]] for c in coords]
            return dist_km, dur_min, polilinea
    except Exception:
        pass
    return _distancia_euclidiana(lat1, lon1, lat2, lon2), None, []


# ─── HEURÍSTICAS ──────────────────────────────────────────────────────────────
def _distancia_euclidiana(lat1, lon1, lat2, lon2):
    """Distancia aproximada en km (Pitágoras sobre grados)."""
    return math.sqrt((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) * 111.32


def heuristica_manhattan(lat1, lon1, lat2, lon2):
    """
    Heurística Manhattan adaptada a coordenadas geográficas.
    Suma las diferencias absolutas en latitud y longitud (en km).
    Ideal para ciudades con calles en cuadrícula.
    """
    dlat = abs(lat1 - lat2) * 111.32
    dlon = abs(lon1 - lon2) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return dlat + dlon


# ─── PENALIZACIONES (casetas + tráfico) ───────────────────────────────────────
def _penalizacion_casetas(lat1, lon1, lat2, lon2, casetas):
    """
    Suma el costo MXN de las casetas que están cerca del segmento.
    casetas: lista de dicts {'lat', 'lng', 'costo'}
    """
    total = 0.0
    for c in casetas:
        # Si la caseta está a menos de 2 km del segmento la contamos
        d1 = _distancia_euclidiana(lat1, lon1, c['lat'], c['lng'])
        d2 = _distancia_euclidiana(lat2, lon2, c['lat'], c['lng'])
        if min(d1, d2) < 2.0:
            total += c['costo']
    return total


def _factor_trafico(lat, lon, zonas_trafico):
    """
    Devuelve un factor multiplicador según las zonas de tráfico activas.
    zonas_trafico: lista de dicts {'lat', 'lng', 'radio_km', 'nivel', 'hora_inicio', 'hora_fin'}
    """
    FACTORES = {'bajo': 1.2, 'medio': 1.5, 'alto': 2.0, 'critico': 3.0}
    hora_actual = datetime.now().time()
    factor = 1.0

    for z in zonas_trafico:
        # ¿Estamos dentro del radio de la zona?
        dist = _distancia_euclidiana(lat, lon, z['lat'], z['lng'])
        if dist > z.get('radio_km', 0.5):
            continue

        # ¿Estamos en el horario de tráfico?
        hi = z.get('hora_inicio')
        hf = z.get('hora_fin')
        if hi and hf:
            if not (hi <= hora_actual <= hf):
                continue

        f = FACTORES.get(z.get('nivel', 'medio'), 1.0)
        factor = max(factor, f)   # tomamos el peor factor que aplique

    return factor

def _factor_riesgo(lat, lon, zonas_riesgo):
    for z in zonas_riesgo:
        dx = (lat - z['lat']) * 111.32
        dy = (lon - z['lng']) * 111.32
        # PRUEBA: Multiplicamos el radio por 10 para "agrandar" la zona de riesgo
        if (dx*dx + dy*dy) < ((z['radio_km'] * 10)**2): 
            print(f"DEBUG: ¡ZONA DETECTADA! Lat:{lat}, Lon:{lon}")
            return 50.0 
    return 1.0

def obtener_casetas_cercanas(lat1, lon1, lat2, lon2, casetas, umbral_km=2.0):
    """
    Identifica qué casetas están cerca del segmento (lat1, lon1) -> (lat2, lon2).
    Retorna (costo_acumulado, lista_casetas_encontradas)
    """
    total_costo = 0.0
    encontradas = []
    
    for c in casetas:
        # Distancia del segmento a la caseta
        d1 = _distancia_euclidiana(lat1, lon1, c['lat'], c['lng'])
        d2 = _distancia_euclidiana(lat2, lon2, c['lat'], c['lng'])
        
        if min(d1, d2) < umbral_km:
            total_costo += c['costo']
            # Evitar duplicados si la caseta aparece en múltiples segmentos
            if c not in encontradas:
                encontradas.append(c)
                
    return total_costo, encontradas


def _costo_segmento(nombre_a, nombre_b, coordenadas,
                    casetas=None, zonas_trafico=None, zonas_riesgo=None,
                    precio_gasolina=20.0, rendimiento_kmL=12.0):
    
    casetas       = casetas or []
    zonas_trafico = zonas_trafico or []
    zonas_riesgo  = zonas_riesgo or []

    lat1, lon1 = coordenadas[nombre_a]
    lat2, lon2 = coordenadas[nombre_b]

    # Obtenemos la distancia real (física)
    dist_km, dur_min = osrm_distancia(lat1, lon1, lat2, lon2)
    
    # --- 1. Penalización y DETECCIÓN de casetas ---
    casetas_en_segmento = []
    penalizacion_casetas = 0.0
    
    for c in casetas:
        # Usamos _distancia_euclidiana para ver si la caseta está cerca del segmento
        d1 = _distancia_euclidiana(lat1, lon1, c['lat'], c['lng'])
        d2 = _distancia_euclidiana(lat2, lon2, c['lat'], c['lng'])
        
        # Si la caseta está a menos de 2km de cualquiera de los extremos del segmento
        if min(d1, d2) < 2.0:
            penalizacion_casetas += c['costo']
            casetas_en_segmento.append(c)

    costo_busqueda = dist_km + (penalizacion_casetas / 10.0)
    
    # 2. Factor de tráfico
    lat_mid = (lat1 + lat2) / 2
    lon_mid = (lon1 + lon2) / 2
    costo_busqueda *= _factor_trafico(lat_mid, lon_mid, zonas_trafico)

    # 3. Factor de Riesgo
    factor_riesgo = _factor_riesgo(lat_mid, lon_mid, zonas_riesgo)
    costo_busqueda *= factor_riesgo

    # Retornamos también la lista de casetas encontradas
    return costo_busqueda, dist_km, dur_min, casetas_en_segmento

# ══════════════════════════════════════════════════════════════════════════════
# ALGORITMO 1 — A* CON HEURÍSTICA MANHATTAN
# ══════════════════════════════════════════════════════════════════════════════
# ... (mantén tus funciones osrm_distancia, osrm_geometria, etc.) ...

def astar_manhattan(coordenadas, origen, destino, casetas=None, zonas_trafico=None, zonas_riesgo=None, precio_gasolina=20.0, rendimiento_kmL=12.0):
    casetas = casetas or []
    zonas_trafico = zonas_trafico or []
    zonas_riesgo = zonas_riesgo or []
    lat_d, lon_d = coordenadas[destino]

    heap = []
    heapq.heappush(heap, (0.0, 0.0, origen, [origen], 0.0, []))
    visitados = {}
    
    camino_final, dist_total_real, tiempo_total, casetas_finales = [], 0.0, 0.0, []

    while heap:
        f, g, nodo_actual, camino, dist_real, casetas_acum = heapq.heappop(heap)
        if nodo_actual in visitados and visitados[nodo_actual] <= g: continue
        visitados[nodo_actual] = g

        if nodo_actual == destino:
            camino_final, dist_total_real, casetas_finales = camino, dist_real, casetas_acum
            break

        for vecino in coordenadas.keys():
            if vecino == nodo_actual: continue
            costo_seg, dist_seg, dur_seg, casetas_seg = _costo_segmento(nodo_actual, vecino, coordenadas, casetas, zonas_trafico, zonas_riesgo)
            nuevo_g = g + costo_seg
            lat_b, lon_b = coordenadas[vecino]
            nuevo_f = nuevo_g + heuristica_manhattan(lat_b, lon_b, lat_d, lon_d)
            
            nuevas_casetas = list({(c.get('nombre') or f"{c['lat']}_{c['lng']}"): c for c in (casetas_acum + casetas_seg)}.values())

            if vecino not in visitados or visitados.get(vecino, float('inf')) > nuevo_g:
                heapq.heappush(heap, (nuevo_f, nuevo_g, vecino, camino + [vecino], dist_real + dist_seg, nuevas_casetas))

    polilinea = []
    for i in range(len(camino_final) - 1):
        lat1, lon1 = coordenadas[camino_final[i]]
        lat2, lon2 = coordenadas[camino_final[i + 1]]
        _, dur, poly_seg = osrm_geometria(lat1, lon1, lat2, lon2)
        polilinea.extend(poly_seg)
        if dur: tiempo_total += dur

    return camino_final, round(dist_total_real, 2), round(tiempo_total, 1), polilinea, casetas_finales



# ══════════════════════════════════════════════════════════════════════════════
# ALGORITMO 2 — COSTO UNIFORME (UCS / DIJKSTRA)
# ══════════════════════════════════════════════════════════════════════════════
def costo_uniforme(coordenadas, origen, destino,
                   casetas=None, zonas_trafico=None, zonas_riesgo=None,
                   precio_gasolina=20.0, rendimiento_kmL=12.0):
    
    casetas       = casetas or []
    zonas_trafico = zonas_trafico or []
    zonas_riesgo  = zonas_riesgo or []

    heap = []
    # Almacenamos: (g, nodo, camino, casetas_acumuladas)
    heapq.heappush(heap, (0.0, origen, [origen], []))

    visitados = {}
    tiempo_total = 0.0
    camino = [origen]
    casetas_finales = []

    while heap:
        g, nodo_actual, camino, casetas_acum = heapq.heappop(heap)

        if nodo_actual in visitados: continue
        visitados[nodo_actual] = g

        if nodo_actual == destino:
            casetas_finales = casetas_acum
            break

        for vecino in coordenadas:
            if vecino == nodo_actual or vecino in visitados: continue

            # AQUÍ ESTABA EL ERROR: Ahora recibimos 4 valores
            costo_seg, _, _, casetas_seg = _costo_segmento(
                nodo_actual, vecino, coordenadas,
                casetas, zonas_trafico, zonas_riesgo,
                precio_gasolina, rendimiento_kmL
            )
            
            # Acumulamos casetas
            nuevas_casetas = list({(c.get('nombre') or f"{c['lat']}_{c['lng']}"): c for c in (casetas_acum + casetas_seg)}.values())
            
            heapq.heappush(heap, (g + costo_seg, vecino, camino + [vecino], nuevas_casetas))

    # Geometría
    polilinea = []
    for i in range(len(camino) - 1):
        lat1, lon1 = coordenadas[camino[i]]
        lat2, lon2 = coordenadas[camino[i + 1]]
        _, dur, poly_seg = osrm_geometria(lat1, lon1, lat2, lon2)
        polilinea.extend(poly_seg)
        if dur: tiempo_total += dur

    dist_total = visitados.get(destino, 0.0)
    
    # IMPORTANTE: Retornamos los 5 valores ahora
    return camino, round(dist_total, 2), round(tiempo_total, 1), polilinea, casetas_finales

# ══════════════════════════════════════════════════════════════════════════════
# ALGORITMO 3 — GENÉTICO EVOLUTIVO (TSP multi-destino)
# ══════════════════════════════════════════════════════════════════════════════
def _distancia_ruta_optimizada(ruta, matriz):
    total = 0.0
    for i in range(len(ruta) - 1):
        origen_i = ruta[i]
        destino_i = ruta[i+1]
        
        if origen_i == destino_i:
            total += 1e6  # Penalización alta por quedarse estancado
            continue
            
        total += matriz.get((origen_i, destino_i), 0.0)
    return total

def _seleccion_ruleta(poblacion, aptitudes):
    """Selecciona un individuo por ruleta proporcional a su aptitud."""
    total = sum(aptitudes)
    if total == 0:
        return random.choice(poblacion)
    r = random.uniform(0, total)
    acum = 0.0
    for ind, apt in zip(poblacion, aptitudes):
        acum += apt
        if acum >= r:
            return ind
    return poblacion[-1]


def _cruce_orden(padre1, padre2):
    """
    Cruce OX (Order Crossover) — preserva el orden relativo
    de las ciudades respetando las restricciones del TSP.
    """
    n     = len(padre1)
    hijo  = [None] * n
    i, j  = sorted(random.sample(range(n), 2))

    # Copia segmento del padre1
    hijo[i:j+1] = padre1[i:j+1]

    # Rellena con el orden del padre2
    pos   = (j + 1) % n
    for gen in padre2[j+1:] + padre2[:j+1]:
        if gen not in hijo:
            hijo[pos] = gen
            pos = (pos + 1) % n

    return hijo


def _mutacion_intercambio(ruta, prob=0.1):
    """Intercambia dos ciudades aleatorias con probabilidad prob."""
    ruta = ruta[:]
    if random.random() < prob:
        i, j = random.sample(range(len(ruta)), 2)
        ruta[i], ruta[j] = ruta[j], ruta[i]
    return ruta


def algoritmo_genetico(coordenadas, origen=None,
                       casetas=None, zonas_trafico=None,
                       max_iter=200, tam_poblacion=60,
                       prob_mutacion=0.08):
    
    ciudades = list(coordenadas.keys())
    if len(ciudades) < 2:
        return ciudades, 0.0, 0.0, []

    # 1. CREAR MATRIZ (Esto evita miles de llamadas a OSRM)
    matriz = crear_matriz_distancias(coordenadas)

    # 2. Población inicial
    ciudades_libres = [c for c in ciudades if c != origen] if origen else ciudades[:]
    poblacion = []
    for _ in range(tam_poblacion):
        ind = ciudades_libres[:]
        random.shuffle(ind)
        if origen: ind = [origen] + ind
        poblacion.append(ind)

    # Función local para evaluar usando la matriz
    def evaluar(ruta):
        # Penalización: si no incluye todos los puntos, costo infinito
        if len(set(ruta)) < len(ciudades):
            return 1e9 
        return _distancia_ruta_optimizada(ruta, matriz)

    mejor_ruta  = poblacion[0]
    mejor_costo = evaluar(mejor_ruta)

    # 3. Ciclo evolutivo
    for _ in range(max_iter):
        costos = [evaluar(ind) for ind in poblacion]
        max_c  = max(costos) + 1e-9
        aptitudes = [max_c - c for c in costos]

        for ind, c in zip(poblacion, costos):
            if c < mejor_costo:
                mejor_costo = c
                mejor_ruta  = ind[:]

        nueva_gen = [mejor_ruta[:]]
        while len(nueva_gen) < tam_poblacion:
            p1 = _seleccion_ruleta(poblacion, aptitudes)
            p2 = _seleccion_ruleta(poblacion, aptitudes)
            hijo = [origen] + _cruce_orden(p1[1:], p2[1:]) if origen else _cruce_orden(p1, p2)
            hijo = _mutacion_intercambio(hijo, prob_mutacion)
            nueva_gen.append(hijo)
        poblacion = nueva_gen

    # 4. Geometría final
    polilinea, tiempo_total = [], 0.0
    for i in range(len(mejor_ruta) - 1):
        lat1, lon1 = coordenadas[mejor_ruta[i]]
        lat2, lon2 = coordenadas[mejor_ruta[i + 1]]
        _, dur, poly_seg = osrm_geometria(lat1, lon1, lat2, lon2)
        polilinea.extend(poly_seg)
        if dur: tiempo_total += dur

    return mejor_ruta, round(mejor_costo, 2), round(tiempo_total, 1), polilinea

# ══════════════════════════════════════════════════════════════════════════════
# UTILIDADES GENERALES
# ══════════════════════════════════════════════════════════════════════════════
def detectar_ruta_critica(pos_actual, puntos_ruta, umbral_km=0.3):
    """
    Devuelve True si la posición actual se desvió más de `umbral_km`
    de todos los puntos de la ruta planeada.
    """
    lat, lon = pos_actual
    for p in puntos_ruta:
        d = _distancia_euclidiana(lat, lon, p['latitud'], p['longitud'])
        if d <= umbral_km:
            return False
    return True


def calcular_combustible(distancia_km, rendimiento_kmL, precio_litro=20.0):
    """Estima litros necesarios y costo en MXN para recorrer distancia_km."""
    litros = distancia_km / rendimiento_kmL if rendimiento_kmL > 0 else 0
    costo  = litros * precio_litro
    return round(litros, 2), round(costo, 2)


def rutas_alternas(coordenadas, origen, destino, casetas=None, zonas_trafico=None, zonas_riesgo=None, n_alternas=2):
    alternas = []
    ruta_principal, _, _, _, _ = astar_manhattan(coordenadas, origen, destino, casetas, zonas_trafico, zonas_riesgo)
    
    for i, z in enumerate(zonas_riesgo):
        if i >= n_alternas: break
        coords_alt = coordenadas.copy()
        nombre_desvio = f"Desvio_{i}"
        coords_alt[nombre_desvio] = (z['lat'] + 0.05, z['lng'] + 0.05)
        
        ruta_alt, dist_alt, tiempo_alt, poly_alt, casetas_alt = astar_manhattan(coords_alt, origen, destino, casetas, zonas_trafico, zonas_riesgo)
        
        if ruta_alt != ruta_principal:
            alternas.append({'ruta': ruta_alt, 'distancia_km': dist_alt, 'tiempo_min': tiempo_alt, 'polilinea': poly_alt, 'casetas': casetas_alt})
    return alternas
def crear_matriz_distancias(coordenadas):
    nombres = list(coordenadas.keys())
    matriz = {}
    for i in nombres:
        for j in nombres:
            if i != j:
                # Usa la función euclidiana para que el genético vuele
                # El genético solo necesita una estimación para comparar rutas
                lat1, lon1 = coordenadas[i]
                lat2, lon2 = coordenadas[j]
                matriz[(i, j)] = _distancia_euclidiana(lat1, lon1, lat2, lon2)
    return matriz